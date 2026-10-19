"""Windows accessibility controls with read-back through native APIs/registry."""

from __future__ import annotations

import ctypes
import platform
import subprocess
import time
from ctypes import wintypes
from typing import Any

from .base import ControlState, SystemControl


def _windows():
    if platform.system() != "Windows":
        raise OSError("This accessibility control is only available on Windows.")
    return ctypes.windll


class AssistiveAppControl(SystemControl):
    def __init__(self, control_id: str, label: str, executable: str):
        self.id, self.label, self.executable = control_id, label, executable

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def _processes(self):
        import psutil

        return [
            process for process in psutil.process_iter(["name"])
            if (process.info.get("name") or "").casefold() == self.executable.casefold()
        ]

    def get_state(self) -> ControlState:
        try:
            return ControlState(self.id, bool(self._processes()))
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError(f"{self.label} state must be true or false.")
        processes = self._processes()
        if value and not processes:
            subprocess.Popen(
                [self.executable], close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self._processes():
                    return
                time.sleep(0.1)
            raise OSError(f"{self.label} did not start.")
        elif not value:
            for process in processes:
                process.terminate()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not self._processes():
                    return
                time.sleep(0.1)
            raise OSError(f"{self.label} did not stop.")


class SystemParameterToggle(SystemControl):
    def __init__(self, control_id: str, label: str, spi_get: int, spi_set: int,
                 flag: int, struct_type):
        self.id, self.label = control_id, label
        self._spi_get, self._spi_set, self._flag, self._struct = spi_get, spi_set, flag, struct_type

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        try:
            windll = _windows()
            settings = self._struct()
            settings.cbSize = ctypes.sizeof(settings)
            if not windll.user32.SystemParametersInfoW(
                self._spi_get, settings.cbSize, ctypes.byref(settings), 0
            ):
                raise ctypes.WinError()
            return ControlState(self.id, bool(settings.dwFlags & self._flag))
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError(f"{self.label} state must be true or false.")
        windll = _windows()
        settings = self._struct()
        settings.cbSize = ctypes.sizeof(settings)
        if not windll.user32.SystemParametersInfoW(
            self._spi_get, settings.cbSize, ctypes.byref(settings), 0
        ):
            raise ctypes.WinError()
        if value:
            settings.dwFlags |= self._flag
        else:
            settings.dwFlags &= ~self._flag
        if not windll.user32.SystemParametersInfoW(
            self._spi_set, settings.cbSize, ctypes.byref(settings), 0x03
        ):
            raise ctypes.WinError()


class RegistryToggle(SystemControl):
    def __init__(self, control_id: str, label: str, key_path: str, value_name: str):
        self.id, self.label = control_id, label
        self._key_path, self._value_name = key_path, value_name

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
                value, _kind = winreg.QueryValueEx(key, self._value_name)
            return ControlState(self.id, bool(value))
        except FileNotFoundError:
            return ControlState(self.id, False)
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError(f"{self.label} state must be true or false.")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
            winreg.SetValueEx(key, self._value_name, 0, winreg.REG_DWORD, int(value))
        _broadcast_setting(self._key_path)


def _broadcast_setting(section: str) -> None:
    windll = _windows()
    result = windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, ctypes.c_wchar_p(section), 0x0002, 1000, None
    )
    if not result:
        raise ctypes.WinError()


class TextSizeControl(SystemControl):
    id = "text_size"
    label = "Text size"
    _key_path = r"Software\Microsoft\Accessibility"

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
                value, _kind = winreg.QueryValueEx(key, "TextScaleFactor")
            return ControlState(self.id, int(value))
        except FileNotFoundError:
            return ControlState(self.id, 100)
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        amount = int(value)
        if not 100 <= amount <= 225:
            raise ValueError("Text size must be between 100 and 225 percent.")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
            winreg.SetValueEx(key, "TextScaleFactor", 0, winreg.REG_DWORD, amount)
        _broadcast_setting(self._key_path)


class HighContrastControl(SystemControl):
    id = "high_contrast"
    label = "High contrast"

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        try:
            class HighContrast(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("dwFlags", wintypes.DWORD),
                    ("lpszDefaultScheme", wintypes.LPWSTR),
                ]

            settings = HighContrast()
            settings.cbSize = ctypes.sizeof(settings)
            if not _windows().user32.SystemParametersInfoW(
                0x0042, settings.cbSize, ctypes.byref(settings), 0
            ):
                raise ctypes.WinError()
            return ControlState(self.id, bool(settings.dwFlags & 1))
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError("High contrast state must be true or false.")

        class HighContrast(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwFlags", wintypes.DWORD),
                ("lpszDefaultScheme", wintypes.LPWSTR),
            ]

        settings = HighContrast()
        settings.cbSize = ctypes.sizeof(settings)
        if not _windows().user32.SystemParametersInfoW(
            0x0042, settings.cbSize, ctypes.byref(settings), 0
        ):
            raise ctypes.WinError()
        settings.dwFlags = (settings.dwFlags | 1) if value else (settings.dwFlags & ~1)
        if not _windows().user32.SystemParametersInfoW(
            0x0043, settings.cbSize, ctypes.byref(settings), 0x03
        ):
            raise ctypes.WinError()


def accessibility_controls() -> list[SystemControl]:
    class _Sticky(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD)]

    class _Filter(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
            ("iWaitMSec", wintypes.DWORD), ("iDelayMSec", wintypes.DWORD),
            ("iRepeatMSec", wintypes.DWORD), ("iBounceMSec", wintypes.DWORD),
        ]

    class _Toggle(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD)]

    # SPI_GET/SET*KEYS = 0x003A/0x003B, 0x0032/0x0033, 0x0034/0x0035.
    return [
        AssistiveAppControl("narrator", "Narrator", "Narrator.exe"),
        AssistiveAppControl("magnifier", "Magnifier", "Magnify.exe"),
        AssistiveAppControl("onscreen_keyboard", "On-screen keyboard", "osk.exe"),
        SystemParameterToggle("sticky_keys", "Sticky keys", 0x003A, 0x003B, 0x00000001, _Sticky),
        SystemParameterToggle("filter_keys", "Filter keys", 0x0032, 0x0033, 0x00000001, _Filter),
        SystemParameterToggle("toggle_keys", "Toggle keys", 0x0034, 0x0035, 0x00000001, _Toggle),
        HighContrastControl(),
        RegistryToggle(
            "mono_audio", "Mono audio",
            r"Software\Microsoft\Multimedia\Audio", "AccessibilityMonoMixState",
        ),
        RegistryToggle(
            "color_filters", "Color filters",
            r"Software\Microsoft\ColorFiltering", "Active",
        ),
        TextSizeControl(),
    ]
