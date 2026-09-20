from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "password",
    "cookie",
    "cookies",
    "credentials",
    "access_token",
    "refresh_token",
    "api_key",
    "authorization",
    "secret",
    "jwt",
}


def sanitize(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            result[str(key)] = (
                "[REDACTED]" if normalized in SENSITIVE_KEYS else sanitize(item)
            )
        return result
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    return value


class JsonStore:
    def __init__(
        self, root: Path, *, max_history_bytes: int = 10 * 1024 * 1024
    ) -> None:
        self.root = root
        self.logs_dir = root / "logs"
        self.state_dir = root / "state"
        self.backups_dir = root / "backups"
        self.max_history_bytes = max_history_bytes
        for path in (self.logs_dir, self.state_dir, self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)

    def _atomic_json(self, path: Path, payload: Any) -> None:
        clean = sanitize(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(clean, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write_latest(self, report: dict[str, Any]) -> None:
        self._atomic_json(self.logs_dir / "latest.json", report)

    def append_history(self, report: dict[str, Any]) -> None:
        path = self.logs_dir / "history.jsonl"
        if path.exists() and path.stat().st_size >= self.max_history_bytes:
            rotated = self.logs_dir / "history.jsonl.1"
            if rotated.exists():
                rotated.unlink()
            path.replace(rotated)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(sanitize(report), ensure_ascii=True, sort_keys=True) + "\n"
            )
        os.chmod(path, 0o600)

    def load_state(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        if not path.exists():
            return {"version": 1, "accounts": {}, "probe_history": []}
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return (
            data
            if isinstance(data, dict)
            else {"version": 1, "accounts": {}, "probe_history": []}
        )

    def write_state(self, state: dict[str, Any]) -> None:
        self._atomic_json(self.state_dir / "state.json", state)

    def write_backup_once(self, filename: str, payload: Any) -> Path:
        path = self.backups_dir / filename
        if not path.exists():
            self._atomic_json(path, payload)
        return path

    def load_backup(self, filename: str) -> dict[str, Any] | None:
        path = self.backups_dir / filename
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise TypeError(f"expected JSON object in backup {filename}")
        return value

    def write_backup(self, filename: str, payload: Any) -> Path:
        path = self.backups_dir / filename
        self._atomic_json(path, payload)
        return path
