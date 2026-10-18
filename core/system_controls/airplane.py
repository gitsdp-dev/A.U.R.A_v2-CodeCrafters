"""Windows airplane-mode approximation implemented by controlling radio states."""

from __future__ import annotations

import asyncio
import platform

from .base import ControlState, SystemControl


class AirplaneModeControl(SystemControl):
    """Airplane mode is not public; radio states are saved and restored per session."""

    id = "airplane_mode"
    label = "Airplane mode"

    def __init__(self):
        self._previous: list[tuple[str, Any]] | None = None

    def is_supported(self) -> bool:
        return platform.system() == "Windows"

    async def _radios(self):
        from winsdk.windows.devices.radios import Radio

        return await Radio.get_radios_async()

    def get_state(self) -> ControlState:
        if not self.is_supported():
            return ControlState(self.id, available=False, error="Airplane mode radio control is only available on Windows.")
        try:
            async def read_state():
                radios = await self._radios()
                if not radios:
                    return None
                return all("off" in str(radio.state).casefold() for radio in radios)

            value = asyncio.run(read_state())
            if value is None:
                return ControlState(self.id, available=False, error="No controllable radios were found.")
            return ControlState(self.id, value)
        except Exception as exc:
            return ControlState(self.id, available=False, error=str(exc))

    def apply_state(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValueError("Airplane mode state must be true or false.")

        async def apply():
            radios = await self._radios()
            from winsdk.windows.devices.radios import RadioState

            if value:
                already_disabled = all(
                    "off" in str(radio.state).casefold() for radio in radios
                )
                if not (already_disabled and self._previous is not None):
                    self._previous = [(str(radio.kind), radio.state) for radio in radios]
                for radio in radios:
                    if "on" in str(radio.state).casefold():
                        await radio.set_state_async(RadioState.OFF)
                return
            if self._previous is not None:
                if len(radios) != len(self._previous):
                    raise OSError("The available radios changed; airplane mode could not restore their exact prior states.")
                for radio, (prior_kind, prior_state) in zip(radios, self._previous):
                    if str(radio.kind) != prior_kind:
                        raise OSError("The radio list changed; airplane mode could not restore exact prior states.")
                    await radio.set_state_async(prior_state)
            else:
                for radio in radios:
                    if radio.state != RadioState.ON:
                        await radio.set_state_async(RadioState.ON)
            self._previous = None

        asyncio.run(apply())
