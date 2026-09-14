"""Batch OCR Studio desktop entry point."""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from pathlib import Path

try:
    import webview
except ImportError:  # pragma: no cover
    webview = None

from engine import RunManager, clean_path, discover_pdfs, parse_path_lines
from storage import JsonStore, new_id, now_iso


ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
APP_VERSION = "0.1.0"


class Api:
    def __init__(self, store: JsonStore, runs: RunManager):
        self.store = store
        self.runs = runs

    def _credential_view(self, credential: dict, usage: dict) -> dict:
        item = dict(credential)
        item.pop("secret_ref", None)
        item["masked"] = f"•••• {item.get('last4', '')}".strip()
        item["usage"] = usage.get("credentials", {}).get(credential["id"], {
            "pages_used": 0, "requests": 0, "dead": False,
        })
        return item

    def state(self) -> dict:
        settings = self.store.settings()
        usage = self.store.usage()
        history = []
        for item in self.store.list_runs()[:50]:
            try:
                detail = self.runs.details(item["id"])
                detail["run"]["pages"] = sum(document.get("page_count") or 0 for document in detail["documents"])
                history.append(detail["run"])
            except ValueError:
                history.append(item)
        return {
            "settings": settings,
            "version": APP_VERSION,
            "credentials": [self._credential_view(item, usage) for item in self.store.credentials()],
            "runs": history,
            "platform": platform.system(),
        }

    def save_settings(self, patch: dict) -> dict:
        settings = self.store.settings()
        settings.update({key: value for key, value in patch.items() if key in {
            "language", "default_output", "force_existing"
        }})
        self.store.save_settings(settings)
        return settings

    def pick_files(self) -> list[str]:
        if webview is None or not webview.windows:
            return []
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("PDF files (*.pdf)", "All files (*.*)"),
        )
        return list(result or [])

    def pick_folder(self) -> list[str]:
        if webview is None or not webview.windows:
            return []
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=True)
        return list(result or [])

    def pick_json(self) -> list[str]:
        if webview is None or not webview.windows:
            return []
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("JSON files (*.json)",),
        )
        return list(result or [])

    def validate_paths(self, text: str, output_dir: str = "", same_folder: bool = False) -> dict:
        paths = parse_path_lines(text)
        output = None if same_folder else (clean_path(output_dir) if output_dir.strip() else Path(self.store.settings()["default_output"]).resolve())
        pairs, warnings = discover_pdfs(paths, output)
        return {
            "paths": [str(path) for path in paths],
            "warnings": warnings,
            "count": len(pairs),
            "files": [{"source": str(pdf), "root": str(root)} for root, pdf in pairs[:500]],
        }

    def create_run(self, text: str, output_dir: str, same_folder: bool, force_existing: bool) -> dict:
        paths = parse_path_lines(text)
        if not paths:
            raise ValueError("Add at least one PDF file or folder")
        output = None if same_folder else (clean_path(output_dir) if output_dir.strip() else Path(self.store.settings()["default_output"]).resolve())
        enabled_keys = sum(1 for item in self.store.credentials() if item.get("enabled", True))
        run = self.runs.create_run(paths, output, bool(force_existing), max(1, enabled_keys))
        return run

    def start_run(self, run_id: str) -> dict:
        return self.runs.start(run_id)

    def get_run(self, run_id: str) -> dict:
        return self.runs.details(run_id)

    def pause_run(self, run_id: str) -> dict:
        return self.runs.pause(run_id)

    def resume_run(self, run_id: str) -> dict:
        return self.runs.resume(run_id)

    def cancel_run(self, run_id: str) -> dict:
        return self.runs.cancel(run_id)

    def open_path(self, path: str) -> bool:
        target = clean_path(path)
        if not target.exists():
            raise ValueError("Path does not exist")
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{target}"')
        else:
            os.system(f'xdg-open "{target}"')
        return True

    def add_credential(self, provider: str, name: str, email: str, secret: str, renewal_date: str = "") -> dict:
        provider = (provider or "mistral").strip().lower()
        name = (name or "").strip()
        email = (email or "").strip()
        secret = (secret or "").strip()
        if not name or not secret:
            raise ValueError("A name and API key are required")
        credentials = self.store.credentials()
        fingerprint = self.store.fingerprint(secret)
        if any(item.get("fingerprint") == fingerprint for item in credentials):
            raise ValueError("That API key is already saved")
        credential_id = new_id("cred")
        self.store.secret_set(credential_id, secret)
        item = {
            "id": credential_id, "provider": provider, "name": name, "email": email,
            "fingerprint": fingerprint, "last4": secret[-4:], "enabled": True,
            "renewal_date": (renewal_date or "").strip(),
            "created_at": now_iso(), "updated_at": now_iso(), "status": "ready",
        }
        credentials.append(item)
        self.store.save_credentials(credentials)
        return self._credential_view(item, self.store.usage())

    def update_credential(self, credential_id: str, name: str, email: str, enabled: bool, renewal_date: str = "", secret: str = "") -> dict:
        credentials = self.store.credentials()
        item = next((value for value in credentials if value["id"] == credential_id), None)
        if not item:
            raise ValueError("Credential not found")
        item["name"] = (name or "").strip() or item["name"]
        item["email"] = (email or "").strip()
        item["enabled"] = bool(enabled)
        item["renewal_date"] = (renewal_date or "").strip()
        item["updated_at"] = now_iso()
        if secret.strip():
            fingerprint = self.store.fingerprint(secret.strip())
            if any(value["id"] != credential_id and value.get("fingerprint") == fingerprint for value in credentials):
                raise ValueError("That API key is already saved")
            self.store.secret_set(credential_id, secret.strip())
            item["fingerprint"] = fingerprint
            item["last4"] = secret.strip()[-4:]
            item["status"] = "ready"
        self.store.save_credentials(credentials)
        return self._credential_view(item, self.store.usage())

    def delete_credential(self, credential_id: str) -> bool:
        credentials = self.store.credentials()
        if not any(item["id"] == credential_id for item in credentials):
            raise ValueError("Credential not found")
        self.store.secret_delete(credential_id)
        self.store.save_credentials([item for item in credentials if item["id"] != credential_id])
        return True

    def import_credentials(self, path: str) -> dict:
        source = clean_path(path)
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("The JSON file must contain a list")
        imported = 0
        skipped = 0
        for index, entry in enumerate(value, start=1):
            if isinstance(entry, str):
                email, secret, name = "", entry.strip(), f"Imported account {index}"
            elif isinstance(entry, dict):
                email = str(entry.get("email", "")).strip()
                secret = str(entry.get("key", entry.get("api_key", ""))).strip()
                name = str(entry.get("name", email or f"Imported account {index}")).strip()
            else:
                skipped += 1
                continue
            if not secret or any(item.get("fingerprint") == self.store.fingerprint(secret) for item in self.store.credentials()):
                skipped += 1
                continue
            self.add_credential("mistral", name, email, secret)
            imported += 1
        return {"imported": imported, "skipped": skipped}

    def test_credential(self, credential_id: str) -> dict:
        credential = next((item for item in self.store.credentials() if item["id"] == credential_id), None)
        if not credential:
            raise ValueError("Credential not found")
        secret = self.store.secret_get(credential_id)
        try:
            from mistralai.client import Mistral
            client = Mistral(api_key=secret)
            models = getattr(client, "models", None)
            if models is not None and hasattr(models, "list"):
                models.list()
            credential["status"] = "verified"
            credential["updated_at"] = now_iso()
            self.store.save_credentials(self.store.credentials())
            return {"ok": True, "message": "Connection succeeded"}
        except Exception as error:
            credential["status"] = "error"
            credential["updated_at"] = now_iso()
            self.store.save_credentials(self.store.credentials())
            return {"ok": False, "message": str(error)}


def main() -> None:
    if webview is None:
        raise SystemExit("Install dependencies first: pip install -r requirements.txt")
    store = JsonStore()
    logging.basicConfig(
        filename=str(store.root / "app.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    runs = RunManager(store)
    api = Api(store, runs)
    window = webview.create_window(
        f"Batch OCR Studio v{APP_VERSION}",
        str(UI_DIR / "index.html"),
        js_api=api,
        width=1280,
        height=820,
        min_size=(980, 640),
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
