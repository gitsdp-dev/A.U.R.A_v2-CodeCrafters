"""Windows master audio controls and output endpoint discovery."""

from __future__ import annotations

import platform
import threading
from typing import Any

from .base import ControlState, SystemControl

_com_state = threading.local()


def _ensure_com_initialized() -> None:
    """Initialize COM once on each control-worker thread."""
    if getattr(_com_state, "initialized", False):
        return
    import comtypes

    comtypes.CoInitialize()
    _com_state.initialized = True


def _endpoint_volume():
    if platform.system() != "Windows":
        raise OSError("Master audio controls are only implemented on Windows.")
    _ensure_com_initialized()
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


class MasterVolumeControl(SystemControl):
    id = "volume"
    label = "Volume"

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        try:
            endpoint = _endpoint_volume()
            return ControlState(self.id, int(round(endpoint.GetMasterVolumeLevelScalar() * 100)))
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        amount = int(value)
        if not 0 <= amount <= 100:
            raise ValueError("Volume must be between 0 and 100.")
        _endpoint_volume().SetMasterVolumeLevelScalar(amount / 100.0, None)


class MuteControl(SystemControl):
    id = "mute"
    label = "Mute"

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        try:
            return ControlState(self.id, bool(_endpoint_volume().GetMute()))
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError("Mute state must be true or false.")
        _endpoint_volume().SetMute(value, None)


class OutputDevicesControl(SystemControl):
    """Enumerates endpoints. Default-device switching is exposed only when available."""

    id = "output_devices"
    label = "Audio output devices"

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    def get_state(self) -> ControlState:
        try:
            devices = self.list_devices()
            return ControlState(self.id, devices)
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        self.set_default_device(str(value))

    def matches(self, requested: Any, actual: Any) -> bool:
        target = str(requested).casefold()
        return any(
            item.get("name", "").casefold() == target and item.get("default")
            for item in actual
        )

    def list_devices(self) -> list[dict[str, Any]]:
        if platform.system() != "Windows":
            raise OSError("Audio output discovery is only implemented on Windows.")
        _ensure_com_initialized()
        from pycaw.pycaw import AudioUtilities

        default = AudioUtilities.GetSpeakers()
        default_id = getattr(default, "id", None)
        result = []
        for item in AudioUtilities.GetAllDevices():
            if getattr(item, "Flow", 0) != 0:
                continue
            name = str(getattr(item, "FriendlyName", "") or "").strip()
            if name:
                result.append({
                    "id": str(getattr(item, "id", "")),
                    "name": name,
                    "default": getattr(item, "id", None) == default_id,
                })
        return result

    def set_default_device(self, device_name: str) -> None:
        """Set the Windows default endpoint through the undocumented policy COM interface."""
        if platform.system() != "Windows":
            raise OSError("Default output switching is only implemented on Windows.")
        _ensure_com_initialized()
        from ctypes import POINTER, c_int, c_void_p, c_wchar_p
        from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown
        from comtypes.client import CreateObject

        class _IPolicyConfig(IUnknown):
            _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetMixFormat", (["in"], c_wchar_p, "device"),
                          (["out"], POINTER(c_void_p), "format")),
                COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], c_wchar_p, "device"),
                          (["in"], c_void_p, "default"), (["out"], POINTER(c_void_p), "format")),
                COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], c_wchar_p, "device")),
                COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], c_wchar_p, "device"),
                          (["in"], c_void_p, "format"), (["in"], c_void_p, "default")),
                COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], c_wchar_p, "device"),
                          (["out"], POINTER(c_void_p), "default"), (["out"], POINTER(c_void_p), "minimum")),
                COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], c_wchar_p, "device"),
                          (["in"], c_void_p, "period")),
                COMMETHOD([], HRESULT, "GetShareMode", (["in"], c_wchar_p, "device"),
                          (["out"], POINTER(c_void_p), "mode")),
                COMMETHOD([], HRESULT, "SetShareMode", (["in"], c_wchar_p, "device"),
                          (["in"], c_void_p, "mode")),
                COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], c_wchar_p, "device"),
                          (["in"], c_void_p, "key"), (["out"], POINTER(c_void_p), "value")),
                COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], c_wchar_p, "device"),
                          (["in"], c_void_p, "key"), (["in"], c_void_p, "value")),
                COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], c_wchar_p, "device"),
                          (["in"], c_int, "role")),
                COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], c_wchar_p, "device"),
                          (["in"], c_int, "visible")),
            ]

        matches = [
            item for item in self.list_devices()
            if item["name"].casefold() == device_name.casefold()
        ]
        if not matches:
            raise ValueError(f"No audio output device named '{device_name}' was found.")
        if len(matches) > 1:
            raise ValueError(f"More than one output device is named '{device_name}'.")
        try:
            policy = CreateObject(
                "{870af99c-171d-4f9e-af0d-e63df40c2bc9}",
                interface=_IPolicyConfig, clsctx=CLSCTX_ALL,
            )
            for role in (0, 1, 2):
                policy.SetDefaultEndpoint(matches[0]["id"], role)
        except Exception as exc:
            raise OSError(f"Windows could not switch the default audio device: {exc}") from exc
