import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from core.system_controls.base import ControlState
from core.system_controls.audio import MasterVolumeControl, _endpoint_volume
from core.system_controls.display import BrightnessControl
from core.system_controls.energy import EnergySaverControl
from core.system_controls.wifi import WifiControl


def test_wifi_status_parses_hardware_and_software_radio_states():
    enabled, ssid, adapter = WifiControl._parse_interface_state(
        "    Name                   : Wireless LAN\n"
        "    SSID                   : Home Wi-Fi\n"
        "    Radio status           : Hardware On\n"
        "                             Software On\n"
    )
    assert enabled is True
    assert ssid == "Home Wi-Fi"
    assert adapter == "Wireless LAN"

    enabled, ssid, _adapter = WifiControl._parse_interface_state(
        "    Name                   : Wi-Fi\n"
        "    Radio status           : Hardware On\n"
        "                             Software Off\n"
    )
    assert enabled is False
    assert ssid == ""


def test_wifi_prefers_native_radio_state_and_uses_ssid_when_connected():
    control = WifiControl()
    with patch("core.system_controls.wifi.platform.system", return_value="Windows"), patch.object(
        control, "_radio_info", return_value=(True, "WinRT radio")
    ), patch(
        "core.system_controls.wifi._run_netsh",
        return_value=(
            "    Name                   : Wireless LAN\n"
            "    SSID                   : Home Wi-Fi\n"
            "    Radio status           : Hardware On\n"
            "                             Software On\n"
        ),
    ):
        state = control.get_state()
    assert state.available
    assert state.value == {"enabled": True, "ssid": "Home Wi-Fi"}


def test_wifi_falls_back_to_radio_report_when_netsh_cannot_read_ssid():
    control = WifiControl()
    with patch("core.system_controls.wifi.platform.system", return_value="Windows"), patch.object(
        control, "_radio_info", return_value=(True, "WinRT radio")
    ), patch(
        "core.system_controls.wifi._run_netsh",
        side_effect=OSError("WLAN AutoConfig service is stopped."),
    ):
        state = control.get_state()
    assert state.available
    assert state.value == {"enabled": True, "ssid": ""}


def test_wifi_reads_status_from_netsh_when_winrt_radios_are_unavailable():
    control = WifiControl()
    with patch("core.system_controls.wifi.platform.system", return_value="Windows"), patch.object(
        control, "_radio_info", return_value=None
    ), patch(
        "core.system_controls.wifi._run_netsh",
        return_value=(
            "    Name                   : Wi-Fi\n"
            "    State                  : disconnected\n"
            "    Radio status           : Hardware On\n"
            "                             Software On\n"
        ),
    ):
        state = control.get_state()
    assert state.available
    assert state.value == {"enabled": True, "ssid": ""}


def test_wifi_connected_ssid_reads_current_interface_and_handles_disconnection():
    control = WifiControl()
    with patch(
        "core.system_controls.wifi._run_netsh",
        return_value="    SSID                   : Home Wi-Fi\n",
    ):
        assert control._connected_ssid() == "Home Wi-Fi"
    with patch(
        "core.system_controls.wifi._run_netsh",
        return_value="    State                  : disconnected\n",
    ):
        assert control._connected_ssid() == ""


def test_wifi_adapter_fallback_requests_uac_only_after_access_denied():
    control = WifiControl()
    access_denied = OSError("Access is denied.")

    async def no_winrt_radios():
        return []

    with patch("core.system_controls.wifi.platform.system", return_value="Windows"), patch.object(
        control, "_radios", no_winrt_radios
    ), patch(
        "core.system_controls.wifi._run_netsh",
        side_effect=[
            "    Name                   : Wireless LAN\n",
            access_denied,
        ],
    ), patch(
        "core.system_controls.wifi.run_as_admin"
    ) as request_admin, patch.object(
        control, "get_state", return_value=ControlState("wifi", {"enabled": True, "ssid": ""})
    ):
        control.apply_state(True)
    request_admin.assert_called_once_with(
        "netsh.exe",
        'interface set interface name="Wireless LAN" admin=enabled',
    )


def test_brightness_uses_native_wmi_when_optional_package_is_absent():
    control = BrightnessControl()
    with patch("core.system_controls.display.platform.system", return_value="Windows"), patch(
        "core.system_controls.display._run_powershell", return_value="[34, 51]"
    ) as run_powershell:
        state = control.get_state()
    assert state.available
    assert state.value == 34
    run_powershell.assert_called_once()


def test_brightness_uses_wmi_to_set_and_read_back_without_optional_package():
    control = BrightnessControl()
    with patch("core.system_controls.display.platform.system", return_value="Windows"), patch(
        "core.system_controls.display._run_powershell",
        side_effect=["", "[62]"],
    ) as run_powershell:
        result = control.set_state(62)
    assert result.success
    assert result.new_state.value == 62
    assert run_powershell.call_count == 2
    assert "WmiSetBrightness" in run_powershell.call_args_list[0].args[0]


def test_brightness_requests_uac_only_after_an_access_denied_error():
    control = BrightnessControl()
    with patch("core.system_controls.display.platform.system", return_value="Windows"), patch(
        "core.system_controls.display._write_brightness",
        side_effect=PermissionError("Access is denied."),
    ), patch(
        "core.system_controls.display.run_as_admin"
    ) as request_admin, patch(
        "core.system_controls.display._read_brightness", return_value=[54]
    ):
        control.apply_state(54)
    request_admin.assert_called_once()
    assert request_admin.call_args.args[0] == "powershell.exe"
    assert "WmiSetBrightness" in request_admin.call_args.args[1]


def test_volume_control_writes_and_reads_the_windows_master_endpoint():
    class Endpoint:
        level = 0.28

        def GetMasterVolumeLevelScalar(self):
            return self.level

        def SetMasterVolumeLevelScalar(self, value, _event_context):
            self.level = value

    endpoint = Endpoint()
    control = MasterVolumeControl()
    with patch("core.system_controls.audio.platform.system", return_value="Windows"), patch(
        "core.system_controls.audio._endpoint_volume", return_value=endpoint
    ):
        result = control.set_state(72)
    assert result.success
    assert result.new_state.value == 72


def test_endpoint_volume_uses_pycaw_audio_device_property():
    endpoint = object()
    speaker = SimpleNamespace(EndpointVolume=endpoint)
    pycaw_module = ModuleType("pycaw.pycaw")
    pycaw_module.AudioUtilities = SimpleNamespace(GetSpeakers=lambda: speaker)
    with patch("core.system_controls.audio.platform.system", return_value="Windows"), patch(
        "core.system_controls.audio._ensure_com_initialized"
    ), patch.dict(sys.modules, {"pycaw.pycaw": pycaw_module}):
        assert _endpoint_volume() is endpoint


def test_energy_saver_requests_one_elevated_action_after_access_denied():
    control = EnergySaverControl()
    denied = type("CommandResult", (), {
        "returncode": 1, "stderr": "Access is denied.", "stdout": "",
    })()
    with patch("core.system_controls.energy.platform.system", return_value="Windows"), patch(
        "core.system_controls.energy.psutil.sensors_battery", return_value=object()
    ), patch(
        "core.system_controls.energy.subprocess.run", return_value=denied
    ), patch(
        "core.system_controls.energy.run_as_admin"
    ) as request_admin:
        control.apply_state(True)
    request_admin.assert_called_once()
    assert request_admin.call_args.args[0] == "powershell.exe"
    assert "powercfg" in request_admin.call_args.args[1]
    assert "SUB_ENERGYSAVER" in request_admin.call_args.args[1]
