"""
updater_ui.py — A.U.R.A update system UI.

Contains:
  * CheckUpdatesButton — icon-only "sync" button (two chasing arrows) that is
                         painted in the live theme colour.  A small "!" badge
                         appears at its bottom-right corner only while an
                         update is waiting; otherwise there is no badge.
  * UpdaterController  — orchestrates the check + download QThreads, owns dialogs
  * UpdateDialog       — "new version" dialog: versions, release notes, progress
  * UpToDateDialog     — friendly "you're all caught up" confirmation
  * UpdateErrorDialog  — readable error with a "Try again" action

Everything is read from ui.C at paint / build time, so whatever accent the
user picks on the colour wheel is picked up automatically (the button repaints
live; dialogs are built fresh each time they open).

Public API used by ui.py is unchanged:
    CheckUpdatesButton(), .set_badge(bool), .has_badge()
    UpdaterController(button, parent_window)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QEasingCurve, QObject, QPointF, QPropertyAnimation, QRectF, Qt, QThread,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPalette,
    QPen, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from updater import (
    BASE_DIR,
    ReleaseChecker,
    ReleaseDownloader,
    SemVer,
    clear_updater_badge,
    has_updater_badge,
    launch_helper,
    set_updater_badge,
)

log = logging.getLogger("aura.updater_ui")


# ─────────────────────────────────────────────────────────────────────────────
# Live palette — always read from ui.C so the colour wheel keeps working
# ─────────────────────────────────────────────────────────────────────────────

_FALLBACK = {
    "BG": "#000000", "PRI": "#ff9224", "BORDER": "#5a341a",
    "TEXT": "#ffdcb0", "TEXT_DIM": "#8a5a34", "TEXT_MED": "#cf9a5c",
    "ACC2": "#ffcc00", "GREEN": "#00ff88", "RED": "#ff3355",
}


def _palette_class():
    """Return the live ``C`` palette class (works whether ui.py is imported
    as ``ui`` or executed as ``__main__``)."""
    for name in ("ui", "__main__"):
        cls = getattr(sys.modules.get(name), "C", None)
        if cls is not None:
            return cls
    try:
        from ui import C  # noqa: PLC0415
        return C
    except Exception:
        return None


def _pal(key: str) -> QColor:
    try:
        col = QColor(getattr(_palette_class(), key))
        if col.isValid():
            return col
    except Exception:
        pass
    return QColor(_FALLBACK[key])


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend: t=0 → a, t=1 → b."""
    return QColor(
        round(a.red()   + (b.red()   - a.red())   * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue()  + (b.blue()  - a.blue())  * t),
    )


def _alpha(c: QColor, a: float) -> QColor:
    out = QColor(c)
    out.setAlpha(max(0, min(255, int(a * 255))))
    return out


def _rgba(c: QColor, a: float) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{max(0, min(255, int(a * 255)))})"


class _Theme:
    """A snapshot of the palette, with a few derived surface colours."""

    def __init__(self) -> None:
        self.pri    = _pal("PRI")
        self.bg     = _pal("BG")
        self.text   = _pal("TEXT")
        self.dim    = _pal("TEXT_DIM")
        self.med    = _pal("TEXT_MED")
        self.ok     = _pal("GREEN")
        self.err    = _pal("RED")
        self.card    = _mix(self.bg, self.pri, 0.055)   # dialog surface
        self.surface = _mix(self.bg, self.pri, 0.100)   # inset panels
        self.line    = _mix(self.bg, self.pri, 0.280)   # hairlines
        self.pri_hi  = self.pri.lighter(118)
        self.pri_lo  = self.pri.darker(112)
        self.on_pri  = _mix(self.bg, self.pri, 0.07)    # text on filled buttons


# ─────────────────────────────────────────────────────────────────────────────
# Typography
# ─────────────────────────────────────────────────────────────────────────────

_UI_FAMILIES   = ["Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text",
                  "Helvetica Neue", "Arial"]
_MONO_FAMILIES = ["Cascadia Mono", "Consolas", "SF Mono", "Courier New"]
_UI_CSS = "'Segoe UI Variable Text','Segoe UI','Inter','Helvetica Neue',Arial,sans-serif"


def _font(px: int, weight=QFont.Weight.Normal, mono: bool = False,
          spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setFamilies(_MONO_FAMILIES if mono else _UI_FAMILIES)
    f.setPixelSize(px)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def _label(text: str, px: int, color: QColor, weight=QFont.Weight.Normal,
           mono: bool = False, spacing: float = 0.0, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(_font(px, weight, mono, spacing))
    lbl.setStyleSheet(f"color: {color.name()}; background: transparent;")
    lbl.setWordWrap(wrap)
    return lbl


def _nice_date(iso: str) -> str:
    """'2026-10-01T09:12:00Z' → '1 Oct 2026' (empty string on any problem)."""
    try:
        d = datetime.strptime((iso or "")[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%b %Y')}"
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Vector glyphs shared by the header button and the dialogs
# ─────────────────────────────────────────────────────────────────────────────

def _paint_sync_icon(p: QPainter, cx: float, cy: float, r: float,
                     color: QColor, angle: float = 0.0, stroke: float = 2.7) -> None:
    """Two chasing arrows (the classic 'sync' mark) centred on (cx, cy)."""
    p.save()
    p.translate(cx, cy)
    p.rotate(angle)

    grad = QLinearGradient(0, -r, 0, r)
    grad.setColorAt(0.0, color.lighter(114))
    grad.setColorAt(1.0, color)
    pen = QPen(QBrush(grad), stroke, Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

    rect = QRectF(-r, -r, 2 * r, 2 * r)
    start, sweep = 158.0, -128.0          # over the top, clockwise
    end_deg = start + sweep               # 30°

    # Arrowhead geometry at the end of the arc (clockwise tangent)
    import math
    th = math.radians(end_deg)
    ex, ey = r * math.cos(th), -r * math.sin(th)
    dx, dy = math.sin(th), math.cos(th)           # direction of travel
    nx, ny = -dy, dx                              # normal
    hl, hw = r * 0.80, r * 0.50                   # head length / half-width
    tip   = QPointF(ex + dx * hl * 0.62, ey + dy * hl * 0.62)
    base  = QPointF(ex - dx * hl * 0.38, ey - dy * hl * 0.38)
    left  = QPointF(base.x() + nx * hw, base.y() + ny * hw)
    right = QPointF(base.x() - nx * hw, base.y() - ny * hw)
    head = QPainterPath()
    head.moveTo(tip); head.lineTo(left); head.lineTo(right); head.closeSubpath()

    for k in range(2):                    # second arrow = first rotated 180°
        p.save()
        p.rotate(180.0 * k)
        arc = QPainterPath()
        arc.arcMoveTo(rect, start)
        arc.arcTo(rect, start, sweep)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        p.drawPath(arc)
        p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(QBrush(color))
        p.drawPath(head)
        p.restore()
    p.restore()


def _paint_exclaim(p: QPainter, cx: float, cy: float, s: float, color: QColor) -> None:
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(color, s * 0.34, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(cx, cy - s * 0.78), QPointF(cx, cy + s * 0.14))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawEllipse(QPointF(cx, cy + s * 0.70), s * 0.2, s * 0.2)


def _paint_check(p: QPainter, cx: float, cy: float, s: float, color: QColor) -> None:
    path = QPainterPath()
    path.moveTo(cx - s * 0.70, cy + s * 0.04)
    path.lineTo(cx - s * 0.18, cy + s * 0.52)
    path.lineTo(cx + s * 0.76, cy - s * 0.50)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(color, s * 0.30, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(path)


# ─────────────────────────────────────────────────────────────────────────────
# CheckUpdatesButton
# ─────────────────────────────────────────────────────────────────────────────

class CheckUpdatesButton(QPushButton):
    """Icon-only update button — two chasing arrows in the live theme colour.

    * Normal      : arrows only, no badge.
    * Checking    : arrows spin (``set_busy(True)``).
    * Update ready: a small "!" badge sits at the bottom-right
                    (``set_badge(True)``); it vanishes with ``set_badge(False)``.

    The button never changes size, so the header layout cannot shift.
    """

    _R = 8.4   # arrow radius in px

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("", parent)
        self._badge = False
        self._busy  = False
        self._angle = 0.0
        self._spin = QTimer(self)
        self._spin.setInterval(16)
        self._spin.timeout.connect(self._tick)

        self.setFlat(True)
        self.setFixedSize(36, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Check for updates")
        self._refresh_tip()

    # ── State ─────────────────────────────────────────────────────────────────

    def set_badge(self, show: bool) -> None:
        """Show or hide the pending-update exclamation badge."""
        if bool(show) != self._badge:
            self._badge = bool(show)
            self._refresh_tip()
            self.update()

    def has_badge(self) -> bool:
        return self._badge

    def set_busy(self, busy: bool) -> None:
        """Spin the arrows while a check is running."""
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy and not self._spin.isActive():
            self._spin.start()
        self._refresh_tip()

    def _tick(self) -> None:
        nxt = self._angle + 7.0
        # When a check ends, glide to the next half-turn (the icon is 180°
        # symmetric) so it settles instead of snapping back.
        if not self._busy and (nxt % 180.0) < 7.0:
            self._angle = 0.0
            self._spin.stop()
        else:
            self._angle = nxt % 360.0
        self.update()

    def _refresh_tip(self) -> None:
        if self._busy:
            self.setToolTip("Checking for updates…")
        elif self._badge:
            self.setToolTip("An update is waiting — click to review it")
        else:
            self.setToolTip("Check for A.U.R.A updates")

    # ── Qt events ─────────────────────────────────────────────────────────────

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = float(self.width()), float(self.height())
        pri, bg = _pal("PRI"), _pal("BG")
        down  = self.isDown()
        hover = self.underMouse()

        # Icon sits slightly up-left so the badge has room in the corner.
        cx, cy = W / 2.0 - 1.0, H / 2.0 - 0.5

        # Soft hover / press glow in the theme colour
        if hover or down:
            glow = QRadialGradient(QPointF(cx, cy), W * 0.62)
            glow.setColorAt(0.0, _alpha(pri, 0.30 if down else 0.20))
            glow.setColorAt(1.0, _alpha(pri, 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 9, 9)

        k = 0.93 if down else 1.0
        _paint_sync_icon(p, cx, cy, self._R * k, pri, self._angle, 3.2 * k)

        # Badge — only while an update is pending
        if self._badge:
            br = 6.0
            bx, by = W - br - 3.0, H - br - 3.0
            warn = _pal("ACC2")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(bg))                                   # cut-out ring
            p.drawEllipse(QPointF(bx, by), br + 1.7, br + 1.7)
            g = QLinearGradient(bx, by - br, bx, by + br)
            g.setColorAt(0.0, warn.lighter(125))
            g.setColorAt(1.0, warn.darker(125))
            p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(bx, by), br, br)
            _paint_exclaim(p, bx, by, 4.3, QColor("#1b1100"))

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# Shared dialog chrome
# ─────────────────────────────────────────────────────────────────────────────

_PAD = 18   # transparent margin around the card (room for the drop shadow)


class _DragBar(QWidget):
    """Title strip that drags the whole dialog."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._off = None

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._off = ev.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._off is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(ev.globalPosition().toPoint() - self._off)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        self._off = None


class _BrandGlyph(QWidget):
    def __init__(self, color: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(18, 18)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _paint_sync_icon(p, 9, 9, 5.2, self._color, 0.0, 1.7)
        p.end()


class _IconBadge(QWidget):
    """Round hero icon — 'sync', 'ok' or 'alert' — tinted with a tone colour."""

    def __init__(self, kind: str, tone: QColor, size: int = 56,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._kind, self._tone = kind, tone
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W = float(self.width())
        c = W / 2.0
        fill = QRadialGradient(QPointF(c, c * 0.8), W * 0.7)
        fill.setColorAt(0.0, _alpha(self._tone, 0.26))
        fill.setColorAt(1.0, _alpha(self._tone, 0.08))
        p.setBrush(QBrush(fill))
        p.setPen(QPen(_alpha(self._tone, 0.55), 1.4))
        p.drawEllipse(QRectF(1, 1, W - 2, W - 2))
        s = W * 0.21
        if self._kind == "ok":
            _paint_check(p, c, c, s * 1.25, self._tone)
        elif self._kind == "alert":
            _paint_exclaim(p, c, c, s * 1.25, self._tone)
        else:
            _paint_sync_icon(p, c, c, W * 0.2, self._tone, 0.0, 3.0)
        p.end()


class _ThemedDialog(QDialog):
    """Frameless, rounded, shadowed, draggable dialog skeleton.

    Subclasses fill ``self.content`` (a QVBoxLayout) and call ``_btn()`` for
    consistent buttons.  The palette is snapshotted once, when the dialog is
    created, so it always matches the colour currently chosen on the wheel.
    """

    def __init__(self, parent: Optional[QWidget], width: int, caption: str) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.t = _Theme()
        self._placed = False
        self._fade: Optional[QPropertyAnimation] = None
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(width + 2 * _PAD)

        t = self.t
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_PAD, _PAD, _PAD, _PAD)

        self._card = QFrame()
        self._card.setObjectName("card")
        self._card.setStyleSheet(self._stylesheet())
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 190))
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        card_l = QVBoxLayout(self._card)
        card_l.setContentsMargins(0, 0, 0, 0)
        card_l.setSpacing(0)

        # Title strip
        bar = _DragBar()
        bar.setFixedHeight(42)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(18, 0, 10, 0)
        bl.setSpacing(8)
        bl.addWidget(_BrandGlyph(t.pri))
        bl.addWidget(_label(caption, 11, t.med, QFont.Weight.DemiBold, spacing=0.8))
        bl.addStretch()
        close = QPushButton("✕")
        close.setObjectName("close")
        close.setFixedSize(26, 26)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Close")
        close.clicked.connect(self.reject)
        bl.addWidget(close)
        card_l.addWidget(bar)

        body = QWidget()
        self.content = QVBoxLayout(body)
        self.content.setContentsMargins(28, 6, 28, 24)
        self.content.setSpacing(0)
        card_l.addWidget(body, 1)

    # ── Styling ───────────────────────────────────────────────────────────────

    def _stylesheet(self) -> str:
        t = self.t
        return f"""
            QFrame#card {{
                background: {t.card.name()};
                border: 1px solid {_rgba(t.pri, 0.38)};
                border-radius: 16px;
            }}
            QPushButton#close {{
                background: transparent; color: {t.dim.name()};
                border: none; border-radius: 7px;
                font-family: {_UI_CSS}; font-size: 12px;
            }}
            QPushButton#close:hover {{
                background: {_rgba(t.pri, 0.16)}; color: {t.text.name()};
            }}
            QPushButton#primary {{
                color: {t.on_pri.name()}; border: none; border-radius: 10px;
                padding: 0 22px; font-family: {_UI_CSS};
                font-size: 13px; font-weight: 600;
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {t.pri_hi.name()}, stop:1 {t.pri_lo.name()});
            }}
            QPushButton#primary:hover {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {t.pri.lighter(135).name()}, stop:1 {t.pri.name()});
            }}
            QPushButton#primary:pressed {{ background: {t.pri_lo.name()}; }}
            QPushButton#primary:disabled {{
                background: {_rgba(t.pri, 0.30)}; color: {_rgba(t.on_pri, 0.65)};
            }}
            QPushButton#ghost {{
                color: {t.med.name()}; background: transparent;
                border: 1px solid {t.line.name()}; border-radius: 10px;
                padding: 0 20px; font-family: {_UI_CSS};
                font-size: 13px; font-weight: 500;
            }}
            QPushButton#ghost:hover {{
                color: {t.text.name()}; background: {_rgba(t.pri, 0.10)};
                border-color: {_rgba(t.pri, 0.60)};
            }}
            QPushButton#ghost:disabled {{ color: {_rgba(t.dim, 0.55)}; }}
        """

    def _btn(self, text: str, kind: str, width: int = 0) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName(kind)            # "primary" | "ghost"
        b.setFixedHeight(38)
        if width:
            b.setMinimumWidth(width)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b

    # ── Placement + fade-in ───────────────────────────────────────────────────

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self._placed:
            return
        self._placed = True
        self.adjustSize()
        par = self.parentWidget()
        if par is not None:
            try:
                c = par.frameGeometry().center()
                self.move(max(0, c.x() - self.width() // 2),
                          max(0, c.y() - self.height() // 2))
            except Exception as exc:  # noqa: BLE001
                log.debug("dialog centring failed: %s", exc)
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(170)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.start()


# ─────────────────────────────────────────────────────────────────────────────
# UpdateDialog — a new release is available
# ─────────────────────────────────────────────────────────────────────────────

class UpdateDialog(_ThemedDialog):
    """Shown when a newer release exists: versions, notes, download progress."""

    #: Emitted once the download + extraction succeeded and the helper launched.
    apply_requested = pyqtSignal(str)   # new version tag

    #: Emitted when the user dismisses the dialog without installing.
    later_clicked = pyqtSignal()

    def __init__(self, local_ver_str: str, release: dict,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, 560, "A.U.R.A  ·  UPDATER")
        self._release       = release
        self._local_ver_str = local_ver_str
        self._new_ver_str   = release.get("tag_name", "?")
        self._thread: Optional[QThread]           = None
        self._worker: Optional[ReleaseDownloader] = None
        self._applying      = False
        self._later_sent    = False
        self._build()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:  # noqa: C901
        t, lay = self.t, self.content

        # Hero row — icon + headline
        hero = QHBoxLayout()
        hero.setSpacing(16)
        hero.addWidget(_IconBadge("sync", t.pri), 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        col.addWidget(_label("A fresh update is ready", 22, t.text, QFont.Weight.DemiBold))
        when = _nice_date(self._release.get("published_at", ""))
        sub = f"Version {self._new_ver_str} is out"
        sub += f" — released {when}." if when else "."
        col.addWidget(_label(sub, 13, t.med, wrap=True))
        hero.addLayout(col, 1)
        lay.addLayout(hero)
        lay.addSpacing(20)

        # Version strip — "installed → available"
        lay.addWidget(self._version_strip())
        lay.addSpacing(20)

        # Release notes
        lay.addWidget(_label("WHAT'S NEW", 11, t.dim, QFont.Weight.DemiBold, spacing=1.4))
        lay.addSpacing(8)
        self._notes = self._make_notes()
        lay.addWidget(self._notes, 1)
        lay.addSpacing(16)

        # Progress (hidden until the download starts)
        self._progress_frame = QWidget()
        pl = QVBoxLayout(self._progress_frame)
        pl.setContentsMargins(0, 0, 0, 14)
        pl.setSpacing(7)
        row = QHBoxLayout()
        self._status_lbl = _label("", 12, t.med)
        self._pct_lbl = _label("0%", 12, t.pri, QFont.Weight.DemiBold, mono=True)
        row.addWidget(self._status_lbl, 1)
        row.addWidget(self._pct_lbl)
        pl.addLayout(row)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setStyleSheet(f"""
            QProgressBar {{ background: {_rgba(t.pri, 0.16)}; border: none; border-radius: 3px; }}
            QProgressBar::chunk {{
                border-radius: 3px;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {t.pri_lo.name()}, stop:1 {t.pri_hi.name()});
            }}
        """)
        pl.addWidget(self._progress)
        lay.addWidget(self._progress_frame)
        self._progress_frame.hide()

        # Footer
        foot = QHBoxLayout()
        foot.setSpacing(10)
        foot.addWidget(_label("A.U.R.A restarts on its own\nonce the update is installed.",
                              11, t.dim), 1)
        self._later_btn = self._btn("Maybe later", "ghost", 120)
        self._later_btn.clicked.connect(self._on_later)
        self._download_btn = self._btn("Download && install", "primary", 170)
        self._download_btn.clicked.connect(self._on_download)
        self._download_btn.setDefault(True)
        foot.addWidget(self._later_btn)
        foot.addWidget(self._download_btn)
        lay.addLayout(foot)

    def _version_strip(self) -> QFrame:
        t = self.t
        frame = QFrame()
        frame.setObjectName("strip")
        frame.setStyleSheet(f"""
            QFrame#strip {{
                background: {t.surface.name()};
                border: 1px solid {t.line.name()};
                border-radius: 12px;
            }}
        """)
        h = QHBoxLayout(frame)
        h.setContentsMargins(20, 14, 20, 14)
        h.setSpacing(0)

        def block(caption: str, value: str, color: QColor, tip: str = "") -> QWidget:
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(3)
            v.addWidget(_label(caption, 10, t.dim, QFont.Weight.DemiBold, spacing=1.3))
            val = _label(value, 17, color, QFont.Weight.Bold, mono=True)
            if tip:
                val.setToolTip(tip)
            v.addWidget(val)
            return w

        local = self._local_ver_str
        if local.lower().startswith("unknown"):
            h.addWidget(block("INSTALLED", "Custom build", t.text, local))
        else:
            h.addWidget(block("INSTALLED", local, t.text))
        h.addStretch(1)
        h.addWidget(_label("→", 20, t.pri, QFont.Weight.DemiBold))
        h.addStretch(1)
        h.addWidget(block("AVAILABLE", self._new_ver_str, t.pri))
        return frame

    def _make_notes(self) -> QTextBrowser:
        t = self.t
        nb = QTextBrowser()
        nb.setReadOnly(True)
        nb.setOpenLinks(False)             # never open URLs automatically
        nb.setOpenExternalLinks(False)
        nb.setFrameShape(QFrame.Shape.NoFrame)
        nb.setMinimumHeight(170)
        nb.setFont(_font(13))
        pal = nb.palette()
        pal.setColor(QPalette.ColorRole.Link, t.pri)
        pal.setColor(QPalette.ColorRole.Text, t.text)
        nb.setPalette(pal)
        nb.setStyleSheet(f"""
            QTextBrowser {{
                background: {t.surface.name()}; color: {t.text.name()};
                border: 1px solid {t.line.name()}; border-radius: 12px;
                padding: 10px 14px; font-family: {_UI_CSS}; font-size: 13px;
                selection-background-color: {_rgba(t.pri, 0.35)};
            }}
            QScrollBar:vertical {{ background: transparent; width: 8px; margin: 6px 2px; border: none; }}
            QScrollBar::handle:vertical {{
                background: {_rgba(t.pri, 0.40)}; border-radius: 3px; min-height: 28px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {_rgba(t.pri, 0.70)}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        body = (self._release.get("body") or "").strip()
        if body:
            try:
                nb.setMarkdown(body)
            except Exception:
                nb.setPlainText(body)
        else:
            nb.setPlainText("The author didn't leave any notes for this release.")
        return nb

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_later(self) -> None:
        if self._applying:
            return
        self.reject()

    def _on_download(self) -> None:
        """Start the download worker thread."""
        if self._thread is not None and self._thread.isRunning():
            return
        self._set_buttons_enabled(False)
        self._download_btn.setText("Downloading…")
        self._progress_frame.show()
        self._progress.setValue(0)
        self._pct_lbl.setText("0%")
        self._set_status("Getting things ready…", self.t.med)
        self._applying = True

        self._thread = QThread(self)
        self._worker = ReleaseDownloader(self._release)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_download_finished)
        self._worker.error.connect(self._on_download_error)
        self._worker.cancelled.connect(self._on_cancelled)

        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._worker.cancelled.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()

    def _set_status(self, text: str, color: QColor) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color.name()}; background: transparent;")

    def _on_progress(self, pct: int, msg: str) -> None:
        self._progress.setValue(pct)
        self._pct_lbl.setText(f"{pct}%")
        self._set_status(msg, self.t.med)

    def _on_download_finished(self, staging_dir: str) -> None:
        """Download + extraction complete — launch the helper."""
        self._progress.setValue(100)
        self._pct_lbl.setText("100%")
        self._download_btn.setText("Installing…")
        self._set_status("All set — A.U.R.A will restart in a moment.", self.t.ok)
        self.apply_requested.emit(self._new_ver_str)

        entry = BASE_DIR / "main.py"
        try:
            launch_helper(
                staging_dir=Path(staging_dir),
                new_version=self._new_ver_str,
                entry_point=entry,
            )
        except Exception as exc:
            log.exception("launch_helper failed")
            self._on_download_error(f"Couldn't start the update helper: {exc}")

    def _reset_after_stop(self) -> None:
        self._applying = False
        self._set_buttons_enabled(True)
        self._download_btn.setText("Download && install")
        self._progress.setValue(0)
        self._pct_lbl.setText("0%")

    def _on_download_error(self, msg: str) -> None:
        self._reset_after_stop()
        self._set_status(f"That didn't work: {msg}", self.t.err)
        log.error("UpdateDialog download error: %s", msg)

    def _on_cancelled(self) -> None:
        self._reset_after_stop()
        self._set_status("Download cancelled.", self.t.med)

    def _cancel_download(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._later_btn.setEnabled(enabled)
        self._download_btn.setEnabled(enabled)

    def closeEvent(self, ev) -> None:
        self._cancel_download()
        super().closeEvent(ev)

    def reject(self) -> None:
        """Esc, the ✕ and "Maybe later" all mean the same thing: not now."""
        self._cancel_download()
        if not self._later_sent:
            self._later_sent = True
            self.later_clicked.emit()
        super().reject()


# ─────────────────────────────────────────────────────────────────────────────
# "Up to date" dialog
# ─────────────────────────────────────────────────────────────────────────────

class UpToDateDialog(_ThemedDialog):
    """Friendly confirmation shown when the installed version is the latest."""

    def __init__(self, version_str: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, 400, "A.U.R.A  ·  UPDATER")
        t, lay = self.t, self.content
        lay.setContentsMargins(28, 4, 28, 24)

        lay.addWidget(_IconBadge("ok", t.ok, 64), 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(16)
        title = _label("You're all caught up", 21, t.text, QFont.Weight.DemiBold)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(title)
        lay.addSpacing(6)
        msg = _label(f"A.U.R.A {version_str} is the newest version.\nNothing to install right now.",
                     13, t.med, wrap=True)
        msg.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(msg)
        lay.addSpacing(22)

        ok = self._btn("Great", "primary", 140)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(ok)
        row.addStretch()
        lay.addLayout(row)


# ─────────────────────────────────────────────────────────────────────────────
# Error dialog
# ─────────────────────────────────────────────────────────────────────────────

class UpdateErrorDialog(_ThemedDialog):
    """Readable error message.  ``exec()`` returns Accepted on "Try again"."""

    def __init__(self, message: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, 440, "A.U.R.A  ·  UPDATER")
        t, lay = self.t, self.content

        head = QHBoxLayout()
        head.setSpacing(16)
        head.addWidget(_IconBadge("alert", t.err, 52), 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        col.addWidget(_label("Couldn't check for updates", 20, t.text, QFont.Weight.DemiBold))
        col.addWidget(_label("Check your internet connection and give it another go.",
                             13, t.med, wrap=True))
        head.addLayout(col, 1)
        lay.addLayout(head)
        lay.addSpacing(18)

        box = QFrame()
        box.setObjectName("errbox")
        box.setStyleSheet(f"""
            QFrame#errbox {{
                background: {_rgba(t.err, 0.08)};
                border: 1px solid {_rgba(t.err, 0.35)};
                border-radius: 12px;
            }}
        """)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 12, 14, 12)
        detail = _label(message, 12, t.text, mono=True, wrap=True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(detail)
        lay.addWidget(box)
        lay.addSpacing(22)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch()
        close = self._btn("Close", "ghost", 110)
        close.clicked.connect(self.reject)
        retry = self._btn("Try again", "primary", 130)
        retry.setDefault(True)
        retry.clicked.connect(self.accept)
        row.addWidget(close)
        row.addWidget(retry)
        lay.addLayout(row)


# ─────────────────────────────────────────────────────────────────────────────
# UpdaterController — orchestrates checks and threads
# ─────────────────────────────────────────────────────────────────────────────

class UpdaterController(QObject):
    """Wired to a CheckUpdatesButton; manages the check workflow.

    Usage (from MainWindow)::

        self._update_ctrl = UpdaterController(self._update_btn, self)
    """

    def __init__(self, button: CheckUpdatesButton, parent_window: QWidget) -> None:
        super().__init__(parent_window)
        self._btn        = button
        self._parent_win = parent_window
        self._check_thread: Optional[QThread]        = None
        self._check_worker: Optional[ReleaseChecker] = None
        self._busy       = False

        # Restore the badge from persisted state on startup
        if has_updater_badge():
            self._btn.set_badge(True)

        self._btn.clicked.connect(self._on_button_clicked)

    def _on_button_clicked(self) -> None:
        if not self._busy:
            self._start_check()

    def _start_check(self) -> None:
        """Spawn the check worker thread."""
        if self._busy:
            return
        self._busy = True
        self._btn.set_busy(True)

        self._check_thread = QThread(self)
        self._check_worker = ReleaseChecker()
        self._check_worker.moveToThread(self._check_thread)

        self._check_thread.started.connect(self._check_worker.run)
        self._check_worker.finished.connect(self._on_check_finished)
        self._check_worker.error.connect(self._on_check_error)

        self._check_worker.finished.connect(self._check_thread.quit)
        self._check_worker.error.connect(self._check_thread.quit)
        self._check_thread.finished.connect(self._check_thread.deleteLater)

        self._check_thread.start()

    def _reset_button(self) -> None:
        self._busy = False
        self._btn.set_busy(False)

    def _on_check_finished(
        self,
        local_ver: Optional[SemVer],
        latest_ver: Optional[SemVer],
        latest_rel: Optional[dict],
        code_matches: bool,
    ) -> None:
        """Slot — runs on the Qt main thread."""
        self._reset_button()

        if latest_ver is None:
            self._show_error("No published releases were found on GitHub.")
            return

        local_str = str(local_ver) if local_ver is not None \
            else "Unknown / modified (based on v2.0.0+)"

        needs_update = not code_matches

        if not needs_update:
            clear_updater_badge()
            self._btn.set_badge(False)
            UpToDateDialog(str(latest_ver), parent=self._parent_win).exec()
            return

        dlg = UpdateDialog(local_str, latest_rel, parent=self._parent_win)
        dlg.later_clicked.connect(lambda: self._on_later(str(latest_ver)))
        dlg.apply_requested.connect(self._on_apply_requested)
        dlg.exec()

    def _on_check_error(self, message: str) -> None:
        self._reset_button()
        log.error("Update check failed: %s", message)
        self._show_error(message)

    def _on_later(self, latest_tag: str) -> None:
        """Persist the badge when the user defers the update."""
        set_updater_badge(latest_tag)
        self._btn.set_badge(True)
        log.info("User deferred update; badge set for tag %s", latest_tag)

    def _on_apply_requested(self, _tag: str) -> None:
        """The helper has been launched (the app is about to quit)."""
        clear_updater_badge()
        self._btn.set_badge(False)

    def _show_error(self, message: str) -> None:
        dlg = UpdateErrorDialog(message, parent=self._parent_win)
        if dlg.exec() == QDialog.DialogCode.Accepted:      # "Try again"
            QTimer.singleShot(0, self._start_check)
