"""Tests for A.U.R.A.'s Advanced Vision System (core/vision/)."""

from __future__ import annotations

import io
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import PIL.Image
import pytest

from core.confirmation import ConfirmationDecision
from core.vision.backends import LocalVisionBackend, VisionBackend
from core.vision.capture import CaptureCancelledError, capture_camera
from core.vision.imaging import MAX_LONG_SIDE_PX, prepare_image_bytes
from core.vision.intents import match_vision_intent
from core.vision.models import (
    VisionCapability,
    VisionImage,
    VisionResult,
    VisionSource,
    VisionTaskType,
)
from core.vision.permissions import (
    VisionPermissionDeniedError,
    VisionPermissionManager,
    describe_image_destination,
)
from core.vision.router import VisionRouter


def _make_png(width: int = 100, height: int = 80, color=(240, 120, 20)) -> bytes:
    buf = io.BytesIO()
    PIL.Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeBroker:
    def __init__(self, decision: ConfirmationDecision) -> None:
        self.decision = decision
        self.plans = []

    def request_confirmation(self, plan):
        self.plans.append(plan)
        return self.decision


def test_image_downscaling_and_hashing():
    raw = _make_png(3000, 1500)
    img = prepare_image_bytes(raw, source=VisionSource.SCREEN, source_label="Screen 1")
    assert max(img.width, img.height) <= MAX_LONG_SIDE_PX
    assert img.width == MAX_LONG_SIDE_PX
    assert img.height == 784
    assert img.mime_type == "image/jpeg"
    assert len(img.sha256) == 64


def test_permission_gating_and_destination_transparency():
    # Disabled capability fails immediately without prompting
    broker = _FakeBroker(ConfirmationDecision.APPROVE)
    mgr = VisionPermissionManager(
        config_loader=lambda: {
            "camera_enabled": False,
            "screen_enabled": True,
            "file_enabled": True,
            "ask_every_time": True,
            "remember_for_session": False,
        },
        broker_provider=lambda: broker,
    )
    with pytest.raises(VisionPermissionDeniedError):
        mgr.ensure_allowed(VisionCapability.CAMERA, source_label="Webcam", task_label="object")
    assert broker.plans == []

    # Denied confirmation raises VisionPermissionDeniedError
    deny_broker = _FakeBroker(ConfirmationDecision.DENY)
    mgr_deny = VisionPermissionManager(
        config_loader=lambda: {
            "camera_enabled": True,
            "ask_every_time": True,
            "remember_for_session": False,
        },
        broker_provider=lambda: deny_broker,
    )
    with pytest.raises(VisionPermissionDeniedError):
        mgr_deny.ensure_allowed(
            VisionCapability.CAMERA,
            source_label="Webcam",
            task_label="camera_object",
            engine_mode="gemini",
        )
    assert "Gemini API" in deny_broker.plans[0].summary
    assert "On-device only" in describe_image_destination("local")


def test_permission_session_grant_when_remember_enabled():
    broker = _FakeBroker(ConfirmationDecision.APPROVE)
    mgr = VisionPermissionManager(
        config_loader=lambda: {
            "screen_enabled": True,
            "ask_every_time": False,
            "remember_for_session": True,
        },
        broker_provider=lambda: broker,
    )
    assert mgr.ensure_allowed(VisionCapability.SCREEN, source_label="Screen", task_label="vqa")
    assert mgr.ensure_allowed(VisionCapability.SCREEN, source_label="Screen", task_label="vqa")
    assert len(broker.plans) == 1
    mgr.clear_session_grants()
    assert mgr.ensure_allowed(VisionCapability.SCREEN, source_label="Screen", task_label="vqa")
    assert len(broker.plans) == 2


def test_camera_always_releases_on_error_and_cancel():
    fake_cap = MagicMock()
    fake_cap.read.return_value = (False, None)
    fake_ui = SimpleNamespace(pause_room_watch=MagicMock(), resume_room_watch=MagicMock())

    with patch("core.camera.open_camera", return_value=fake_cap):
        with pytest.raises(Exception):
            capture_camera(ui=fake_ui, warmup_frames=1, timeout_s=2.0)

    fake_cap.release.assert_called_once()
    fake_ui.pause_room_watch.assert_called_once()
    fake_ui.resume_room_watch.assert_called_once()

    # Cancellation before opening camera
    ev = threading.Event()
    ev.set()
    with pytest.raises(CaptureCancelledError):
        capture_camera(cancel_event=ev)


def test_backend_fallback_and_session_cache():
    class _FailGemini(VisionBackend):
        name = "gemini"
        supports_multimodal = True

        def is_available(self):
            return True

        def analyze_image(self, image, prompt, task=VisionTaskType.GENERAL, timeout_s=30.0):
            return VisionResult(
                ok=False, task=task, backend="gemini", model="gemini", summary="", error="quota"
            )

    class _OkLocal(VisionBackend):
        name = "local"
        supports_multimodal = False
        calls = 0

        def analyze_image(self, image, prompt, task=VisionTaskType.GENERAL, timeout_s=30.0):
            self.calls += 1
            return VisionResult(
                ok=True,
                task=task,
                backend="local",
                model="ocr",
                summary="Extracted text",
                image_hash=image.sha256,
            )

    notices = []
    local = _OkLocal()
    router = VisionRouter(
        gemini_backend=_FailGemini(),
        local_backend=local,
        config_loader=lambda: {"mode": "gemini"},
        notifier=lambda title, body: notices.append((title, body)),
    )
    img = prepare_image_bytes(_make_png(200, 200), source=VisionSource.SCREEN)

    res1 = router.analyze(img, "read this", task=VisionTaskType.OCR)
    assert res1.ok is True
    assert res1.backend == "local"
    assert res1.cached is False
    assert len(notices) >= 1

    res2 = router.analyze(img, "read this", task=VisionTaskType.OCR)
    assert res2.ok is True
    assert res2.cached is True
    assert local.calls == 1


def test_local_backend_degrades_gracefully_with_pdf_text():
    img = VisionImage(
        data=b"\x89PNG\r\n\x1a\n",
        mime_type="image/png",
        sha256="abc123",
        width=1,
        height=1,
        source=VisionSource.PDF,
        source_label="report.pdf",
        extracted_text="Total revenue increased by 18% in Q4.",
    )
    backend = LocalVisionBackend()
    with patch.object(backend, "_detect_local_vlm", return_value=None), patch.object(
        backend, "_reason_over_text_locally", return_value="Revenue grew 18% in Q4."
    ):
        res = backend.analyze_image(img, "Summarize this", task=VisionTaskType.DOCUMENT)
    assert res.ok is True
    assert "Revenue grew 18% in Q4." in res.summary
    assert "No local vision model is configured" in res.summary


def test_intent_routing():
    assert match_vision_intent("explain this architecture").task == VisionTaskType.DIAGRAM
    assert match_vision_intent("read this page and summarize it").task == VisionTaskType.DOCUMENT
    assert match_vision_intent("explain this chart").task == VisionTaskType.CHART
    assert match_vision_intent("extract the text from this image").task == VisionTaskType.OCR
    cam = match_vision_intent("what is this object")
    assert cam.task == VisionTaskType.CAMERA_OBJECT
    assert cam.source == VisionSource.CAMERA
    scr = match_vision_intent("what's on my screen")
    assert scr.task == VisionTaskType.SCREENSHOT
    assert scr.source == VisionSource.SCREEN
    rem = match_vision_intent("remember what's in this document", has_selected_file=True)
    assert rem.remember_summary is True
    assert rem.source == VisionSource.FILE
    assert match_vision_intent("what time is it") is None


def test_action_loader_discovers_vision_tools():
    from pathlib import Path
    from core.action_loader import discover_actions

    reg = discover_actions(Path("actions"), logger=lambda _msg: None)
    names = {rec.name for rec in reg._actions.values()}
    assert "analyze_vision" in names
    assert "vision_followup" in names
    assert "remember_vision_summary" in names
