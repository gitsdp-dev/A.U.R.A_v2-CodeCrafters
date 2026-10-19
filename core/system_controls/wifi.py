"""Windows Wi-Fi radio, scan and saved-network operations."""

from __future__ import annotations

import asyncio
import platform
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any

from .base import ControlState, SystemControl
from .elevation import is_access_denied, run_as_admin


def _run_netsh(*args: str, timeout: float = 8.0) -> str:
    result = subprocess.run(
        ["netsh", *args], capture_output=True, text=True, timeout=timeout,
        check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode:
        raise OSError(output.strip() or "The Windows Wi-Fi command failed.")
    return output


def _named_argument(key: str, value: str) -> str:
    if '"' in value:
        raise ValueError(f"{key} contains an unsupported quote character.")
    return f'{key}="{value}"'


class WifiControl(SystemControl):
    id = "wifi"
    label = "Wi-Fi"
    elevation_timeout_seconds = 28.0

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    async def _radios(self):
        from winsdk.windows.devices.radios import Radio, RadioKind

        radios = await Radio.get_radios_async()
        wifi_kind = getattr(RadioKind, "WI_FI", None)
        return [
            radio for radio in radios
            if radio.kind == wifi_kind
            or "wi_fi" in str(radio.kind).casefold()
            or "wifi" in str(radio.kind).casefold()
        ]

    def _radio_info(self) -> tuple[bool, str] | None:
        async def read():
            radios = await self._radios()
            if not radios:
                return None
            from winsdk.windows.devices.radios import RadioState

            states = [radio.state for radio in radios]
            if all(state == RadioState.ON for state in states):
                return True
            if all(state == RadioState.OFF for state in states):
                return False
            return any(state == RadioState.ON for state in states)

        enabled = asyncio.run(read())
        return (enabled, "WinRT radio") if enabled is not None else None

    @staticmethod
    def _parse_interface_state(text: str) -> tuple[bool | None, str, str]:
        ssid_match = re.search(
            r"^\s*SSID\s*:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE
        )
        name_match = re.search(
            r"^\s*Name\s*:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE
        )
        radio_match = re.search(
            r"^\s*Radio status\s*:\s*Hardware\s+(On|Off)\s+Software\s+(On|Off)\s*$",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
        if radio_match:
            enabled = all(
                status.casefold() == "on"
                for status in radio_match.groups()
            )
        else:
            enabled = None
        ssid = ssid_match.group(1).strip() if ssid_match else ""
        adapter = name_match.group(1).strip() if name_match else ""
        return enabled, ssid, adapter

    def get_state(self) -> ControlState:
        if not self.is_supported():
            return ControlState(self.id, available=False, error="Wi-Fi control is only available on Windows.")
        radio_error = None
        try:
            try:
                radio = self._radio_info()
            except Exception as exc:
                radio = None
                radio_error = exc
            try:
                text = _run_netsh("wlan", "show", "interfaces")
                text_enabled, ssid, _adapter = self._parse_interface_state(text)
            except Exception as exc:
                if radio is None:
                    if radio_error:
                        raise OSError(
                            f"Windows Radio API failed: {radio_error}; "
                            f"netsh could not read Wi-Fi state: {exc}"
                        ) from exc
                    raise
                return ControlState(self.id, {"enabled": radio[0], "ssid": ""})
            if radio is not None:
                return ControlState(self.id, {"enabled": radio[0], "ssid": ssid})
            if text_enabled is None:
                raise OSError("Windows did not report the Wi-Fi hardware and software radio states.")
            return ControlState(self.id, {"enabled": text_enabled, "ssid": ssid})
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def _connected_ssid(self) -> str:
        """Return the active WLAN name, or an empty string when disconnected."""
        interfaces = _run_netsh("wlan", "show", "interfaces")
        _enabled, ssid, _adapter = self._parse_interface_state(interfaces)
        return ssid

    def apply_state(self, value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("enabled")
        if not isinstance(value, bool):
            raise ValueError("Wi-Fi state must be true or false.")
        radio_error = None
        try:
            async def set_radio():
                radios = await self._radios()
                if not radios:
                    return False
                from winsdk.windows.devices.radios import RadioState

                target = RadioState.ON if value else RadioState.OFF
                for radio in radios:
                    await radio.set_state_async(target)
                return True

            if asyncio.run(set_radio()):
                return
        except Exception as exc:
            radio_error = exc
        try:
            interfaces = _run_netsh("wlan", "show", "interfaces")
        except Exception as exc:
            if radio_error:
                raise OSError(
                    f"Windows Radio API failed: {radio_error}; "
                    f"netsh could not locate the Wi-Fi adapter: {exc}"
                ) from exc
            raise
        _enabled, _ssid, adapter = self._parse_interface_state(interfaces)
        if not adapter:
            raise OSError("Windows did not report the Wi-Fi adapter name.")
        args = (
            "interface set interface "
            + _named_argument("name", adapter)
            + " admin="
            + ("enabled" if value else "disabled")
        )
        try:
            _run_netsh("interface", "set", "interface", _named_argument("name", adapter),
                       "admin=" + ("enabled" if value else "disabled"))
        except OSError as exc:
            if not is_access_denied(exc):
                if radio_error:
                    raise OSError(
                        f"Windows Radio API failed: {radio_error}; "
                        f"netsh could not change Wi-Fi: {exc}"
                    ) from exc
                raise
            run_as_admin("netsh.exe", args)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = self.get_state()
            if state.available and state.value.get("enabled") is value:
                return
            time.sleep(0.25)
        raise OSError("Windows did not verify the requested Wi-Fi radio state.")

    def matches(self, requested: Any, actual: Any) -> bool:
        expected = requested.get("enabled") if isinstance(requested, dict) else requested
        return isinstance(actual, dict) and actual.get("enabled") is expected

    def scan_networks(self) -> list[dict[str, Any]]:
        text = _run_netsh("wlan", "show", "networks", "mode=bssid", timeout=15)
        connected = self._connected_ssid()
        profiles = _run_netsh("wlan", "show", "profiles")
        saved = {
            match.group(1).strip().casefold()
            for match in re.finditer(
                r"^\s*All User Profile\s*:\s*(.+?)\s*$",
                profiles,
                re.MULTILINE | re.IGNORECASE,
            )
        }
        networks: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in text.splitlines():
            ssid = re.match(r"^\s*SSID\s+\d+\s*:\s*(.*)$", line, re.IGNORECASE)
            if ssid:
                if current:
                    networks.append(current)
                name = ssid.group(1).strip()
                current = {
                    "ssid": name, "signal": 0, "security": "Unknown",
                    "secured": False, "connected": name == connected,
                    "saved": name.casefold() in saved,
                }
            elif current:
                signal = re.match(r"^\s*Signal\s*:\s*(\d+)%", line, re.IGNORECASE)
                security = re.match(r"^\s*Authentication\s*:\s*(.+)$", line, re.IGNORECASE)
                if signal:
                    current["signal"] = int(signal.group(1))
                if security:
                    current["security"] = security.group(1).strip()
                    current["secured"] = current["security"].casefold() not in {
                        "open", "none", "unknown",
                    }
        if current:
            networks.append(current)
        return networks

    def connect(self, ssid: str, password: str | None = None) -> None:
        name = str(ssid).strip()
        if not name:
            raise ValueError("Choose a Wi-Fi network first.")
        if password:
            root = ET.Element("WLANProfile", xmlns="http://www.microsoft.com/networking/WLAN/profile/v1")
            ET.SubElement(root, "name").text = name
            ssid_config = ET.SubElement(root, "SSIDConfig")
            ET.SubElement(ET.SubElement(ssid_config, "SSID"), "name").text = name
            ET.SubElement(root, "connectionType").text = "ESS"
            ET.SubElement(root, "connectionMode").text = "manual"
            security = ET.SubElement(root, "MSM")
            security_config = ET.SubElement(security, "security")
            auth = ET.SubElement(security_config, "authEncryption")
            ET.SubElement(auth, "authentication").text = "WPA2PSK"
            ET.SubElement(auth, "encryption").text = "AES"
            ET.SubElement(auth, "useOneX").text = "false"
            shared_key = ET.SubElement(security_config, "sharedKey")
            ET.SubElement(shared_key, "keyType").text = "passPhrase"
            ET.SubElement(shared_key, "protected").text = "false"
            key = ET.SubElement(shared_key, "keyMaterial")
            key.text = str(password)
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory(prefix="aura-wifi-") as folder:
                profile = Path(folder) / "profile.xml"
                profile.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
                _run_netsh(
                    "wlan", "add", "profile",
                    _named_argument("filename", str(profile)), "user=current",
                )
        _run_netsh("wlan", "connect", _named_argument("name", name), timeout=15)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if self._connected_ssid().casefold() == name.casefold():
                return
            time.sleep(0.5)
        raise OSError(f"Windows did not verify a connection to '{name}'.")

    def disconnect(self) -> None:
        _run_netsh("wlan", "disconnect")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._connected_ssid():
                return
            time.sleep(0.3)
        raise OSError("Windows did not verify that Wi-Fi disconnected.")

    def forget(self, ssid: str) -> None:
        name = str(ssid).strip()
        if not name:
            raise ValueError("Choose a saved Wi-Fi network first.")
        _run_netsh("wlan", "delete", "profile", _named_argument("name", name))
        if name.casefold() in {
            match.group(1).strip().casefold()
            for match in re.finditer(
                r"^\s*All User Profile\s*:\s*(.+?)\s*$",
                _run_netsh("wlan", "show", "profiles"),
                re.MULTILINE | re.IGNORECASE,
            )
        }:
            raise OSError(f"Windows did not verify that the saved profile '{name}' was removed.")
