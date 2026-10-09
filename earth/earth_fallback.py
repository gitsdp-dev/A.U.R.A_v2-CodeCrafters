from __future__ import annotations

import math

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class EarthFallbackWidget(QWidget):
    """Fallback painter-based globe that keeps A.U.R.A usable when WebEngine is missing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0.0
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._tick)
        self._tmr.start(30)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(
            "INTERACTIVE EARTH MAP UNAVAILABLE\n"
            "Install/repair PyQt6-WebEngine and PyQt6-WebChannel"
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        label.setStyleSheet("color: #d7f4ff; background: transparent;")
        layout.addWidget(label)

    def _tick(self):
        self._angle += 0.02
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(5, 10, 18))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        radius = min(self.width(), self.height()) * 0.32
        painter.setPen(QPen(QColor(108, 194, 255, 170), 1))
        painter.setBrush(QColor(14, 51, 76, 210))
        painter.drawEllipse(int(cx - radius), int(cy - radius), int(radius * 2), int(radius * 2))
        for deg in range(0, 360, 30):
            ang = math.radians(deg + self._angle * 90)
            x1 = cx + math.cos(ang) * radius * 0.45
            y1 = cy + math.sin(ang) * radius * 0.45
            x2 = cx + math.cos(ang) * radius * 0.92
            y2 = cy + math.sin(ang) * radius * 0.92
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
        painter.setPen(QPen(QColor(98, 167, 255, 120), 2))
        painter.drawEllipse(int(cx - radius * 1.1), int(cy - radius * 1.1), int(radius * 2.2), int(radius * 2.2))
