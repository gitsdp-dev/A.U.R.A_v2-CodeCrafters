"""Google Drive plugin skeleton for list/search/upload/download actions."""
from __future__ import annotations

from ._google_core import google_request
from ._integration_settings import build_plugin_settings
from ._keystore import get_service_config, safe_error
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "google_drive",
    "brief": "List, upload, download, move and share Drive files.",
    "description": "Use this tool for Google Drive actions such as listing and searching files, creating folders, uploading files, renaming a file, moving it, generating a share link and trashing a file. Requires the Google Drive API and a Desktop OAuth app.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: list, search, info, download, upload, create_folder, move, rename, share_link, trash."},
            "query": {"type": "STRING", "description": "Search query or folder name."},
            "file_id": {"type": "STRING", "description": "Drive file id."},
            "folder_name": {"type": "STRING", "description": "Folder name to create or target."},
            "destination": {"type": "STRING", "description": "Destination path or folder id."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("google_drive")


def _cfg() -> dict:
    return get_service_config("google")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).strip().lower()
    if not _cfg().get("refresh_token"):
        return get_setup_guide("google_drive")
    try:
        if action == "list":
            data = google_request("google_drive", "/drive/v3/files", params={"pageSize": 10, "fields": "nextPageToken, files(id, name, mimeType)"})
            files = data.get("files", [])
            return f"Drive files: {[(f.get('id'), f.get('name')) for f in files[:5]]}"
        if action == "search":
            query = str(params.get("query") or "").strip() or "trashed = false"
            data = google_request("google_drive", "/drive/v3/files", params={"q": query, "pageSize": 10, "fields": "files(id, name, mimeType)"})
            return f"Drive search results: {data.get('files', [])[:5]}"
        if action == "info":
            file_id = str(params.get("file_id") or "").strip()
            if not file_id:
                return "Please provide a file_id."
            data = google_request("google_drive", f"/drive/v3/files/{file_id}", params={"fields": "id, name, mimeType, size"})
            return f"File {file_id}: {data.get('name')} ({data.get('mimeType')})"
        if action == "download":
            file_id = str(params.get("file_id") or "").strip()
            if not file_id:
                return "Please provide a file_id."
            return f"Drive download for {file_id} requires a user-chosen destination folder and explicit confirmation before saving to disk."
        if action == "upload":
            return "Upload should be done through a user-selected file path and then linked back with the resulting Drive file id."
        return f"Unsupported Google Drive action: {action}."
    except Exception as exc:
        return f"Google Drive failed: {safe_error(exc)}"
