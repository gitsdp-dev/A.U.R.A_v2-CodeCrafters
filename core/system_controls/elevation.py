"""Launch one narrowly scoped Windows operation with on-demand UAC consent."""

from __future__ import annotations

import ctypes
import platform
from ctypes import wintypes


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_as_admin(executable: str, arguments: str, timeout_seconds: float = 25) -> None:
    """Run only the requested OS command elevated; never relaunch A.U.R.A."""
    if platform.system() != "Windows":
        raise OSError("On-demand administrator approval is only available on Windows.")
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = arguments
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.WinError()
        if getattr(error, "winerror", None) == 1223:
            raise PermissionError("Administrator approval was cancelled.") from error
        raise OSError(f"Windows could not request administrator approval: {error}") from error
    if not info.hProcess:
        raise OSError("Windows did not return the elevated operation process handle.")

    kernel32 = ctypes.windll.kernel32
    try:
        wait_result = kernel32.WaitForSingleObject(
            info.hProcess, int(max(1, timeout_seconds) * 1000)
        )
        if wait_result == _WAIT_TIMEOUT:
            raise TimeoutError("The administrator-approved operation timed out.")
        if wait_result != _WAIT_OBJECT_0:
            raise ctypes.WinError()
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError()
        if exit_code.value:
            raise OSError(
                f"The administrator-approved command failed with exit code {exit_code.value}."
            )
    finally:
        kernel32.CloseHandle(info.hProcess)


def is_access_denied(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in ("access is denied", "access denied", "permission denied", "elevation required")
    )
