"""
ui_learning.py — A.U.R.A Adaptive Intelligence Review Dialog (PyQt6).

HUD-styled review interface for managing learned experiences, preferences, and skills:
- Toggle learning subsystem on/off.
- View real-time learning metrics (verified count, hit rate, budget).
- View, search, edit, promote, reject, or delete individual lessons.
- Clear-all learning history with confirmation.
- Conforms to MARK XL HUD amber/gold styling.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from PyQt6.QtCore import QRect, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizeGrip,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.learning import LearningEngine, LearningSettings, get_learning_engine, load_settings, save_settings

LOGGER = logging.getLogger("aura.ui.learning")

# Theme Palette (Matches the NEURAL MAP / MARK XL HUD in ui.py)
COLOR_BG = "#06080b"
COLOR_PANEL = "#0b0e13"
COLOR_BORDER = "#4a3812"
COLOR_PRI = "#ffa826"
COLOR_PRI_DIM = "#9a6a18"
COLOR_TEXT = "#f2dcb0"
COLOR_TEXT_DIM = "#9d8660"
COLOR_GREEN = "#7fe0a0"
COLOR_RED = "#ff5c4d"
COLOR_LINE_STRONG = "#a9761c"

_MONO = '"Space Mono", "JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New", monospace'

_STYLE_TEMPLATE = """
QWidget {
    color: @TEXT@;
    font-family: @MONO@;
    font-size: 11px;
}
QLabel { background: transparent; }
QLabel#title { color: @PRI@; font-size: 12px; font-weight: 700; }
QLabel#sectionLabel {
    color: @PRI@; font-size: 11px; font-weight: 700;
    border-left: 3px solid @PRI@; padding-left: 8px;
}
QLabel#stat {
    color: @TEXT@; font-size: 10px; font-weight: 700;
    border-left: 2px solid @PRI@; padding-left: 8px;
}
QFrame#rule {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(255, 168, 38, 70%), stop:1 rgba(255, 168, 38, 0%));
    max-height: 1px; min-height: 1px; border: none;
}
QFrame#statsFrame {
    background: rgba(255, 168, 38, 5%);
    border: 1px solid rgba(255, 168, 38, 45%);
    border-left: 3px solid @PRI@;
    border-radius: 3px;
}
QPushButton {
    background: rgba(255, 168, 38, 6%);
    color: @PRI@;
    border: 1px solid @LINE_STRONG@; border-radius: 3px;
    padding: 7px 12px; font-weight: 700;
}
QPushButton:hover {
    background: rgba(255, 168, 38, 16%);
    border-color: @PRI@; color: #ffd08a;
}
QPushButton:pressed { background: @PRI@; color: #120c02; }
QPushButton:disabled { color: #6b5a3a; border-color: @BORDER@; background: transparent; }
QPushButton#primaryBtn { background: @PRI@; color: #120c02; border: 1px solid @PRI@; }
QPushButton#primaryBtn:hover { background: #ffb84a; border-color: #ffb84a; color: #120c02; }
QPushButton#primaryBtn:pressed { background: #e08f12; }
QPushButton#dangerBtn { border-color: @RED@; color: @RED@; background: transparent; }
QPushButton#dangerBtn:hover { background: rgba(255, 92, 77, 16%); color: #ff8a7d; border-color: #ff8a7d; }
QPushButton#closeBtn {
    background: transparent; color: @DIM@;
    border: 1px solid @BORDER@; padding: 0px; font-size: 12px;
}
QPushButton#closeBtn:hover { color: @RED@; border-color: @RED@; background: rgba(255, 92, 77, 12%); }
QCheckBox { spacing: 8px; font-weight: 700; color: @PRI@; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid @LINE_STRONG@; background: @PANEL@; border-radius: 2px;
}
QCheckBox::indicator:hover { border-color: @PRI@; }
QCheckBox::indicator:checked { background: @PRI@; border-color: @PRI@; }
QTableWidget {
    background: #090b0f; color: @TEXT@;
    border: 1px solid @LINE_STRONG@; border-radius: 3px;
    gridline-color: transparent;
    alternate-background-color: rgba(255, 168, 38, 3%);
    selection-background-color: rgba(255, 168, 38, 20%);
    selection-color: @PRI@;
    outline: none;
}
QTableWidget::item { padding: 4px 8px; border: none; }
QTableWidget::item:selected { background: rgba(255, 168, 38, 20%); color: @PRI@; }
QTableWidget QLineEdit {
    background: #0d1015; color: @TEXT@; border: 1px solid @PRI@; padding: 2px 6px;
}
QHeaderView { background: transparent; }
QHeaderView::section {
    background: #080a0e; color: @PRI@;
    border: none; border-bottom: 1px solid @LINE_STRONG@;
    padding: 7px 8px; font-weight: 700;
}
QTableCornerButton::section { background: #080a0e; border: none; }
QScrollBar:vertical {
    background: rgba(255, 168, 38, 6%); width: 8px; margin: 0px; border: none;
}
QScrollBar::handle:vertical {
    background: rgba(255, 168, 38, 55%); border-radius: 3px; min-height: 28px; margin: 1px;
}
QScrollBar::handle:vertical:hover { background: @PRI@; }
QScrollBar:horizontal {
    background: rgba(255, 168, 38, 6%); height: 8px; margin: 0px; border: none;
}
QScrollBar::handle:horizontal {
    background: rgba(255, 168, 38, 55%); border-radius: 3px; min-width: 28px; margin: 1px;
}
QScrollBar::handle:horizontal:hover { background: @PRI@; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QToolTip { background: @PANEL@; color: @TEXT@; border: 1px solid @LINE_STRONG@; padding: 4px 6px; }
QMessageBox, QInputDialog { background: @BG@; }
QMessageBox QLabel, QInputDialog QLabel { color: @TEXT@; font-size: 12px; }
QInputDialog QLineEdit {
    background: #090b0f; color: @TEXT@; border: 1px solid @LINE_STRONG@;
    border-radius: 3px; padding: 7px 10px;
    selection-background-color: @PRI@; selection-color: #120c02;
}
QInputDialog QLineEdit:focus { border-color: @PRI@; }
"""


def _render_style() -> str:
    tokens = {
        "BG": COLOR_BG, "PANEL": COLOR_PANEL, "BORDER": COLOR_BORDER, "PRI": COLOR_PRI,
        "TEXT": COLOR_TEXT, "DIM": COLOR_TEXT_DIM, "RED": COLOR_RED,
        "LINE_STRONG": COLOR_LINE_STRONG, "MONO": _MONO,
    }
    css = _STYLE_TEMPLATE
    for key, val in tokens.items():
        css = css.replace(f"@{key}@", val)
    return css


def _spaced(widget: QWidget, px: float) -> None:
    f = widget.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px)
    widget.setFont(f)


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
        pen = QPen(QColor(COLOR_PRI), 2)
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
        LOGGER.debug("HUD paint skipped", exc_info=True)


class LearningReviewDialog(QDialog):
    """PyQt6 HUD-styled dialog for inspecting and curating learned knowledge."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("learningDialog")
        self.setWindowTitle("A.U.R.A. ADAPTIVE INTELLIGENCE // KNOWLEDGE MATRIX")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(760, 480)
        self.resize(920, 620)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._drag_pos = None

        self._engine = get_learning_engine()
        self._settings = load_settings()

        self._setup_style()
        self._build_ui()
        self._load_data()

    def _setup_style(self) -> None:
        self.setStyleSheet(_render_style())

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

    @staticmethod
    def _section(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        _spaced(lbl, 2.0)
        return lbl

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 16)
        layout.setSpacing(12)

        # 1. Header Bar (draggable)
        hdr = QWidget()
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        title_lbl = QLabel("●  ADAPTIVE INTELLIGENCE SUBSYSTEM // LIVE STATE")
        title_lbl.setObjectName("title")
        _spaced(title_lbl, 3.0)
        hdr_layout.addWidget(title_lbl)

        hdr_layout.addStretch()

        self.toggle_cb = QCheckBox("LEARNING ACTIVE")
        self.toggle_cb.setChecked(self._settings.enabled)
        self.toggle_cb.toggled.connect(self._on_toggle_enabled)
        _spaced(self.toggle_cb, 1.0)
        hdr_layout.addWidget(self.toggle_cb)

        x_btn = QPushButton("✕")
        x_btn.setObjectName("closeBtn")
        x_btn.setFixedSize(26, 26)
        x_btn.clicked.connect(self.close)
        hdr_layout.addWidget(x_btn)

        hdr.mousePressEvent = self._hdr_press
        hdr.mouseMoveEvent = self._hdr_move
        layout.addWidget(hdr)

        rule = QFrame()
        rule.setObjectName("rule")
        layout.addWidget(rule)

        # 2. Stats Summary Frame
        layout.addWidget(self._section("METRICS"))
        self.stats_frame = QFrame()
        self.stats_frame.setObjectName("statsFrame")
        sf_lay = QHBoxLayout(self.stats_frame)
        sf_lay.setContentsMargins(14, 10, 14, 10)
        sf_lay.setSpacing(18)

        self.lbl_verified = QLabel("VERIFIED: 0")
        self.lbl_provisional = QLabel("PROVISIONAL: 0")
        self.lbl_preferences = QLabel("PREFERENCES: 0")
        self.lbl_hit_rate = QLabel("HIT RATE: 0.0%")
        self.lbl_budget = QLabel("REFLECTIONS TODAY: 0")

        for lbl in (self.lbl_verified, self.lbl_provisional, self.lbl_preferences, self.lbl_hit_rate, self.lbl_budget):
            lbl.setObjectName("stat")
            _spaced(lbl, 0.8)
            sf_lay.addWidget(lbl)
        sf_lay.addStretch()
        layout.addWidget(self.stats_frame)

        # 3. Main Knowledge Table
        layout.addWidget(self._section("KNOWLEDGE MATRIX"))
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ID", "TYPE", "SCOPE", "CONF", "STATUS", "LESSON / RULE TEXT"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setHighlightSections(False)
        _spaced(self.table.horizontalHeader(), 1.5)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        # Cosmetic: no grid / row numbers, subtle zebra rows. Cells are read-only so a stray
        # double-click can't turn the ID cell into free text (int(...) in _selected_row_info).
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setCornerButtonEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        # 4. Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        self.btn_promote = QPushButton("PROMOTE TO VERIFIED")
        self.btn_promote.setObjectName("primaryBtn")
        self.btn_promote.clicked.connect(self._on_promote_selected)
        btn_layout.addWidget(self.btn_promote)

        self.btn_edit = QPushButton("EDIT LESSON")
        self.btn_edit.clicked.connect(self._on_edit_selected)
        btn_layout.addWidget(self.btn_edit)

        self.btn_delete = QPushButton("DELETE ITEM")
        self.btn_delete.clicked.connect(self._on_delete_selected)
        btn_layout.addWidget(self.btn_delete)

        btn_layout.addStretch()

        self.btn_refresh = QPushButton("REFRESH")
        self.btn_refresh.clicked.connect(self._load_data)
        btn_layout.addWidget(self.btn_refresh)

        self.btn_maintenance = QPushButton("OPTIMIZE NOW")
        self.btn_maintenance.clicked.connect(self._on_run_maintenance)
        btn_layout.addWidget(self.btn_maintenance)

        self.btn_clear = QPushButton("CLEAR ALL")
        self.btn_clear.setObjectName("dangerBtn")
        self.btn_clear.clicked.connect(self._on_clear_all)
        btn_layout.addWidget(self.btn_clear)

        self.btn_close = QPushButton("CLOSE")
        self.btn_close.clicked.connect(self.close)
        btn_layout.addWidget(self.btn_close)

        layout.addLayout(btn_layout)

        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(self))
        layout.addLayout(grip_row)

        for btn in self.findChildren(QPushButton):
            if btn.objectName() != "closeBtn":
                _spaced(btn, 0.8)

    def _on_toggle_enabled(self, checked: bool) -> None:
        self._settings.enabled = checked
        save_settings(self._settings)
        if isinstance(self._engine, LearningEngine):
            self._engine.settings.enabled = checked
        self._load_data()

    def _load_data(self) -> None:
        """Fetch records and stats from learning database."""
        stats = self._engine.get_stats()
        self.lbl_verified.setText(f"VERIFIED: {stats.get('experiences_verified', 0)}")
        self.lbl_provisional.setText(f"PROVISIONAL: {stats.get('experiences_provisional', 0)}")
        self.lbl_preferences.setText(f"PREFERENCES: {stats.get('preferences_verified', 0)}")
        hit_pct = float(stats.get("retrieval_hit_rate", 0.0)) * 100.0
        self.lbl_hit_rate.setText(f"HIT RATE: {hit_pct:.1f}%")
        self.lbl_budget.setText(f"REFLECTIONS TODAY: {stats.get('reflections_today', 0)}")

        if not isinstance(self._engine, LearningEngine):
            self.table.setRowCount(0)
            return

        def _fetch_all(conn: sqlite3.Connection):
            c = conn.cursor()
            # Fetch experiences
            c.execute("""
                SELECT id, 'EXP' as item_type, scope, confidence, status, lesson
                FROM experiences
                ORDER BY updated_at DESC LIMIT 200
            """)
            exp_rows = [dict(r) for r in c.fetchall()]

            # Fetch preferences
            c.execute("""
                SELECT id, 'PREF' as item_type, scope, confidence, status, (pref_key || ' = ' || pref_value) as lesson
                FROM learned_preferences
                ORDER BY updated_at DESC LIMIT 100
            """)
            pref_rows = [dict(r) for r in c.fetchall()]
            return exp_rows + pref_rows

        items = self._engine.db.execute_read(_fetch_all) or []
        self.table.setRowCount(len(items))

        for row_idx, item in enumerate(items):
            id_item = QTableWidgetItem(str(item["id"]))
            type_item = QTableWidgetItem(item["item_type"])
            scope_item = QTableWidgetItem(item["scope"])
            conf_item = QTableWidgetItem(f"{float(item['confidence']):.2f}")
            status_item = QTableWidgetItem(item["status"].upper())
            lesson_item = QTableWidgetItem(item["lesson"])

            # Color status
            if item["status"] == "verified":
                status_item.setForeground(QColor(COLOR_GREEN))
            elif item["status"] == "provisional":
                status_item.setForeground(QColor(COLOR_PRI))
            else:
                status_item.setForeground(QColor(COLOR_RED))

            self.table.setItem(row_idx, 0, id_item)
            self.table.setItem(row_idx, 1, type_item)
            self.table.setItem(row_idx, 2, scope_item)
            self.table.setItem(row_idx, 3, conf_item)
            self.table.setItem(row_idx, 4, status_item)
            self.table.setItem(row_idx, 5, lesson_item)

    def _selected_row_info(self) -> tuple[int, str] | None:
        selected = self.table.selectedItems()
        if not selected:
            return None
        row = selected[0].row()
        item_id = int(self.table.item(row, 0).text())
        item_type = self.table.item(row, 1).text()
        return item_id, item_type

    def _on_promote_selected(self) -> None:
        info = self._selected_row_info()
        if not info or not isinstance(self._engine, LearningEngine):
            return
        item_id, item_type = info
        table_name = "experiences" if item_type == "EXP" else "learned_preferences"
        now = time.time()

        def _promote(conn: sqlite3.Connection):
            conn.execute(f"UPDATE {table_name} SET status = 'verified', confidence = 0.90, updated_at = ? WHERE id = ?", (now, item_id))
            conn.execute("""
                INSERT INTO learning_audit (table_name, record_id, action, old_status, new_status, reason, timestamp)
                VALUES (?, ?, 'promote', 'provisional', 'verified', 'User manual promotion via HUD', ?)
            """, (table_name, item_id, now))

        self._engine.db.execute_write(_promote)
        self._load_data()

    def _on_edit_selected(self) -> None:
        info = self._selected_row_info()
        if not info or not isinstance(self._engine, LearningEngine):
            return
        item_id, item_type = info
        current_text = self.table.item(self.table.currentRow(), 5).text()

        new_text, ok = QInputDialog.getText(
            self, "EDIT KNOWLEDGE ITEM", "Update Lesson / Rule Text:", text=current_text
        )
        if ok and new_text.strip():
            now = time.time()
            clean_text = new_text.strip()
            table_name = "experiences" if item_type == "EXP" else "learned_preferences"

            def _edit(conn: sqlite3.Connection):
                if table_name == "experiences":
                    conn.execute("UPDATE experiences SET lesson = ?, updated_at = ? WHERE id = ?", (clean_text, now, item_id))
                else:
                    parts = clean_text.split("=", 1)
                    val = parts[1].strip() if len(parts) > 1 else clean_text
                    conn.execute("UPDATE learned_preferences SET pref_value = ?, updated_at = ? WHERE id = ?", (val, now, item_id))

            self._engine.db.execute_write(_edit)
            self._load_data()

    def _on_delete_selected(self) -> None:
        info = self._selected_row_info()
        if not info or not isinstance(self._engine, LearningEngine):
            return
        item_id, item_type = info
        table_name = "experiences" if item_type == "EXP" else "learned_preferences"

        def _del(conn: sqlite3.Connection):
            conn.execute(f"DELETE FROM {table_name} WHERE id = ?", (item_id,))
            conn.execute("""
                INSERT INTO learning_audit (table_name, record_id, action, reason, timestamp)
                VALUES (?, ?, 'delete', 'User manual deletion via HUD', ?)
            """, (table_name, item_id, time.time()))

        self._engine.db.execute_write(_del)
        self._load_data()

    def _on_run_maintenance(self) -> None:
        if isinstance(self._engine, LearningEngine):
            res = self._engine.run_maintenance()
            QMessageBox.information(self, "MAINTENANCE COMPLETE", f"Consolidation Results:\n{res}")
            self._load_data()

    def _on_clear_all(self) -> None:
        reply = QMessageBox.question(
            self,
            "CONFIRM PURGE",
            "Are you sure you want to completely clear all learned experiences, preferences, and skills?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and isinstance(self._engine, LearningEngine):
            def _purge(conn: sqlite3.Connection):
                conn.execute("DELETE FROM experiences")
                conn.execute("DELETE FROM learned_preferences")
                conn.execute("DELETE FROM learned_skills")
                conn.execute("DELETE FROM feedback")
                conn.execute("DELETE FROM strategy_stats")
                conn.execute("DELETE FROM budget_counters")
            self._engine.db.execute_write(_purge)
            self._load_data()