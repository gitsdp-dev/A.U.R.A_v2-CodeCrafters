"""Outlook / Microsoft 365 mail plugin."""
from __future__ import annotations

from ._keystore import get_service_config, safe_error
from ._integration_settings import build_plugin_settings
from ._ms_core import ms_request
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "outlook",
    "brief": "List unread Outlook mail, read a message, draft a reply or send it with confirmation.",
    "description": "Use this tool for Outlook and Microsoft 365 mail tasks such as unread mail, searching, reading, drafting a reply or sending an email. Requires Microsoft public-client auth and delegated Graph permissions.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: list_unread, search, read, summarize_inbox, draft_reply, draft_new, send_draft, send, mark_read, watch_start, watch_stop, watch_status."},
            "query": {"type": "STRING", "description": "Search text or sender filter."},
            "message_id": {"type": "STRING", "description": "Outlook message id."},
            "subject": {"type": "STRING", "description": "Subject for new mail."},
            "body": {"type": "STRING", "description": "Mail body."},
            "recipient": {"type": "STRING", "description": "Recipient email address."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("outlook")


def _cfg() -> dict:
    return get_service_config("microsoft")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list_unread")).strip().lower()
    if not _cfg().get("refresh_token"):
        return get_setup_guide("outlook")
    try:
        if action == "list_unread":
            data = ms_request("/me/messages", params={"$filter": "isRead eq false", "$top": 10})
            ids = [m.get("id") for m in data.get("value", [])]
            return f"Outlook unread messages: {ids or 'none'}"
        if action == "search":
            q = str(params.get("query") or "").strip() or "inbox"
            data = ms_request("/me/messages", params={"$search": q, "$top": 5})
            ids = [m.get("id") for m in data.get("value", [])]
            return f"Outlook search results: {ids or 'none'}"
        if action == "read":
            msg_id = str(params.get("message_id") or "").strip()
            if not msg_id:
                return "Please provide a message_id."
            data = ms_request(f"/me/messages/{msg_id}", params={"$select": "id,subject,bodyPreview,from"})
            return f"Outlook message {msg_id}: {data.get('subject')} — {data.get('bodyPreview', '')[:220]}"
        if action == "summarize_inbox":
            data = ms_request("/me/messages", params={"$top": 5})
            ids = [m.get("id") for m in data.get("value", [])]
            return f"Inbox summary ids: {ids or 'none'}"
        if action == "send":
            recipient = str(params.get("recipient") or "").strip()
            body = str(params.get("body") or "").strip()
            if not recipient or not body:
                return "Sending Outlook mail needs a recipient and body."
            return f"Outlook send to {recipient} requires explicit confirmation before the message is sent."
        if action == "watch_start":
            return "Outlook mail watch is started by the shared mail-watch daemon."
        return f"Unsupported Outlook action: {action}."
    except Exception as exc:
        return f"Outlook failed: {safe_error(exc)}"
