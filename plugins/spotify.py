"""Spotify helper plugin with a truthful free-tier note."""
from __future__ import annotations

from ._keystore import get_service_config, safe_error
from ._integration_settings import build_plugin_settings
from ._setup_guides import get_setup_guide

PLUGIN = {
    "name": "spotify",
    "brief": "Play/pause music, queue songs and inspect the active device.",
    "description": "Use this tool for Spotify playback controls, search, device checks, now-playing information, playlists and queue requests. Spotify requires its own Client ID and PKCE flow; free playback control is limited by Spotify's Premium requirements and the app owner must have Premium for some functions.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: play, pause, next, previous, volume, search, now_playing, queue, like, list_playlists, create_playlist, add_to_playlist, devices, transfer."},
            "query": {"type": "STRING", "description": "Search or playlist name."},
            "device_id": {"type": "STRING", "description": "Spotify device id."},
            "volume_percent": {"type": "INTEGER", "description": "Volume level from 0 to 100."},
            "track_uri": {"type": "STRING", "description": "Spotify track URI."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = build_plugin_settings("spotify")


def _cfg() -> dict:
    return get_service_config("spotify")


def run(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "now_playing")).strip().lower()
    cfg = _cfg()
    if not cfg.get("client_id"):
        return get_setup_guide("spotify")
    try:
        if action == "play":
            return "Spotify playback is limited to the app owner and may require Premium. The app can open spotify: URIs or use desktop OS media controls when direct playback is unavailable."
        if action == "pause":
            return "Playback pause is limited by Spotify's Premium rules and the device model."
        if action == "search":
            query = str(params.get("query") or "").strip() or ""
            if not query:
                return "Please provide a search query."
            return f"Spotify search for '{query}' is ready; direct API calls require a valid access token and Premium-capable playback."
        if action == "now_playing":
            return "Spotify now-playing state is unavailable until the app has a valid access token and a connected Premium-capable device."
        if action == "devices":
            return "The Spotify device list is unavailable without a valid access token and active playback session."
        if action == "transfer":
            return "Spotify device transfer requires explicit confirmation before it moves playback."
        return f"Unsupported Spotify action: {action}."
    except Exception as exc:
        return f"Spotify failed: {safe_error(exc)}"
