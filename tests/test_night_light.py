import sys
from unittest.mock import patch

from core.system_controls import ControlState
from core.system_controls.night_light import (
    NightLightControl,
    _SETTINGS_KEY,
    _STATE_KEY,
)


class _FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    KEY_QUERY_VALUE = 0x0001
    REG_BINARY = 3

    def __init__(self, state: bytes, settings: bytes):
        self.data = {
            _STATE_KEY: bytearray(state),
            _SETTINGS_KEY: bytearray(settings),
        }

    def OpenKey(self, _root, path, _reserved, _access):
        if path not in self.data:
            raise FileNotFoundError(path)
        return _FakeKey(self, path)


class _FakeKey:
    def __init__(self, registry, path):
        self.registry = registry
        self.path = path

    def __enter__(self):
        return self.path

    def __exit__(self, *_args):
        return False


class _FakeRegistryOperations(_FakeRegistry):
    def QueryValueEx(self, path, name):
        assert name == "Data"
        return bytes(self.data[path]), self.REG_BINARY

    def SetValueEx(self, path, name, _reserved, value_type, value):
        assert name == "Data"
        assert value_type == self.REG_BINARY
        self.data[path] = bytearray(value)

    def FlushKey(self, _path):
        return None


def _state_blob(enabled: bool) -> bytes:
    size = 43 if enabled else 41
    data = bytearray(range(size))
    data[18] = 0x15 if enabled else 0x13
    return bytes(data)


def _settings_blob(strength: int = 0) -> bytes:
    temperature = round(6500 - strength / 100 * (6500 - 1200))
    high = temperature // 64
    low = round((temperature - high * 64) * 2 + 128)
    data = bytearray(0x25)
    data[10] = 1
    data[0x23] = low
    data[0x24] = high
    return bytes(data)


def test_native_toggle_preserves_cloudstore_state_and_reads_back():
    registry = _FakeRegistryOperations(_state_blob(False), _settings_blob())
    control = NightLightControl()
    with patch.dict(sys.modules, {"winreg": registry}), patch(
        "core.system_controls.night_light.platform.system", return_value="Windows"
    ):
        before = control.get_state()
        assert before.available
        assert before.value == {"enabled": False, "strength": 0}

        result = control.set_state(True)
        assert result.success
        assert result.new_state.value["enabled"] is True
        enabled = bytes(registry.data[_STATE_KEY])
        assert len(enabled) == 43
        assert enabled[18] == 0x15
        assert enabled[23:25] == b"\x10\x00"
        assert enabled[25:] == _state_blob(False)[23:41]
        assert enabled[22] == 0

        result = control.set_state(False)
        assert result.success
        disabled = bytes(registry.data[_STATE_KEY])
        assert len(disabled) == 41
        assert disabled[18] == 0x13
        assert disabled[23:] == enabled[25:]


def test_native_intensity_changes_windows_settings_and_is_verified():
    registry = _FakeRegistryOperations(_state_blob(True), _settings_blob())
    control = NightLightControl()
    with patch.dict(sys.modules, {"winreg": registry}), patch(
        "core.system_controls.night_light.platform.system", return_value="Windows"
    ):
        result = control.set_state({"enabled": True, "strength": 50})
        assert result.success
        assert result.new_state.value == {"enabled": True, "strength": 50}


def test_missing_or_unknown_native_format_is_reported_unavailable():
    registry = _FakeRegistryOperations(_state_blob(False), _settings_blob())
    registry.data[_STATE_KEY][18] = 0x99
    control = NightLightControl()
    with patch.dict(sys.modules, {"winreg": registry}), patch(
        "core.system_controls.night_light.platform.system", return_value="Windows"
    ):
        state = control.get_state()
        assert not state.available
        assert "unsupported Windows format" in state.error


def test_native_night_light_is_not_claimed_on_other_platforms():
    control = NightLightControl()
    with patch("core.system_controls.night_light.platform.system", return_value="Linux"):
        state: ControlState = control.get_state()
    assert not state.available
    assert "only available on Windows" in state.error
