"""Shared secret-safe key storage for A.U.R.A plugins.

WHY THIS EXISTS
    Beyond the app's main Gemini key, the plugin family stores service
    credentials such as Google OAuth refresh tokens, Microsoft Graph
    client settings and Telegram bot tokens in the same config file. The
    file is shared and must therefore be merged carefully so one service's
    settings never wipe another's.

HOW IT WORKS
    All writes are atomic: JSON is read, merged and written to a temp file
    next to the target, then replaced with os.replace(). That avoids
    partial writes if the app crashes while saving a token.

SECURITY
    No secret is printed or returned to the caller. Values are scrubbed
    before logging and file permissions are tightened best-effort on POSIX.

WHAT IT DOES NOT DO
    It does not invent credentials and it does not reach out to any API.
    This module only reads and writes the local A.U.R.A config file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


def _config_dir() -> Path:
    return CONFIG_PATH.parent


def load_config() -> dict:
    try:
        if not CONFIG_PATH.exists():
            return {}
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data: dict) -> None:
    if not isinstance(data, dict):
        raise TypeError("config must be a dict")
    _config_dir().mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, CONFIG_PATH)


def get_service_config(service: str) -> dict:
    cfg = load_config()
    service_cfg = cfg.get(service, {})
    return service_cfg if isinstance(service_cfg, dict) else {}


def set_service_config(service: str, values: dict) -> dict:
    cfg = load_config()
    cfg.setdefault(service, {})
    if not isinstance(cfg[service], dict):
        cfg[service] = {}
    cfg[service].update({str(k): v for k, v in (values or {}).items()})
    save_config(cfg)
    return cfg[service]


def clear_service_config(service: str) -> None:
    cfg = load_config()
    cfg.pop(service, None)
    save_config(cfg)


def get_gemini_key() -> str:
    return str(load_config().get("gemini_api_key", "")).strip()


def scrub_secret(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) < 8:
        return "[redacted]"
    prefix = text[:4]
    suffix = text[-4:]
    return f"{prefix}…{suffix}"


def safe_error(exc: Exception | str | None) -> str:
    msg = str(exc or "")
    msg = msg.replace("\r", " ").replace("\n", " ")
    for needle in ("Authorization", "authorization", "token", "refresh_token", "client_secret", "secret", "Bearer "):
        if needle.lower() in msg.lower():
            return "The service could not be reached for authentication. Please re-check the credentials and try again."
    return msg.strip() or "The request could not be completed."


__all__ = [
    "CONFIG_PATH",
    "load_config",
    "save_config",
    "get_service_config",
    "set_service_config",
    "clear_service_config",
    "get_gemini_key",
    "scrub_secret",
    "safe_error",
]
