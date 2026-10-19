"""Battery saver threshold control, reported unavailable on devices without a battery."""

from __future__ import annotations

import platform
import re
import subprocess
from typing import Any

import psutil

from .base import ControlState, SystemControl
from .elevation import is_access_denied, run_as_admin


def _battery_saver_threshold() -> int:
    result = subprocess.run(
        ["powercfg", "/q", "SCHEME_CURRENT", "SUB_ENERGYSAVER", "ESBATTTHRESHOLD"],
        capture_output=True, text=True, timeout=8, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise OSError((result.stderr or result.stdout).strip() or "Could not read the battery-saver threshold.")
    match = re.search(r"Current DC Power Setting Index:\s*0x([0-9a-f]+)", result.stdout, re.IGNORECASE)
    if not match:
        raise OSError("Windows did not report a battery-saver threshold.")
    return int(match.group(1), 16)


class EnergySaverControl(SystemControl):
    id = "energy_saver"
    label = "Energy saver"
    elevation_timeout_seconds = 28.0

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        if not self.is_supported():
            return ControlState(self.id, available=False, error="Energy saver is only supported on Windows.")
        try:
            if psutil.sensors_battery() is None:
                return ControlState(
                    self.id, available=False,
                    error="Energy saver is unavailable because this device has no battery.",
                )
            threshold = _battery_saver_threshold()
            return ControlState(self.id, threshold > 0, detail=f"Battery-saver threshold: {threshold}%")
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError("Energy saver state must be true or false.")
        if psutil.sensors_battery() is None:
            raise OSError("Energy saver is unavailable because this device has no battery.")
        threshold = "100" if value else "0"
        commands = (
            ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT",
             "SUB_ENERGYSAVER", "ESBATTTHRESHOLD", threshold],
            ["powercfg", "/setactive", "SCHEME_CURRENT"],
        )
        for index, command in enumerate(commands):
            result = subprocess.run(
                command,
                capture_output=True, text=True, timeout=8, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                error = (result.stderr or result.stdout).strip()
                if not is_access_denied(error):
                    raise OSError(
                        error or "Windows could not update the battery-saver power plan."
                    )
                elevated_command = (
                    "$ErrorActionPreference = 'Stop'; "
                    f"& powercfg {subprocess.list2cmdline(command[1:])}; "
                    "if ($LASTEXITCODE -ne 0) { throw 'Windows could not update the power plan.' }"
                )
                if index == 0:
                    elevated_command += (
                        "; & powercfg /setactive SCHEME_CURRENT; "
                        "if ($LASTEXITCODE -ne 0) { throw 'Windows could not activate the power plan.' }"
                    )
                run_as_admin(
                    "powershell.exe",
                    subprocess.list2cmdline([
                        "-NoProfile", "-NonInteractive", "-Command", elevated_command,
                    ]),
                )
                return
