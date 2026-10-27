"""Small IDE adapters with a common open/goto/focus interface."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class IDEAdapter:
    """Common interface for optional editor integrations."""

    name = "generic"

    def open_file(self, path: Path, line: int | None = None) -> bool:
        """Open a file in the detected editor."""
        return False

    def goto_line(self, path: Path, line: int) -> bool:
        """Open a file at a line when the editor supports it."""
        return self.open_file(path, line)

    def get_active_file_hint(self) -> str:
        """Return a best-effort active file hint."""
        return ""

    def focus_window(self) -> bool:
        """Best-effort focus operation."""
        return False


class VSCodeAdapter(IDEAdapter):
    name = "vscode"

    def open_file(self, path: Path, line: int | None = None) -> bool:
        code = shutil.which("code") or shutil.which("code.cmd")
        if not code:
            return False
        target = f"{path}:{line}" if line else str(path)
        subprocess.Popen([code, "--goto", target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True


class JetBrainsAdapter(IDEAdapter):
    name = "jetbrains"

    def open_file(self, path: Path, line: int | None = None) -> bool:
        launcher = shutil.which("idea") or shutil.which("pycharm") or shutil.which("charm")
        if not launcher:
            return False
        target = f"{path}:{line}" if line else str(path)
        subprocess.Popen([launcher, target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True


def detect_adapter() -> IDEAdapter:
    """Select an installed CLI adapter, falling back to generic behavior."""
    if shutil.which("code") or shutil.which("code.cmd"):
        return VSCodeAdapter()
    if any(shutil.which(name) for name in ("idea", "pycharm", "charm")):
        return JetBrainsAdapter()
    return IDEAdapter()


def active_window_title() -> str:
    """Return the foreground window title when pygetwindow is installed."""
    try:
        import pygetwindow
        window = pygetwindow.getActiveWindow()
        return str(window.title) if window else ""
    except Exception:
        return ""