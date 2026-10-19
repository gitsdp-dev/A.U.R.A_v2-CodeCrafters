"""Local system settings controls."""

from .accessibility import accessibility_controls
from .airplane import AirplaneModeControl
from .audio import MasterVolumeControl, MuteControl, OutputDevicesControl
from .base import ControlResult, ControlState, SystemControl
from .captions import LiveCaptionsControl
from .display import BrightnessControl
from .energy import EnergySaverControl
from .manager import SystemControlsManager
from .night_light import NightLightControl
from .wifi import WifiControl


def create_system_controls_manager(config: dict | None = None) -> SystemControlsManager:
    """Build the default registry, respecting feature and disabled-control settings."""
    settings = config if isinstance(config, dict) else {}
    disabled = settings.get("disabled_controls", [])
    disabled_ids = set(disabled) if isinstance(disabled, list) else set()
    controls = [
        MasterVolumeControl(), MuteControl(), OutputDevicesControl(),
        BrightnessControl(), WifiControl(), AirplaneModeControl(),
        EnergySaverControl(), LiveCaptionsControl(), NightLightControl(),
        *accessibility_controls(),
    ]
    return SystemControlsManager(
        [control for control in controls if control.id not in disabled_ids],
        timeout_seconds=float(settings.get("operation_timeout_seconds", 8)),
    )


__all__ = [
    "ControlResult", "ControlState", "SystemControl", "SystemControlsManager",
    "create_system_controls_manager",
]
