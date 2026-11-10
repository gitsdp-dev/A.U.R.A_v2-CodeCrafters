from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

_LOGGER = logging.getLogger(__name__)


class EarthBridge(QObject):
    """Compatibility bridge for callers retained from the local globe."""

    maximizeRequested = pyqtSignal()
    requestCompleted = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)

    @pyqtSlot()
    def maximize(self):
        try:
            self.maximizeRequested.emit()
        except Exception:
            _LOGGER.exception("Could not forward the legacy God's Eye maximize request")

    @pyqtSlot(str, str)
    def requestJson(self, request_id: str, url: str):
        try:
            _LOGGER.debug(
                "Ignored legacy Earth JSON request %s for %s; OSIRIS loads its own data",
                request_id,
                url,
            )
        except Exception:
            _LOGGER.exception("Could not handle a legacy God's Eye JSON request")
