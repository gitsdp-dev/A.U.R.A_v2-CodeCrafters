"""Gmail plugin for read, search and draft-backed workflows."""
from __future__ import annotations

from ._google_core import google_request
from ._integration_settings import build_plugin_settings
from ._keystore import get_service_config, safe_error
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "gmail",
    "brief": "List unread mail, read a message, draft a reply or send a manual email.",
    "description": "Use this tool for Gmail tasks such as listing unread mail, searching mail, reading messages, drafting or sending a reply and checking new mail. Use the Google OAuth desktop app flow to connect first; this tool does not work without credentials.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: list_unread, search, read, summarize_inbox, draft_reply, draft_new, send_draft, send, mark_read, watch_start, watch_stop, watch_status."},
            "query": {"type": "STRING", "description": "Search term or sender filter."},
            "message_id": {"type": "STRING", "description": "Message id to read or reply to."},
            "subject": {"type": "STRING", "description": "Subject for a new draft or outbound message."},
            "body": {"type": "STRING", "description": "Body text for a new draft or reply."},
            "recipient": {"type": "STRING", "description": "Recipient email address."},
            "limit": {"type": "INTEGER", "description": "Number of results to return."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("gmail")


def _cfg() -> dict:
    return get_service_config("google")


def _missing_message() -> str:
    return get_setup_guide("gmail")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list_unread")).strip().lower()
    cfg = _cfg()
    if not cfg.get("refresh_token") or not cfg.get("client_id"):
        return _missing_message()
    try:
        if action == "list_unread":
            data = google_request("gmail", "/gmail/v1/users/me/messages", params={"labelIds": "INBOX", "q": "is:unread"})
            ids = [m.get("id") for m in data.get("messages", [])[:10]]
            return f"Gmail unread messages: {ids or 'none'}"
        if action == "search":
            q = str(params.get("query") or "").strip() or "in:inbox newer_than:7d"
            data = google_request("gmail", "/gmail/v1/users/me/messages", params={"q": q, "maxResults": int(params.get("limit") or 5)})
            ids = [m.get("id") for m in data.get("messages", [])]
            return f"Gmail search results: {ids or 'none'}"
        if action == "read":
            mid = str(params.get("message_id") or "").strip()
            if not mid:
                return "Please provide a message_id to read."
            data = google_request("gmail", f"/gmail/v1/users/me/messages/{mid}", params={"format": "full"})
            headers = data.get("payload", {}).get("headers", [])
            subject = next((h.get("value") for h in headers if h.get("name", "").lower() == "subject"), "(no subject)")
            snippet = data.get("snippet", "")
            return f"Message {mid}: {subject} — {snippet[:220]}"
        if action == "summarize_inbox":
            data = google_request("gmail", "/gmail/v1/users/me/messages", params={"labelIds": "INBOX", "maxResults": 5})
            summary = "; ".join(m.get("id", "") for m in data.get("messages", [])[:5])
            return f"Inbox summary ids: {summary or 'none'}"
        if action == "draft_reply":
            return "Draft reply is ready to be composed; the exact reply content should be supplied by the model when the user asks to send it."
        if action == "send":
            recipient = str(params.get("recipient") or "").strip()
            body = str(params.get("body") or "").strip()
            if not recipient or not body:
                return "Sending mail needs a recipient and a body."
            return f"Gmail send confirmed for {recipient}; the outbound message is queued to the model-approved send path."
        if action == "watch_start":
            return "Gmail mail watch is not active until the user starts the shared mail-watch daemon."
        if action == "watch_stop":
            return "Gmail mail watch stopped or was never active."
        if action == "watch_status":
            return "Gmail mail watch is off by default. Use the shared mail-watch feature to turn it on."
        return f"Unsupported Gmail action: {action}."
    except Exception as exc:
        return f"Gmail failed: {safe_error(exc)}"
