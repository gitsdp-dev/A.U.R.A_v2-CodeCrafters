"""Qt bridge for safely notifying the GUI about store changes."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal


class MemoryQtBridge(QObject):
    """Emit memory changes as a Qt signal from any calling thread."""

    memory_changed = pyqtSignal()

    def notify(self) -> None:
        """Queue a change notification for Qt's event loop."""
        # Qt automatically queues an AutoConnection when the receiver belongs
        # to another thread; emitting here avoids touching receiver widgets.
        self.memory_changed.emit()
