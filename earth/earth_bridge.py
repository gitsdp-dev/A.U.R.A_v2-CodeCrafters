from __future__ import annotations

import json
import threading
from urllib.parse import urlparse

import requests
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot


class EarthBridge(QObject):
    maximizeRequested = pyqtSignal()
    requestCompleted = pyqtSignal(str, str)

    _ALLOWED_HOSTS = {
        "api.open-meteo.com",
        "geocoding-api.open-meteo.com",
        "nominatim.openstreetmap.org",
        "restcountries.com",
        "router.project-osrm.org",
        "www.wikidata.org",
    }

    def __init__(self, parent=None):
        super().__init__(parent)

    @pyqtSlot()
    def maximize(self):
        self.maximizeRequested.emit()

    @pyqtSlot(str, str)
    def requestJson(self, request_id: str, url: str):
        thread = threading.Thread(
            target=self._fetch_json,
            args=(request_id, url),
            name="earth-json-request",
            daemon=True,
        )
        thread.start()

    def _fetch_json(self, request_id: str, url: str):
        try:
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in self._ALLOWED_HOSTS
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("Earth data source is not permitted.")

            response = requests.get(
                url,
                headers={"User-Agent": "AURA-Earth/1.0", "Accept": "application/json"},
                timeout=12,
                allow_redirects=False,
            )
            response.raise_for_status()
            if len(response.content) > 12 * 1024 * 1024:
                raise ValueError("Earth data response exceeded the size limit.")
            result = {
                "ok": True,
                "status": response.status_code,
                "body": response.text,
            }
        except (requests.RequestException, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        self.requestCompleted.emit(request_id, json.dumps(result))
