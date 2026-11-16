"""Small command bridge for the local viewer page."""

from __future__ import annotations

_COMMANDS = {"rotate", "zoom_in", "zoom_out", "reset", "wireframe", "explode", "grid", "screenshot"}


def dispatch(player, command: str) -> bool:
    """Send only a fixed viewer command set through the app's browser bridge."""
    value = str(command or "").strip().casefold()
    if value not in _COMMANDS or player is None:
        return False
    runner = getattr(player, "run_cad_viewer_command", None)
    if not callable(runner):
        return False
    runner(value)
    return True


def viewer_url(html_path: str, model_path: str) -> str:
    """Build a local-file URL with an encoded STL path."""
    from pathlib import Path
    from urllib.parse import quote

    html_uri = Path(html_path).resolve().as_uri()
    model_uri = Path(model_path).resolve().as_uri()
    return html_uri + "?model=" + quote(model_uri, safe=":/")
