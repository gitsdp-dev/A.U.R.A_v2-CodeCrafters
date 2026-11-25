"""Secret storage for private ICS URLs, app passwords, and OAuth tokens.

Uses OS keyring when available, falling back to a user-only-permission JSON file
under the per-user calendar data directory. Never logs secret values.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path

from core.calendar.models import get_user_data_dir

LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "AURA.Calendar"


class SecretStore:
    """Stores calendar secrets via OS keyring with a private-file fallback."""

    def __init__(self, fallback_path: Path | None = None, force_file_only: bool = False) -> None:
        self._fallback_path = (
            Path(fallback_path)
            if fallback_path is not None
            else (get_user_data_dir() / ".calendar_secrets.json")
        )
        self._lock = threading.RLock()
        self._using_keyring = False if force_file_only else self._probe_keyring()

    @property
    def using_keyring(self) -> bool:
        return self._using_keyring

    @staticmethod
    def _probe_keyring() -> bool:
        try:
            import keyring
            from keyring.errors import NoKeyringError

            backend = keyring.get_keyring()
            # Reject fail-only / null backends
            if backend is None or "fail" in type(backend).__name__.lower():
                return False
            # Quick non-destructive check
            try:
                keyring.get_password(SERVICE_NAME, "__aura_probe__")
            except NoKeyringError:
                return False
            return True
        except Exception:
            return False

    def set_secret(self, key: str, value: str) -> None:
        if not key:
            raise ValueError("Secret key must be non-empty.")
        if self._using_keyring:
            try:
                import keyring

                keyring.set_password(SERVICE_NAME, key, value)
                return
            except Exception as exc:
                LOGGER.warning("Keyring write failed; falling back to private file (%s).", type(exc).__name__)
                self._using_keyring = False

        with self._lock:
            data = self._read_file()
            data[key] = value
            self._write_file(data)

    def get_secret(self, key: str) -> str | None:
        if not key:
            return None
        if self._using_keyring:
            try:
                import keyring

                val = keyring.get_password(SERVICE_NAME, key)
                if val is not None:
                    return val
            except Exception as exc:
                LOGGER.warning("Keyring read failed; checking private file (%s).", type(exc).__name__)
        with self._lock:
            return self._read_file().get(key)

    def delete_secret(self, key: str) -> None:
        if not key:
            return
        if self._using_keyring:
            try:
                import keyring

                keyring.delete_password(SERVICE_NAME, key)
            except Exception:
                pass
        with self._lock:
            data = self._read_file()
            if key in data:
                del data[key]
                self._write_file(data)

    def _read_file(self) -> dict[str, str]:
        if not self._fallback_path.exists():
            return {}
        try:
            raw = json.loads(self._fallback_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): str(v) for k, v in raw.items()}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _write_file(self, data: dict[str, str]) -> None:
        self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._fallback_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self._fallback_path)
        try:
            os.chmod(self._fallback_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
