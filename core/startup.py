"""Windows startup elevation for the A.U.R.A. application entry point."""

from __future__ import annotations

import ctypes
import platform
import subprocess
import sys
from pathlib import Path


def _report_elevation_failure(message: str) -> None:
    if sys.stderr is not None:
        print(f"[Startup] {message}", file=sys.stderr)
        return
    try:
        message_box = ctypes.windll.user32.MessageBoxW
        message_box.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ]
        message_box.restype = ctypes.c_int
        message_box(
            None, message, "A.U.R.A. - Administrator permission required", 0x10,
        )
    except (AttributeError, OSError) as exc:
        raise RuntimeError(f"Could not display the administrator-permission prompt: {exc}") from exc


def ensure_windows_administrator() -> None:
    """Relaunch through UAC when needed; return only in an elevated process."""
    if platform.system() != "Windows":
        return

    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except (AttributeError, OSError) as exc:
        _report_elevation_failure(f"Could not check administrator status: {exc}")
        raise SystemExit(1) from exc

    arguments = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    parameters = subprocess.list2cmdline(arguments)
    executable = sys.executable
    working_directory = str(Path.cwd())
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None,
        "runas",
        executable,
        parameters or None,
        working_directory,
        1,
    )
    if result is None or result <= 32:
        error_code = ctypes.windll.kernel32.GetLastError()
        reason = (
            "Administrator approval was cancelled."
            if error_code == 1223
            else f"Windows could not start A.U.R.A. with administrator rights (code {result})."
        )
        _report_elevation_failure(reason)
        raise SystemExit(1)
    raise SystemExit(0)
