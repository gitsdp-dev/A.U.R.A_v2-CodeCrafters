"""Floating PyQt6 panel for local system controls."""

from __future__ import annotations

import html
import math
import platform
import threading
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PyQt6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QVBoxLayout,
    QWidget,
)

from core.system_controls import ControlResult, ControlState, SystemControlsManager


_ACCESSIBILITY_CONTROLS = (
    ("Narrator", "narrator"),
    ("Magnifier", "magnifier"),
    ("On-screen keyboard", "onscreen_keyboard"),
    ("Sticky keys", "sticky_keys"),
    ("Filter keys", "filter_keys"),
    ("Toggle keys", "toggle_keys"),
    ("High contrast", "high_contrast"),
    ("Mono audio", "mono_audio"),
    ("Color filters", "color_filters"),
)

# Control id -> vector glyph name (drawn with QPainter, see _paint_glyph).
_TILE_ICONS = {
    "wifi": "wifi",
    "airplane_mode": "airplane",
    "accessibility": "accessibility",
    "energy_saver": "leaf",
    "live_captions": "captions",
    "night_light": "sun",
}


# ----------------------------------------------------------------------------
# Palette helpers
# ----------------------------------------------------------------------------

def _color(value: Any, fallback: str = "#888888") -> QColor:
    color = QColor(str(value))
    return color if color.isValid() else QColor(fallback)


def _mix(base: Any, other: Any, amount: float) -> QColor:
    """Linear blend of two colours (amount 0 -> base, 1 -> other)."""
    a = base if isinstance(base, QColor) else _color(base)
    b = other if isinstance(other, QColor) else _color(other)
    return QColor(
        round(a.red() + (b.red() - a.red()) * amount),
        round(a.green() + (b.green() - a.green()) * amount),
        round(a.blue() + (b.blue() - a.blue()) * amount),
    )


def _rounded_path(rect: QRectF, radius: float, corners: str = "all",
                  inner: float = 2.0) -> QPainterPath:
    """Rounded rectangle; 'left' / 'right' squares off the opposite side for split tiles."""
    tl = tr = bl = br = radius
    if corners == "left":
        tr = br = inner
    elif corners == "right":
        tl = bl = inner
    path = QPainterPath()
    path.moveTo(rect.left() + tl, rect.top())
    path.lineTo(rect.right() - tr, rect.top())
    path.arcTo(QRectF(rect.right() - 2 * tr, rect.top(), 2 * tr, 2 * tr), 90, -90)
    path.lineTo(rect.right(), rect.bottom() - br)
    path.arcTo(QRectF(rect.right() - 2 * br, rect.bottom() - 2 * br, 2 * br, 2 * br), 0, -90)
    path.lineTo(rect.left() + bl, rect.bottom())
    path.arcTo(QRectF(rect.left(), rect.bottom() - 2 * bl, 2 * bl, 2 * bl), -90, -90)
    path.lineTo(rect.left(), rect.top() + tl)
    path.arcTo(QRectF(rect.left(), rect.top(), 2 * tl, 2 * tl), 180, -90)
    path.closeSubpath()
    return path


# ----------------------------------------------------------------------------
# Vector glyphs (24 x 24 design grid, stroked, theme-coloured)
# ----------------------------------------------------------------------------

def _paint_glyph(p: QPainter, glyph: str, rect: QRectF, color: QColor,
                 level: int = 3, weight: float = 1.7) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.translate(rect.center())
    scale = min(rect.width(), rect.height()) / 24.0
    p.scale(scale, scale)
    p.translate(-12.0, -12.0)
    pen = QPen(color, weight)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if glyph == "wifi":
        dim = QColor(color)
        dim.setAlpha(int(color.alpha() * 0.28))
        for index, radius in enumerate((14.0, 9.0, 4.4)):
            lit = level >= 3 - index
            p.setPen(QPen(color if lit else dim, weight, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawArc(QRectF(12 - radius, 18.5 - radius, radius * 2, radius * 2),
                      45 * 16, 90 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(12, 18.5), 1.6, 1.6)
    elif glyph == "airplane":
        p.translate(12, 12)
        p.rotate(45)
        p.scale(0.86, 0.86)
        p.translate(-12, -12)
        plane = QPainterPath()
        plane.moveTo(12, 2.5)
        plane.cubicTo(13.3, 2.5, 13.9, 3.6, 13.9, 5.0)
        for x, y in ((13.9, 9.2), (21.5, 14.0), (21.5, 16.0), (13.9, 13.7), (13.9, 18.6),
                     (16.3, 20.2), (16.3, 21.7), (12, 20.6), (7.7, 21.7), (7.7, 20.2),
                     (10.1, 18.6), (10.1, 13.7), (2.5, 16.0), (2.5, 14.0), (10.1, 9.2),
                     (10.1, 5.0)):
            plane.lineTo(x, y)
        plane.cubicTo(10.1, 3.6, 10.7, 2.5, 12, 2.5)
        plane.closeSubpath()
        p.drawPath(plane)
    elif glyph == "accessibility":
        p.drawEllipse(QPointF(12, 4.8), 1.9, 1.9)
        p.drawPolyline([QPointF(4.5, 8.8), QPointF(12, 10.4), QPointF(19.5, 8.8)])
        p.drawLine(QPointF(12, 10.4), QPointF(12, 15.0))
        p.drawLine(QPointF(12, 15.0), QPointF(8.6, 21.0))
        p.drawLine(QPointF(12, 15.0), QPointF(15.4, 21.0))
    elif glyph == "leaf":
        leaf = QPainterPath()
        leaf.moveTo(5, 19)
        leaf.cubicTo(5, 10.5, 10, 5, 19.5, 5)
        leaf.cubicTo(19.5, 14, 14.5, 19, 5, 19)
        leaf.closeSubpath()
        p.drawPath(leaf)
        p.drawLine(QPointF(5, 19), QPointF(13.5, 10.5))
    elif glyph == "captions":
        p.drawRoundedRect(QRectF(3, 5.5, 18, 13), 2.8, 2.8)
        p.drawLine(QPointF(7, 10.6), QPointF(10, 10.6))
        p.drawLine(QPointF(12.5, 10.6), QPointF(17, 10.6))
        p.drawLine(QPointF(7, 14.2), QPointF(12, 14.2))
        p.drawLine(QPointF(14.5, 14.2), QPointF(17, 14.2))
    elif glyph == "sun":
        p.drawEllipse(QPointF(12, 12), 3.7, 3.7)
        for ray in range(8):
            angle = ray * math.pi / 4
            p.drawLine(
                QPointF(12 + 6.9 * math.cos(angle), 12 + 6.9 * math.sin(angle)),
                QPointF(12 + 9.4 * math.cos(angle), 12 + 9.4 * math.sin(angle)),
            )
    elif glyph == "chevron":
        p.drawPolyline([QPointF(9.5, 5.5), QPointF(16, 12), QPointF(9.5, 18.5)])
    elif glyph == "chevron_left":
        p.drawPolyline([QPointF(14.5, 5.5), QPointF(8, 12), QPointF(14.5, 18.5)])
    elif glyph == "close":
        p.drawLine(QPointF(6.2, 6.2), QPointF(17.8, 17.8))
        p.drawLine(QPointF(17.8, 6.2), QPointF(6.2, 17.8))
    elif glyph == "check":
        p.drawPolyline([QPointF(5, 12.8), QPointF(10, 17.5), QPointF(19, 7)])
    elif glyph == "gear":
        gear = QPainterPath()
        first = True
        for tooth in range(8):
            base = tooth * math.pi / 4
            for delta, radius in ((-0.30, 7.4), (-0.20, 9.8), (0.20, 9.8), (0.30, 7.4)):
                point = QPointF(12 + radius * math.cos(base + delta),
                                12 + radius * math.sin(base + delta))
                if first:
                    gear.moveTo(point)
                    first = False
                else:
                    gear.lineTo(point)
        gear.closeSubpath()
        p.drawPath(gear)
        p.drawEllipse(QPointF(12, 12), 3.2, 3.2)
    elif glyph in ("volume", "volume_off"):
        body = QPainterPath()
        body.moveTo(4, 9.5)
        for x, y in ((8, 9.5), (12.8, 5.3), (12.8, 18.7), (8, 14.5), (4, 14.5)):
            body.lineTo(x, y)
        body.closeSubpath()
        p.drawPath(body)
        if glyph == "volume":
            p.drawArc(QRectF(8.2, 7.4, 9.2, 9.2), -40 * 16, 80 * 16)
            p.drawArc(QRectF(4.8, 4.0, 16.0, 16.0), -40 * 16, 80 * 16)
        else:
            p.drawLine(QPointF(16.2, 9.6), QPointF(21.2, 14.4))
            p.drawLine(QPointF(21.2, 9.6), QPointF(16.2, 14.4))
    elif glyph == "mixer":
        for y, knob in ((6.5, 15.0), (12.0, 8.0), (17.5, 13.0)):
            p.drawLine(QPointF(3, y), QPointF(knob - 2.4, y))
            p.drawLine(QPointF(knob + 2.4, y), QPointF(21, y))
            p.drawEllipse(QPointF(knob, y), 2.2, 2.2)
    elif glyph == "text_size":
        font = QFont("Segoe UI")
        font.setPixelSize(14)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.drawText(QRectF(0, 0, 24, 24), Qt.AlignmentFlag.AlignCenter, "Aa")
    p.restore()


def _draw_switch(p: QPainter, rect: QRectF, on: bool, enabled: bool,
                 palette: dict[str, str]) -> None:
    pri, dim = _color(palette["PRI"]), _color(palette["TEXT_DIM"])
    dark, panel = _color(palette["DARK"]), _color(palette["PANEL"])
    radius = rect.height() / 2
    if on:
        fill = pri if enabled else _mix(panel, pri, 0.45)
        edge, knob, knob_x = fill, dark, rect.right() - radius
    else:
        fill = QColor(0, 0, 0, 0)
        edge = dim if enabled else _mix(dim, panel, 0.5)
        knob, knob_x = edge, rect.left() + radius
    p.setPen(QPen(edge, 1.4))
    p.setBrush(fill)
    p.drawRoundedRect(rect.adjusted(0.7, 0.7, -0.7, -0.7), radius, radius)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(knob)
    p.drawEllipse(QPointF(knob_x, rect.center().y()), 4.6 if on else 4.0, 4.6 if on else 4.0)


# ----------------------------------------------------------------------------
# Self-painted widgets
# ----------------------------------------------------------------------------

class _GlyphButton(QPushButton):
    """Flat or tile-style button that paints a vector glyph with the AURA palette."""

    def __init__(self, palette: dict[str, str], glyph: str, *, corners: str = "all",
                 radius: float = 12.0, variant: str = "tile", icon_size: float = 22.0,
                 chevron: bool = False, weight: float = 1.7, parent=None):
        super().__init__(parent)
        self._pal = palette
        self._glyph = glyph
        self._corners = corners
        self._radius = radius
        self._variant = variant
        self._icon_size = icon_size
        self._chevron = chevron
        self._weight = weight
        self._hover = False
        self._on = False
        self._kb_focus = False
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def set_on(self, on: bool) -> None:
        self._on = bool(on)
        self.update()

    def focusInEvent(self, event) -> None:
        self._kb_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
        )
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._kb_focus = False
        super().focusOutEvent(event)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(40, 36)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = self._pal
        panel, text = _color(pal["PANEL"]), _color(pal["TEXT"])
        dim, pri = _color(pal["TEXT_DIM"]), _color(pal["PRI"])
        dark, ghost = _color(pal["DARK"]), _color(pal["PRI_GHO"])
        enabled, down = self.isEnabled(), self.isDown()
        hover = self._hover and enabled
        on = self.isChecked() or self._on
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self._variant == "tile":
            if on:
                fill = pri if enabled else _mix(panel, pri, 0.5)
                if enabled and down:
                    fill = fill.darker(112)
                elif hover:
                    fill = fill.lighter(108)
                edge, icon = fill.lighter(118), dark
            else:
                amount = 0.03 if not enabled else 0.04 if down else 0.12 if hover else 0.07
                fill, edge = _mix(panel, text, amount), _mix(panel, text, amount + 0.09)
                icon = text if enabled else dim
        else:
            fill, edge = QColor(0, 0, 0, 0), QColor(0, 0, 0, 0)
            if enabled and down:
                fill = _mix(panel, pri, 0.25)
            elif hover:
                fill = ghost
            icon = (pri if hover else text) if enabled else dim

        path = _rounded_path(rect, self._radius, self._corners)
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawPath(path)

        size = self._icon_size
        cx, cy = rect.center().x(), rect.center().y()
        if self._chevron:
            small = max(10.0, size * 0.55)
            total = size + 4 + small
            left = cx - total / 2
            _paint_glyph(p, self._glyph, QRectF(left, cy - size / 2, size, size), icon,
                         weight=self._weight)
            _paint_glyph(p, "chevron", QRectF(left + size + 4, cy - small / 2, small, small),
                         icon, weight=2.2)
        else:
            _paint_glyph(p, self._glyph, QRectF(cx - size / 2, cy - size / 2, size, size),
                         icon, weight=self._weight)

        if self.hasFocus() and self._kb_focus:
            p.setPen(QPen(text, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(_rounded_path(rect.adjusted(2, 2, -2, -2), max(self._radius - 2, 2),
                                     self._corners))


class _GlyphLabel(QWidget):
    """Static glyph (optionally dimmed signal strength) for list rows and slider leads."""

    def __init__(self, palette: dict[str, str], glyph: str, size: int = 22,
                 color_key: str = "TEXT_DIM", level: int = 3, parent=None):
        super().__init__(parent)
        self._pal = palette
        self._glyph = glyph
        self._color_key = color_key
        self._level = level
        self.setFixedSize(size, size)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        _paint_glyph(p, self._glyph, QRectF(self.rect()), _color(self._pal[self._color_key]),
                     level=self._level)


class _RowButton(QPushButton):
    """Full-width list row: 'switch' (label + toggle), 'check' (selectable item),
    'link' (label + chevron) or 'toggle' (bare switch with no label)."""

    def __init__(self, palette: dict[str, str], text: str = "", kind: str = "switch",
                 parent=None):
        super().__init__(text, parent)
        self._pal = palette
        self._kind = kind
        self._marked = False
        self._note = ""
        self._hover = False
        self._kb_focus = False
        self.setCheckable(kind in ("switch", "toggle"))
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        if kind == "toggle":
            self.setFixedSize(46, 26)
        else:
            self.setMinimumHeight(42)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_marked(self, marked: bool) -> None:
        self._marked = bool(marked)
        self.update()

    def set_note(self, note: str) -> None:
        self._note = note
        self.update()

    def focusInEvent(self, event) -> None:
        self._kb_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
        )
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._kb_focus = False
        super().focusOutEvent(event)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(46, 26) if self._kind == "toggle" else QSize(220, 42)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = self._pal
        panel, text = _color(pal["PANEL"]), _color(pal["TEXT"])
        dim, pri = _color(pal["TEXT_DIM"]), _color(pal["PRI"])
        enabled = self.isEnabled()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self._kind == "toggle":
            _draw_switch(p, QRectF(rect.center().x() - 19, rect.center().y() - 10, 38, 20),
                         self.isChecked(), enabled, pal)
            return

        hover = self._hover and enabled
        amount = 0.04 if (self.isDown() and enabled) else 0.11 if hover else 0.06
        fill, edge = _mix(panel, text, amount), _mix(panel, text, amount + 0.08)
        marked = self._kind == "check" and self._marked
        if marked:
            fill, edge = _mix(panel, pri, 0.14), _mix(panel, pri, 0.55)
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawPath(_rounded_path(rect, 10.0))

        right = rect.right() - 14
        cy = rect.center().y()
        if self._kind == "switch":
            _draw_switch(p, QRectF(right - 38, cy - 10, 38, 20), self.isChecked(), enabled, pal)
            right -= 48
        elif self._kind == "check" and marked:
            _paint_glyph(p, "check", QRectF(right - 16, cy - 8, 16, 16), pri, weight=2.2)
            right -= 26
        elif self._kind == "link":
            _paint_glyph(p, "chevron", QRectF(right - 12, cy - 6, 12, 12),
                         text if hover else dim, weight=2.2)
            right -= 22

        note_font = QFont("Segoe UI")
        note_font.setPixelSize(11)
        if self._note:
            p.setFont(note_font)
            p.setPen(dim)
            width = QFontMetrics(note_font).horizontalAdvance(self._note)
            p.drawText(QRectF(right - width, 0, width, rect.height() + 1),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self._note)
            right -= width + 10

        font = QFont("Segoe UI")
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(text if enabled else dim)
        available = max(20, int(right - 14))
        shown = QFontMetrics(font).elidedText(self.text(), Qt.TextElideMode.ElideRight, available)
        p.drawText(QRectF(14, 0, available, rect.height() + 1),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown)

        if self.hasFocus() and self._kb_focus:
            p.setPen(QPen(text, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(_rounded_path(rect.adjusted(2, 2, -2, -2), 8.0))


# ----------------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------------

class SystemControlsPanel(QDialog):
    """Palette-aware, non-modal control panel; OS work never blocks the Qt thread."""

    operation_finished = pyqtSignal(str, object)

    def __init__(self, manager: SystemControlsManager, palette: dict[str, str],
                 refresh_seconds: int = 3, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.palette = palette
        self.states: dict[str, ControlState] = {}
        self._page = "main"
        self._status = QLabel()
        self._values: dict[str, QLabel] = {}
        self._tiles: dict[str, QPushButton] = {}
        self._tile_labels: dict[str, str] = {}
        self._tile_captions: dict[str, QLabel] = {}
        self._tile_arrows: dict[str, QPushButton] = {}
        self._sliders: dict[str, QSlider] = {}
        self._mute_button: QPushButton | None = None
        self._network_list: QVBoxLayout | None = None
        self._wifi_toggle: QPushButton | None = None
        self._scan_button: QPushButton | None = None
        self._active_operations: set[str] = set()
        self._snapshot_in_flight = False
        self._refresh_after_snapshot = False
        self._state_revision = 0
        self._snapshot_revision = 0
        self.setObjectName("SystemControlsPanel")
        self.setWindowTitle("System Controls")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._drag_offset = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumWidth(438)
        self.setMaximumWidth(480)
        self.setStyleSheet(self._dialog_style())
        self.operation_finished.connect(self._operation_done)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QWidget()
        self._head = head
        header = QHBoxLayout(head)
        header.setContentsMargins(16, 12, 10, 2)
        header.setSpacing(8)
        self._back = _GlyphButton(palette, "chevron_left", variant="ghost", radius=9.0,
                                  icon_size=18.0, weight=2.0)
        self._back.setAccessibleName("Back")
        self._back.setToolTip("Back")
        self._back.setFixedSize(32, 32)
        self._back.clicked.connect(lambda: self._show_page("main"))
        self._back.hide()
        heading = QVBoxLayout()
        heading.setSpacing(1)
        title = QLabel("SYSTEM CONTROLS")
        title_font = QFont("Segoe UI", 10, QFont.Weight.Bold)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.6)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {palette['PRI']};")
        subtitle = QLabel("A.U.R.A.  /  LIVE DEVICE STATE")
        subtitle_font = QFont("Segoe UI", 7)
        subtitle_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet(f"color: {palette['TEXT_DIM']};")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addWidget(self._back)
        header.addLayout(heading)
        header.addStretch()
        close = _GlyphButton(palette, "close", variant="ghost", radius=9.0,
                             icon_size=16.0, weight=2.0)
        close.setAccessibleName("Close system controls")
        close.setToolTip("Close system controls")
        close.setFixedSize(32, 32)
        close.clicked.connect(self.close)
        header.addWidget(close)
        root.addWidget(head)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_main_page())
        self._stack.addWidget(self._build_wifi_page())
        self._stack.addWidget(self._build_accessibility_page())
        self._stack.addWidget(self._build_output_page())
        self._stack.addWidget(self._build_night_light_page())
        body = QVBoxLayout()
        body.setContentsMargins(16, 10, 16, 14)
        body.addWidget(self._stack)
        root.addLayout(body, 1)

        # Footer band: live status on the left, Windows Settings on the right.
        self._footer = QWidget()
        self._footer.setMinimumHeight(48)
        footer = QHBoxLayout(self._footer)
        footer.setContentsMargins(18, 6, 10, 6)
        footer.setSpacing(8)
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {palette['TEXT_DIM']}; font-size: 10px;")
        footer.addWidget(self._status, 1)
        settings = _GlyphButton(palette, "gear", variant="ghost", radius=9.0,
                                icon_size=20.0, weight=1.6)
        settings.setAccessibleName("Open Windows Settings")
        settings.setToolTip("Open Windows Settings")
        settings.setFixedSize(36, 36)
        settings.clicked.connect(lambda _checked=False: self._open_settings())
        footer.addWidget(settings)
        root.addWidget(self._footer)

        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(refresh_seconds)) * 1000)
        self._timer.timeout.connect(self.refresh)
        self._render_states()
        self.refresh()
        self._timer.start()

    # -- styling -------------------------------------------------------------

    def _dialog_style(self) -> str:
        p = self.palette
        track = _mix(p["PANEL"], p["TEXT"], 0.40).name()
        track_off = _mix(p["PANEL"], p["TEXT"], 0.14).name()
        pri_dim = _mix(p["PANEL"], p["PRI"], 0.40).name()
        field = _mix(p["PANEL"], p["TEXT"], 0.07).name()
        edge = _mix(p["PANEL"], p["TEXT"], 0.16).name()
        scroll = _mix(p["PANEL"], p["TEXT"], 0.22).name()
        ring = (
            f"qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, "
            f"stop:0 {p['PRI']}, stop:0.56 {p['PRI']}, stop:0.62 {p['TEXT']}, stop:1 {p['TEXT']})"
        )
        ring_off = (
            f"qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, "
            f"stop:0 {pri_dim}, stop:0.56 {pri_dim}, stop:0.62 {track}, stop:1 {track})"
        )
        return (
            f"QLabel {{ color: {p['TEXT']}; background: transparent; }}"
            f"QLabel:disabled {{ color: {p['TEXT_DIM']}; }}"
            f"QToolTip {{ background: {p['PANEL']}; color: {p['TEXT']}; "
            f"border: 1px solid {edge}; padding: 4px 8px; }}"
            f"QSlider {{ min-height: 24px; background: transparent; }}"
            f"QSlider::groove:horizontal {{ height: 4px; background: {track}; border-radius: 2px; }}"
            f"QSlider::add-page:horizontal {{ background: {track}; border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {p['PRI']}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ width: 18px; height: 18px; margin: -7px 0; "
            f"border-radius: 9px; background: {ring}; }}"
            f"QSlider::sub-page:horizontal:disabled {{ background: {pri_dim}; }}"
            f"QSlider::add-page:horizontal:disabled {{ background: {track_off}; }}"
            f"QSlider::handle:horizontal:disabled {{ background: {ring_off}; }}"
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}"
            f"QScrollBar::handle:vertical {{ background: {scroll}; border-radius: 3px; "
            f"min-height: 24px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {p['PRI']}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical "
            f"{{ background: transparent; }}"
            f"QMessageBox, QInputDialog {{ background: {p['PANEL']}; color: {p['TEXT']}; }}"
            f"QMessageBox QPushButton, QInputDialog QPushButton {{ color: {p['TEXT']}; "
            f"background: {field}; border: 1px solid {edge}; border-radius: 8px; "
            f"padding: 6px 18px; min-width: 56px; }}"
            f"QMessageBox QPushButton:hover, QInputDialog QPushButton:hover "
            f"{{ background: {p['PRI_GHO']}; border-color: {p['PRI']}; }}"
            f"QInputDialog QLineEdit {{ color: {p['TEXT']}; background: {field}; "
            f"border: 1px solid {edge}; border-radius: 8px; padding: 6px 8px; "
            f"selection-background-color: {p['PRI']}; selection-color: {p['DARK']}; }}"
        )

    def _button_style(self, compact: bool = False) -> str:
        p = self.palette
        idle = _mix(p["PANEL"], p["TEXT"], 0.07).name()
        hover = _mix(p["PANEL"], p["TEXT"], 0.13).name()
        edge = _mix(p["PANEL"], p["TEXT"], 0.16).name()
        padding = "4px 10px" if compact else "7px 16px"
        size = 10 if compact else 11
        return (
            f"QPushButton {{ color: {p['TEXT']}; background: {idle}; "
            f"border: 1px solid {edge}; border-radius: {7 if compact else 9}px; "
            f"padding: {padding}; font-size: {size}px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {p['PRI_GHO']}; border-color: {p['PRI']}; }}"
            f"QPushButton:pressed {{ background: {hover}; }}"
            f"QPushButton:checked {{ color: {p['DARK']}; background: {p['PRI']}; "
            f"border: 1px solid {p['PRI']}; }}"
            f"QPushButton:disabled {{ color: {p['TEXT_DIM']}; background: {p['DARK']}; "
            f"border-color: {p['BORDER']}; }}"
        )

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = self.palette
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        shape = QPainterPath()
        shape.addRoundedRect(rect, 14, 14)
        p.fillPath(shape, _color(pal["PANEL"]))
        p.save()
        p.setClipPath(shape)
        band_top = self._footer.geometry().top() if hasattr(self, "_footer") else self.height()
        if band_top < self.height():
            p.fillRect(QRectF(0, band_top, self.width(), self.height() - band_top),
                       _mix(pal["PANEL"], "#000000", 0.30))
            p.setPen(QPen(_mix(pal["PANEL"], pal["TEXT"], 0.10), 1))
            p.drawLine(QPointF(0, band_top + 0.5), QPointF(self.width(), band_top + 0.5))
        glow = QLinearGradient(0, 0, self.width(), 0)
        accent = _color(pal["PRI"])
        glow.setColorAt(0.0, accent)
        glow.setColorAt(0.45, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.fillRect(QRectF(0, 0, self.width(), 2), glow)
        p.restore()
        p.setPen(QPen(_color(pal["BORDER"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shape)

    @staticmethod
    def _flat_scroll(scroll: QScrollArea, inner: QWidget) -> QScrollArea:
        """QScrollArea.setWidget() forces an opaque fill; keep the panel surface instead."""
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        inner.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        return scroll

    def _caption_label(self, text: str) -> QLabel:
        caption = QLabel(text)
        caption.setFont(QFont("Segoe UI", 9))
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        return caption

    def _set_caption(self, caption: QLabel, text: str) -> None:
        shown = QFontMetrics(caption.font()).elidedText(text, Qt.TextElideMode.ElideRight, 118)
        caption.setText(shown)
        caption.setToolTip(text if shown != text else "")

    # -- pages ---------------------------------------------------------------

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(14)
        rows = (
            (("Wi-Fi", "wifi", "wifi"), ("Airplane mode", "airplane_mode", "airplane_mode"),
             ("Accessibility", None, "accessibility")),
            (("Energy saver", "energy_saver", "energy_saver"),
             ("Live captions", "live_captions", "live_captions"),
             ("Night light", "night_light", "night_light")),
        )
        for row, definitions in enumerate(rows):
            for column, (label, control_id, page_id) in enumerate(definitions):
                key = control_id or page_id
                has_arrow = control_id in {"wifi", "night_light", "accessibility"}
                button = _GlyphButton(
                    self.palette, _TILE_ICONS.get(key, ""), variant="tile", radius=12.0,
                    icon_size=24.0, corners="left" if has_arrow else "all",
                    chevron=control_id is None,
                )
                button.setFixedHeight(56)
                button.setAccessibleName(label)
                button.setCheckable(control_id is not None)
                button.clicked.connect(
                    lambda _checked=False, cid=control_id, pid=page_id:
                    self._tile_clicked(cid, pid)
                )
                self._tiles[key] = button
                self._tile_labels[key] = label
                caption = self._caption_label(label)
                self._tile_captions[key] = caption
                cell = QWidget()
                cell_layout = QVBoxLayout(cell)
                cell_layout.setContentsMargins(0, 0, 0, 0)
                cell_layout.setSpacing(7)
                if has_arrow:
                    split = QWidget()
                    split_layout = QHBoxLayout(split)
                    split_layout.setContentsMargins(0, 0, 0, 0)
                    split_layout.setSpacing(2)
                    split_layout.addWidget(button, 1)
                    arrow = _GlyphButton(
                        self.palette, "chevron", variant="tile", radius=12.0,
                        icon_size=14.0, corners="right", weight=2.2,
                    )
                    arrow.setToolTip(f"Open {label} settings")
                    arrow.setAccessibleName(f"Open {label} submenu")
                    arrow.setFixedSize(34, 56)
                    arrow.clicked.connect(
                        lambda _checked=False, target=page_id: self._show_page(target)
                    )
                    self._tile_arrows[key] = arrow
                    split_layout.addWidget(arrow)
                    cell_layout.addWidget(split)
                else:
                    cell_layout.addWidget(button)
                cell_layout.addWidget(caption)
                grid.addWidget(cell, row, column)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(
            f"background: {_mix(self.palette['PANEL'], self.palette['TEXT'], 0.12).name()}; "
            f"border: none;"
        )
        layout.addWidget(divider)
        self._add_slider(layout, "Brightness", "brightness", 100)
        self._add_slider(layout, "Volume", "volume", 100)
        layout.addStretch()
        return page

    def _add_slider(self, parent: QVBoxLayout, label: str, control_id: str,
                    maximum: int) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        glyph = {"brightness": "sun", "volume": "volume", "text_size": "text_size"}.get(
            control_id, "sun")
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, maximum)
        slider.setAccessibleName(label)
        value = QLabel("--%")
        value.setMinimumWidth(38)
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value.setStyleSheet(f"color: {self.palette['TEXT_DIM']}; font-size: 11px;")
        slider.valueChanged.connect(lambda amount, target=value: target.setText(f"{amount}%"))
        slider.sliderReleased.connect(
            lambda cid=control_id, control=slider: self._set_state(cid, control.value())
        )
        if control_id == "volume":
            mute = _GlyphButton(self.palette, "volume", variant="ghost", radius=9.0,
                                icon_size=21.0, weight=1.6)
            mute.setFixedSize(34, 34)
            mute.setAccessibleName("Mute")
            mute.setToolTip("Toggle mute")
            mute.clicked.connect(self._toggle_mute)
            self._mute_button = mute
            row.addWidget(mute)
        else:
            lead = _GlyphLabel(self.palette, glyph, size=22, color_key="TEXT")
            lead.setFixedWidth(34)
            row.addWidget(lead)
        row.addWidget(slider, 1)
        row.addWidget(value)
        if control_id == "volume":
            output = _GlyphButton(self.palette, "mixer", variant="ghost", radius=9.0,
                                  icon_size=18.0, chevron=True, weight=1.6)
            output.setAccessibleName("Choose audio output")
            output.setToolTip("Choose audio output")
            output.setFixedSize(50, 34)
            output.clicked.connect(lambda: self._show_page("output_devices"))
            row.addWidget(output)
        else:
            row.addSpacing(50 + row.spacing())
        parent.addLayout(row)
        self._sliders[control_id] = slider
        self._values[control_id] = value

    def _build_wifi_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(10)
        self._wifi_state = QLabel("Wi-Fi")
        self._wifi_state.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        header.addWidget(self._wifi_state, 1)
        self._wifi_toggle = _RowButton(self.palette, "", "toggle")
        self._wifi_toggle.setToolTip("Turn Wi-Fi on or off")
        self._wifi_toggle.clicked.connect(self._toggle_wifi_from_submenu)
        header.addWidget(self._wifi_toggle)
        refresh = QPushButton("Scan")
        refresh.setStyleSheet(self._button_style(compact=True))
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self._scan_networks)
        self._scan_button = refresh
        header.addWidget(refresh)
        layout.addLayout(header)
        network_container = QWidget()
        self._network_list = QVBoxLayout(network_container)
        self._network_list.setContentsMargins(0, 0, 4, 0)
        self._network_list.setSpacing(6)
        scroll = self._flat_scroll(QScrollArea(), network_container)
        scroll.setMinimumHeight(200)
        layout.addWidget(scroll, 1)
        more = _RowButton(self.palette, "More Wi-Fi settings", "link")
        more.clicked.connect(lambda: self._open_settings("ms-settings:network-wifi"))
        layout.addWidget(more)
        return page

    def _build_accessibility_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(6)
        for label, control_id in _ACCESSIBILITY_CONTROLS:
            button = _RowButton(self.palette, label, "switch")
            button.clicked.connect(
                lambda checked, cid=control_id: self._set_state(cid, bool(checked))
            )
            self._tiles[control_id] = button
            layout.addWidget(button)
        layout.addSpacing(6)
        self._add_slider(layout, "Text size", "text_size", 225)
        self._sliders["text_size"].setMinimum(100)
        layout.addSpacing(6)
        more = _RowButton(self.palette, "More accessibility settings", "link")
        more.clicked.connect(lambda: self._open_settings("ms-settings:easeofaccess"))
        layout.addWidget(more)
        for label, uri in (
            ("Mouse pointer settings", "ms-settings:easeofaccess-mousepointer"),
            ("Voice access settings", "ms-settings:accessibility-speech"),
        ):
            button = _RowButton(self.palette, label, "link")
            button.clicked.connect(lambda _checked=False, target=uri: self._open_settings(target))
            layout.addWidget(button)
        scroll = QScrollArea()
        return self._flat_scroll(scroll, page)

    def _build_output_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(8)
        heading = QLabel("OUTPUT DEVICE")
        heading_font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        heading_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        heading.setFont(heading_font)
        heading.setStyleSheet(f"color: {self.palette['TEXT_DIM']};")
        layout.addWidget(heading)
        self._output_list = QVBoxLayout()
        self._output_list.setSpacing(6)
        layout.addLayout(self._output_list)
        layout.addStretch()
        scroll = QScrollArea()
        return self._flat_scroll(scroll, page)

    def _build_night_light_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        title = QLabel("INTENSITY")
        title_font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {self.palette['TEXT_DIM']};")
        layout.addWidget(title)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setAccessibleName("Windows Night light intensity")
        amount = QLabel("--")
        amount.setMinimumWidth(38)
        amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        amount.setStyleSheet(f"color: {self.palette['TEXT_DIM']}; font-size: 11px;")
        slider.valueChanged.connect(lambda value: amount.setText(f"{value}%"))
        slider.sliderReleased.connect(
            lambda control=slider: self._set_night_light_strength(control.value())
        )
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_GlyphLabel(self.palette, "sun", size=22, color_key="TEXT"))
        row.addWidget(slider, 1)
        row.addWidget(amount)
        layout.addLayout(row)
        explanation = QLabel(
            "Adjusts the intensity of Windows' native Night light feature."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(f"color: {self.palette['TEXT_DIM']}; font-size: 11px;")
        layout.addWidget(explanation)
        settings = _RowButton(self.palette, "Open Windows Night light settings", "link")
        settings.clicked.connect(lambda: self._open_settings("ms-settings:nightlight"))
        layout.addWidget(settings)
        layout.addStretch()
        self._sliders["night_light_strength"] = slider
        self._values["night_light_strength"] = amount
        return page

    # -- behaviour (unchanged) -----------------------------------------------

    def _set_night_light_strength(self, strength: int) -> None:
        state = self.states.get("night_light")
        current = state.value if state and isinstance(state.value, dict) else {}
        self._set_state("night_light", {
            "enabled": bool(current.get("enabled", False)),
            "strength": int(strength),
        })

    def _tile_clicked(self, control_id: str | None, page_id: str) -> None:
        if control_id is None:
            self._show_page(page_id)
            return
        state = self.states.get(control_id)
        if state is None or not state.available:
            self._status.setText(
                state.error if state and state.error else "Waiting for the system state."
            )
            self.refresh(force=True)
            self._render_states()
            return
        if control_id == "wifi":
            target = not bool((state.value or {}).get("enabled", False))
            if not target and not self._confirm_disruption("Wi-Fi"):
                self.refresh()
                return
        else:
            value = state.value
            if control_id == "night_light" and isinstance(value, dict):
                value = value.get("enabled", False)
            target = not bool(value)
        if control_id == "airplane_mode" and target and not self._confirm_disruption("Airplane mode"):
            self.refresh()
            return
        self._set_state(control_id, target)

    def _confirm_disruption(self, label: str) -> bool:
        answer = QMessageBox.question(
            self, "Confirm network interruption",
            f"{label} may disconnect A.U.R.A. from the internet. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _set_state(self, control_id: str, value: Any) -> None:
        self._run(
            control_id,
            lambda: self.manager.set_state(control_id, value),
        )

    def _toggle_mute(self) -> None:
        state = self.states.get("mute")
        self._set_state("mute", not bool(state.value) if state else True)

    def _run(self, key: str, callback) -> None:
        if key == "snapshot":
            if self._snapshot_in_flight:
                return
            self._snapshot_in_flight = True
            self._snapshot_revision = self._state_revision
            self._status.setText("Checking current system state…")
        else:
            if key in self._active_operations:
                self._status.setText(f"{key.replace('_', ' ').title()} is already updating.")
                return
            self._active_operations.add(key)
            self._set_operation_busy(key, True)
            self._status.setText(f"Updating {key.replace('_', ' ')}…")

        def worker():
            try:
                result = callback()
            except Exception as exc:
                result = exc
            self.operation_finished.emit(key, result)

        threading.Thread(target=worker, name="aura-system-panel", daemon=True).start()

    def _set_operation_busy(self, key: str, busy: bool) -> None:
        state = self.states.get(key)
        available = bool(state and state.available)
        if key in self._tiles:
            self._tiles[key].setEnabled(available and not busy)
        if key in self._sliders:
            self._sliders[key].setEnabled(available and not busy)
        if key == "wifi":
            if self._wifi_toggle is not None:
                self._wifi_toggle.setEnabled(available and not busy)
            if self._tiles.get("wifi") is not None:
                self._tiles["wifi"].setEnabled(available and not busy)
        if key == "wifi_scan" and self._scan_button is not None:
            self._scan_button.setEnabled(not busy)

    def _operation_done(self, key: str, result: object) -> None:
        if key == "snapshot":
            self._snapshot_in_flight = False
        else:
            self._active_operations.discard(key)
            self._set_operation_busy(key, False)
        if isinstance(result, ControlResult):
            if not result.success:
                self._status.setText(result.error)
                self._toast(result.error)
            else:
                self._state_revision += 1
                self.states[result.new_state.control_id] = result.new_state
                self._status.setText(result.new_state.detail or "Setting updated and verified.")
        elif isinstance(result, Exception):
            self._status.setText(str(result))
            self._toast(str(result))
        elif key == "wifi_scan":
            if isinstance(result, list):
                self._show_networks(result)
                self._status.setText(f"{len(result)} Wi-Fi network(s) found.")
            else:
                self._status.setText("Wi-Fi scan returned an unexpected result.")
        elif key == "snapshot":
            if self._snapshot_revision == self._state_revision:
                if isinstance(result, dict):
                    self.states = result
                    self._render_states()
                    self._status.setText("System state synchronized.")
                else:
                    self._status.setText("System state returned an unexpected result.")
            else:
                self._refresh_after_snapshot = True
        elif key == "output_devices":
            if isinstance(result, list):
                self._show_output_devices(result)
            else:
                self._status.setText("Audio device discovery returned an unexpected result.")
        elif key == "wifi_connect":
            self._status.setText("Wi-Fi connection verified.")
        elif key == "wifi_disconnect":
            self._status.setText("Wi-Fi disconnection verified.")
        elif key == "wifi_forget":
            self._status.setText("Saved Wi-Fi profile removed and verified.")
        elif key == "open_settings":
            return
        if key != "snapshot":
            self._render_states()
            self.refresh(force=True)
        elif self._refresh_after_snapshot:
            self._refresh_after_snapshot = False
            QTimer.singleShot(0, self.refresh)

    def _toast(self, message: str) -> None:
        try:
            from core.notifications.bus import notify

            notify("A.U.R.A.", "System Controls", str(message)[:220], priority=50)
        except Exception as exc:
            print(f"[SystemControls] Could not publish notification: {exc}")

    def refresh(self, force: bool = False) -> None:
        if self._snapshot_in_flight:
            if force:
                self._refresh_after_snapshot = True
            return
        if not self.isVisible() and self._timer.isActive():
            return
        self._run("snapshot", self.manager.snapshot)

    def update_control_state(self, control_id: str, state: ControlState) -> None:
        """Apply a verified in-app state change without waiting for the next poll."""
        self._state_revision += 1
        self.states[control_id] = state
        self._render_states()

    def _render_states(self) -> None:
        for control_id, button in self._tiles.items():
            state = self.states.get(control_id)
            caption = self._tile_captions.get(control_id)
            label = self._tile_labels.get(control_id, control_id.replace("_", " ").title())
            arrow = self._tile_arrows.get(control_id)
            if state is None:
                is_navigation = control_id == "accessibility"
                button.setEnabled(is_navigation)
                button.setToolTip(
                    "Open accessibility settings."
                    if is_navigation else "Waiting for the system state."
                )
                if caption is not None:
                    self._set_caption(caption, label)
                continue
            value = state.value
            if control_id == "wifi" and isinstance(value, dict):
                value = value.get("enabled", False)
            elif control_id == "night_light" and isinstance(value, dict):
                value = value.get("enabled", False)
            button.blockSignals(True)
            button.setChecked(bool(value) if state.available else False)
            button.setEnabled(
                state.available and control_id not in self._active_operations
            )
            button.setToolTip(state.error or state.detail or "")
            if not state.available:
                detail = "Unavailable"
            elif control_id == "wifi" and isinstance(state.value, dict):
                detail = state.value.get("ssid") or ("On" if value else "Off")
            elif isinstance(value, bool):
                detail = "On" if value else "Off"
            else:
                detail = str(state.value)
            button.setAccessibleName(f"{label}, {detail}")
            if isinstance(button, _RowButton):
                button.set_note("" if state.available else "Unavailable")
            if arrow is not None and isinstance(arrow, _GlyphButton):
                arrow.set_on(bool(value) and state.available)
            if caption is not None:
                caption.setEnabled(state.available)
                if control_id == "wifi" and state.available and isinstance(state.value, dict) \
                        and state.value.get("ssid"):
                    self._set_caption(caption, str(state.value["ssid"]))
                else:
                    self._set_caption(caption, label)
            button.blockSignals(False)
        for control_id, slider in self._sliders.items():
            if control_id == "night_light_strength":
                night_state = self.states.get("night_light")
                value = night_state.value if night_state and isinstance(night_state.value, dict) else {}
                strength = value.get("strength")
                slider.blockSignals(True)
                if isinstance(strength, (int, float)):
                    slider.setValue(int(strength))
                slider.blockSignals(False)
                slider.setEnabled(bool(
                    night_state and night_state.available and value.get("enabled")
                    and isinstance(strength, (int, float))
                    and not self._active_operations.intersection(
                        {"night_light", "night_light_strength"}
                    )
                ))
                self._values[control_id].setText(
                    f"{int(strength)}%" if isinstance(strength, (int, float)) else "--"
                )
                continue
            state = self.states.get(control_id)
            if state is None or not state.available or state.value is None:
                slider.setEnabled(False)
                if control_id in self._values:
                    self._values[control_id].setText("—")
                continue
            raw = state.value
            if control_id == "wifi" and isinstance(raw, dict):
                raw = raw.get("enabled", False)
            if isinstance(raw, (int, float)):
                slider.blockSignals(True)
                slider.setValue(int(raw))
                slider.blockSignals(False)
                slider.setEnabled(control_id not in self._active_operations)
                self._values[control_id].setText(f"{int(raw)}%")
        mute_state = self.states.get("mute")
        if self._mute_button is not None and isinstance(self._mute_button, _GlyphButton):
            muted = bool(mute_state and mute_state.available and mute_state.value)
            self._mute_button.set_glyph("volume_off" if muted else "volume")
        wifi_state = self.states.get("wifi")
        if wifi_state:
            value = wifi_state.value if isinstance(wifi_state.value, dict) else {}
            if self._wifi_toggle is not None:
                self._wifi_toggle.blockSignals(True)
                self._wifi_toggle.setChecked(bool(value.get("enabled")) and wifi_state.available)
                self._wifi_toggle.blockSignals(False)
            if wifi_state.available:
                ssid = value.get("ssid") or "Not connected"
                self._wifi_state.setText(
                    f"Wi-Fi {'On' if value.get('enabled') else 'Off'}  ·  {ssid}"
                )
            else:
                self._wifi_state.setText(wifi_state.error or "Wi-Fi state unavailable")
        devices = self.states.get("output_devices")
        if devices and devices.available and self._page == "output_devices":
            self._show_output_devices(devices.value or [])

    def _show_page(self, page: str) -> None:
        indexes = {
            "main": 0, "wifi": 1, "accessibility": 2,
            "output_devices": 3, "night_light": 4,
        }
        page = page if page in indexes else "main"
        self._page = page
        self._stack.setCurrentIndex(indexes[page])
        self._back.setVisible(page != "main")
        if page == "wifi":
            self._scan_networks()
        elif page == "output_devices":
            self._run("output_devices", lambda: self.manager.call("output_devices", "list_devices"))
        self.refresh()

    def _scan_networks(self) -> None:
        self._run("wifi_scan", lambda: self.manager.call("wifi", "scan_networks"))

    def _toggle_wifi_from_submenu(self) -> None:
        state = self.states.get("wifi")
        value = state.value if state and isinstance(state.value, dict) else {}
        target = not bool(value.get("enabled", False))
        if not target and not self._confirm_disruption("Wi-Fi"):
            self._render_states()
            return
        self._set_state("wifi", target)

    def _show_networks(self, networks: list[dict]) -> None:
        if self._network_list is None:
            return
        while self._network_list.count():
            item = self._network_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        pal = self.palette
        dim = pal["TEXT_DIM"]
        if not networks:
            empty = QLabel("No networks found or Wi-Fi scan unavailable.")
            empty.setStyleSheet(f"color: {dim};")
            self._network_list.addWidget(empty)
        for network in networks:
            connected = bool(network.get("connected"))
            try:
                signal = int(network.get("signal") or 0)
            except (TypeError, ValueError):
                signal = 0
            level = 3 if signal >= 67 else 2 if signal >= 34 else 1
            ssid = html.escape(str(network.get("ssid") or "(hidden)"))
            meta = [f"{signal}%"]
            if network.get("security"):
                meta.append(html.escape(str(network.get("security"))))
            elif network.get("secured"):
                meta.append("Secured")
            if connected:
                meta.append("Connected")
            container = QFrame()
            container.setObjectName("netRow")
            edge = pal["PRI"] if connected else _mix(pal["PANEL"], pal["TEXT"], 0.14).name()
            fill = _mix(pal["PANEL"], pal["PRI"], 0.12).name() if connected \
                else _mix(pal["PANEL"], pal["TEXT"], 0.06).name()
            container.setStyleSheet(
                f"QFrame#netRow {{ background: {fill}; border: 1px solid {edge}; "
                f"border-radius: 10px; }}"
            )
            row = QHBoxLayout(container)
            row.setContentsMargins(12, 9, 10, 9)
            row.setSpacing(10)
            row.addWidget(_GlyphLabel(pal, "wifi", size=22,
                                      color_key="PRI" if connected else "TEXT", level=level))
            text = QLabel(
                f"<span style='font-size:13px; font-weight:600;'>{ssid}</span><br>"
                f"<span style='color:{dim}; font-size:10px;'>{'  ·  '.join(meta)}</span>"
            )
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setWordWrap(True)
            row.addWidget(text, 1)
            connect = QPushButton("Connect")
            connect.setStyleSheet(self._button_style(compact=True))
            connect.setCursor(Qt.CursorShape.PointingHandCursor)
            connect.clicked.connect(
                lambda _checked=False, item=dict(network): self._connect_network(item)
            )
            row.addWidget(connect)
            if connected:
                disconnect = QPushButton("Disconnect")
                disconnect.setStyleSheet(self._button_style(compact=True))
                disconnect.setCursor(Qt.CursorShape.PointingHandCursor)
                disconnect.clicked.connect(
                    lambda: self._run("wifi_disconnect", lambda: self.manager.call("wifi", "disconnect"))
                )
                row.addWidget(disconnect)
            if network.get("saved"):
                forget = QPushButton("Forget")
                forget.setStyleSheet(self._button_style(compact=True))
                forget.setCursor(Qt.CursorShape.PointingHandCursor)
                forget.clicked.connect(
                    lambda _checked=False, ssid=str(network.get("ssid") or ""):
                    self._run("wifi_forget", lambda: self.manager.call("wifi", "forget", ssid))
                )
                row.addWidget(forget)
            self._network_list.addWidget(container)
        self._network_list.addStretch()

    def _connect_network(self, network: dict) -> None:
        password = None
        if network.get("secured"):
            password, accepted = QInputDialogWithPassword.get(self, str(network.get("ssid") or "Wi-Fi"))
            if not accepted:
                return
        self._run(
            "wifi_connect",
            lambda: self.manager.call("wifi", "connect", str(network.get("ssid") or ""), password),
        )

    def _show_output_devices(self, devices: list[dict]) -> None:
        while self._output_list.count():
            item = self._output_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not devices:
            empty = QLabel("No audio output devices were found.")
            empty.setStyleSheet(f"color: {self.palette['TEXT_DIM']};")
            self._output_list.addWidget(empty)
        for device in devices:
            name = str(device.get("name") or "")
            button = _RowButton(self.palette, name, "check")
            button.set_marked(bool(device.get("default")))
            button.clicked.connect(lambda _checked=False, target=name: self._set_state("output_devices", target))
            self._output_list.addWidget(button)

    def _open_settings(self, uri: str = "ms-settings:") -> None:
        def open_uri():
            if platform.system() == "Windows":
                import os

                os.startfile(uri)
                return "opened"
            raise OSError("The Windows Settings app is not available on this platform.")

        self._run("open_settings", open_uri)

    def show_at(self, global_point) -> None:
        """Show the panel centred on the screen that hosts the A.U.R.A. window.

        `global_point` is kept for API compatibility; it is only used to pick the
        screen when the panel has no parent window.
        """
        from PyQt6.QtGui import QGuiApplication

        parent = self.parentWidget()
        screen = None
        if parent is not None:
            screen = parent.window().screen()
        if screen is None and global_point is not None:
            screen = QGuiApplication.screenAt(global_point)
        screen = screen or self.screen() or QGuiApplication.primaryScreen()
        bounds = screen.availableGeometry()
        self.setMaximumHeight(max(220, bounds.height() - 16))
        if self.layout() is not None:
            self.layout().activate()
        self.adjustSize()
        width = min(self.width(), bounds.width() - 16)
        height = min(self.height(), bounds.height() - 16)
        x = bounds.left() + (bounds.width() - width) // 2
        y = bounds.top() + (bounds.height() - height) // 2
        self.setGeometry(x, y, width, height)
        self.show()
        self.refresh(force=True)
        self.raise_()
        self.activateWindow()

    def mousePressEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self._head.geometry().contains(event.position().toPoint())):
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self._drag_offset
            screen = self.screen()
            if screen is not None:
                bounds = screen.availableGeometry()
                target.setX(min(max(bounds.left(), target.x()), bounds.right() - self.width() + 1))
                target.setY(min(max(bounds.top(), target.y()), bounds.bottom() - self.height() + 1))
            self.move(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._page != "main":
                self._show_page("main")
            else:
                self.close()
            event.accept()
            return
        super().keyPressEvent(event)


class QInputDialogWithPassword:
    """Small helper that returns an accepted masked password without logging it."""

    @staticmethod
    def get(parent, network: str) -> tuple[str, bool]:
        from PyQt6.QtWidgets import QInputDialog

        dialog = QInputDialog(parent)
        dialog.setWindowTitle("Connect to Wi-Fi")
        dialog.setLabelText(f"Password for {network}:")
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
        accepted = dialog.exec()
        return dialog.textValue(), bool(accepted)