"""Small atomic JSON persistence layer for Batch OCR Studio."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import keyring
except ImportError:  # pragma: no cover - handled by the UI at runtime
    keyring = None


APP_NAME = "Batch OCR Studio"
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
SERVICE_NAME = "batch-ocr-studio"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class JsonStore:
    def __init__(self, root: Path = DATA_DIR):
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def read(self, name: str, default: Any) -> Any:
        path = self.root / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    def write(self, name: str, value: Any) -> None:
        atomic_write_json(self.root / name, value)

    def settings(self) -> dict[str, Any]:
        defaults = {
            "language": "en",
            "default_output": str(self.root / "output"),
            "force_existing": False,
        }
        current = self.read("settings.json", {})
        defaults.update(current if isinstance(current, dict) else {})
        return defaults

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self.write("settings.json", settings)
        return settings

    def credentials(self) -> list[dict[str, Any]]:
        value = self.read("credentials.json", [])
        return value if isinstance(value, list) else []

    def save_credentials(self, credentials: list[dict[str, Any]]) -> None:
        self.write("credentials.json", credentials)

    def usage(self) -> dict[str, Any]:
        value = self.read("usage.json", {})
        return value if isinstance(value, dict) else {}

    def save_usage(self, usage: dict[str, Any]) -> None:
        self.write("usage.json", usage)

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "run.json"

    def documents_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "documents.json"

    def save_run(self, run: dict[str, Any]) -> None:
        path = self.run_path(run["id"])
        atomic_write_json(path, run)

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.run_path(run_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def save_documents(self, run_id: str, documents: list[dict[str, Any]]) -> None:
        atomic_write_json(self.documents_path(run_id), documents)

    def load_documents(self, run_id: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.documents_path(run_id).read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def list_runs(self) -> list[dict[str, Any]]:
        result = []
        for path in self.runs_dir.glob("*/run.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    result.append(value)
            except (json.JSONDecodeError, OSError):
                continue
        return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)

    @staticmethod
    def fingerprint(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]

    def secret_set(self, credential_id: str, secret: str) -> None:
        if keyring is None:
            raise RuntimeError("The keyring package is not installed")
        keyring.set_password(SERVICE_NAME, credential_id, secret)

    def secret_get(self, credential_id: str) -> str:
        if keyring is None:
            raise RuntimeError("The keyring package is not installed")
        secret = keyring.get_password(SERVICE_NAME, credential_id)
        if not secret:
            raise RuntimeError("No API key is stored for this credential")
        return secret

    def secret_delete(self, credential_id: str) -> None:
        if keyring is None:
            return
        try:
            keyring.delete_password(SERVICE_NAME, credential_id)
        except Exception:
            pass
