"""Telegram chat registry and message log.

WHY THIS EXISTS
    The Telegram chat-brain plugin needs a lightweight, local registry of chats
    that the bot has seen, along with a bounded message log so the user can pick
    a chat or reply to a recent message without polling Telegram every time.

HOW IT WORKS
    The registry is a simple JSON dictionary keyed by numeric chat id. Each entry
    stores the chat type, title, username, status and the last time the bot saw it.
    Writes are atomic to avoid leaving the registry half-written.

SECURITY
    The data is local-only, never shared and never printed. Only the bot token and
    recent chat metadata are retained. The log is bounded and purged on age/size.

WHAT IT DOES NOT DO
    It does not poll Telegram, does not log private data beyond modest metadata,
    and does not read or write message bodies outside the local bounded cache.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
REGISTRY_PATH = CONFIG_DIR / "tg_chats.json"
LOG_PATH = CONFIG_DIR / "tg_chat_log.json"


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_registry() -> dict[str, dict[str, Any]]:
    _ensure_dir()
    try:
        if not REGISTRY_PATH.exists():
            return {}
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_registry(data: dict[str, dict[str, Any]]) -> None:
    _ensure_dir()
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, REGISTRY_PATH)


def load_log() -> list[dict[str, Any]]:
    _ensure_dir()
    try:
        if not LOG_PATH.exists():
            return []
        data = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_log(entries: list[dict[str, Any]]) -> None:
    _ensure_dir()
    tmp = LOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, LOG_PATH)


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def chat_display_name(chat: dict[str, Any]) -> str:
    title = _safe_str(chat.get("title"))
    username = _safe_str(chat.get("username"))
    return title or username or f"Chat {chat.get('id', 'unknown')}"


def upsert_chat(chat: dict[str, Any]) -> dict[str, Any]:
    registry = load_registry()
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return chat
    entry = registry.get(chat_id, {}) if isinstance(registry.get(chat_id), dict) else {}
    merged = {**entry, **chat}
    merged["id"] = int(chat_id) if chat_id.isdigit() else chat_id
    registry[chat_id] = merged
    save_registry(registry)
    return merged


def forget_chat(chat_id: str | int) -> bool:
    registry = load_registry()
    key = str(chat_id)
    if key in registry:
        del registry[key]
        save_registry(registry)
        return True
    return False


def list_chats() -> list[dict[str, Any]]:
    registry = load_registry()
    return sorted(registry.values(), key=lambda item: str(item.get("title") or item.get("username") or "").lower())


def add_log_entry(entry: dict[str, Any]) -> None:
    entries = load_log()
    entries.append(entry)
    max_entries = 300
    if len(entries) > max_entries:
        entries = entries[-max_entries:]
    save_log(entries)


def read_recent_messages(limit: int = 20) -> list[dict[str, Any]]:
    entries = load_log()
    entries = sorted(entries, key=lambda item: item.get("timestamp", 0), reverse=True)
    return entries[: max(1, int(limit))]


def build_from_updates(updates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Returns the chats that were seen inside the incoming Telegram update list."""
    seen: list[dict[str, Any]] = []
    for update in updates or []:
        chat = None
        raw = update or {}
        for key in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
            if key in raw and isinstance(raw.get(key), dict):
                chat = raw.get(key).get("chat")
                break
        if isinstance(chat, dict):
            chat_id = chat.get("id")
            if chat_id is not None:
                entry = {
                    "id": chat_id,
                    "type": chat.get("type", "unknown"),
                    "title": chat.get("title") or chat.get("first_name") or chat.get("username") or "",
                    "username": chat.get("username") or "",
                    "can_post": bool(chat.get("can_post_messages", True)),
                }
                seen.append(entry)
    return seen


def prune_old_logs(days: int = 30) -> None:
    import time

    logs = load_log()
    cutoff = time.time() - (days * 86400)
    keep = [entry for entry in logs if float(entry.get("timestamp", 0) or 0) >= cutoff]
    save_log(keep)


__all__ = [
    "REGISTRY_PATH",
    "LOG_PATH",
    "chat_display_name",
    "load_registry",
    "save_registry",
    "upsert_chat",
    "forget_chat",
    "list_chats",
    "add_log_entry",
    "read_recent_messages",
    "build_from_updates",
    "prune_old_logs",
]
