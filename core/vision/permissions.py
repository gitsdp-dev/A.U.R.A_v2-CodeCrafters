"""Permission gating and transparent confirmation for A.U.R.A. Vision."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from core.confirmation import (
    ConfirmationDecision,
    ConfirmationPlan,
    RiskLevel,
    get_confirmation_broker,
)
from core.vision.models import VisionCapability
from memory.config_manager import get_local_llm_config, get_vision_config

LOGGER = logging.getLogger(__name__)


class VisionPermissionDeniedError(PermissionError):
    """Raised when the user or settings deny a vision capture/analysis request."""


def describe_image_destination(mode: str | None = None) -> str:
    """Return a transparent, user-facing description of where the image goes."""
    resolved = (mode or get_local_llm_config().get("mode", "gemini")).strip().lower()
    if resolved == "local":
        return "On-device only (local processing; nothing is sent to the cloud)."
    if resolved == "fallback":
        return (
            "Sent to the Gemini API (Google) using your API key, or stays "
            "on-device if falling back to the local engine."
        )
    return "Sent to the Gemini API (Google) using your configured API key."


class VisionPermissionManager:
    """Enforces independent capability toggles and per-session confirmation."""

    def __init__(
        self,
        config_loader: Callable[[], dict] = get_vision_config,
        broker_provider: Callable[[], Any] = get_confirmation_broker,
    ) -> None:
        self._config_loader = config_loader
        self._broker_provider = broker_provider
        self._lock = threading.Lock()
        self._session_grants: set[VisionCapability] = set()

    def clear_session_grants(self) -> None:
        with self._lock:
            self._session_grants.clear()

    def has_session_grant(self, capability: VisionCapability) -> bool:
        with self._lock:
            return capability in self._session_grants

    def grant_for_session(self, capability: VisionCapability) -> None:
        with self._lock:
            self._session_grants.add(capability)

    def ensure_allowed(
        self,
        capability: VisionCapability,
        *,
        source_label: str,
        task_label: str,
        engine_mode: str | None = None,
    ) -> bool:
        """Verify Settings toggle and prompt via ConfirmationBroker if required."""
        cfg = self._config_loader()
        toggle_key = {
            VisionCapability.CAMERA: "camera_enabled",
            VisionCapability.SCREEN: "screen_enabled",
            VisionCapability.FILE: "file_enabled",
        }[capability]

        if not cfg.get(toggle_key, True):
            cap_name = {
                VisionCapability.CAMERA: "Camera vision",
                VisionCapability.SCREEN: "Screen capture",
                VisionCapability.FILE: "File/image analysis",
            }[capability]
            raise VisionPermissionDeniedError(
                f"{cap_name} is disabled in Settings -> Permissions."
            )

        ask_every_time = bool(cfg.get("ask_every_time", True))
        remember_session = bool(cfg.get("remember_for_session", False)) and not ask_every_time

        if remember_session and self.has_session_grant(capability):
            return True

        broker = self._broker_provider()
        if broker is None:
            # No interactive GUI broker (e.g. headless test without mock): deny by default.
            raise VisionPermissionDeniedError(
                "Confirmation dialog is unavailable; vision access denied for safety."
            )

        destination = describe_image_destination(engine_mode)
        cap_title = {
            VisionCapability.CAMERA: "Allow Camera Vision",
            VisionCapability.SCREEN: "Allow Screen Capture",
            VisionCapability.FILE: "Allow File / Image Analysis",
        }[capability]
        risk = (
            RiskLevel.MEDIUM
            if capability in (VisionCapability.CAMERA, VisionCapability.SCREEN)
            else RiskLevel.LOW
        )
        plan = ConfirmationPlan(
            action_id=f"vision.{capability.value}",
            title=cap_title,
            summary=(
                f"Capture/analyze: {source_label} for '{task_label}'.\n"
                f"Destination: {destination}\n"
                "Raw images stay in memory and are not saved to disk."
            ),
            operations=(
                f"Source: {source_label}",
                f"Task: {task_label}",
                f"Destination: {destination}",
            ),
            risk_level=risk,
            reversible=True,
            read_only=True,
            details=(
                "No background monitoring is used.",
                "You can stop or cancel at any time.",
            ),
        )
        decision = broker.request_confirmation(plan)
        if decision == ConfirmationDecision.DENY:
            raise VisionPermissionDeniedError(
                f"Permission denied for {capability.value} vision."
            )

        if remember_session:
            self.grant_for_session(capability)
        return True
