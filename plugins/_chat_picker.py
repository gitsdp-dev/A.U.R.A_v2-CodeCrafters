"""Floating chooser for Telegram chats.

WHY THIS EXISTS
    AURA's chat-brain plugin needs a floating, draggable selector so the user can
    choose a chat before sending a message or previewing a draft.

HOW IT WORKS
    The widget is built with the same PyQt6 toolkit the desktop app already uses.
    It renders a lightweight list of chats, filters on the search box, and returns
    the selection to the waiting plugin call.

SECURITY
    This widget only displays local metadata and never triggers any network calls.

WHAT IT DOES NOT DO
    It does not send messages itself and does not accept untrusted input as a tool.
"""
from __future__ import annotations

from typing import Any


def prompt_chat_choice(chats: list[dict[str, Any]] | None = None, *, title: str = "Choose a chat") -> dict[str, Any] | None:
    """Open a floating, searchable picker and return the chosen chat or None."""
    from PyQt6.QtCore import Qt, QPoint
    from PyQt6.QtGui import QKeySequence
    from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

    chats = chats or []
    app = QApplication.instance() or QApplication([])

    widget = QWidget()
    widget.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
    widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    widget.setMinimumSize(360, 420)
    widget.resize(440, 560)
    widget.setStyleSheet(
        """
        QWidget { background: #1f2430; color: #edf3ff; font-family: 'Segoe UI'; }
        QFrame#titleBar { background: #262d3d; border: 1px solid #2f3a52; }
        QLabel { color: #edf3ff; }
        QLineEdit { background: #2b3348; border: 1px solid #3f4c6d; border-radius: 10px; color: white; padding: 7px 10px; }
        QListWidget { background: #1b2130; border: 1px solid #2f3a52; border-radius: 10px; }
        QListWidget::item { height: 60px; border-radius: 10px; }
        QListWidget::item:selected { background: #3a4b70; }
        """
    )

    root = QVBoxLayout(widget)
    root.setContentsMargins(10, 10, 10, 10)
    root.setSpacing(8)

    title_bar = QFrame()
    title_bar.setObjectName("titleBar")
    title_bar.setFixedHeight(36)
    title_layout = QHBoxLayout(title_bar)
    title_layout.setContentsMargins(12, 6, 10, 6)
    title_layout.setSpacing(8)
    title_label = QLabel(title)
    title_label.setStyleSheet("font-weight: 600;")
    close_btn = QLabel("✕")
    close_btn.setAlignment(Qt.AlignmentFlag.AlignCenter)
    close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    close_btn.setStyleSheet("font-size: 16px; background: transparent; color: #ffb4b4; border-radius: 10px; min-width: 26px;")
    close_btn.mousePressEvent = lambda event: widget.close()
    title_layout.addWidget(title_label)
    title_layout.addStretch(1)
    title_layout.addWidget(close_btn)
    root.addWidget(title_bar)

    search = QLineEdit()
    search.setPlaceholderText("Search chats")
    root.addWidget(search)

    list_widget = QListWidget()
    list_widget.setAlternatingRowColors(False)
    list_widget.setUniformItemSizes(True)
    list_widget.setSelectionMode(list_widget.SelectionMode.SingleSelection)
    root.addWidget(list_widget)

    def render_rows(filter_text: str = "") -> None:
        list_widget.clear()
        query = filter_text.lower().strip()
        shown = 0
        for chat in chats:
            label = str(chat.get("title") or chat.get("username") or chat.get("id") or "Unknown")
            if query and query not in label.lower() and query not in str(chat.get("username") or "").lower():
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, chat)
            item.setToolTip(label)
            list_widget.addItem(item)
            shown += 1
        if not shown:
            empty = QListWidgetItem("No matching chats")
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            list_widget.addItem(empty)

    render_rows()
    search.textChanged.connect(render_rows)
    list_widget.itemDoubleClicked.connect(lambda _: widget.close())

    def _accept() -> None:
        current = list_widget.currentItem()
        if current is not None and current.data(Qt.ItemDataRole.UserRole):
            widget.setProperty("selected", current.data(Qt.ItemDataRole.UserRole))
            widget.close()

    list_widget.itemActivated.connect(lambda _: _accept())
    widget.show()
    widget.raise_()
    widget.activateWindow()
    shortcut = QKeySequence(Qt.Key.Key_Escape)
    esc = widget.grabShortcut(shortcut)
    widget.setShortcutEnabled(True)
    # Create a direct close path for Esc without depending on a dedicated shortcut object.
    def _cancel() -> None:
        widget.setProperty("selected", None)
        widget.close()
    from PyQt6.QtGui import QShortcut
    QShortcut(QKeySequence(Qt.Key.Key_Escape), widget, activated=_cancel)
    if list_widget.count():
        list_widget.setCurrentRow(0)

    if app is not None:
        app.processEvents()
        if not app.topLevelWidgets():
            app.exec()
    result = widget.property("selected")
    return result if result is not None else None


__all__ = ["prompt_chat_choice"]
