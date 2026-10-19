"""Windows Night light control through the per-user native CloudStore settings."""

from __future__ import annotations

import platform
from typing import Any

from .base import ControlState, SystemControl


_STATE_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount"
    r"\Current\default$windows.data.bluelightreduction.bluelightreductionstate"
    r"\windows.data.bluelightreduction.bluelightreductionstate"
)
_SETTINGS_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount"
    r"\Current\default$windows.data.bluelightreduction.settings"
    r"\windows.data.bluelightreduction.settings"
)
_NIGHT_LIGHT_ON = 0x15
_NIGHT_LIGHT_OFF = 0x13
_MIN_TEMPERATURE = 1200
_MAX_TEMPERATURE = 6500


def _read_cloudstore_value(key_path: str) -> bytes:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
        value, value_type = winreg.QueryValueEx(key, "Data")
    if value_type != winreg.REG_BINARY or not isinstance(value, bytes):
        raise OSError("Windows Night light returned an unsupported settings format.")
    return value


def _write_cloudstore_value(key_path: str, value: bytes) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    ) as key:
        winreg.SetValueEx(key, "Data", 0, winreg.REG_BINARY, value)
        winreg.FlushKey(key)


def _advance_revision(data: bytearray) -> None:
    for index in range(10, min(15, len(data))):
        if data[index] != 0xFF:
            data[index] += 1
            return
    raise OSError("Windows Night light revision data is invalid.")


def _decode_strength(settings: bytes) -> int:
    if len(settings) < 0x25:
        raise OSError("Windows Night light strength data is incomplete.")
    low, high = settings[0x23], settings[0x24]
    temperature = high * 64 + (low - 128) / 2
    if not _MIN_TEMPERATURE <= temperature <= _MAX_TEMPERATURE:
        raise OSError("Windows Night light strength data is invalid.")
    return round(
        (_MAX_TEMPERATURE - temperature)
        / (_MAX_TEMPERATURE - _MIN_TEMPERATURE)
        * 100
    )


def _encode_strength(settings: bytes, strength: int) -> bytes:
    if len(settings) < 0x25:
        raise OSError("Windows Night light strength data is incomplete.")
    temperature = round(
        _MAX_TEMPERATURE
        - strength / 100 * (_MAX_TEMPERATURE - _MIN_TEMPERATURE)
    )
    high = temperature // 64
    low = round((temperature - high * 64) * 2 + 128)
    if not 0 <= low <= 255 or not 0 <= high <= 255:
        raise OSError("Windows Night light strength is outside the supported range.")
    updated = bytearray(settings)
    updated[0x23] = low
    updated[0x24] = high
    _advance_revision(updated)
    return bytes(updated)


def _encode_enabled_state(data: bytes, enabled: bool) -> bytes:
    if len(data) < 19:
        raise OSError("Windows Night light state data is incomplete.")
    current = data[18]
    expected_length = 43 if current == _NIGHT_LIGHT_ON else 41
    if len(data) != expected_length:
        raise OSError("Windows Night light state data is incomplete.")
    expected = _NIGHT_LIGHT_ON if enabled else _NIGHT_LIGHT_OFF
    if current == expected:
        return data
    if current not in {_NIGHT_LIGHT_ON, _NIGHT_LIGHT_OFF}:
        raise OSError("Windows Night light state uses an unsupported Windows format.")

    if enabled:
        updated = bytearray(43)
        updated[:22] = data[:22]
        updated[23:25] = b"\x10\x00"
        updated[25:43] = data[23:41]
        updated[18] = _NIGHT_LIGHT_ON
    else:
        updated = bytearray(41)
        updated[:22] = data[:22]
        updated[23:41] = data[25:43]
        updated[18] = _NIGHT_LIGHT_OFF
    _advance_revision(updated)
    return bytes(updated)


class NightLightControl(SystemControl):
    """Read and set Windows' native Night light state and intensity."""

    id = "night_light"
    label = "Night light"

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        if not self.is_supported():
            return ControlState(
                self.id, available=False,
                error="Native Night light control is only available on Windows.",
            )
        try:
            state = _read_cloudstore_value(_STATE_KEY)
            if len(state) < 19:
                raise OSError("Windows Night light state data is incomplete.")
            marker = state[18]
            if marker not in {_NIGHT_LIGHT_ON, _NIGHT_LIGHT_OFF}:
                raise OSError("Windows Night light state uses an unsupported Windows format.")
            settings = _read_cloudstore_value(_SETTINGS_KEY)
            strength = _decode_strength(settings)
            return ControlState(
                self.id,
                {"enabled": marker == _NIGHT_LIGHT_ON, "strength": strength},
                detail="Windows Night light state verified.",
            )
        except FileNotFoundError:
            return ControlState(
                self.id,
                available=False,
                error="Native Night light settings are not initialized for this Windows profile.",
            )
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not self.is_supported():
            raise OSError("Native Night light control is only available on Windows.")
        if isinstance(value, bool):
            enabled = value
            strength = None
        elif isinstance(value, dict):
            enabled = value.get("enabled")
            strength = value.get("strength")
            if not isinstance(enabled, bool):
                raise ValueError("Night light enabled state must be true or false.")
            if strength is not None:
                strength = int(strength)
                if not 0 <= strength <= 100:
                    raise ValueError("Night light strength must be between 0 and 100.")
        else:
            raise ValueError("Night light must be set to true/false or an enabled/strength object.")

        state = _read_cloudstore_value(_STATE_KEY)
        if len(state) < 19 or state[18] not in {_NIGHT_LIGHT_ON, _NIGHT_LIGHT_OFF}:
            raise OSError("Windows Night light state uses an unsupported Windows format.")

        if strength is not None:
            settings = _read_cloudstore_value(_SETTINGS_KEY)
            updated_settings = _encode_strength(settings, strength)
            if updated_settings != settings:
                _write_cloudstore_value(_SETTINGS_KEY, updated_settings)

        updated_state = _encode_enabled_state(state, enabled)
        if updated_state != state:
            _write_cloudstore_value(_STATE_KEY, updated_state)

    def matches(self, requested: Any, actual: Any) -> bool:
        if not isinstance(actual, dict):
            return False
        if isinstance(requested, bool):
            return actual.get("enabled") is requested
        if isinstance(requested, dict):
            for key, value in requested.items():
                actual_value = actual.get(key)
                if key == "strength" and isinstance(actual_value, (int, float)):
                    if abs(actual_value - value) > 1:
                        return False
                elif actual_value != value:
                    return False
            return True
        return False
