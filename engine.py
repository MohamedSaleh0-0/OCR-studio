"""OCR execution and resumable run management for the desktop application."""

from __future__ import annotations

import base64
import hashlib
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from pypdf import PdfReader, PdfWriter

try:
    from mistralai.client import Mistral
    from mistralai.client.errors import SDKError
except ImportError:  # pragma: no cover - shown as an application error
    Mistral = None

    class SDKError(Exception):
        pass

from storage import JsonStore, now_iso, new_id


MODEL = "mistral-ocr-latest"
MAX_RETRIES_PER_KEY = 3
BASE_BACKOFF_SECONDS = 5
MAX_SPLIT_DEPTH = 6
SIZE_LIMIT_MARKERS = (
    "too large", "exceeds the maximum", "maximum file size",
    "maximum number of pages", "page limit", "too many pages",
    "payload too large", "request entity too large", "file size limit",
)


def clean_path(value: str) -> Path:
    value = str(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1].strip()
    return Path(value).expanduser().resolve()


def parse_path_lines(text: str) -> list[Path]:
    result = []
    seen = set()
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        path = clean_path(line)
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def discover_pdfs(input_paths: list[Path], output_dir: Path | None) -> tuple[list[tuple[Path, Path]], list[str]]:
    found: list[tuple[Path, Path]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    output_dir = output_dir.resolve() if output_dir else None

    for path in input_paths:
        path = Path(path).expanduser().resolve()
        if not path.exists():
            warnings.append(f"Missing path: {path}")
            continue
        candidates = [path] if path.is_file() else sorted(
            path.rglob("*.pdf"), key=lambda item: str(item).casefold()
        )
        root = path.parent if path.is_file() else path
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
                if resolved == path and path.is_file():
                    warnings.append(f"Not a PDF: {path}")
                continue
            if output_dir and (resolved == output_dir or output_dir in resolved.parents):
                continue
            key = str(resolved).casefold()
            if key not in seen:
                seen.add(key)
                found.append((root, resolved))
    return found, warnings


def output_path_for(root: Path, pdf_path: Path, output_dir: Path | None) -> Path:
    if output_dir is None:
        return pdf_path.with_suffix(".md")
    relative = pdf_path.relative_to(root)
    # Central output mode writes Markdown files directly under the selected
    # output folder. Keep the PDF's relative subfolders, but don't create an
    # extra folder named after the input root.
    return output_dir / relative.with_suffix(".md")


def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


class KeyManager:
    def __init__(self, store: JsonStore, credentials: list[dict]):
        self.store = store
        self.credentials = [item for item in credentials if item.get("enabled", True)]
        self.busy: set[str] = set()
        self.index = 0
        self.condition = threading.Condition()
        usage = store.usage()
        previous_credentials = usage.get("credentials", {}) if isinstance(usage, dict) else {}
        self.usage = usage if usage.get("month") == current_month() else {
            "month": current_month(), "credentials": {}
        }
        self.usage.setdefault("credentials", {})
        for credential in self.credentials:
            default_entry = {
                "pages_used": 0, "requests": 0, "dead": False,
                "last_used_at": None, "last_error": None,
            }
            if usage.get("month") != current_month():
                old_entry = previous_credentials.get(credential["id"], {})
                dead_until = old_entry.get("dead_until")
                if dead_until and dead_until > datetime.now().date().isoformat():
                    default_entry.update({"dead": True, "dead_until": dead_until, "reason": old_entry.get("reason")})
            entry = self.usage["credentials"].setdefault(credential["id"], default_entry)
            renewal_date = credential.get("renewal_date", "")
            if entry.get("dead") and renewal_date:
                try:
                    if date.fromisoformat(renewal_date) <= date.today():
                        entry["dead"] = False
                        entry["dead_until"] = None
                        credential["status"] = "ready"
                        self._update_credential_status(credential["id"], "ready")
                except ValueError:
                    pass
        self.store.save_usage(self.usage)

    def acquire(self, skip: set[str] | None = None) -> tuple[dict | None, str | None]:
        skip = skip or set()
        with self.condition:
            while True:
                available = [
                    item for item in self.credentials
                    if item["id"] not in skip
                    and item["id"] not in self.busy
                    and not self.usage["credentials"].get(item["id"], {}).get("dead")
                ]
                for offset in range(len(self.credentials)):
                    index = (self.index + offset) % len(self.credentials)
                    item = self.credentials[index]
                    if item not in available:
                        continue
                    self.busy.add(item["id"])
                    self.index = (index + 1) % len(self.credentials)
                    try:
                        secret = self.store.secret_get(item["id"])
                    except Exception as error:
                        self.busy.discard(item["id"])
                        self.usage["credentials"][item["id"]]["last_error"] = str(error)
                        self.usage["credentials"][item["id"]]["dead"] = True
                        self.store.save_usage(self.usage)
                        continue
                    return item, secret
                live = [
                    item for item in self.credentials
                    if item["id"] not in skip
                    and not self.usage["credentials"].get(item["id"], {}).get("dead")
                ]
                if not live:
                    return None, None
                self.condition.wait(timeout=0.5)

    def release(self, credential_id: str) -> None:
        with self.condition:
            self.busy.discard(credential_id)
            self.condition.notify_all()

    def mark_dead(self, credential_id: str, error: str, exhausted: bool = False) -> None:
        with self.condition:
            item = self.usage["credentials"].setdefault(credential_id, {})
            credential = next((value for value in self.credentials if value["id"] == credential_id), {})
            item.update({"dead": True, "last_error": error, "reason": "credits_exhausted" if exhausted else "unavailable"})
            if exhausted:
                item["dead_until"] = credential.get("renewal_date") or None
                self._update_credential_status(credential_id, "credits_exhausted")
            else:
                self._update_credential_status(credential_id, "unavailable")
            self.store.save_usage(self.usage)
            self.condition.notify_all()

    def _update_credential_status(self, credential_id: str, status: str) -> None:
        credentials = self.store.credentials()
        for credential in credentials:
            if credential.get("id") == credential_id:
                credential["status"] = status
                credential["updated_at"] = now_iso()
                self.store.save_credentials(credentials)
                return

    def record_success(self, credential_id: str, page_count: int) -> None:
        with self.condition:
            item = self.usage["credentials"].setdefault(credential_id, {})
            item["pages_used"] = item.get("pages_used", 0) + page_count
            item["requests"] = item.get("requests", 0) + 1
            item["last_used_at"] = now_iso()
            item["last_error"] = None
            self.store.save_usage(self.usage)


class NeedsSplitError(Exception):
    pass


def is_size_limit_error(status, message: str) -> bool:
    return status in (413, 422) or any(marker in message for marker in SIZE_LIMIT_MARKERS)


def split_pdf_in_half(pdf_path: Path, tmp_dir: Path) -> list[Path]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    midpoint = max(1, page_count // 2)
    result = []
    for start, end in ((0, midpoint), (midpoint, page_count)):
        if start >= end:
            continue
        writer = PdfWriter()
        for page_index in range(start, end):
            writer.add_page(reader.pages[page_index])
        path = tmp_dir / f"{pdf_path.stem}_part{start:05d}-{end - 1:05d}.pdf"
        with path.open("wb") as handle:
            writer.write(handle)
        result.append(path)
    return result


def ocr_one_pdf(pdf_path: Path, api_key: str) -> str:
    if Mistral is None:
        raise RuntimeError("The mistralai package is not installed")
    client = Mistral(api_key=api_key)
    encoded = base64.b64encode(pdf_path.read_bytes()).decode("utf-8")
    response = client.ocr.process(
        model=MODEL,
        document={"type": "document_url", "document_url": f"data:application/pdf;base64,{encoded}"},
    )
    return "\n\n---\n\n".join(
        f"<!-- page {index + 1} -->\n\n{page.markdown}"
        for index, page in enumerate(response.pages)
    )


def ocr_with_rotation(pdf_path: Path, key_manager: KeyManager, logger: logging.Logger) -> tuple[str | None, str | None]:
    page_count = len(PdfReader(str(pdf_path)).pages)
    attempted: set[str] = set()
    while True:
        credential, secret = key_manager.acquire(attempted)
        if credential is None or secret is None:
            return None, "No enabled API key is available"
        credential_id = credential["id"]
        attempted.add(credential_id)
        try:
            for attempt in range(1, MAX_RETRIES_PER_KEY + 1):
                try:
                    markdown = ocr_one_pdf(pdf_path, secret)
                    key_manager.record_success(credential_id, page_count)
                    return markdown, None
                except SDKError as error:
                    status = getattr(error, "status_code", None)
                    message = str(error).lower()
                    if is_size_limit_error(status, message):
                        raise NeedsSplitError(message)
                    if status == 429 or "rate limit" in message or "too many requests" in message:
                        time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                        continue
                    credit_markers = ("insufficient", "credit", "balance", "quota", "payment required", "out of funds", "billing")
                    if status in (401, 402) or any(marker in message for marker in credit_markers):
                        key_manager.mark_dead(
                            credential_id,
                            message[:240],
                            exhausted=status == 402 or any(marker in message for marker in credit_markers),
                        )
                        break
                    time.sleep(BASE_BACKOFF_SECONDS * attempt)
                except Exception as error:
                    logger.warning("OCR retry for %s: %s", pdf_path.name, error)
                    time.sleep(BASE_BACKOFF_SECONDS * attempt)
        finally:
            key_manager.release(credential_id)


def ocr_recursive(pdf_path: Path, key_manager: KeyManager, tmp_root: Path, logger: logging.Logger, depth: int = 0) -> tuple[str | None, str | None]:
    try:
        return ocr_with_rotation(pdf_path, key_manager, logger)
    except NeedsSplitError as error:
        page_count = len(PdfReader(str(pdf_path)).pages)
        if page_count <= 1 or depth >= MAX_SPLIT_DEPTH:
            return None, str(error)
        halves = split_pdf_in_half(pdf_path, tmp_root)
        pieces = []
        try:
            for half in halves:
                markdown, failure = ocr_recursive(half, key_manager, tmp_root, logger, depth + 1)
                if markdown is None:
                    return None, failure or "Split OCR failed"
                pieces.append(markdown)
            return "\n\n---\n\n".join(pieces), None
        finally:
            for half in halves:
                half.unlink(missing_ok=True)


class RunManager:
    def __init__(self, store: JsonStore):
        self.store = store
        self.lock = threading.RLock()
        self.controls: dict[str, dict[str, threading.Event]] = {}
        self.threads: dict[str, threading.Thread] = {}
        for run in self.store.list_runs():
            if run.get("status") == "running":
                run["status"] = "paused"
                run["message"] = "Application closed while this run was active"
                self.store.save_run(run)
                documents = self.store.load_documents(run["id"])
                for document in documents:
                    if document.get("status") == "running":
                        document["status"] = "pending"
                self.store.save_documents(run["id"], documents)

    def create_run(self, input_paths: list[Path], output_dir: Path | None, force_existing: bool, workers: int) -> dict:
        pairs, warnings = discover_pdfs(input_paths, output_dir)
        if not pairs:
            raise ValueError("No PDF files were found")
        run_id = new_id("run")
        documents = []
        for root, pdf_path in pairs:
            out_path = output_path_for(root, pdf_path, output_dir)
            exists = out_path.exists()
            documents.append({
                "id": new_id("doc"), "source_path": str(pdf_path), "output_path": str(out_path),
                "root": str(root), "status": "pending" if force_existing or not exists else "skipped",
                "page_count": None, "attempts": 0, "error": None,
                "started_at": None, "finished_at": None, "credential_id": None,
            })
        run = {
            "id": run_id, "status": "created", "created_at": now_iso(), "started_at": None,
            "finished_at": None, "input_paths": [str(path) for path in input_paths],
            "output_dir": str(output_dir) if output_dir else "", "output_mode": "central" if output_dir else "same_folder",
            "force_existing": force_existing,
            "workers": max(1, int(workers)), "total": len(documents),
            "message": "Ready", "warnings": warnings,
        }
        with self.lock:
            self.store.save_run(run)
            self.store.save_documents(run_id, documents)
        return self.details(run_id)

    def details(self, run_id: str) -> dict:
        run = self.store.load_run(run_id)
        if not run:
            raise ValueError("Run not found")
        documents = self.store.load_documents(run_id)
        counts: dict[str, int] = {}
        for document in documents:
            status = document.get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
        run["counts"] = counts
        return {"run": run, "documents": documents}

    def start(self, run_id: str) -> dict:
        with self.lock:
            if run_id in self.threads and self.threads[run_id].is_alive():
                return self.details(run_id)
            run = self.store.load_run(run_id)
            if not run:
                raise ValueError("Run not found")
            run["status"] = "running"
            run["started_at"] = run.get("started_at") or now_iso()
            run["message"] = "Processing"
            self.store.save_run(run)
            pause = threading.Event()
            cancel = threading.Event()
            self.controls[run_id] = {"pause": pause, "cancel": cancel}
            thread = threading.Thread(target=self._execute, args=(run_id,), daemon=True, name=f"run-{run_id}")
            self.threads[run_id] = thread
            thread.start()
        return self.details(run_id)

    def pause(self, run_id: str) -> dict:
        control = self.controls.get(run_id)
        run = self.store.load_run(run_id)
        if control and run and run.get("status") == "running":
            control["pause"].set()
            run["status"] = "paused"
            run["message"] = "Paused"
            self.store.save_run(run)
        return self.details(run_id)

    def resume(self, run_id: str) -> dict:
        control = self.controls.get(run_id)
        run = self.store.load_run(run_id)
        if control and run and run.get("status") == "paused":
            control["pause"].clear()
            run["status"] = "running"
            run["message"] = "Processing"
            self.store.save_run(run)
            return self.details(run_id)
        return self.start(run_id)

    def cancel(self, run_id: str) -> dict:
        control = self.controls.get(run_id)
        run = self.store.load_run(run_id)
        if control and run:
            control["cancel"].set()
            run["status"] = "cancelled"
            run["message"] = "Cancelled"
            self.store.save_run(run)
        return self.details(run_id)

    def _execute(self, run_id: str) -> None:
        run = self.store.load_run(run_id)
        if not run:
            return
        documents = self.store.load_documents(run_id)
        credentials = self.store.credentials()
        if not credentials:
            run["status"] = "failed"
            run["message"] = "Add an API key before starting OCR"
            run["finished_at"] = now_iso()
            self.store.save_run(run)
            return
        logger = logging.getLogger(f"batch-ocr-studio.{run_id}")
        key_manager = KeyManager(self.store, credentials)
        pending = [document for document in documents if document.get("status") == "pending"]
        if not pending:
            run["status"] = "completed"
            run["finished_at"] = now_iso()
            self.store.save_run(run)
            return
        controls = self.controls[run_id]
        worker_count = min(len(credentials), len(pending))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ocr") as executor:
            futures = {
                executor.submit(self._process_document, run_id, document, key_manager, controls, logger): document
                for document in pending
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    document = futures[future]
                    document["status"] = "failed"
                    document["error"] = str(error)
                    self.store.save_documents(run_id, documents)
        documents = self.store.load_documents(run_id)
        run = self.store.load_run(run_id) or run
        if run.get("status") not in ("cancelled", "failed"):
            run["status"] = "completed" if not any(item.get("status") == "failed" for item in documents) else "completed_with_errors"
            run["message"] = "Finished"
            run["finished_at"] = now_iso()
            self.store.save_run(run)
        self.controls.pop(run_id, None)

    def _process_document(self, run_id: str, document: dict, key_manager: KeyManager, controls: dict, logger: logging.Logger) -> None:
        while controls["pause"].is_set() and not controls["cancel"].is_set():
            time.sleep(0.25)
        if controls["cancel"].is_set():
            return
        with self.lock:
            documents = self.store.load_documents(run_id)
            current = next((item for item in documents if item["id"] == document["id"]), document)
            current["status"] = "running"
            current["started_at"] = now_iso()
            current["attempts"] = current.get("attempts", 0) + 1
            self.store.save_documents(run_id, documents)
        result_status = "failed"
        result_error = "OCR failed"
        source_path = Path(current["source_path"])
        output_path = Path(current["output_path"])
        try:
            page_count = len(PdfReader(source_path).pages)
            with self.lock:
                documents = self.store.load_documents(run_id)
                current = next((item for item in documents if item["id"] == document["id"]), current)
                current["page_count"] = page_count
                self.store.save_documents(run_id, documents)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_root = output_path.parent / ".chunks_tmp" / hashlib.sha1(current["source_path"].encode()).hexdigest()[:10]
            markdown, error = ocr_recursive(source_path, key_manager, tmp_root, logger)
            shutil.rmtree(tmp_root, ignore_errors=True)
            if markdown is None:
                result_error = error or "OCR failed"
            else:
                temporary = output_path.with_name(output_path.name + ".tmp")
                temporary.write_text(markdown, encoding="utf-8")
                temporary.replace(output_path)
                result_status = "completed"
                result_error = None
        except Exception as error:
            result_error = str(error)
        with self.lock:
            documents = self.store.load_documents(run_id)
            current = next((item for item in documents if item["id"] == document["id"]), current)
            current["status"] = result_status
            current["error"] = result_error
            current["finished_at"] = now_iso()
            self.store.save_documents(run_id, documents)
