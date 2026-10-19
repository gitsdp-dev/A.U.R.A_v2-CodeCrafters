"""Windows Live Captions process control."""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from typing import Any

import psutil

from .base import ControlState, SystemControl


class LiveCaptionsControl(SystemControl):
    id = "live_captions"
    label = "Live captions"
    executable = "LiveCaptions.exe"

    def is_supported(self) -> bool:
        try:
            return platform.system() == "Windows" and sys.getwindowsversion().build >= 22621
        except (AttributeError, OSError):
            return False

    def _processes(self):
        return [
            item for item in psutil.process_iter(["name"])
            if (item.info.get("name") or "").casefold() == self.executable.casefold()
        ]

    def get_state(self) -> ControlState:
        if not self.is_supported():
            return ControlState(self.id, available=False, error="Live captions are only available on Windows 11.")
        try:
            return ControlState(self.id, bool(self._processes()))
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError("Live captions state must be true or false.")
        processes = self._processes()
        if value and not processes:
            subprocess.Popen(
                [self.executable], close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if self._processes():
                    return
                time.sleep(0.1)
            raise OSError("Windows Live Captions did not start.")
        elif not value:
            for process in processes:
                process.terminate()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not self._processes():
                    return
                time.sleep(0.1)
            raise OSError("Windows Live Captions did not stop.")
