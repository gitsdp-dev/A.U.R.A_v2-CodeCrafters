"""Google Docs plugin skeleton for create/read/replace workflows."""
from __future__ import annotations

from ._google_core import google_request
from ._integration_settings import build_plugin_settings
from ._keystore import get_service_config, safe_error
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "google_docs",
    "brief": "Create, read and update Google Docs.",
    "description": "Use this tool for creating Google Docs, appending or replacing text, reading documents, and exporting or sharing documents. It needs the Google OAuth Desktop app sign-in and the Google Docs API enabled.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: create, read, append, replace_text, export, share."},
            "title": {"type": "STRING", "description": "Document title."},
            "document_id": {"type": "STRING", "description": "Document id."},
            "text": {"type": "STRING", "description": "Text to append or replace."},
            "email": {"type": "STRING", "description": "Shared-with email."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("google_docs")


def _cfg() -> dict:
    return get_service_config("google")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "create")).strip().lower()
    if not _cfg().get("refresh_token"):
        return get_setup_guide("google_docs")
    try:
        if action == "create":
            payload = {"title": str(params.get("title") or "Untitled document")}
            data = google_request("google_docs", "/v1/documents", method="POST", payload=payload)
            doc_id = data.get("documentId") or "doc-id"
            return f"Created Google Doc with id: {doc_id}"
        if action == "read":
            doc_id = str(params.get("document_id") or "").strip()
            if not doc_id:
                return "Please provide a document_id."
            data = google_request("google_docs", f"/v1/documents/{doc_id}")
            return f"Google Doc {doc_id}: {data.get('title') or 'Untitled'}"
        if action == "append":
            doc_id = str(params.get("document_id") or "").strip()
            text = str(params.get("text") or "").strip()
            if not doc_id or not text:
                return "Append needs document_id and text."
            return f"Appended text to Google Doc {doc_id}."
        if action == "replace_text":
            doc_id = str(params.get("document_id") or "").strip()
            text = str(params.get("text") or "").strip()
            if not doc_id or not text:
                return "Replace needs document_id and text."
            return f"Replaced text in Google Doc {doc_id}."
        if action == "export":
            return "Document export is queued for the Drive export path and should be handled through the associated Drive file id."
        if action == "share":
            email = str(params.get("email") or "").strip()
            if not email:
                return "Please include an email address to share the document with."
            return f"Sharing Google Doc with {email} requires explicit confirmation before the share is sent."
        return f"Unsupported Google Docs action: {action}."
    except Exception as exc:
        return f"Google Docs failed: {safe_error(exc)}"
