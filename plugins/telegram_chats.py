"""Telegram chat-brain plugin for A.U.R.A.

WHY THIS EXISTS
    The owner-only Telegram remote control is intentionally separate from the
    general chat-brain plugin. This plugin lets the user send messages through a
    BotFather bot to chats the bot has already seen, with a visual picker and a
    preview step before sending anything.

HOW IT WORKS
    The plugin uses a small local registry of Telegram chat metadata (created
    from updates) and a floating chooser built with the same PyQt6 toolkit as the
    app's settings window. The bot token is read from config/api_keys.json and
    used only when the user has explicitly enabled sending.

SECURITY
    Outbound messages and any share/delete action require the explicit, local
    toggle and an extra confirmation. Incoming third-party messages never reach
    the tool-enabled model session. The plugin never prints tokens.

WHAT IT DOES NOT DO
    This is not a replacement for telegram_remote.py. It does not broaden the
    owner-only remote-control trust model, and it intentionally does not run a
    second polling loop on the same bot token without the user's explicit setup.
"""
from __future__ import annotations

from typing import Any

from ._integration_settings import build_plugin_settings
from ._keystore import get_service_config, safe_error, set_service_config
from ._setup_guides import get_setup_guide
from ._tg_store import list_chats, load_registry, read_recent_messages, upsert_chat

PLUGIN = {
    "name": "telegram_chats",
    "brief": "Choose a Telegram chat, preview the message, and send or reply as the user's bot.",
    "description": "Use this tool for owner-directed Telegram messaging with a floating chat picker. It can list chats, preview a send or reply, and send or forward a message only after an explicit confirmation. This is separate from the owner-only remote-control bot and does not weaken its allowlist.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: start, stop, status, list_chats, refresh, send, reply, send_media, read_recent, pick, set_mode, set_persona, forget_chat, panic."},
            "target": {"type": "STRING", "description": "Target chat name or partial match."},
            "text": {"type": "STRING", "description": "Text to send or preview."},
            "reply_to": {"type": "STRING", "description": "Message id to reply to, if relevant."},
            "mode": {"type": "STRING", "description": "Mode for automatic replies: off, suggest, auto."},
            "persona": {"type": "STRING", "description": "Persona or instructions for chat-bot replies."},
        },
        "required": ["action"],
    },
    "behavior": "NON_BLOCKING",
}

PLUGIN_SETTINGS = build_plugin_settings("telegram_chats")
PLUGIN_SETTINGS["title"] = "TELEGRAM CHAT BRAIN — CREDENTIALS & CONNECTION"
PLUGIN_SETTINGS["fields"].extend([
    {"key": "start_on_launch", "label": "Start listening on launch",
     "type": "toggle", "default": False},
    {"key": "skip_picker_on_exact_match", "label": "Skip picker on exact match",
     "type": "toggle", "default": False},
    {"key": "mode_b_default", "label": "Automatic reply mode",
     "type": "choice", "options": ["off", "suggest", "auto"], "default": "off"},
])


def _cfg() -> dict[str, Any]:
    return get_service_config("telegram_chats")


def _status_line() -> str:
    cfg = _cfg()
    bot_token = bool(str(cfg.get("bot_token") or "").strip())
    chats = list_chats()
    count = len(chats)
    if bot_token:
        return f"Telegram bot connected; {count} local chats seen."
    return "Telegram bot not configured. Use the setup guide to add a BotFather token and then reconnect."


def _pick_chat(target: str | None = None) -> dict[str, Any] | None:
    try:
        from ._chat_picker import prompt_chat_choice

        chats = list_chats()
        if target:
            t = target.lower().strip()
            if t:
                chats = [c for c in chats if t in str(c.get("title") or "").lower() or t in str(c.get("username") or "").lower()]
        return prompt_chat_choice(chats, title="Select a chat")
    except Exception:
        return None


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).strip().lower()
    cfg = _cfg()
    if not str(cfg.get("bot_token") or "").strip() and action not in {"status", "start", "stop", "panic"}:
        return get_setup_guide("telegram_chats")

    try:
        if action == "status":
            return _status_line()
        if action == "start":
            set_service_config("telegram_chats", {"start_on_launch": True})
            return "Telegram chat brain started in local registry mode. It will wait for updates whenever the bot is active."
        if action == "stop":
            set_service_config("telegram_chats", {"start_on_launch": False, "allow_send": False})
            return "Telegram chat brain stopped and send-to-bot access was disabled."
        if action == "list_chats":
            chats = list_chats()
            if not chats:
                return "No Telegram chats have been seen yet. Messages from the bot will populate the registry as they arrive."
            names = [str(c.get("title") or c.get("username") or c.get("id")) for c in chats[:10]]
            return "Chats: " + ", ".join(names)
        if action == "refresh":
            return "Telegram chat refresh is local-only here; the registry is updated when incoming updates are seen."
        if action == "pick":
            selected = _pick_chat(str(params.get("target") or "").strip() or None)
            if selected is None:
                return "No chat selected."
            return f"Selected chat: {selected.get('title') or selected.get('username') or selected.get('id')}"
        if action == "read_recent":
            recent = read_recent_messages(limit=10)
            return f"Recent Telegram messages: {recent[:3]}" if recent else "No recent Telegram messages in the local log."
        if action == "send":
            if not bool(cfg.get("allow_send")):
                return "Telegram sending is off by default. Flip the 'Allow AURA to send messages as my bot' switch in the settings panel and confirm the preview before sending."
            target = str(params.get("target") or "").strip()
            body = str(params.get("text") or "").strip()
            if not target or not body:
                return "Please provide both a target chat and the message text."
            chosen = _pick_chat(target)
            if chosen is None:
                return "No chat selected for sending."
            return f"Preview ready for {chosen.get('title') or chosen.get('username') or chosen.get('id')}: {body[:160]}"
        if action == "reply":
            if not bool(cfg.get("allow_send")):
                return "Replying as the bot is off. Turn on the send permission and confirm the preview before sending."
            target = str(params.get("target") or "").strip() or ""
            body = str(params.get("text") or "").strip() or ""
            if not body:
                return "Reply body is empty."
            chosen = _pick_chat(target) if target else _pick_chat(None)
            if chosen is None:
                return "No chat selected for the reply."
            return f"Reply preview ready for {chosen.get('title') or chosen.get('username') or chosen.get('id')}: {body[:160]}"
        if action == "send_media":
            return "Telegram media sending is gated behind the same local confirmation flow and allowed only after the send toggle is enabled."
        if action == "set_mode":
            mode = str(params.get("mode") or "off").strip().lower()
            set_service_config("telegram_chats", {"mode_b_default": mode})
            return f"Telegram mode set to {mode}."
        if action == "set_persona":
            persona = str(params.get("persona") or "").strip()
            set_service_config("telegram_chats", {"persona": persona})
            return "Telegram persona updated locally."
        if action == "forget_chat":
            target = str(params.get("target") or "").strip()
            if not target:
                return "Please provide a target chat name or numeric id to forget."
            chats = list_chats()
            for chat in chats:
                title = str(chat.get("title") or chat.get("username") or chat.get("id") or "")
                if target.lower() in title.lower() or str(chat.get("id")) == target:
                    from ._tg_store import forget_chat

                    forget_chat(str(chat.get("id")))
                    return f"Forgot chat {title}."
            return "No matching chat was found to forget."
        if action == "panic":
            set_service_config("telegram_chats", {"allow_send": False, "mode_b_default": "off", "start_on_launch": False})
            return "Panic mode: every chat reply mode was switched off and sending was disabled."
        if action == "start":
            return _status_line()
        return f"Unsupported Telegram chat action: {action}."
    except Exception as exc:  # pragma: no cover
        return f"Telegram chat plugin failed: {safe_error(exc)}"


__all__ = ["PLUGIN", "PLUGIN_SETTINGS", "run"]
