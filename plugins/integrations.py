"""Service status and setup plugin for A.U.R.A integrations."""
from __future__ import annotations

from ._keystore import clear_service_config, load_config, safe_error
from ._integration_settings import build_plugin_settings
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "integrations",
    "brief": "Check which services are connected and print the setup guide for any service.",
    "description": "Shows which Google, Microsoft, Spotify and Telegram services are connected, prints the setup guide for a requested service, and can disconnect a service. Use this to check status and to fix missing credentials without guessing.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: status, setup, connect, disconnect."},
            "service": {"type": "STRING", "description": "Service name such as gmail, google_drive, outlook, onedrive, spotify, telegram_chats."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("gmail")


def _status_text() -> str:
    services = ["google", "microsoft", "spotify", "telegram_chats"]
    lines = []
    cfg = load_config()
    for service in services:
        data = cfg.get(service, {})
        if isinstance(data, dict):
            present = [k for k, v in data.items() if v not in (None, "", [], {}, False)]
            lines.append(f"{service}: {'connected' if present else 'missing'}")
        else:
            lines.append(f"{service}: missing")
    return "; ".join(lines)


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).strip().lower()
    service = str(params.get("service", "")).strip().lower()
    try:
        if action == "status":
            return _status_text()
        if action == "setup":
            if not service:
                return "Please tell me which service to guide you through. Example: gmail, outlook, spotify, telegram_chats."
            return get_setup_guide(service)
        if action == "disconnect":
            if not service:
                return "I need a service name to disconnect. Example: gmail, outlook, spotify, telegram_chats."
            clear_service_config(service)
            return f"Disconnected {service} and removed its saved credentials from config/api_keys.json."
        if action == "connect":
            if not service:
                return "I need a service name to connect. Example: gmail, outlook, spotify, telegram_chats."
            return get_setup_guide(service)
        return f"Unknown integrations action: {action}. Use status, setup, connect or disconnect."
    except Exception as exc:  # pragma: no cover
        return f"Integration setup failed: {safe_error(exc)}"
