"""ui_calendar.py — Calendar Settings dialog & floating Agenda panel in AURA HUD style."""

from __future__ import annotations

import logging
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.calendar.models import CalendarError, get_local_tz
from core.calendar.providers.ics_feed import validate_feed_url
from core.calendar.service import CalendarService, get_calendar_service
from core.calendar.summarizer import format_agenda_lines

_LOG = logging.getLogger("aura.ui.calendar")

# Palette — matches the NEURAL MAP panel (near-black, amber HUD accents)
BG = "#06080b"
PANEL = "#0b0e13"
FIELD = "#090b0f"
LINE = "#2e2410"
LINE_STRONG = "#a9761c"
ACCENT = "#ffa826"
TEXT = "#f2dcb0"
MUTED = "#9d8660"
DANGER = "#ff5c4d"
OK = "#7fd18b"
BLUE = "#5b8def"

_MONO = '"Space Mono", "JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New", monospace'

_STYLE_TEMPLATE = """
QWidget {
    color: @TEXT@;
    font-family: @MONO@;
    font-size: 11px;
}
QLabel { background: transparent; }
QLabel#title {
    color: @ACCENT@; font-size: 12px; font-weight: 700;
}
QLabel#sectionLabel {
    color: @ACCENT@; font-size: 11px; font-weight: 700;
    border-left: 3px solid @ACCENT@; padding-left: 8px;
}
QLabel#noteLabel { color: @MUTED@; font-size: 10px; }
QLabel#statusBox {
    color: #c9b07a; font-size: 10px;
    background: rgba(0, 0, 0, 35%);
    border: 1px solid #4a3a14; border-radius: 3px;
    padding: 6px 8px;
}
QFrame#rule {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(255, 168, 38, 70%), stop:1 rgba(255, 168, 38, 0%));
    max-height: 1px; min-height: 1px; border: none;
}
QFrame#card {
    background: rgba(255, 168, 38, 5%);
    border: 1px solid rgba(255, 168, 38, 45%);
    border-left: 3px solid @ACCENT@;
    border-radius: 3px;
}
QLineEdit, QSpinBox {
    background: @FIELD@; color: @TEXT@;
    border: 1px solid @LINE_STRONG@; border-radius: 3px;
    padding: 7px 10px;
    selection-background-color: @ACCENT@; selection-color: #120c02;
}
QLineEdit { placeholder-text-color: @MUTED@; }
QLineEdit:focus, QSpinBox:focus { border-color: @ACCENT@; background: #0d1015; }
QSpinBox { padding: 6px 24px 6px 10px; min-width: 56px; }
QSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; background: @PANEL@;
    border-left: 1px solid @LINE@; border-bottom: 1px solid @LINE@;
    border-top-right-radius: 3px;
}
QSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; background: @PANEL@;
    border-left: 1px solid @LINE@;
    border-bottom-right-radius: 3px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background: rgba(255, 168, 38, 18%);
}
QTextEdit, QListWidget {
    background: @FIELD@; color: @TEXT@;
    border: 1px solid @LINE_STRONG@; border-radius: 3px;
    padding: 4px;
    selection-background-color: @ACCENT@; selection-color: #120c02;
}
QTextEdit { padding: 8px 10px; }
QListWidget { outline: none; }
QPushButton {
    background: rgba(255, 168, 38, 6%);
    color: @ACCENT@;
    border: 1px solid @LINE_STRONG@; border-radius: 3px;
    padding: 7px 10px; font-weight: 700;
}
QPushButton:hover {
    background: rgba(255, 168, 38, 16%);
    border-color: @ACCENT@; color: #ffd08a;
}
QPushButton:pressed { background: @ACCENT@; color: #120c02; }
QPushButton:disabled { color: #6b5a3a; border-color: @LINE@; background: transparent; }
QPushButton#primaryBtn {
    background: @ACCENT@; color: #120c02; border: 1px solid @ACCENT@;
}
QPushButton#primaryBtn:hover { background: #ffb84a; border-color: #ffb84a; color: #120c02; }
QPushButton#primaryBtn:pressed { background: #e08f12; }
QPushButton#primaryBtn:disabled { background: #5a4010; border-color: #5a4010; color: #2a1d08; }
QPushButton#dangerBtn { border-color: @DANGER@; color: @DANGER@; background: transparent; }
QPushButton#dangerBtn:hover { background: rgba(255, 92, 77, 16%); color: #ff8a7d; border-color: #ff8a7d; }
QPushButton#closeBtn {
    background: transparent; color: @MUTED@;
    border: 1px solid @LINE@; padding: 0px; font-size: 12px;
}
QPushButton#closeBtn:hover { color: @DANGER@; border-color: @DANGER@; background: rgba(255, 92, 77, 12%); }
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid @LINE_STRONG@; background: @FIELD@; border-radius: 2px;
}
QCheckBox::indicator:hover { border-color: @ACCENT@; }
QCheckBox::indicator:checked { background: @ACCENT@; border-color: @ACCENT@; }
QScrollArea { background: transparent; border: none; }
QWidget#qt_scrollarea_viewport { background: transparent; }
QWidget#hudBody { background: transparent; }
QScrollBar:vertical {
    background: rgba(255, 168, 38, 6%); width: 8px; margin: 0px; border: none;
}
QScrollBar::handle:vertical {
    background: rgba(255, 168, 38, 55%); border-radius: 3px;
    min-height: 28px; margin: 1px;
}
QScrollBar::handle:vertical:hover { background: @ACCENT@; }
QScrollBar:horizontal {
    background: rgba(255, 168, 38, 6%); height: 8px; margin: 0px; border: none;
}
QScrollBar::handle:horizontal {
    background: rgba(255, 168, 38, 55%); border-radius: 3px;
    min-width: 28px; margin: 1px;
}
QScrollBar::handle:horizontal:hover { background: @ACCENT@; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QToolTip {
    background: @PANEL@; color: @TEXT@;
    border: 1px solid @LINE_STRONG@; padding: 4px 6px;
}
QMessageBox, QInputDialog { background: @BG@; }
QMessageBox QLabel, QInputDialog QLabel { color: @TEXT@; font-size: 12px; }
"""


def _render_style(template: str) -> str:
    tokens = {
        "BG": BG, "PANEL": PANEL, "FIELD": FIELD, "LINE": LINE,
        "LINE_STRONG": LINE_STRONG, "ACCENT": ACCENT, "TEXT": TEXT,
        "MUTED": MUTED, "DANGER": DANGER, "OK": OK, "BLUE": BLUE, "MONO": _MONO,
    }
    for key, val in tokens.items():
        template = template.replace(f"@{key}@", val)
    return template


_STYLE = _render_style(_STYLE_TEMPLATE)

_ARROW_CACHE: dict[str, str] = {}


def _arrow_icons() -> dict[str, str]:
    """Create the two tiny spin-box arrow PNGs once (needs a QApplication)."""
    if _ARROW_CACHE:
        return _ARROW_CACHE
    try:
        folder = Path(tempfile.gettempdir()) / "aura_hud_icons"
        folder.mkdir(parents=True, exist_ok=True)
        for name, pts in (
            ("up", [(0.5, 4.5), (4.0, 0.8), (7.5, 4.5)]),
            ("down", [(0.5, 0.8), (4.0, 4.5), (7.5, 0.8)]),
        ):
            pm = QPixmap(8, 5)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
            p.end()
            target = folder / f"spin_{name}_{ACCENT.lstrip('#')}.png"
            if pm.save(str(target), "PNG"):
                _ARROW_CACHE[name] = target.as_posix()
    except Exception:  # cosmetic only — never let icons break the dialog
        _LOG.debug("Could not create spin-box arrow icons", exc_info=True)
    return _ARROW_CACHE


def _style() -> str:
    css = _STYLE
    icons = _arrow_icons()
    if "up" in icons and "down" in icons:
        css += (
            f"\nQSpinBox::up-arrow {{ image: url({icons['up']}); width: 8px; height: 5px; }}"
            f"\nQSpinBox::down-arrow {{ image: url({icons['down']}); width: 8px; height: 5px; }}\n"
        )
    return css


def _spaced(widget: QWidget, px: float) -> None:
    f = widget.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px)
    widget.setFont(f)


def _polish_hud(root: QWidget) -> None:
    """Letter-spacing + upper-case button captions, like the Neural Map panel."""
    try:
        for lbl in root.findChildren(QLabel):
            name = lbl.objectName()
            if name == "title":
                _spaced(lbl, 3.0)
            elif name == "sectionLabel":
                _spaced(lbl, 2.0)
        for btn in root.findChildren(QPushButton):
            if btn.objectName() == "closeBtn":
                continue
            btn.setText(btn.text().upper())
            _spaced(btn, 0.8)
    except Exception:
        _LOG.debug("HUD polish skipped", exc_info=True)


def _paint_hud_chrome(widget: QWidget) -> None:
    """Dark gradient panel + hairline border + amber corner brackets."""
    try:
        p = QPainter(widget)
        w, h = widget.width(), widget.height()
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor("#0d1016"))
        grad.setColorAt(1.0, QColor("#050607"))
        p.fillRect(QRect(0, 0, w, h), QBrush(grad))
        glow = QLinearGradient(0, 0, 0, 70)
        glow.setColorAt(0.0, QColor(255, 168, 38, 22))
        glow.setColorAt(1.0, QColor(255, 168, 38, 0))
        p.fillRect(QRect(0, 0, w, 70), QBrush(glow))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 168, 38, 60), 1))
        p.drawRect(0, 0, w - 1, h - 1)
        pen = QPen(QColor(ACCENT), 2)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        n = 16
        p.drawLine(1, 1, 1 + n, 1)
        p.drawLine(1, 1, 1, 1 + n)
        p.drawLine(w - 2, 1, w - 2 - n, 1)
        p.drawLine(w - 2, 1, w - 2, 1 + n)
        p.drawLine(1, h - 2, 1 + n, h - 2)
        p.drawLine(1, h - 2, 1, h - 2 - n)
        p.drawLine(w - 2, h - 2, w - 2 - n, h - 2)
        p.drawLine(w - 2, h - 2, w - 2, h - 2 - n)
        p.end()
    except Exception:
        _LOG.debug("HUD paint skipped", exc_info=True)


class _BulletDelegate(QStyledItemDelegate):
    """Round-bullet rows like the 'TOP HUBS' list (amber = ON, blue = OFF)."""

    def paint(self, painter, option, index) -> None:
        painter.save()
        try:
            rect = option.rect
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
            if selected:
                painter.fillRect(rect, QColor(255, 168, 38, 36))
                painter.fillRect(QRect(rect.left(), rect.top(), 2, rect.height()), QColor(ACCENT))
            elif hover:
                painter.fillRect(rect, QColor(255, 168, 38, 16))
            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            dot = QColor(ACCENT) if text.startswith("[ON]") else QColor(BLUE)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot)
            painter.drawEllipse(QPointF(rect.left() + 13, rect.center().y() + 0.5), 3.0, 3.0)
            painter.setPen(QColor(ACCENT if selected else TEXT))
            painter.setFont(option.font)
            area = rect.adjusted(26, 0, -8, 0)
            elided = option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, max(10, area.width()))
            painter.drawText(
                area,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                elided,
            )
        except Exception:
            _LOG.debug("Bullet delegate paint skipped", exc_info=True)
        finally:
            painter.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), 28))


class CalendarSettingsDialog(QDialog):
    """Draggable, scrollable HUD-style Calendar settings & connections dialog."""

    _test_done_sig = pyqtSignal(bool, str)
    _connect_done_sig = pyqtSignal(bool, str)

    def __init__(self, parent=None, service: CalendarService | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("calendarDialog")
        self.setWindowTitle("A.U.R.A. — Calendar & Schedule")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(560, 540)
        self.resize(620, 680)
        self.setStyleSheet(_style())
        self._service = service or get_calendar_service()
        self._drag_pos: QPoint | None = None

        self._test_done_sig.connect(self._on_test_done)
        self._connect_done_sig.connect(self._on_connect_done)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(10)

        # ── Draggable header ─────────────────────────────────────────────────
        hdr = QWidget()
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(0, 0, 0, 4)
        title_lbl = QLabel("●  CALENDAR & SCHEDULE")
        title_lbl.setObjectName("title")
        hdr_lay.addWidget(title_lbl)
        hdr_lay.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        hdr_lay.addWidget(close_btn)
        hdr.mousePressEvent = self._hdr_press
        hdr.mouseMoveEvent = self._hdr_move
        outer.addWidget(hdr)

        sep = QFrame()
        sep.setObjectName("rule")
        outer.addWidget(sep)

        privacy_lbl = QLabel("AURA only sees calendars you connect here. Nothing is connected by default.")
        privacy_lbl.setObjectName("statusBox")
        privacy_lbl.setWordWrap(True)
        outer.addWidget(privacy_lbl)

        if not self._service.secrets.using_keyring:
            sec_lbl = QLabel("Note: OS keyring unavailable; feed links are stored in a user-only private file.")
            sec_lbl.setObjectName("statusBox")
            sec_lbl.setWordWrap(True)
            outer.addWidget(sec_lbl)

        # ── Scrollable body (prevents clipping at any window size) ───────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        body = QWidget()
        body.setObjectName("hudBody")
        body.setAutoFillBackground(False)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(2, 2, 6, 2)
        body_lay.setSpacing(10)

        # Connected calendars list
        sec1 = QLabel("CONNECTED CALENDARS")
        sec1.setObjectName("sectionLabel")
        body_lay.addWidget(sec1)

        self._cal_list = QListWidget()
        self._cal_list.setMinimumHeight(120)
        self._cal_list.setItemDelegate(_BulletDelegate(self._cal_list))
        self._cal_list.setMouseTracking(True)
        body_lay.addWidget(self._cal_list)

        cal_btns = QHBoxLayout()
        cal_btns.setSpacing(6)
        toggle_btn = QPushButton("Enable / Disable")
        toggle_btn.clicked.connect(self._toggle_selected)
        cal_btns.addWidget(toggle_btn)

        refresh_btn = QPushButton("Refresh Feeds")
        refresh_btn.clicked.connect(self._refresh_feeds)
        cal_btns.addWidget(refresh_btn)

        disconnect_btn = QPushButton("Disconnect && Delete Data")
        disconnect_btn.setObjectName("dangerBtn")
        disconnect_btn.clicked.connect(self._disconnect_selected)
        cal_btns.addWidget(disconnect_btn)
        body_lay.addLayout(cal_btns)

        # Add calendar by link
        sec2 = QLabel("ADD CALENDAR BY PRIVATE LINK (READ-ONLY HTTPS .ICS)")
        sec2.setObjectName("sectionLabel")
        body_lay.addWidget(sec2)

        link_card = QFrame()
        link_card.setObjectName("card")
        link_lay = QVBoxLayout(link_card)
        link_lay.setContentsMargins(14, 12, 12, 12)
        link_lay.setSpacing(8)

        self._link_name = QLineEdit()
        self._link_name.setPlaceholderText("Calendar name (e.g. Google Personal)")
        link_lay.addWidget(self._link_name)

        self._link_url = QLineEdit()
        self._link_url.setPlaceholderText("Paste secret HTTPS iCal (.ics) URL")
        self._link_url.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        link_lay.addWidget(self._link_url)

        link_btns = QHBoxLayout()
        link_btns.setSpacing(6)
        self._test_btn = QPushButton("Test Link")
        self._test_btn.clicked.connect(self._test_link)
        link_btns.addWidget(self._test_btn)

        self._add_link_btn = QPushButton("Connect Calendar")
        self._add_link_btn.setObjectName("primaryBtn")
        self._add_link_btn.clicked.connect(self._connect_link)
        link_btns.addWidget(self._add_link_btn)
        link_lay.addLayout(link_btns)

        self._link_status = QLabel("")
        self._link_status.setObjectName("noteLabel")
        self._link_status.setWordWrap(True)
        link_lay.addWidget(self._link_status)
        body_lay.addWidget(link_card)

        # Import / Export .ics file
        sec3 = QLabel("LOCAL CALENDAR (.ICS IMPORT / EXPORT)")
        sec3.setObjectName("sectionLabel")
        body_lay.addWidget(sec3)

        file_row = QHBoxLayout()
        imp_btn = QPushButton("Import .ics File…")
        imp_btn.clicked.connect(self._import_ics)
        file_row.addWidget(imp_btn)
        exp_btn = QPushButton("Export Local Calendar…")
        exp_btn.clicked.connect(self._export_ics)
        file_row.addWidget(exp_btn)
        body_lay.addLayout(file_row)

        # Preferences
        sec4 = QLabel("PREFERENCES")
        sec4.setObjectName("sectionLabel")
        body_lay.addWidget(sec4)

        pref_card = QFrame()
        pref_card.setObjectName("card")
        pref_lay = QVBoxLayout(pref_card)
        pref_lay.setContentsMargins(14, 12, 12, 12)
        pref_lay.setSpacing(8)

        rem_row = QHBoxLayout()
        rem_row.addWidget(QLabel("Default reminder (minutes before):"))
        rem_row.addStretch()
        self._rem_spin = QSpinBox()
        self._rem_spin.setRange(0, 1440)
        rem_row.addWidget(self._rem_spin)
        pref_lay.addLayout(rem_row)

        hrs_row = QHBoxLayout()
        hrs_row.addWidget(QLabel("Working hours (start – end):"))
        hrs_row.addStretch()
        self._work_start = QSpinBox()
        self._work_start.setRange(0, 23)
        self._work_end = QSpinBox()
        self._work_end.setRange(1, 24)
        hrs_row.addWidget(self._work_start)
        hrs_row.addWidget(QLabel("to"))
        hrs_row.addWidget(self._work_end)
        pref_lay.addLayout(hrs_row)

        # Wrapped caption beside the (same) checkbox so the long text never forces a sideways scroll.
        ai_row = QHBoxLayout()
        ai_row.setSpacing(8)
        self._ai_polish_chk = QCheckBox()
        ai_row.addWidget(self._ai_polish_chk, 0, Qt.AlignmentFlag.AlignTop)
        ai_lbl = QLabel("AI-polished summaries (sends minimal event titles to active AI engine)")
        ai_lbl.setWordWrap(True)
        ai_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        ai_lbl.mousePressEvent = lambda _ev: self._ai_polish_chk.toggle()
        ai_row.addWidget(ai_lbl, 1)
        pref_lay.addLayout(ai_row)

        save_pref_btn = QPushButton("Save Preferences")
        save_pref_btn.setObjectName("primaryBtn")
        save_pref_btn.clicked.connect(self._save_preferences)
        pref_lay.addWidget(save_pref_btn)

        body_lay.addWidget(pref_card)
        body_lay.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(self))
        outer.addLayout(grip_row)

        _polish_hud(self)
        self._reload_ui()

    def paintEvent(self, event) -> None:
        _paint_hud_chrome(self)

    def _hdr_press(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def _hdr_move(self, ev) -> None:
        if ev.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            ev.accept()

    def _reload_ui(self) -> None:
        self._cal_list.clear()
        for cal in self._service.store.list_calendars(enabled_only=False):
            status = "ON" if cal.enabled else "OFF"
            ro = "Read-Only" if cal.read_only else "Read/Write"
            item = QListWidgetItem(f"[{status}] {cal.name}  ({cal.provider}, {ro})")
            item.setData(Qt.ItemDataRole.UserRole, cal.id)
            self._cal_list.addItem(item)

        s = self._service.store.get_settings()
        self._rem_spin.setValue(int(s.get("default_reminder_minutes", 15)))
        self._work_start.setValue(int(s.get("work_start_hour", 9)))
        self._work_end.setValue(int(s.get("work_end_hour", 18)))
        self._ai_polish_chk.setChecked(bool(s.get("ai_polish_enabled", False)))

    def _selected_calendar_id(self) -> str | None:
        item = self._cal_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _toggle_selected(self) -> None:
        cid = self._selected_calendar_id()
        if not cid:
            return
        cal = self._service.store.get_calendar(cid)
        if cal:
            self._service.store.set_calendar_enabled(cid, not cal.enabled)
            self._reload_ui()

    def _disconnect_selected(self) -> None:
        cid = self._selected_calendar_id()
        if not cid:
            return
        if cid == "local":
            QMessageBox.information(self, "Local Calendar", "Use 'Enable / Disable' for the built-in local calendar.")
            return
        self._service.disconnect_calendar(cid)
        self._reload_ui()

    def _refresh_feeds(self) -> None:
        self._link_status.setText("Refreshing feeds…")
        threading.Thread(target=self._do_refresh_worker, daemon=True).start()

    def _do_refresh_worker(self) -> None:
        res = self._service.refresh_all()
        ok_count = sum(1 for h in res.values() if h.ok)
        self._test_done_sig.emit(True, f"Refreshed {ok_count}/{len(res)} feed(s).")

    def _test_link(self) -> None:
        url = self._link_url.text().strip()
        try:
            validate_feed_url(url)
        except CalendarError as exc:
            self._link_status.setText(f"Invalid link: {exc}")
            return
        self._test_btn.setEnabled(False)
        self._link_status.setText("Testing link…")

        def _worker():
            import requests

            try:
                r = requests.get(url, timeout=5, allow_redirects=False, stream=True)
                code = r.status_code
                r.close()
                if code == 200:
                    self._test_done_sig.emit(True, "Link reachable (HTTP 200).")
                else:
                    self._test_done_sig.emit(False, f"Server returned HTTP {code}.")
            except Exception as exc:
                self._test_done_sig.emit(False, f"Could not reach link: {type(exc).__name__}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_test_done(self, ok: bool, msg: str) -> None:
        self._test_btn.setEnabled(True)
        self._link_status.setText(msg)

    def _connect_link(self) -> None:
        name = self._link_name.text().strip() or "Subscribed Calendar"
        url = self._link_url.text().strip()
        try:
            validate_feed_url(url)
        except CalendarError as exc:
            self._link_status.setText(f"Invalid link: {exc}")
            return
        self._add_link_btn.setEnabled(False)
        self._link_status.setText("Connecting & syncing…")

        def _worker():
            try:
                _cal, health = self._service.connect_calendar(name, "ics_feed", url=url, sync_now=True)
                self._connect_done_sig.emit(health.ok, health.message)
            except Exception as exc:
                self._connect_done_sig.emit(False, str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_connect_done(self, ok: bool, msg: str) -> None:
        self._add_link_btn.setEnabled(True)
        self._link_status.setText(msg)
        if ok:
            self._link_name.clear()
            self._link_url.clear()
        self._reload_ui()

    def _import_ics(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import .ics File", "", "iCalendar (*.ics)")
        if not path:
            return
        try:
            count = self._service.import_ics(path)
            QMessageBox.information(self, "Import Complete", f"Imported {count} event(s) into local calendar.")
        except Exception as exc:
            QMessageBox.warning(self, "Import Failed", str(exc))

    def _export_ics(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Local Calendar", "aura_schedule.ics", "iCalendar (*.ics)")
        if not path:
            return
        try:
            count = self._service.export_ics(path)
            QMessageBox.information(self, "Export Complete", f"Exported {count} event(s).")
        except Exception as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))

    def _save_preferences(self) -> None:
        self._service.store.set_setting("default_reminder_minutes", self._rem_spin.value())
        self._service.store.set_setting("work_start_hour", self._work_start.value())
        self._service.store.set_setting("work_end_hour", self._work_end.value())
        self._service.store.set_setting("ai_polish_enabled", self._ai_polish_chk.isChecked())
        self._link_status.setText("Preferences saved.")


class AgendaPanel(QWidget):
    """Draggable, resizable floating HUD panel for 'show my schedule'."""

    closed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("AgendaPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(340, 260)
        self.resize(440, 380)
        self.setStyleSheet(_style())
        self._drag_pos: QPoint | None = None
        self._closed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 16, 14)
        lay.setSpacing(8)

        hdr = QWidget()
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(0, 0, 0, 2)
        self._title_lbl = QLabel("●  SCHEDULE")
        self._title_lbl.setObjectName("title")
        hdr_lay.addWidget(self._title_lbl)
        hdr_lay.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(22, 22)
        close_btn.clicked.connect(self.close)
        hdr_lay.addWidget(close_btn)
        hdr.mousePressEvent = self._hdr_press
        hdr.mouseMoveEvent = self._hdr_move
        lay.addWidget(hdr)

        sep = QFrame()
        sep.setObjectName("rule")
        lay.addWidget(sep)

        self._body = QTextEdit()
        self._body.setReadOnly(True)
        self._body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._body, 1)

        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(self))
        lay.addLayout(grip_row)

        _polish_hud(self)

    def paintEvent(self, event) -> None:
        _paint_hud_chrome(self)

    def set_agenda(self, title: str, text: str) -> None:
        self._closed = False
        self._title_lbl.setText(f"●  SCHEDULE — {title.upper()[:36]}")
        self._body.setPlainText(text or "No events scheduled.")

    def _hdr_press(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def _hdr_move(self, ev) -> None:
        if ev.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            ev.accept()

    def closeEvent(self, event) -> None:
        self._closed = True
        self.closed.emit()
        super().closeEvent(event)