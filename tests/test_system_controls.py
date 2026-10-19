from core.system_controls.base import ControlState, SystemControl
from core.system_controls.intent import match_system_control_intent
from core.system_controls.manager import SystemControlsManager
from memory import config_manager


class _FakeControl(SystemControl):
    id = "fake"
    label = "Fake"

    def __init__(self, initial=0, fail=False):
        self.value = initial
        self.fail = fail
        self.reads = 0

    def is_supported(self):
        return True

    def get_state(self):
        self.reads += 1
        return ControlState(self.id, self.value)

    def apply_state(self, value):
        if self.fail:
            raise PermissionError("Access is denied")
        self.value = value


class _StaleControl(_FakeControl):
    def apply_state(self, value):
        pass


def test_set_state_reads_back_and_notifies_after_verified_change():
    control = _FakeControl(10)
    manager = SystemControlsManager([control])
    events = []
    manager.subscribe(lambda control_id, state: events.append((control_id, state.value)))
    try:
        result = manager.set_state("fake", 55)
        assert result.success
        assert result.new_state.value == 55
        assert control.reads == 1
        assert events == [("fake", 55)]
    finally:
        manager.close()


def test_failed_change_returns_read_state_and_permission_reason():
    control = _FakeControl(10, fail=True)
    manager = SystemControlsManager([control])
    try:
        result = manager.set_state("fake", 55)
        assert not result.success
        assert result.new_state.value == 10
        assert "administrator rights" in result.error
    finally:
        manager.close()


def test_set_state_rejects_a_change_that_did_not_read_back():
    manager = SystemControlsManager([_StaleControl(10)])
    try:
        result = manager.set_state("fake", 55)
        assert not result.success
        assert result.new_state.value == 10
        assert "did not change" in result.error
    finally:
        manager.close()


def test_system_control_voice_intent_examples():
    examples = {
        "open quick settings": {"operation": "open_panel"},
        "close system settings": {"operation": "close_panel"},
        "set volume to 100%": {"operation": "set", "control_id": "volume", "value": 100},
        "set brightness to 50%": {"operation": "set", "control_id": "brightness", "value": 50},
        "volume 30": {"operation": "set", "control_id": "volume", "value": 30},
        "set volume to fifty": {"operation": "set", "control_id": "volume", "value": 50},
        "increase volume by 10": {"operation": "adjust", "control_id": "volume", "delta": 10},
        "decrease brightness by 10": {"operation": "adjust", "control_id": "brightness", "delta": -10},
        "brightness to minimum": {"operation": "set", "control_id": "brightness", "value": 0},
        "turn Wi-Fi off": {"operation": "set", "control_id": "wifi", "value": False},
        "turn Wi-Fi on": {"operation": "set", "control_id": "wifi", "value": True},
        "connect to Home Network": {"operation": "wifi_connect", "network": "home network"},
        "disconnect Wi-Fi": {"operation": "wifi_disconnect"},
        "list Wi-Fi networks": {"operation": "wifi_scan"},
        "which Wi-Fi am I connected to": {"operation": "status", "control_id": "wifi"},
        "turn airplane mode on": {"operation": "set", "control_id": "airplane_mode", "value": True},
        "turn energy saver off": {"operation": "set", "control_id": "energy_saver", "value": False},
        "enable battery saver": {"operation": "set", "control_id": "energy_saver", "value": True},
        "turn live captions on": {"operation": "set", "control_id": "live_captions", "value": True},
        "turn night light off": {"operation": "set", "control_id": "night_light", "value": False},
        "set night light strength to 60%": {
            "operation": "set", "control_id": "night_light_strength", "value": 60,
        },
        "turn on narrator": {"operation": "set", "control_id": "narrator", "value": True},
        "turn off magnifier": {"operation": "set", "control_id": "magnifier", "value": False},
        "enable sticky keys": {"operation": "set", "control_id": "sticky_keys", "value": True},
        "enable high contrast": {"operation": "set", "control_id": "high_contrast", "value": True},
        "set text size to 150%": {"operation": "set", "control_id": "text_size", "value": 150},
        "enable mono audio": {"operation": "set", "control_id": "mono_audio", "value": True},
        "switch audio output to headphones": {
            "operation": "output_switch", "device": "headphones",
        },
        "which speaker is being used": {"operation": "status", "control_id": "output_devices"},
        "what is the volume?": {"operation": "status", "control_id": "volume"},
        "is night light on?": {"operation": "status", "control_id": "night_light"},
    }
    for phrase, expected in examples.items():
        assert match_system_control_intent(phrase) == expected, phrase
    assert match_system_control_intent("tell me a joke") is None


def test_system_controls_config_has_safe_bounded_defaults(monkeypatch):
    monkeypatch.setattr(
        config_manager, "load_api_keys",
        lambda: {
            "system_controls_refresh_seconds": 0,
            "disabled_controls": ["wifi"],
            "operation_timeout_seconds": 500,
        },
    )
    config = config_manager.get_system_controls_config()
    assert config["system_controls_enabled"] is True
    assert config["system_controls_refresh_seconds"] == 1
    assert config["confirm_network_disruption"] is True
    assert config["disabled_controls"] == ["wifi"]
    assert config["operation_timeout_seconds"] == 30
