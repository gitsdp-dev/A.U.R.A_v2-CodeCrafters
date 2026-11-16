from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
)

import platform_bg
from core import audio_devices
from memory.config_manager import (
    get_background_assistant_config,
    get_input_device,
    get_start_at_login_enabled,
    get_wake_word_enabled,
    get_wake_word_sensitivity,
    save_start_at_login_enabled,
)

_LOG = logging.getLogger("aura.background")
_SERVER_NAME = (
    f"aura-v2-single-instance-{os.getuid()}"
    if hasattr(os, "getuid")
    else "aura-v2-single-instance"
)
_WAKE_PATTERN = re.compile(
    r"\bhey\s+(?:aura|ora|orra|aurora|a\s*u\s*r\s*a)\b",
    re.IGNORECASE,
)
_WAKE_GREETING = "Hey, how can I help you today?"


def _contains_wake_phrase(text: str) -> bool:
    return bool(_WAKE_PATTERN.search(str(text or "")))


def _resample_for_vosk(samples: np.ndarray, source_rate: int) -> bytes:
    if source_rate == 16000 or samples.size == 0:
        return samples.astype(np.int16, copy=False).tobytes()
    if source_rate > 16000 and samples.size >= 4:
        # Boxcar low-pass before decimation to limit aliasing.
        taps = max(1, int(round(source_rate / 16000)))
        if taps > 1:
            kernel = np.ones(taps, dtype=np.float32) / taps
            samples = np.convolve(samples.astype(np.float32), kernel, mode="same")
    output_size = max(1, round(samples.size * 16000 / source_rate))
    positions = np.linspace(0, samples.size - 1, output_size, dtype=np.float64)
    return (
        np.interp(positions, np.arange(samples.size), samples)
        .astype(np.int16)
        .tobytes()
    )


class SingleInstance(QObject):
    """Local-only single-instance guard with a show-window activation message."""

    show_requested = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)
        self._pending_show = False
        self._show_callback_bound = False

    def start(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(_SERVER_NAME)
        if socket.waitForConnected(350):
            socket.write(b"SHOW\n")
            socket.waitForBytesWritten(350)
            socket.disconnectFromServer()
            return False

        QLocalServer.removeServer(_SERVER_NAME)
        if self._server.listen(_SERVER_NAME):
            return True

        socket = QLocalSocket()
        socket.connectToServer(_SERVER_NAME)
        if socket.waitForConnected(700):
            socket.write(b"SHOW\n")
            socket.waitForBytesWritten(350)
            socket.disconnectFromServer()
            return False
        raise RuntimeError(
            "Could not establish AURA's single-instance server: "
            f"{self._server.errorString()}"
        )

    def bind_show_callback(self, callback) -> None:
        self.show_requested.connect(callback)
        self._show_callback_bound = True
        if self._pending_show:
            self._pending_show = False
            self.show_requested.emit()

    def close(self) -> None:
        self._server.close()
        QLocalServer.removeServer(_SERVER_NAME)

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            socket.readyRead.connect(
                lambda current=socket: self._read_activation(current)
            )
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read_activation(socket)

    def _read_activation(self, socket) -> None:
        message = bytes(socket.readAll()).decode("utf-8", errors="replace")
        if "SHOW" in message:
            if self._show_callback_bound:
                self.show_requested.emit()
            else:
                self._pending_show = True
        socket.disconnectFromServer()


class _WakeSignals(QObject):
    wake_detected = pyqtSignal()
    microphone_active = pyqtSignal(bool)
    error = pyqtSignal(str)


class WakeWordListener:
    """Vosk-based offline wake listener. Its input stream ends before wake emits."""

    def __init__(self, sensitivity: float):
        self.signals = _WakeSignals()
        self._sensitivity = sensitivity
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_trigger = 0.0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._listen,
            name="aura-wake-word",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def _listen(self) -> None:
        microphone_permission = platform_bg.mic_permission_check()
        if microphone_permission is False:
            self.signals.error.emit(
                "A.U.R.A. does not have microphone permission. Enable it in "
                "System Settings → Privacy & Security → Microphone, then restart "
                "A.U.R.A."
            )
            self.signals.microphone_active.emit(False)
            return
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel

            from core.vosk_models import DEFAULT_VOSK_MODEL, ensure_vosk_model
            from memory.config_manager import get_local_llm_config

            SetLogLevel(-1)
            model_name = get_local_llm_config().get(
                "vosk_model", DEFAULT_VOSK_MODEL
            )
            model = Model(ensure_vosk_model(model_name))
            try:
                recognizer = KaldiRecognizer(
                    model,
                    16000,
                    json.dumps([
                        "hey aura",
                        "hey ora",
                        "hey orra",
                        "hey aurora",
                        "hey a u r a",
                        "[unk]",
                    ]),
                )
            except (TypeError, RuntimeError, ValueError) as exc:
                _LOG.warning(
                    "Wake-word grammar unsupported; using the full Vosk vocabulary: %s",
                    exc,
                )
                recognizer = KaldiRecognizer(model, 16000)
        except Exception as exc:  # noqa: BLE001 - native Vosk/model failures vary by version
            self.signals.error.emit(f"Offline wake-word model unavailable: {exc}")
            self.signals.microphone_active.emit(False)
            return

        frames: queue.Queue[bytes] = queue.Queue(maxsize=24)

        def audio_callback(indata, _frames, _time_info, status):
            if status:
                _LOG.warning("Wake-word microphone status: %s", status)
            data = bytes(indata)
            try:
                frames.put_nowait(data)
            except queue.Full:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frames.put_nowait(data)
                except queue.Full:
                    pass

        try:
            device_name = get_input_device()
            device = (
                audio_devices.resolve(device_name, "input") if device_name else None
            )
        except Exception as exc:  # noqa: BLE001 - PortAudio backends raise driver-specific errors
            self.signals.error.emit(f"Could not select the wake-word microphone: {exc}")
            self.signals.microphone_active.emit(False)
            return
        if device_name and device is None:
            self.signals.error.emit(
                f"Microphone '{device_name}' is unavailable; using the default input."
            )
        try:
            query_device = device if device is not None else sd.default.device[0]
            device_info = sd.query_devices(query_device, "input")
            stream_rate = round(
                float(device_info.get("default_samplerate", 16000))
            )
            if stream_rate <= 0:
                stream_rate = 16000
            try:  # Prefer Vosk's native rate: no resampling, better accuracy.
                sd.check_input_settings(
                    device=device, samplerate=16000, channels=1, dtype="int16"
                )
                stream_rate = 16000
            except Exception:  # noqa: BLE001 - fall back to the device rate
                pass
        except Exception as exc:  # noqa: BLE001 - PortAudio backends vary by platform
            _LOG.warning("Could not query the microphone sample rate: %s", exc)
            stream_rate = 16000

        speech_seen_until = 0.0
        noise_floor = 40.0
        sens = float(self._sensitivity)
        while not self._stop.is_set():
            detected = False
            self.signals.microphone_active.emit(True)
            try:
                with sd.RawInputStream(
                    samplerate=stream_rate,
                    blocksize=4000,
                    channels=1,
                    dtype="int16",
                    device=device,
                    callback=audio_callback,
                ):
                    while not self._stop.is_set():
                        try:
                            audio = frames.get(timeout=0.15)
                        except queue.Empty:
                            continue
                        samples = np.frombuffer(audio, dtype=np.int16)
                        if samples.size == 0:
                            continue
                        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                        now = time.monotonic()
                        threshold = max(35.0, noise_floor * (1.5 + (1.0 - sens) * 2.0))
                        if rms >= threshold:
                            speech_seen_until = now + 1.5
                        else:  # adapt to the room's ambient level
                            noise_floor = 0.95 * noise_floor + 0.05 * rms
                        audio = _resample_for_vosk(samples, stream_rate)
                        if recognizer.AcceptWaveform(audio):
                            text = json.loads(recognizer.Result()).get("text", "")
                        else:
                            text = json.loads(
                                recognizer.PartialResult()
                            ).get("partial", "")
                        if (
                            _contains_wake_phrase(text)
                            and now <= speech_seen_until
                            and now - self._last_trigger >= 2.5
                        ):
                            self._last_trigger = now
                            detected = True
                            break
            except Exception as exc:  # noqa: BLE001 - capture PortAudio and recognizer failures
                if self._stop.is_set():
                    break
                _LOG.exception("Wake-word microphone failed")
                self.signals.error.emit(f"Wake-word microphone unavailable: {exc}")
                self._stop.wait(5.0)
            finally:
                self.signals.microphone_active.emit(False)
            if detected:
                # Emit only after the input stream is closed so the live
                # session can grab the microphone immediately.
                self.signals.wake_detected.emit()
                return
        self.signals.microphone_active.emit(False)


class _DismissSignals(QObject):
    dismiss_requested = pyqtSignal(str)
    error = pyqtSignal(str)


class GlobalDismissHook:
    """Temporary, non-swallowing global click/Esc hook, active only for the orb."""

    def __init__(self):
        self.signals = _DismissSignals()
        self._handle = None
        self._started_at = 0.0

    def start(self) -> bool:
        self.stop()
        self._started_at = time.monotonic()
        self._handle = platform_bg.start_global_click_hook(self._request)
        if self._handle.message:
            self.signals.error.emit(self._handle.message)
        return self._handle.fallback

    def orb_visible(self) -> None:
        self._started_at = time.monotonic()

    @property
    def fallback_active(self) -> bool:
        return bool(self._handle and self._handle.fallback)

    def _request(self, reason: str) -> None:
        if time.monotonic() - self._started_at < 0.3:
            return
        if self._handle is not None and not self._handle.stop_event.is_set():
            if reason == "escape":
                self._handle.stop_event.set()
            self.signals.dismiss_requested.emit(reason)

    def stop(self) -> None:
        platform_bg.stop_global_click_hook(self._handle)
        self._handle = None


class _ClickCatcher(QWidget):
    dismiss_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet("background: rgba(0, 0, 0, 1);")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def show_catcher(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.virtualGeometry())
        self.show()
        self.lower()

    def mousePressEvent(self, _event) -> None:
        point = _event.globalPosition().toPoint()
        self.dismiss_requested.emit(f"click:{point.x()}:{point.y()}")

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss_requested.emit("escape")
            return
        super().keyPressEvent(event)


def set_windows_startup(enabled: bool) -> None:
    """Compatibility wrapper for the historical Windows-only public helper."""
    if not platform_bg.is_windows():
        raise OSError("The legacy Windows startup helper is Windows-only.")
    if enabled:
        platform_bg.autostart_enable()
    else:
        platform_bg.autostart_disable()


class BackgroundController(QObject):
    """Owns tray residency, the floating orb, wake listener, and app shutdown."""

    _show_window_signal = pyqtSignal()
    _quit_requested_signal = pyqtSignal()

    def __init__(
        self,
        app: QApplication,
        ui,
        aura,
        single_instance: SingleInstance,
        background_mode: bool,
        runner_done: threading.Event,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.app = app
        self.ui = ui
        self.aura = aura
        self.single_instance = single_instance
        self.background_mode = background_mode
        self.runner_done = runner_done
        self.paused = False
        self._overlay_active = False
        self._overlay_had_session = False
        self._overlay_started_at = 0.0
        self._overlay_state = "CLOSED"
        self._overlay_last_activity = time.monotonic()
        self._activity_state = "IDLE_ACTIVE"
        self._background_settings = get_background_assistant_config()
        self._wake_listener: WakeWordListener | None = None
        self._listener_blocked = False
        self._last_sensitivity: float | None = None
        self._hook = GlobalDismissHook()
        self._hook.signals.dismiss_requested.connect(self._dismiss_overlay)
        self._hook.signals.error.connect(self._show_error)
        self._click_catcher: _ClickCatcher | None = None
        self._permission_warning_shown = False
        self._accessibility_warning_shown = False
        self._show_window_signal.connect(self._show_main_window)
        self._quit_requested_signal.connect(self.quit)
        self.ui.on_show_main_window = self._show_window_signal.emit
        self.ui.on_quit_requested = self._quit_requested_signal.emit
        self.single_instance.bind_show_callback(self._show_window_signal.emit)

        icon_path = platform_bg.tray_icon_path()
        icon = QIcon(str(icon_path))
        self.app.setWindowIcon(icon)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setProperty("_aura_tray_resident", True)
        self.app.setProperty("_aura_quitting", False)
        self.app._aura_quit_callback = self.quit
        platform_bg.configure_background_app(background_mode)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("A.U.R.A. — starting")
        self._build_tray_menu()
        self._tray_available = platform_bg.tray_available()
        if self._tray_available:
            self.tray.show()
        else:
            _LOG.error(
                "No system tray is available; A.U.R.A. continues in the background "
                "and can be opened by launching it again. Enable a tray/AppIndicator "
                "extension for tray controls."
            )
            self.app.setProperty("_aura_tray_resident", False)
            QTimer.singleShot(0, self._show_tray_fallback)
        self.ui._win._background_wake_sig.connect(self._show_overlay)
        self.ui._win._background_dismiss_sig.connect(self._dismiss_overlay)
        self.ui._win._state_sig.connect(self._background_ui_state)
        self.ui._win._background_orb.text_input.textEdited.connect(
            self._background_text_activity
        )
        self.ui._win._background_hidden_sig.connect(self._main_window_hidden)
        self.ui._win._background_settings_sig.connect(self._settings_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._reconcile)
        self._timer.start()
        self._reconcile()

    def _build_tray_menu(self) -> None:
        menu = QMenu()
        open_action = menu.addAction("Open AURA")
        open_action.triggered.connect(self._show_window_signal.emit)
        self.pause_action = menu.addAction("Pause Listening")
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self._toggle_pause)
        self.startup_action = menu.addAction("Start at login")
        self.startup_action.setCheckable(True)
        startup_enabled = platform_bg.autostart_is_enabled()
        if platform_bg.is_windows():
            startup_enabled = get_start_at_login_enabled()
        self.startup_action.setChecked(startup_enabled)
        self.startup_action.setEnabled(platform_bg.autostart_supported())
        self.startup_action.toggled.connect(self._toggle_startup)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window_signal.emit()

    def _show_main_window(self) -> None:
        platform_bg.configure_background_app(False)
        self.ui._win._show_window_sig.emit()
        self.aura.set_background_idle_mode(False)
        if self._overlay_active:
            self._dismiss_overlay("main window opened", cancel_session=False)
        if (
            not self.paused
            and self.aura.is_waiting_for_wake()
        ):
            self._stop_wake_listener()
            self.aura.wake_from_word()

    def _show_tray_fallback(self) -> None:
        if self.app.property("_aura_quitting"):
            return
        self.ui._win.menuBar().addMenu(self.menu)

    def _main_window_hidden(self) -> None:
        if self.app.property("_aura_quitting"):
            return
        self.aura.set_background_idle_mode(True)
        self.aura.dismiss_active_session()
        self.aura.interrupt(log_message=False)

    def _toggle_pause(self, paused: bool) -> None:
        self.paused = bool(paused)
        self.pause_action.setText("Resume Listening" if paused else "Pause Listening")
        self.aura.set_listening_paused(paused)
        if paused:
            self._stop_wake_listener()
            self.aura.interrupt(log_message=False)
            self._dismiss_overlay("paused", cancel_session=False)
        else:
            self._listener_blocked = False
        self._reconcile()

    def _toggle_startup(self, enabled: bool) -> None:
        try:
            if enabled:
                platform_bg.autostart_enable()
            else:
                platform_bg.autostart_disable()
            save_start_at_login_enabled(enabled)
        except Exception as exc:  # noqa: BLE001 - OS autostart APIs vary by platform
            self.startup_action.blockSignals(True)
            self.startup_action.setChecked(not enabled)
            self.startup_action.blockSignals(False)
            self.tray.showMessage(
                "A.U.R.A.",
                f"Could not update login startup: {exc}",
                QSystemTrayIcon.MessageIcon.Warning,
                6000,
            )
            _LOG.exception("Could not update login startup")

    def _settings_changed(self) -> None:
        self._listener_blocked = False
        self._background_settings = get_background_assistant_config()
        if hasattr(self.aura, "_background_assistant_config"):
            self.aura._background_assistant_config = dict(
                self._background_settings
            )
        self._reconcile()

    def _reconcile(self) -> None:
        if self.app.property("_aura_quitting"):
            return
        window = self.ui._win
        ready = bool(window._ready and window._face_gate.is_set())
        active = self.aura.is_session_active()
        if self._overlay_active and active:
            self._overlay_had_session = True
        self._maybe_auto_hide()
        should_listen_for_wake = (
            (self.background_mode or self.aura.is_waiting_for_wake())
            and ready
            and get_wake_word_enabled()
            and not self.paused
            and not active
            and not self._overlay_active
            and not self._listener_blocked
            and not window.isVisible()
        )
        sensitivity = get_wake_word_sensitivity()
        if should_listen_for_wake:
            if self._wake_listener is None:
                self._create_wake_listener(sensitivity)
            elif self._last_sensitivity != sensitivity:
                self._stop_wake_listener()
                self._create_wake_listener(sensitivity)
            elif not self._wake_listener.is_running:
                self._wake_listener.start()
        else:
            self._stop_wake_listener()

        self._update_tray_status(active)

    def _create_wake_listener(self, sensitivity: float) -> None:
        listener = WakeWordListener(sensitivity)
        listener.signals.wake_detected.connect(self._wake_detected)
        listener.signals.microphone_active.connect(self._microphone_active)
        listener.signals.error.connect(self._wake_error)
        self._wake_listener = listener
        self._last_sensitivity = sensitivity
        listener.start()

    def _stop_wake_listener(self) -> None:
        listener, self._wake_listener = self._wake_listener, None
        if listener is not None:
            listener.stop()
        self._microphone_active(False)

    def _wake_detected(self) -> None:
        if self.paused or self.app.property("_aura_quitting"):
            return
        if not self._show_overlay():
            return
        self.ui.show_background_reply(_WAKE_GREETING)
        self.aura.wake_from_word(greeting=True)

    def _show_overlay(self) -> bool:
        if self.paused or self.app.property("_aura_quitting"):
            return False
        orb = self.ui._win._background_orb
        if self._overlay_active and orb.isVisible():
            return True
        if self.ui._win.isVisible():
            return False
        self._overlay_active = True
        self._overlay_had_session = self.aura.is_session_active()
        self._overlay_started_at = time.monotonic()
        self._overlay_last_activity = self._overlay_started_at
        self._set_overlay_state("LISTENING", "wake word activated")
        self._stop_wake_listener()
        try:
            platform_bg.configure_orb(self.ui._win._background_orb)
        except Exception:  # noqa: BLE001 - cosmetic platform tweaks must never block the orb
            _LOG.exception("configure_orb failed; continuing")
        # No global click hook / click catcher: the orb closes only via its X button.
        self.ui._win.show_background_orb()
        try:
            self._hook.orb_visible()
        except Exception:  # noqa: BLE001
            _LOG.exception("orb_visible failed")
        if not orb.isVisible():
            _LOG.warning("Background orb was requested but is not visible")
        self._update_tray_status(self.aura.is_session_active())
        return True

    def _set_overlay_state(self, state: str, reason: str) -> None:
        if self._overlay_state == state:
            return
        self._overlay_state = state
        _LOG.info("Background assistant state=%s reason=%s", state, reason)

    def _background_ui_state(self, state: str) -> None:
        self._activity_state = str(state).upper()
        self._overlay_last_activity = time.monotonic()

    def _background_text_activity(self, _text: str) -> None:
        self._overlay_last_activity = time.monotonic()

    def _maybe_auto_hide(self) -> None:
        return  # orb closes only via its X button
        if not self._overlay_active:
            return
        settings = self._background_settings
        if not settings["auto_hide_enabled"]:
            return
        orb = self.ui._win._background_orb
        busy_states = {
            "USER_SPEAKING", "PROCESSING", "THINKING", "STREAMING", "SPEAKING"
        }
        if (
            self._activity_state in busy_states
            or getattr(self.aura, "_is_speaking", False)
            or getattr(self.aura, "_local_turn_active", threading.Event()).is_set()
            or orb.text_input.hasFocus()
            or bool(orb.text_input.text().strip())
        ):
            self._overlay_last_activity = time.monotonic()
            return
        timeout = settings["auto_hide_minutes"] * 60
        if time.monotonic() - self._overlay_last_activity >= timeout:
            self._dismiss_overlay("auto-hide after inactivity")

    def _dismiss_overlay(self, reason: str, cancel_session: bool = True) -> None:
        if reason.startswith("click:") or reason == "escape":
            return  # outside clicks / Esc never close the orb
        if reason.startswith("click:") and self._overlay_active:
            try:
                _kind, x, y = reason.split(":", 2)
                orb = self.ui._win._background_orb
                rect = orb.geometry().adjusted(-4, -4, 4, 4)
                try:
                    dpr = float(orb.devicePixelRatioF()) or 1.0
                except Exception:  # noqa: BLE001
                    dpr = 1.0
                fx, fy = float(x), float(y)
                if rect.contains(int(fx), int(fy)) or rect.contains(
                    int(fx / dpr), int(fy / dpr)
                ):
                    return
            except (TypeError, ValueError):
                _LOG.warning("Ignoring malformed click dismissal position: %s", reason)
                return
        if self._overlay_active:
            self._set_overlay_state("CLOSING", reason)
        self._hook.stop()
        if self._click_catcher is not None:
            self._click_catcher.hide()
        if cancel_session:
            self.aura.dismiss_active_session()
            self.aura.interrupt(log_message=False)
        if self._overlay_active:
            self._overlay_active = False
            self._overlay_had_session = False
            self.ui._win.hide_background_orb()
            self._set_overlay_state("IDLE_ACTIVE", "orb dismissed")
        self._reconcile()

    def _wake_error(self, message: str) -> None:
        _LOG.error("%s", message)
        self._listener_blocked = True
        self._stop_wake_listener()
        if self._tray_available:
            self.tray.showMessage(
                "A.U.R.A. wake word unavailable",
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                7000,
            )
        else:
            self._show_error(message)

    def _show_error(self, message: str) -> None:
        _LOG.error("%s", message)
        if "Global click dismissal needs" in message:
            if self._accessibility_warning_shown:
                return
            self._accessibility_warning_shown = True
        if self._tray_available:
            self.tray.showMessage(
                "A.U.R.A.",
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                6000,
            )
        elif not self._permission_warning_shown:
            self._permission_warning_shown = True
            QMessageBox.warning(None, "A.U.R.A.", message)

    def _show_click_catcher(self) -> None:
        if self._click_catcher is None:
            self._click_catcher = _ClickCatcher()
            self._click_catcher.dismiss_requested.connect(self._dismiss_overlay)
        self._click_catcher.show_catcher()

    def _microphone_active(self, active: bool) -> None:
        self._wake_mic_active = bool(active)

    def _update_tray_status(self, session_active: bool) -> None:
        if getattr(self, "_wake_mic_active", False):
            status = "Wake-word microphone active — listening for Hey AURA"
        elif session_active:
            status = "Microphone active — AURA is listening"
        elif self.paused:
            status = "Listening paused"
        elif not get_wake_word_enabled():
            status = "Wake word disabled — enable it in Customize settings"
        else:
            status = "A.U.R.A. is running in the background"
        self.tray.setToolTip(f"A.U.R.A. — {status}")

    def quit(self) -> None:
        if self.app.property("_aura_quitting"):
            return
        self.app.setProperty("_aura_quitting", True)
        if self._overlay_active:
            self._dismiss_overlay("tray Quit")
        else:
            self._set_overlay_state("CLOSING", "tray Quit")
        self._timer.stop()
        self._hook.stop()
        if self._click_catcher is not None:
            self._click_catcher.close()
        self._stop_wake_listener()
        self.aura.request_shutdown()
        self.single_instance.close()
        self.ui._win.close()
        QTimer.singleShot(5000, self.app.quit)
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(100)
        self._quit_timer.timeout.connect(self._quit_when_runner_stops)
        self._quit_timer.start()

    def _quit_when_runner_stops(self) -> None:
        if self.runner_done.is_set():
            self._quit_timer.stop()
            self.app.quit()


def show_or_start_aura() -> None:
    """Ask the current process to show its window; launch safely if absent."""
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if socket.waitForConnected(350):
        socket.write(b"SHOW\n")
        socket.waitForBytesWritten(350)
        socket.disconnectFromServer()
        return

    platform_bg.launch_app("AURA")