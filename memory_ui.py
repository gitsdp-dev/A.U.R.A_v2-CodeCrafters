"""Standalone, non-blocking memory editor dialog (modernized HUD style)."""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSizeGrip,
    QStyle, QStyledItemDelegate, QTextEdit, QVBoxLayout, QWidget,
)

from memory.models import MemoryCategory, MemoryEntry, MemorySource, Priority
from memory.service import MemoryService

# ── Palette (black + orange HUD) ────────────────────────────────────────────
BG = "#050505"
PANEL = "#0b0b0c"
FIELD = "#0f0f10"
LINE = "#2a1f16"
LINE_STRONG = "#8a5a34"
ACCENT = "#ff9d35"
TEXT = "#ffdcb0"
MUTED = "#a88a68"
DANGER = "#ff5c4d"
OK = "#7fd18b"

_ROLE_ID = Qt.ItemDataRole.UserRole
_ROLE_META = Qt.ItemDataRole.UserRole + 1

STYLE = f"""
QDialog#memoryDialog {{
    background: {BG};
    border: 1px solid {LINE_STRONG};
}}
QWidget {{
    color: {TEXT};
    font-family: "Segoe UI", "Inter", "SF Pro Text", sans-serif;
    font-size: 13px;
}}
QLabel#title {{
    color: {ACCENT}; font-size: 15px; font-weight: 700; letter-spacing: 4px;
}}
QLabel#chip {{
    color: {MUTED}; background: {PANEL}; border: 1px solid {LINE};
    padding: 2px 10px; font-size: 11px; letter-spacing: 1px;
}}
QLabel#sectionLabel {{
    color: {MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 2px;
}}
QLabel#status {{ color: {MUTED}; font-size: 11px; letter-spacing: 1px; }}
QLabel#empty {{ color: {MUTED}; font-size: 12px; }}
QFrame#rule {{ background: {LINE}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {LINE}; }}

QLineEdit, QTextEdit, QComboBox {{
    background: {FIELD}; color: {TEXT}; border: 1px solid {LINE};
    padding: 8px 10px; selection-background-color: {ACCENT}; selection-color: #000;
}}
QLineEdit:hover, QTextEdit:hover, QComboBox:hover {{ border-color: {LINE_STRONG}; }}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {FIELD}; color: {TEXT}; border: 1px solid {LINE_STRONG};
    selection-background-color: {ACCENT}; selection-color: #000; outline: 0;
}}

QPushButton {{
    background: transparent; color: {ACCENT}; border: 1px solid {LINE_STRONG};
    padding: 8px 16px; font-size: 11px; font-weight: 600; letter-spacing: 2px;
}}
QPushButton:hover {{ border-color: {ACCENT}; background: rgba(255,157,53,0.08); }}
QPushButton:pressed {{ background: rgba(255,157,53,0.18); }}
QPushButton:disabled {{ color: #5a4a3a; border-color: {LINE}; }}
QPushButton#primary {{ background: {ACCENT}; color: #000; border-color: {ACCENT}; }}
QPushButton#primary:hover {{ background: #ffb35c; }}
QPushButton#primary:disabled {{ background: #3a2a18; color: #6a5a48; border-color: #3a2a18; }}
QPushButton#danger {{ color: {DANGER}; border-color: #5a2a25; }}
QPushButton#danger:hover {{ border-color: {DANGER}; background: rgba(255,92,77,0.10); }}
QPushButton#danger:disabled {{ color: #5a3a36; border-color: {LINE}; }}
QPushButton#close {{
    border: none; padding: 4px 10px; font-size: 14px; letter-spacing: 0;
    color: {MUTED};
}}
QPushButton#close:hover {{ color: {DANGER}; background: transparent; }}

QListWidget {{
    background: {PANEL}; border: 1px solid {LINE}; outline: 0;
}}
QListWidget::item {{ border: none; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {LINE_STRONG}; min-height: 28px; border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QMessageBox {{ background: {BG}; }}
QToolTip {{ background: {FIELD}; color: {TEXT}; border: 1px solid {LINE_STRONG}; padding: 4px; }}
"""


class _EntryDelegate(QStyledItemDelegate):
    """Two-line list row: title on top, muted metadata beneath, accent bar when selected."""

    ROW_HEIGHT = 58

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 (Qt API)
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802 (Qt API)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        if selected:
            painter.fillRect(rect, QColor(255, 157, 53, 30))
            painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), QColor(ACCENT))
        elif hovered:
            painter.fillRect(rect, QColor(255, 157, 53, 12))

        painter.setPen(QPen(QColor(LINE), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        pad = 16
        text_w = rect.width() - pad * 2

        title_font = QFont(option.font)
        title_font.setPointSizeF(option.font.pointSizeF() + 0.5)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(ACCENT if selected else TEXT))
        title = painter.fontMetrics().elidedText(
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""), Qt.TextElideMode.ElideRight, text_w)
        painter.drawText(
            QRect(rect.left() + pad, rect.top() + 9, text_w, 20),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)

        meta_font = QFont(option.font)
        meta_font.setPointSizeF(max(7.0, option.font.pointSizeF() - 2.0))
        meta_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        painter.setFont(meta_font)
        painter.setPen(QColor(MUTED))
        meta = painter.fontMetrics().elidedText(
            str(index.data(_ROLE_META) or ""), Qt.TextElideMode.ElideRight, text_w)
        painter.drawText(
            QRect(rect.left() + pad, rect.top() + 31, text_w, 18),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, meta)
        painter.restore()


class MemoryDialog(QDialog):
    """Edit local long-term memories without blocking the HUD."""

    def __init__(self, service: MemoryService | None = None, parent=None) -> None:
        super().__init__(parent)
        self.service = service or MemoryService()
        self._selected_id: str | None = None
        self._dirty = False
        self._loading = False
        self._drag_ok = False
        self.setObjectName("memoryDialog")
        self.setWindowTitle("MEMORY")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(900, 600)
        self.resize(980, 640)
        self.setModal(False)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self._set_status(""))
        self._build()
        self.refresh()

    # ── Construction ────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.setStyleSheet(STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 14)
        root.setSpacing(14)

        # Title bar (drag area)
        self._titlebar = QWidget()
        header = QHBoxLayout(self._titlebar)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        self.header = QLabel("MEMORY")
        self.header.setObjectName("title")
        self.count = QLabel()
        self.count.setObjectName("chip")
        close = QPushButton("\u2715")
        close.setObjectName("close")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Close (Esc)")
        close.clicked.connect(self.close)
        header.addWidget(self.header)
        header.addWidget(self.count)
        header.addStretch()
        header.addWidget(close)
        root.addWidget(self._titlebar)
        root.addWidget(self._rule())

        # Toolbar
        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, description, aliases")
        self.search.setObjectName("memorySearch")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.category_filter = QComboBox()
        self.category_filter.setMinimumWidth(150)
        self.category_filter.addItem("ALL CATEGORIES", None)
        for category in MemoryCategory:
            self.category_filter.addItem(category.value.upper(), category)
        self.category_filter.currentIndexChanged.connect(self.refresh)
        self.new_button = QPushButton("+  NEW")
        self.new_button.setObjectName("primary")
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.clicked.connect(self._new)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.category_filter)
        filters.addWidget(self.new_button)
        root.addLayout(filters)

        # Body: list (left) + editor card (right)
        body = QHBoxLayout()
        body.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._section("ENTRIES"))
        self.list_box = QListWidget()
        self.list_box.setObjectName("memoryList")
        self.list_box.setItemDelegate(_EntryDelegate(self.list_box))
        self.list_box.setMouseTracking(True)
        self.list_box.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list_box.currentRowChanged.connect(self._select)
        left.addWidget(self.list_box, 1)
        self.empty_label = QLabel("No memories match your filters.")
        self.empty_label.setObjectName("empty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        left.addWidget(self.empty_label)
        body.addLayout(left, 4)

        card = QFrame()
        card.setObjectName("card")
        form = QVBoxLayout(card)
        form.setContentsMargins(20, 18, 20, 18)
        form.setSpacing(8)

        self.mode_label = self._section("NEW ENTRY")
        form.addWidget(self.mode_label)
        form.addSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(12)
        cat_col, pri_col = QVBoxLayout(), QVBoxLayout()
        cat_col.setSpacing(4)
        pri_col.setSpacing(4)
        self.category = QComboBox()
        for value in MemoryCategory:
            self.category.addItem(value.value.upper(), value)
        self.priority = QComboBox()
        for value in Priority:
            self.priority.addItem(value.value.upper(), value)
        cat_col.addWidget(self._section("CATEGORY"))
        cat_col.addWidget(self.category)
        pri_col.addWidget(self._section("PRIORITY"))
        pri_col.addWidget(self.priority)
        row.addLayout(cat_col, 1)
        row.addLayout(pri_col, 1)
        form.addLayout(row)

        form.addWidget(self._section("TITLE"))
        self.title = QLineEdit()
        self.title.setPlaceholderText("Short, descriptive title")
        form.addWidget(self.title)

        desc_head = QHBoxLayout()
        desc_head.addWidget(self._section("DESCRIPTION"))
        desc_head.addStretch()
        self.char_count = QLabel("0 chars")
        self.char_count.setObjectName("status")
        desc_head.addWidget(self.char_count)
        form.addLayout(desc_head)
        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setMinimumHeight(150)
        self.description.setPlaceholderText(
            "For a protocol, enter ordered tasks separated by commas, "
            "for example: Open VS Code, Open Spotify, Open YouTube"
        )
        form.addWidget(self.description, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.delete_button = QPushButton("DELETE")
        self.delete_button.setObjectName("danger")
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self._delete)
        self.save_button = QPushButton("SAVE")
        self.save_button.setObjectName("primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setToolTip("Ctrl+S")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.delete_button)
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        form.addSpacing(4)
        form.addLayout(buttons)
        body.addWidget(card, 6)
        root.addLayout(body, 1)

        # Footer
        root.addWidget(self._rule())
        footer = QHBoxLayout()
        footer.setSpacing(12)
        export_button = QPushButton("EXPORT")
        export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        export_button.clicked.connect(self._export)
        self.status = QLabel()
        self.status.setObjectName("status")
        hint = QLabel("Stored locally on this device.")
        hint.setObjectName("status")
        footer.addWidget(export_button)
        footer.addWidget(self.status)
        footer.addStretch()
        footer.addWidget(hint)
        footer.addWidget(QSizeGrip(self), 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        root.addLayout(footer)

        # Dirty tracking
        self.title.textChanged.connect(self._mark_dirty)
        self.description.textChanged.connect(self._mark_dirty)
        self.description.textChanged.connect(self._update_char_count)
        self.category.currentIndexChanged.connect(self._mark_dirty)
        self.priority.currentIndexChanged.connect(self._mark_dirty)

        self._shortcuts: list[QShortcut] = []
        for keys, handler, target, context in (
            ("Ctrl+N", self._new, self, Qt.ShortcutContext.WindowShortcut),
            ("Ctrl+S", self._save, self, Qt.ShortcutContext.WindowShortcut),
            ("Ctrl+F", self._focus_search, self, Qt.ShortcutContext.WindowShortcut),
            # Only when the entry list has focus, so Delete still edits text in the fields.
            ("Delete", self._delete, self.list_box, Qt.ShortcutContext.WidgetShortcut),
        ):
            shortcut = QShortcut(QKeySequence(keys), target)
            shortcut.setContext(context)
            shortcut.activated.connect(lambda handler=handler: handler())
            self._shortcuts.append(shortcut)

    @staticmethod
    def _rule() -> QFrame:
        line = QFrame()
        line.setObjectName("rule")
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    # ── State helpers ───────────────────────────────────────────────────────
    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _set_status(self, text: str, color: str = MUTED, timeout_ms: int = 0) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
        self._status_timer.stop()
        if timeout_ms:
            self._status_timer.start(timeout_ms)

    def _mark_dirty(self, *_args) -> None:
        if self._loading:
            return
        self._dirty = True
        self._set_status("\u25CF UNSAVED CHANGES", ACCENT)

    def _update_char_count(self) -> None:
        n = len(self.description.toPlainText())
        self.char_count.setText(f"{n} char{'s' if n != 1 else ''}")

    def _sync_controls(self) -> None:
        editing = self._selected_id is not None
        self.mode_label.setText("EDIT ENTRY" if editing else "NEW ENTRY")
        self.delete_button.setEnabled(editing)
        self.save_button.setText("SAVE CHANGES" if editing else "CREATE")

    # ── Behaviour ───────────────────────────────────────────────────────────
    def refresh(self) -> None:
        """Refresh the list while preserving the selected entry."""
        selected = self._selected_id
        query = self.search.text().casefold().strip()
        category = self.category_filter.currentData()
        self.list_box.blockSignals(True)
        self.list_box.clear()
        for entry in self.service.store.list(category=category):
            haystack = " ".join([entry.title, entry.description, *entry.aliases]).casefold()
            if query and query not in haystack:
                continue
            item = QListWidgetItem(entry.title or "(untitled)")
            item.setData(_ROLE_ID, entry.id)
            item.setData(_ROLE_META, "  \u00B7  ".join([
                entry.category.value.upper(), entry.source.value.upper(),
                entry.priority.value.upper()]))
            self.list_box.addItem(item)
        self.list_box.blockSignals(False)
        total = len(self.service.store.list())
        self.count.setText(f"{total} {'ENTRY' if total == 1 else 'ENTRIES'}")
        self.empty_label.setVisible(self.list_box.count() == 0)

        index = self._row_for_id(selected) if selected else -1
        if index < 0 and self.list_box.count():
            index = 0
        self.list_box.blockSignals(True)
        self.list_box.setCurrentRow(index)
        self.list_box.blockSignals(False)
        self._select(index)

    def _row_for_id(self, entry_id: str | None) -> int:
        for row in range(self.list_box.count()):
            if self.list_box.item(row).data(_ROLE_ID) == entry_id:
                return row
        return -1

    def _new(self) -> None:
        self._selected_id = None
        self._loading = True
        self.title.clear()
        self.description.clear()
        self.category.setCurrentIndex(0)
        self.priority.setCurrentIndex(min(2, self.priority.count() - 1))
        self._loading = False
        self._dirty = False
        self.list_box.blockSignals(True)
        self.list_box.setCurrentRow(-1)
        self.list_box.clearSelection()
        self.list_box.blockSignals(False)
        self._set_status("")
        self._sync_controls()
        self.title.setFocus()

    def _select(self, index: int) -> None:
        item = self.list_box.item(index) if index >= 0 else None
        entry_id = item.data(_ROLE_ID) if item else None
        if not entry_id:
            self._sync_controls()
            return
        entry = self.service.store.get(entry_id)
        if entry is None:
            return
        self._loading = True
        self._selected_id = entry.id
        self.category.setCurrentIndex(self.category.findData(entry.category))
        self.title.setText(entry.title)
        self.description.setPlainText(entry.description)
        self.priority.setCurrentIndex(self.priority.findData(entry.priority))
        self._loading = False
        self._dirty = False
        self._update_char_count()
        self._set_status("")
        self._sync_controls()

    def _save(self) -> None:
        try:
            category = self.category.currentData()
            if self._selected_id:
                self.service.store.update(
                    self._selected_id, category=category.value, title=self.title.text(),
                    description=self.description.toPlainText(), priority=self.priority.currentData().value,
                )
            else:
                entry = self.service.store.add(MemoryEntry(
                    category=category, title=self.title.text(),
                    description=self.description.toPlainText(),
                    priority=self.priority.currentData(), source=MemorySource.MANUAL,
                ))
                self._selected_id = entry.id
            self.refresh()
            self._dirty = False
            self._set_status("\u2713 SAVED", OK, 2500)
        except (KeyError, ValueError, TypeError) as error:
            QMessageBox.warning(self, "Memory", str(error))

    def _delete(self) -> None:
        if not self._selected_id:
            return
        if QMessageBox.question(self, "Delete memory", "Delete this memory?") != QMessageBox.StandardButton.Yes:
            return
        self.service.store.delete(self._selected_id)
        self._new()
        self.refresh()
        self._set_status("ENTRY DELETED", MUTED, 2500)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export memory", "", "JSON (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(self.service.store.export_all(), indent=2), encoding="utf-8")
                self._set_status(f"\u2713 EXPORTED TO {Path(path).name}", OK, 3500)
            except OSError as error:
                QMessageBox.warning(self, "Export memory", str(error))

    # ── Frameless window: drag by title bar, close on Esc ───────────────────
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton and self._titlebar.geometry().contains(
                event.position().toPoint()):
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)