from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import QRect

from background_assistant import (
    BackgroundController,
    _contains_wake_phrase,
    _resample_for_vosk,
)


@pytest.mark.parametrize(
    "phrase",
    ["Hey AURA", "hey ora", "hey orra", "Hey Aurora", "hey a u r a"],
)
def test_wake_phrase_accepts_common_vosk_transcriptions(phrase):
    assert _contains_wake_phrase(phrase)


def test_wake_phrase_requires_the_assistant_name():
    assert not _contains_wake_phrase("hey there")


def test_native_sample_rate_audio_is_resampled_for_vosk():
    audio = np.arange(4800, dtype=np.int16)

    resampled = _resample_for_vosk(audio, 48000)

    assert len(resampled) == 1600 * 2


def test_background_overlay_can_activate_while_main_window_is_visible(monkeypatch):
    shown = []
    window = SimpleNamespace(
        isVisible=lambda: True,
        _background_orb=object(),
        show_background_orb=lambda: shown.append(True),
    )
    controller = SimpleNamespace(
        paused=False,
        app=SimpleNamespace(property=lambda _name: False),
        ui=SimpleNamespace(_win=window),
        aura=SimpleNamespace(is_session_active=lambda: False),
        _overlay_active=False,
        _overlay_had_session=False,
        _overlay_started_at=0.0,
        _overlay_last_activity=0.0,
        _overlay_state="CLOSED",
        _set_overlay_state=lambda state, _reason: setattr(
            controller, "_overlay_state", state
        ),
        _stop_wake_listener=lambda: None,
        _hook=SimpleNamespace(start=lambda: False, orb_visible=lambda: None),
        _update_tray_status=lambda _active: None,
    )
    monkeypatch.setattr(
        "background_assistant.platform_bg.configure_orb", lambda _orb: None
    )

    assert BackgroundController._show_overlay(controller) is True
    assert controller._overlay_active is True
    assert shown == [True]


def test_wake_shows_greeting_and_starts_configured_brain_immediately():
    controller = SimpleNamespace(
        paused=False,
        app=SimpleNamespace(property=lambda _name: False),
        _show_overlay=lambda: True,
        ui=SimpleNamespace(show_background_reply=lambda text: setattr(
            controller, "shown_reply", text
        )),
        aura=SimpleNamespace(wake_from_word=lambda **kwargs: setattr(
            controller, "wake_options", kwargs
        )),
    )

    BackgroundController._wake_detected(controller)

    assert controller.shown_reply == "Hey, how can I help you today?"
    assert controller.wake_options == {"greeting": True}


def test_open_window_releases_background_idle_mode():
    show_signal = SimpleNamespace(emit=lambda: setattr(controller, "shown", True))
    controller = SimpleNamespace(
        ui=SimpleNamespace(_win=SimpleNamespace(_show_window_sig=show_signal)),
        aura=SimpleNamespace(set_background_idle_mode=lambda enabled: setattr(
            controller, "idle_mode", enabled
        ), is_waiting_for_wake=lambda: False),
        _overlay_active=False,
        paused=False,
    )

    BackgroundController._show_main_window(controller)

    assert controller.shown is True
    assert controller.idle_mode is False


def test_click_inside_live_orb_geometry_does_not_dismiss():
    orb = SimpleNamespace(geometry=lambda: QRect(100, 100, 600, 220))
    controller = SimpleNamespace(
        _overlay_active=True,
        ui=SimpleNamespace(_win=SimpleNamespace(_background_orb=orb)),
    )

    BackgroundController._dismiss_overlay(controller, "click:120:140")

    assert controller._overlay_active is True


def test_reconcile_keeps_orb_open_when_connection_takes_longer_than_timeout():
    face_ready = SimpleNamespace(is_set=lambda: True)
    window = SimpleNamespace(_ready=False, _face_gate=face_ready, isVisible=lambda: False)
    controller = SimpleNamespace(
        app=SimpleNamespace(property=lambda _name: False),
        ui=SimpleNamespace(_win=window),
        aura=SimpleNamespace(
            is_session_active=lambda: False,
            is_waiting_for_wake=lambda: False,
        ),
        _overlay_active=True,
        _overlay_had_session=False,
        _overlay_started_at=0.0,
        background_mode=True,
        paused=True,
        _listener_blocked=False,
        _wake_listener=None,
        _last_sensitivity=None,
        _maybe_auto_hide=lambda: None,
        _stop_wake_listener=lambda: None,
        _update_tray_status=lambda _active: None,
        _dismiss_overlay=lambda reason: setattr(controller, "dismissed", reason),
    )

    BackgroundController._reconcile(controller)

    assert controller._overlay_active is True
    assert not hasattr(controller, "dismissed")


def test_main_window_shown_stops_wake_listener_and_wakes_engine():
    stopped = []
    woken = []
    state = []
    controller = SimpleNamespace(
        app=SimpleNamespace(property=lambda _name: False),
        ui=SimpleNamespace(set_state=lambda s: state.append(s)),
        aura=SimpleNamespace(
            set_background_idle_mode=lambda enabled: setattr(controller, "idle_mode", enabled),
            is_waiting_for_wake=lambda: True,
            wake_from_word=lambda: woken.append(True),
        ),
        _stop_wake_listener=lambda: stopped.append(True),
        _overlay_active=False,
        paused=False,
    )

    BackgroundController._main_window_shown(controller)

    assert controller.idle_mode is False
    assert len(stopped) >= 1
    assert woken == [True]
    assert state == ["LISTENING"]


def test_reconcile_does_not_listen_when_main_window_is_active():
    started = []
    window = SimpleNamespace(_ready=True, _face_gate=SimpleNamespace(is_set=lambda: True), isVisible=lambda: True)
    controller = SimpleNamespace(
        app=SimpleNamespace(property=lambda _name: False),
        ui=SimpleNamespace(_win=window),
        aura=SimpleNamespace(
            _background_idle_mode=False,
            is_session_active=lambda: False,
            is_waiting_for_wake=lambda: False,
        ),
        _overlay_active=False,
        background_mode=True,
        paused=False,
        _listener_blocked=False,
        _wake_listener=None,
        _maybe_auto_hide=lambda: None,
        _create_wake_listener=lambda s: started.append(s),
        _stop_wake_listener=lambda: None,
        _update_tray_status=lambda _active: None,
    )

    BackgroundController._reconcile(controller)

    assert len(started) == 0


def test_clap_wake_activates_engine_and_hud():
    from aura_features import FeatureRegistry
    registry = FeatureRegistry()

    woken = []
    bg_shown = []
    hud_timer_started = []
    state_set = []

    hud_tmr = SimpleNamespace(
        isActive=lambda: False,
        start=lambda: hud_timer_started.append(True),
    )
    win = SimpleNamespace(
        isMinimized=lambda: False,
        isVisible=lambda: True,
        showNormal=lambda: None,
        activateWindow=lambda: None,
        raise_=lambda: None,
        hud=SimpleNamespace(_tmr=hud_tmr),
    )
    ui = SimpleNamespace(
        _win=win,
        show_cinematic_opening=lambda: None,
        set_state=lambda s: state_set.append(s),
    )
    aura = SimpleNamespace(
        set_background_idle_mode=lambda enabled: setattr(aura, "idle_mode", enabled),
        is_waiting_for_wake=lambda: True,
        wake_from_word=lambda: woken.append(True),
        warmup_engine=lambda: None,
    )
    bg_controller = SimpleNamespace(
        _stop_wake_listener=lambda: None,
        _show_main_window=lambda: bg_shown.append(True),
    )

    registry._on_double_clap(None, ui, aura, bg_controller)

    assert bg_shown == [True]
    assert woken == [True]
    assert aura.idle_mode is False
    assert hud_timer_started == [True]
    assert state_set == ["LISTENING"]
