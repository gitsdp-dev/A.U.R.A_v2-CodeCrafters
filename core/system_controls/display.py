"""Windows display brightness via screen-brightness-control."""

from __future__ import annotations

import platform
import json
import subprocess
from typing import Any

from .base import ControlState, SystemControl
from .elevation import is_access_denied, run_as_admin


def _run_powershell(command: str, timeout: float = 10) -> str:
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference = 'Stop'; " + command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise OSError(message or "Windows could not communicate with the display.")
    return result.stdout.strip()


def _wmi_brightness_values() -> list[int]:
    output = _run_powershell(
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "| Select-Object -ExpandProperty CurrentBrightness | ConvertTo-Json -Compress"
    )
    if not output:
        raise OSError("No brightness-controllable display was found.")
    values = json.loads(output)
    values = values if isinstance(values, list) else [values]
    result = [int(value) for value in values]
    if not result or any(not 0 <= value <= 100 for value in result):
        raise OSError("Windows returned an invalid display brightness value.")
    return result


def _set_wmi_brightness(amount: int) -> None:
    command = (
        "$monitors = @(Get-CimInstance -Namespace root/WMI "
        "-ClassName WmiMonitorBrightnessMethods); "
        "if ($monitors.Count -eq 0) { throw 'No brightness-controllable display was found.' }; "
        f"$results = @($monitors | ForEach-Object {{ Invoke-CimMethod -InputObject $_ "
        f"-MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={amount}}} }}); "
        "if ($results | Where-Object { $_.ReturnValue -ne 0 }) "
        "{ throw 'Windows rejected the brightness change.' }"
    )
    _run_powershell(command)


def _read_brightness() -> list[int]:
    try:
        import screen_brightness_control as sbc
    except ModuleNotFoundError:
        return _wmi_brightness_values()
    try:
        values = sbc.get_brightness()
        if values:
            return [int(value) for value in values]
    except Exception as controller_error:
        try:
            return _wmi_brightness_values()
        except Exception as wmi_error:
            raise OSError(
                f"Display controller read failed: {controller_error}; "
                f"Windows WMI read failed: {wmi_error}"
            ) from wmi_error
    return _wmi_brightness_values()


def _write_brightness(amount: int) -> None:
    try:
        import screen_brightness_control as sbc
    except ModuleNotFoundError:
        _set_wmi_brightness(amount)
        return
    try:
        displays = sbc.list_monitors()
        if not displays:
            raise OSError("No brightness-controllable display was found.")
        sbc.set_brightness(amount)
    except Exception as exc:
        try:
            _set_wmi_brightness(amount)
        except Exception as wmi_error:
            raise OSError(
                f"Display controller write failed: {exc}; "
                f"Windows WMI write failed: {wmi_error}"
            ) from wmi_error


class BrightnessControl(SystemControl):
    id = "brightness"
    label = "Brightness"
    elevation_timeout_seconds = 28.0

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        if not self.is_supported():
            return ControlState(self.id, available=False, error="Brightness is not supported on this platform.")
        try:
            values = _read_brightness()
            if not values:
                return ControlState(
                    self.id, available=False,
                    error="No brightness-controllable display was found.",
                )
            return ControlState(self.id, int(values[0]))
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        amount = int(value)
        if not 0 <= amount <= 100:
            raise ValueError("Brightness must be between 0 and 100.")
        try:
            _write_brightness(amount)
        except Exception as exc:
            if not is_access_denied(exc):
                raise
            args = subprocess.list2cmdline([
                "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference = 'Stop'; "
                + "$monitors = @(Get-CimInstance -Namespace root/WMI "
                "-ClassName WmiMonitorBrightnessMethods); "
                "if ($monitors.Count -eq 0) { throw 'No brightness-controllable display was found.' }; "
                f"$results = @($monitors | ForEach-Object {{ Invoke-CimMethod -InputObject $_ "
                f"-MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={amount}}} }}); "
                "if ($results | Where-Object { $_.ReturnValue -ne 0 }) "
                "{ throw 'Windows rejected the brightness change.' }",
            ])
            run_as_admin("powershell.exe", args)
