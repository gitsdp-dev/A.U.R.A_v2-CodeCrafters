"""Open the generated model in A.U.R.A.'s existing floating browser."""

from __future__ import annotations

from pathlib import Path

from aura_cad.viewer.bridge import viewer_url

_VIEWER_HTML = Path(__file__).with_name("assets") / "viewer.html"


class CadViewerPanel:
    """A thin adapter; the app owns the floating browser and its lifecycle."""

    def open(self, player, stl_path: str) -> str:
        url = viewer_url(str(_VIEWER_HTML), stl_path)
        opener = getattr(player, "open_local_file_in_browser", None)
        if not callable(opener):
            raise RuntimeError("The in-app browser cannot open a local CAD preview.")
        opener(url)
        return url
