"""OneDrive helper plugin."""
from __future__ import annotations

from ._keystore import get_service_config, safe_error
from ._integration_settings import build_plugin_settings
from ._ms_core import ms_request
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "onedrive",
    "brief": "List, search and share OneDrive files.",
    "description": "Use this tool for OneDrive file listing, metadata fetches, sharing links and downloads. It requires the Microsoft public-client flow and delegated Files.ReadWrite permissions.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: list, search, info, download, upload, create_folder, move, rename, share_link, trash."},
            "query": {"type": "STRING", "description": "Search query or folder name."},
            "file_id": {"type": "STRING", "description": "OneDrive file id."},
            "folder_name": {"type": "STRING", "description": "Folder name to create or target."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("onedrive")


def _cfg() -> dict:
    return get_service_config("microsoft")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).strip().lower()
    if not _cfg().get("refresh_token"):
        return get_setup_guide("onedrive")
    try:
        if action == "list":
            data = ms_request("/me/drive/root/children", params={"$top": 10})
            names = [f.get("name") for f in data.get("value", [])]
            return f"OneDrive root items: {names or 'none'}"
        if action == "search":
            q = str(params.get("query") or "").strip() or ""
            if q:
                data = ms_request(f"/me/drive/root/search(q='{q}')", params={"$top": 10})
            else:
                data = ms_request("/me/drive/root/children", params={"$top": 10})
            return f"OneDrive search results: {data.get('value', [])[:5]}"
        if action == "info":
            file_id = str(params.get("file_id") or "").strip()
            if not file_id:
                return "Please provide a file_id."
            data = ms_request(f"/me/drive/items/{file_id}")
            return f"OneDrive item {file_id}: {data.get('name')}"
        if action == "download":
            return "Download is allowed only to a user-chosen destination folder and requires explicit confirmation before saving to disk."
        if action == "share_link":
            return "Share link creation requires explicit confirmation before it is sent."
        return f"Unsupported OneDrive action: {action}."
    except Exception as exc:
        return f"OneDrive failed: {safe_error(exc)}"
