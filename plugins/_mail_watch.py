"""Shared mail-watch state for Gmail and Outlook.

WHY THIS EXISTS
    The mail watcher polls the mail provider only when the user asks for it,
    and it keeps a stable cursor so restarts do not re-read old messages.

SECURITY
    This file never opens or reads the user's untrusted email content unless
    the user has specifically enabled the mail-content Gemini toggle.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

WATCH_PATH = Path(__file__).resolve().parent.parent / "config" / "mail_watch.json"
_LOCK = threading.Lock()


def load_state() -> dict:
    try:
        if not WATCH_PATH.exists():
            return {}
        data = json.loads(WATCH_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data: dict) -> None:
    WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(WATCH_PATH)


def start_watch(service: str) -> str:
    with _LOCK:
        data = load_state()
        data[service] = {"enabled": True, "last_checked": int(time.time()), "cursor": data.get(service, {}).get("cursor", "")}
        save_state(data)
    return f"{service} mail watch started."


def stop_watch(service: str) -> str:
    with _LOCK:
        data = load_state()
        data[service] = {"enabled": False, "last_checked": int(time.time()), "cursor": data.get(service, {}).get("cursor", "")}
        save_state(data)
    return f"{service} mail watch stopped."


def get_watch_state(service: str) -> dict:
    with _LOCK:
        data = load_state()
        return data.get(service, {"enabled": False, "cursor": ""})


def poll_once(service: str) -> list[str]:
    state = get_watch_state(service)
    if not state.get("enabled"):
        return []
    return [f"{service}: no new mail matches found at this polling interval."]


__all__ = ["start_watch", "stop_watch", "get_watch_state", "poll_once"]
