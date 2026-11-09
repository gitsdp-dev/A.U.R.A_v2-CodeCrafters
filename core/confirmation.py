"""Confirmation plans, a Qt-thread dialog, and the process-local permission broker."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QKeyEvent, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ConfirmationDecision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    SESSION_READ = "session_read"


@dataclass(frozen=True)
class ConfirmationPlan:
    action_id: str
    title: str
    summary: str
    operations: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.MEDIUM
    paths: tuple[str, ...] = ()
    item_count: int = 0
    total_size: int = 0
    reversible: bool = True
    requires_admin: bool = False
    restart_required: bool = False
    read_only: bool = False
    allow_session_read: bool = False
    details: tuple[str, ...] = ()
    session_grant_path: str | None = None

    def __post_init__(self):
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("A confirmation plan needs an action id and title.")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("A confirmation plan needs an action id and title.")
        if not isinstance(self.summary, str):
            raise TypeError("A confirmation plan summary must be text.")
        if not isinstance(self.risk_level, RiskLevel):
            raise TypeError("A confirmation plan has an invalid risk level.")
        for values in (self.operations, self.paths, self.details):
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(item, str) for item in values
            ):
                raise ValueError("Confirmation paths and operations must be text.")
        if self.item_count < 0 or self.total_size < 0:
            raise ValueError("Confirmation counts and sizes cannot be negative.")
        if self.allow_session_read and not self.read_only:
            raise ValueError("Session read grants are available only for read-only plans.")
        if self.allow_session_read and not self.session_grant_path:
            raise ValueError("A session read grant needs an exact folder scope.")
        if self.session_grant_path is not None and not isinstance(self.session_grant_path, str):
            raise TypeError("The session grant scope must be a path string.")


@dataclass
class _PendingRequest:
    plan: ConfirmationPlan
    event: threading.Event = field(default_factory=threading.Event)
    decision: ConfirmationDecision = ConfirmationDecision.DENY
    deadline: float = 0.0
    dialog: ConfirmationDialog | None = None


def _middle_elide(value: str, limit: int = 76) -> str:
    if len(value) <= limit:
        return value
    left = (limit - 3) // 2
    return f"{value[:left]}...{value[-(limit - left - 3):]}"


_SANS = '"Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Inter", "Helvetica Neue", sans-serif'
_MONO = '"Cascadia Mono", "Consolas", "SF Mono", "Menlo", monospace'

_RISK_STYLE = {
    RiskLevel.LOW: ("#62c995", "Low risk"),
    RiskLevel.MEDIUM: ("#e6b74f", "Medium risk"),
    RiskLevel.HIGH: ("#ef8b32", "High risk"),
    RiskLevel.CRITICAL: ("#ed5d55", "Critical risk"),
}

_CARD_WIDTH = 500
_SHADOW_MARGIN = 22
_BODY_MAX_HEIGHT = 340


def _qcolor(value, fallback: str = "#ff9224") -> QColor:
    color = QColor(str(value))
    return color if color.isValid() else QColor(fallback)


def _rgba(color: QColor, alpha: int) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


def _on_color(color: QColor) -> str:
    """Readable text colour for a filled button of the given colour."""
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return "#0a0e11" if luminance > 150 else "#ffffff"


class _RiskGlyph(QWidget):
    """A small shield: a tick when the request is routine, an exclamation when it is not."""

    def __init__(self, color: QColor, alert: bool, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._alert = alert
        self.setFixedSize(40, 40)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(self._color)
        fill.setAlpha(34)
        ring = QColor(self._color)
        ring.setAlpha(90)
        painter.setPen(QPen(ring, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(0.5, 0.5, 39, 39), 12, 12)

        painter.translate(2, 2)
        pen = QPen(self._color, 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        shield = QPainterPath()
        shield.moveTo(18, 8)
        shield.lineTo(26, 11)
        shield.lineTo(26, 18)
        shield.cubicTo(26, 23, 22, 27, 18, 29)
        shield.cubicTo(14, 27, 10, 23, 10, 18)
        shield.lineTo(10, 11)
        shield.closeSubpath()
        painter.drawPath(shield)

        if self._alert:
            painter.drawLine(QPointF(18, 14.5), QPointF(18, 19.5))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color)
            painter.drawEllipse(QPointF(18, 23), 1.2, 1.2)
        else:
            tick = QPainterPath()
            tick.moveTo(14.2, 18.6)
            tick.lineTo(16.9, 21.2)
            tick.lineTo(21.8, 15.4)
            painter.drawPath(tick)


class ConfirmationDialog(QDialog):
    """A standalone, draggable, deny-by-default permission note."""

    def __init__(self, plan: ConfirmationPlan, accent: str = "#ff9224", parent=None):
        super().__init__(parent)
        self.plan = plan
        self.decision = ConfirmationDecision.DENY
        self._drag_offset: QPoint | None = None
        self._placed = False
        self._fade: QPropertyAnimation | None = None
        self._lock_timer: QTimer | None = None
        self._lock_remaining = 0

        accent_color = _qcolor(accent)
        risk_hex, risk_label = _RISK_STYLE[plan.risk_level]
        risk_color = QColor(risk_hex)
        severe = plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        action_color = risk_color if severe else accent_color

        self.setWindowTitle("A.U.R.A permission request")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(False)
        self.setFixedWidth(_CARD_WIDTH + 2 * _SHADOW_MARGIN)
        self.setStyleSheet(self._build_stylesheet(accent_color, action_color, risk_color))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW_MARGIN, _SHADOW_MARGIN - 8, _SHADOW_MARGIN, _SHADOW_MARGIN + 8)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("card")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(44)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 170))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(22, 20, 14, 16)
        header_row.setSpacing(14)
        header_row.addWidget(_RiskGlyph(risk_color, severe), 0, Qt.AlignmentFlag.AlignTop)

        heading = QVBoxLayout()
        heading.setSpacing(2)
        title = QLabel(_middle_elide(plan.title, 90))
        title.setObjectName("title")
        title.setWordWrap(True)
        title.setToolTip(plan.title)
        heading.addWidget(title)
        subtitle = QLabel("A.U.R.A needs your permission")
        subtitle.setObjectName("subtitle")
        heading.addWidget(subtitle)
        header_row.addLayout(heading, 1)

        side = QVBoxLayout()
        side.setSpacing(8)
        close = QToolButton()
        close.setObjectName("close")
        close.setText("\u00d7")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Deny and close")
        close.clicked.connect(self._deny)
        side.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        badge = QLabel(risk_label)
        badge.setObjectName("riskBadge")
        side.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
        header_row.addLayout(side)
        card_layout.addWidget(header)
        card_layout.addWidget(self._divider())

        # ── Body (scrolls only when the plan is long) ───────────────────
        body = QWidget()
        body.setObjectName("body")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 16, 22, 16)
        body_layout.setSpacing(14)

        summary_text = (plan.summary or "").strip()
        if summary_text and summary_text != plan.title.strip():
            summary = QLabel(_middle_elide(summary_text, 220))
            summary.setObjectName("summary")
            summary.setWordWrap(True)
            summary.setToolTip(plan.summary)
            body_layout.addWidget(summary)

        if plan.paths:
            body_layout.addLayout(self._section("Where", self._path_panel(plan.paths)))

        if plan.operations:
            body_layout.addLayout(self._section("What will happen", self._operation_list(plan.operations)))

        scroll = QScrollArea()
        scroll.setObjectName("scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        natural = max(body_layout.totalHeightForWidth(_CARD_WIDTH), body.sizeHint().height()) + 2
        scroll.setFixedHeight(min(natural, _BODY_MAX_HEIGHT))
        if natural <= _BODY_MAX_HEIGHT:
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        card_layout.addWidget(scroll)

        # Safety facts stay pinned below the scroll area so a long list can never hide them.
        pinned = QWidget()
        pinned_col = QVBoxLayout(pinned)
        pinned_col.setContentsMargins(22, 0, 22, 16)
        pinned_col.setSpacing(10)
        pinned_col.addWidget(self._facts_panel(plan))
        if plan.requires_admin:
            pinned_col.addWidget(
                self._notice("Administrator rights needed", "Windows will ask you to approve.", "#e6b74f")
            )
        if plan.restart_required:
            pinned_col.addWidget(
                self._notice("Restart may be needed", "The change might not take effect until you restart.", "#6fb3e0")
            )
        card_layout.addWidget(pinned)
        card_layout.addWidget(self._divider())

        # ── Footer ──────────────────────────────────────────────────────
        footer = QWidget()
        footer_col = QVBoxLayout(footer)
        footer_col.setContentsMargins(22, 14, 22, 18)
        footer_col.setSpacing(12)

        self.confirm_input = None
        if plan.risk_level is RiskLevel.CRITICAL:
            instruction = QLabel("Type CONFIRM to enable approval.")
            instruction.setObjectName("instruction")
            footer_col.addWidget(instruction)
            self.confirm_input = QLineEdit()
            self.confirm_input.setPlaceholderText("CONFIRM")
            self.confirm_input.textChanged.connect(self._refresh_approve)
            footer_col.addWidget(self.confirm_input)

        self.session_button = None
        if plan.allow_session_read and plan.read_only:
            self.session_button = QPushButton("Allow reading this folder for this session")
            self.session_button.setObjectName("outline")
            self.session_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.session_button.clicked.connect(self._allow_session)
            footer_col.addWidget(self.session_button)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        hint = QLabel("Esc to deny")
        hint.setObjectName("hint")
        buttons.addWidget(hint)
        buttons.addStretch(1)
        self.deny_button = QPushButton("Deny")
        self.deny_button.setObjectName("ghost")
        self.deny_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.deny_button.clicked.connect(self._deny)
        buttons.addWidget(self.deny_button)
        self._approve_label = "Approve this once"
        self.approve_button = QPushButton(self._approve_label)
        self.approve_button.setObjectName("primary")
        self.approve_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.approve_button.clicked.connect(self._approve)
        buttons.addWidget(self.approve_button)
        footer_col.addLayout(buttons)
        card_layout.addWidget(footer)

        self.deny_button.setDefault(True)
        self.deny_button.setFocus()

        self._approve_delay_elapsed = plan.risk_level in (RiskLevel.LOW, RiskLevel.MEDIUM)
        if severe:
            self.approve_button.setEnabled(False)
            self._lock_remaining = 2
            self.approve_button.setText(f"{self._approve_label} ({self._lock_remaining}s)")
            self._lock_timer = QTimer(self)
            self._lock_timer.setInterval(1000)
            self._lock_timer.timeout.connect(self._tick_lock)
            self._lock_timer.start()
        self._refresh_approve()
        self.adjustSize()

    # ── Building blocks ─────────────────────────────────────────────────
    @staticmethod
    def _build_stylesheet(accent: QColor, action: QColor, risk: QColor) -> str:
        sheet = """
        ConfirmationDialog { background: transparent; }
        #card { background: #0b1014; border: 1px solid rgba(255, 255, 255, 28); border-radius: 14px; }
        #card QLabel { background: transparent; color: #d5dce1; font-family: @SANS@; font-size: 13px; }
        QLabel#title { color: #f4f7f9; font-size: 16px; font-weight: 600; }
        QLabel#subtitle { color: #8a979f; font-size: 12px; }
        QLabel#riskBadge { color: @RISK@; background: @RISK_BG@; border: 1px solid @RISK_LINE@;
            border-radius: 10px; padding: 2px 10px; font-size: 12px; font-weight: 600; }
        QLabel#summary { color: #b9c3ca; line-height: 150%; }
        QLabel#sectionLabel { color: #8a979f; font-size: 12px; font-weight: 600; }
        QLabel#pathText { color: #cfd8de; font-family: @MONO@; font-size: 12px; }
        QLabel#opText { color: #d5dce1; }
        QLabel#factValue { color: #f0f4f6; font-weight: 600; }
        QLabel#factLabel { color: #8a979f; font-size: 11px; }
        QLabel#noticeTitle { color: #f0f4f6; font-weight: 600; }
        QLabel#noticeText { color: #9aa6ad; font-size: 12px; }
        QLabel#instruction { color: #e8edf0; }
        QLabel#hint { color: #66737b; font-size: 12px; }
        #inset { background: #10171c; border: 1px solid rgba(255, 255, 255, 18); border-radius: 10px; }
        #dot { background: @ACCENT@; border-radius: 2px; }
        #rule { background: rgba(255, 255, 255, 20); border: none; }
        #cellRule { background: rgba(255, 255, 255, 20); border: none; }
        #notice { background: #10171c; border: 1px solid rgba(255, 255, 255, 18); border-radius: 10px; }
        #body, QScrollArea#scroll { background: transparent; border: none; }
        QScrollArea#scroll > QWidget#qt_scrollarea_viewport { background: transparent; }
        QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
        QScrollBar::handle:vertical { background: rgba(255, 255, 255, 44); border-radius: 3px; min-height: 28px; }
        QScrollBar::handle:vertical:hover { background: rgba(255, 255, 255, 80); }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QToolButton#close { background: transparent; color: #8a979f; border: none; border-radius: 6px;
            font-size: 20px; min-width: 26px; min-height: 26px; }
        QToolButton#close:hover { background: rgba(255, 255, 255, 22); color: #f4f7f9; }
        QLineEdit { background: #10171c; color: #f4f7f9; border: 1px solid rgba(255, 255, 255, 40);
            border-radius: 8px; padding: 8px 10px; font-family: @MONO@; font-size: 13px; }
        QLineEdit:focus { border: 1px solid @RISK@; }
        QPushButton { font-family: @SANS@; font-size: 13px; font-weight: 600; border-radius: 8px;
            min-height: 20px; padding: 7px 18px; }
        QPushButton#ghost { background: rgba(255, 255, 255, 12); color: #e8edf0;
            border: 1px solid rgba(255, 255, 255, 34); }
        QPushButton#ghost:hover { background: rgba(255, 255, 255, 26); }
        QPushButton#ghost:focus { border: 1px solid rgba(255, 255, 255, 120); }
        QPushButton#outline { background: transparent; color: @ACCENT@; border: 1px solid @ACCENT_LINE@; }
        QPushButton#outline:hover { background: @ACCENT_BG@; }
        QPushButton#outline:disabled { color: #66737b; border: 1px solid rgba(255, 255, 255, 18); }
        QPushButton#primary { background: @ACTION@; color: @ON_ACTION@; border: 1px solid @ACTION@; }
        QPushButton#primary:hover { background: @ACTION_HOVER@; border: 1px solid @ACTION_HOVER@; }
        QPushButton#primary:disabled { background: rgba(255, 255, 255, 12); color: #66737b;
            border: 1px solid rgba(255, 255, 255, 18); }
        """
        tokens = {
            "@SANS@": _SANS,
            "@MONO@": _MONO,
            "@ACCENT@": accent.name(),
            "@ACCENT_LINE@": _rgba(accent, 120),
            "@ACCENT_BG@": _rgba(accent, 30),
            "@ACTION@": action.name(),
            "@ACTION_HOVER@": action.lighter(115).name(),
            "@ON_ACTION@": _on_color(action),
            "@RISK@": risk.name(),
            "@RISK_BG@": _rgba(risk, 30),
            "@RISK_LINE@": _rgba(risk, 100),
        }
        for token, value in tokens.items():
            sheet = sheet.replace(token, value)
        return sheet

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("rule")
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _section(label: str, content: QWidget) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(7)
        heading = QLabel(label)
        heading.setObjectName("sectionLabel")
        column.addWidget(heading)
        column.addWidget(content)
        return column

    @staticmethod
    def _path_panel(paths) -> QFrame:
        panel = QFrame()
        panel.setObjectName("inset")
        column = QVBoxLayout(panel)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(6)
        for path in paths[:15]:
            label = QLabel(_middle_elide(str(path), 58))
            label.setObjectName("pathText")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setToolTip(str(path))
            column.addWidget(label)
        if len(paths) > 15:
            more = QLabel(f"and {len(paths) - 15} more locations")
            more.setObjectName("factLabel")
            column.addWidget(more)
        return panel

    @staticmethod
    def _operation_list(operations) -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        for operation in operations[:15]:
            row = QHBoxLayout()
            row.setSpacing(10)
            dot_column = QVBoxLayout()
            dot_column.setContentsMargins(0, 7, 0, 0)
            dot = QFrame()
            dot.setObjectName("dot")
            dot.setFixedSize(5, 5)
            dot_column.addWidget(dot)
            dot_column.addStretch(1)
            row.addLayout(dot_column)
            text = QLabel(_middle_elide(operation, 180))
            text.setObjectName("opText")
            text.setWordWrap(True)
            text.setToolTip(operation)
            row.addWidget(text, 1)
            column.addLayout(row)
        if len(operations) > 15:
            more = QLabel(f"and {len(operations) - 15} more items")
            more.setObjectName("factLabel")
            column.addWidget(more)
        return holder

    def _facts_panel(self, plan: ConfirmationPlan) -> QFrame:
        panel = QFrame()
        panel.setObjectName("inset")
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        cells: list[tuple[str, str, str | None]] = []
        if plan.item_count:
            cells.append((str(plan.item_count), "Items", None))
        if plan.total_size:
            cells.append((self._format_size(plan.total_size), "Total size", None))
        cells.append(
            ("Yes", "Can be undone", "#62c995") if plan.reversible
            else ("No", "Can be undone", "#ed5d55")
        )

        for index, (value, label, color) in enumerate(cells):
            if index:
                rule = QFrame()
                rule.setObjectName("cellRule")
                rule.setFixedWidth(1)
                row.addWidget(rule)
            cell = QVBoxLayout()
            cell.setContentsMargins(16, 10, 16, 10)
            cell.setSpacing(1)
            value_label = QLabel(value)
            value_label.setObjectName("factValue")
            if color:
                value_label.setStyleSheet(f"color: {color};")
            cell.addWidget(value_label)
            name_label = QLabel(label)
            name_label.setObjectName("factLabel")
            cell.addWidget(name_label)
            row.addLayout(cell, 1)
        return panel

    @staticmethod
    def _notice(title: str, detail: str, color: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("notice")
        frame.setStyleSheet(f"#notice {{ border-left: 3px solid {color}; }}")
        column = QVBoxLayout(frame)
        column.setContentsMargins(14, 10, 14, 10)
        column.setSpacing(1)
        heading = QLabel(title)
        heading.setObjectName("noticeTitle")
        column.addWidget(heading)
        text = QLabel(detail)
        text.setObjectName("noticeText")
        text.setWordWrap(True)
        column.addWidget(text)
        return frame

    @staticmethod
    def _format_size(value: int) -> str:
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    # ── Behaviour ───────────────────────────────────────────────────────
    def _tick_lock(self):
        self._lock_remaining -= 1
        if self._lock_remaining <= 0:
            self._enable_after_delay()
        else:
            self.approve_button.setText(f"{self._approve_label} ({self._lock_remaining}s)")

    def _enable_after_delay(self):
        self._approve_delay_elapsed = True
        if self._lock_timer is not None:
            self._lock_timer.stop()
        self.approve_button.setText(self._approve_label)
        self._refresh_approve()

    def _refresh_approve(self):
        typed = self.confirm_input is None or self.confirm_input.text() == "CONFIRM"
        enabled = self._approve_delay_elapsed and typed
        self.approve_button.setEnabled(enabled)
        if self.session_button is not None:
            self.session_button.setEnabled(enabled)

    def _approve(self):
        if self.approve_button.isEnabled():
            self.decision = ConfirmationDecision.APPROVE
            self.accept()

    def _allow_session(self):
        self.decision = ConfirmationDecision.SESSION_READ
        self.accept()

    def _deny(self):
        self.decision = ConfirmationDecision.DENY
        self.reject()

    def closeEvent(self, event):
        self.decision = ConfirmationDecision.DENY
        super().closeEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self._deny()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._placed:
            return
        self._placed = True
        self._place_on_screen()
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(160)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _place_on_screen(self):
        anchor = self.parentWidget()
        if anchor is not None and anchor.isVisible():
            area = anchor.frameGeometry()
        else:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            area = screen.availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.top() + int((area.height() - self.height()) * 0.38)
        self.move(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class ConfirmationBroker(QObject):
    """Worker callers block on events; all Qt widgets are created on this object's GUI thread."""

    _requested = pyqtSignal(object)

    def __init__(
        self,
        parent=None,
        timeout: float = 60.0,
        audit_path: str | Path | None = None,
        accent_provider=None,
    ):
        super().__init__(parent)
        self.timeout = max(0.1, float(timeout))
        self.audit_path = Path(audit_path) if audit_path else Path(__file__).resolve().parents[1] / "memory" / "file_access_audit.jsonl"
        self.accent_provider = accent_provider
        self.accent = "#ff9224"
        self.enabled = True
        self._queue: deque[_PendingRequest] = deque()
        self._active: _PendingRequest | None = None
        self._session_read_folders: set[str] = set()
        self._lock = threading.RLock()
        self._audit_lock = threading.Lock()
        self._requested.connect(self._enqueue, Qt.ConnectionType.QueuedConnection)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def request_confirmation(
        self, plan: ConfirmationPlan, timeout: float | None = None
    ) -> ConfirmationDecision:
        if not self.enabled:
            self._audit(plan, ConfirmationDecision.DENY, "access disabled")
            return ConfirmationDecision.DENY
        if QThread.currentThread() == self.thread():
            raise RuntimeError("Confirmation requests must run off the Qt GUI thread.")
        if (
            plan.read_only
            and plan.allow_session_read
            and plan.session_grant_path
            and self._has_session_grant(plan.session_grant_path)
        ):
            self._audit(plan, ConfirmationDecision.SESSION_READ, "session grant")
            return ConfirmationDecision.SESSION_READ

        wait_seconds = self.timeout if timeout is None else max(0.0, float(timeout))
        request = _PendingRequest(plan=plan, deadline=time.monotonic() + wait_seconds)
        try:
            self._audit(plan, ConfirmationDecision.DENY, "requested")
        except OSError:
            return ConfirmationDecision.DENY
        self._requested.emit(request)
        if not request.event.wait(wait_seconds):
            request.decision = ConfirmationDecision.DENY
            request.event.set()
            self._audit(plan, ConfirmationDecision.DENY, "timeout")
            return ConfirmationDecision.DENY
        try:
            self._audit(plan, request.decision, "resolved")
        except OSError:
            return ConfirmationDecision.DENY
        if request.decision is ConfirmationDecision.SESSION_READ and plan.session_grant_path:
            with self._lock:
                self._session_read_folders.add(
                    os.path.normcase(os.path.realpath(plan.session_grant_path))
                )
        return request.decision

    def _enqueue(self, request: _PendingRequest):
        if request.event.is_set():
            return
        self._queue.append(request)
        self._present_next()

    def _present_next(self):
        if self._active is not None:
            return
        while self._queue:
            request = self._queue.popleft()
            if request.event.is_set() or request.deadline <= time.monotonic():
                request.decision = ConfirmationDecision.DENY
                request.event.set()
                continue
            self._active = request
            accent = self.accent_provider() if self.accent_provider is not None else self.accent
            dialog = ConfirmationDialog(request.plan, accent=accent, parent=self.parent())
            request.dialog = dialog
            dialog.finished.connect(lambda _result, item=request: self._finish(item, item.dialog.decision))
            remaining_ms = max(1, int((request.deadline - time.monotonic()) * 1000))
            QTimer.singleShot(remaining_ms, lambda item=request: self._finish(item, ConfirmationDecision.DENY))
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return

    def _finish(self, request: _PendingRequest, decision: ConfirmationDecision):
        if not request.event.is_set():
            request.decision = decision
            request.event.set()
        if request.dialog is not None:
            request.dialog.hide()
            request.dialog.deleteLater()
        if self._active is request:
            self._active = None
        QTimer.singleShot(0, self._present_next)

    def _audit(self, plan: ConfirmationPlan, decision: ConfirmationDecision, result: str):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
            "action_id": plan.action_id,
            "paths_or_settings": list(plan.paths),
            "operations": list(plan.operations),
            "item_count": plan.item_count,
            "total_size": plan.total_size,
            "risk": plan.risk_level.value,
            "decision": decision.value,
            "result": result,
        }
        with self._audit_lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def record_operation_result(
        self, plan: ConfirmationPlan, decision: ConfirmationDecision, result: str
    ):
        """Append a completion record without recording file contents or secrets."""
        self._audit(plan, decision, result[:500])

    def _has_session_grant(self, path: str) -> bool:
        candidate = os.path.normcase(os.path.realpath(path))
        with self._lock:
            return any(
                candidate == granted or candidate.startswith(granted.rstrip(os.sep) + os.sep)
                for granted in self._session_read_folders
            )

    def session_read_folders(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._session_read_folders))

    def revoke_session_read(self, folder: str | None = None):
        with self._lock:
            if folder is None:
                self._session_read_folders.clear()
            else:
                normalized = os.path.normcase(os.path.realpath(folder))
                self._session_read_folders.discard(normalized)

    def clear_session_grants(self):
        self.revoke_session_read()

    def shutdown(self):
        self.clear_session_grants()
        if self._active is not None:
            self._finish(self._active, ConfirmationDecision.DENY)
        while self._queue:
            request = self._queue.popleft()
            request.decision = ConfirmationDecision.DENY
            request.event.set()


_broker: ConfirmationBroker | None = None
_broker_lock = threading.Lock()


def configure_confirmation_broker(parent=None, **kwargs) -> ConfirmationBroker:
    """Install the GUI-thread-owned broker for actions and plugins to share."""
    global _broker
    with _broker_lock:
        _broker = ConfirmationBroker(parent=parent, **kwargs)
        return _broker


def get_confirmation_broker() -> ConfirmationBroker | None:
    with _broker_lock:
        return _broker


def set_confirmation_broker_enabled(enabled: bool):
    broker = get_confirmation_broker()
    if broker is not None:
        broker.enabled = bool(enabled)