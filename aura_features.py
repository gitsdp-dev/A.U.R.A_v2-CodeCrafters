"""
aura_features.py — Feature Registry for A.U.R.A. v2

Provides a centralized, single lifecycle manager for new features
(Presence Awareness, Local Chat Memory, Clap Detection / Fast Startup).
Ensures clean start, pause, restart, and shutdown with zero orphan threads.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import Qt
from aura_presence import PresenceManager

_LOG = logging.getLogger("aura.features")

_REGISTRY_INSTANCE: FeatureRegistry | None = None


class FeatureRegistry:
    """Central lifecycle registry for A.U.R.A. v2 extensions."""

    def __init__(self) -> None:
        self.presence_manager: PresenceManager | None = None
        self.chat_memory_manager: Any = None
        self.clap_detector: Any = None
        self._initialized = False

    def initialize(self, app, ui, aura, bg_controller=None) -> None:
        """Initialize all enabled features with application references."""
        if self._initialized:
            return

        _LOG.info("Initializing FeatureRegistry...")
        try:
            # 1. Presence Awareness
            def _silence_on_away():
                stop_fn = getattr(aura, "stop_notification_readout", None)
                if callable(stop_fn):
                    stop_fn()

            self.presence_manager = PresenceManager(
                interrupt_callback=_silence_on_away
            )

            # Connect catch-up card to UI toast manager
            if ui and hasattr(ui, "_win"):
                win = ui._win
                self.presence_manager.catchup_card_requested.connect(
                    lambda card: self._show_catchup_card(ui, card)
                )
                # Listen to Windows session lock/unlock
                self.presence_manager.start_monitoring(app, win)

            # Spoken & visual catch-up hooks
            if aura:
                self.presence_manager.away_started.connect(
                    lambda: self._on_away_started(aura)
                )
                self.presence_manager.away_ended.connect(
                    lambda data: self._on_away_ended(aura, data)
                )

            _LOG.info("PresenceManager initialized successfully.")
        except Exception as exc:
            _LOG.exception("Failed to initialize PresenceManager: %s", exc)

        # 2. Conversation Recall
        try:
            from aura_chat_memory import ChatMemoryManager
            self.chat_memory_manager = ChatMemoryManager()
            from memory.config_manager import get_chat_memory_config
            cfg = get_chat_memory_config()
            retention_days = cfg.get("retention_days", 90)
            if retention_days > 0:
                self.chat_memory_manager.delete_older_than(retention_days)
            _LOG.info("ChatMemoryManager initialized successfully.")
        except Exception as exc:
            _LOG.exception("Failed to initialize ChatMemoryManager: %s", exc)

        # 3. Clap Detection & Fast Startup
        try:
            from aura_clap import ClapDetector
            from memory.config_manager import get_clap_config
            clap_cfg = get_clap_config()
            self.clap_detector = ClapDetector(
                sensitivity=float(clap_cfg.get("sensitivity", 0.65))
            )
            self.clap_detector.enabled = bool(clap_cfg.get("enabled", True))
            if self.presence_manager:
                self.clap_detector.set_away_callback(
                    lambda: bool(self.presence_manager.is_away) if self.presence_manager else False
                )
            if ui and hasattr(ui, "_win"):
                win = ui._win
                self.clap_detector.set_foreground_callback(
                    lambda: bool(win.isVisible() and not win.isMinimized() and win.isActiveWindow())
                )
            self.clap_detector.double_clap_detected.connect(
                lambda: self._on_double_clap(app, ui, aura, bg_controller),
                Qt.ConnectionType.QueuedConnection
            )
            _LOG.info("ClapDetector initialized successfully.")
        except Exception as exc:
            _LOG.exception("Failed to initialize ClapDetector: %s", exc)

        self._initialized = True

    def _show_catchup_card(self, ui, card_notification) -> None:
        """Display catch-up notification card in the UI."""
        try:
            if hasattr(ui, "_win") and hasattr(ui._win, "_notification_toasts"):
                ui._win._notification_toasts.enqueue(card_notification)
            if hasattr(ui, "_win") and hasattr(ui._win, "_notification_panel"):
                if ui._win._notification_panel.isVisible():
                    ui._win._notification_panel.refresh()
        except Exception as exc:
            _LOG.debug("Error displaying catch-up card: %s", exc)

    def _on_away_started(self, aura) -> None:
        """Proactively notify and hold when user leaves."""
        try:
            msg = "Away Mode is active. I will hold all notifications, reminders, and background tasks safely until you return."
            if hasattr(aura, "ui") and aura.ui:
                aura.ui.write_log(f"AURA: {msg}")
                aura.ui.show_content(
                    "PRESENCE: AWAY MODE",
                    "Away Mode is ACTIVE.\n\nAll incoming notifications, reminders, and tasks are being safely held.\nSay 'I\\'m back' or unlock your screen when you return."
                )
                aura.ui.show_background_reply("Away Mode active. Notifications held.")
        except Exception as exc:
            _LOG.warning("Failed to log away started: %s", exc)

    def _on_away_ended(self, aura, data: dict) -> None:
        """Speak and display catch-up summary when the user returns via OS unlock."""
        summary = data.get("summary")
        if not summary:
            return
        # If triggered via voice/text command, _handle_presence_command has already answered
        if "command" in str(data.get("reason", "")).lower():
            return

        from memory.config_manager import get_presence_config
        cfg = get_presence_config()
        if not cfg.get("speak_summary_on_return", True):
            return

        try:
            count = data.get("count", 0)
            if count > 0:
                reply_text = f"{summary} Say 'read details' to hear everything."
            else:
                reply_text = "Welcome back! I kept watch while you were away. You have no missed notifications or reminders—all systems are quiet and up to date."

            if hasattr(aura, "ui") and aura.ui:
                aura.ui.write_log(f"AURA: {reply_text}")
                aura.ui.show_content("PRESENCE: WELCOME BACK", reply_text)
                aura.ui.show_background_reply(reply_text)

            if hasattr(aura, "speak_presence_reply"):
                aura.speak_presence_reply(reply_text)
            elif hasattr(aura, "speak_catchup"):
                aura.speak_catchup(reply_text)
        except Exception as exc:
            _LOG.warning("Failed to deliver catch-up summary: %s", exc)

    def _on_double_clap(self, app, ui, aura, bg_controller=None) -> None:
        """Handle 2-clap wake event: bring to foreground, cinematic open, warm up, greet."""
        _LOG.info("2-Clap Wake triggered!")
        try:
            # 1. Bring window to foreground & wake assistant engine
            if bg_controller:
                bg_controller._stop_wake_listener()
                bg_controller._show_main_window()
            elif ui and hasattr(ui, "show_main_window"):
                ui.show_main_window()
            elif ui and hasattr(ui, "_win") and ui._win:
                win = ui._win
                if win.isMinimized() or not win.isVisible():
                    win.showNormal()
                win.activateWindow()
                win.raise_()

            if aura:
                aura.set_background_idle_mode(False)
                if aura.is_waiting_for_wake():
                    aura.wake_from_word()

            if ui and hasattr(ui, "_win") and hasattr(ui._win, "hud"):
                if hasattr(ui._win.hud, "_tmr") and not ui._win.hud._tmr.isActive():
                    ui._win.hud._tmr.start()

            if ui and hasattr(ui, "set_state"):
                ui.set_state("LISTENING")

            # 2. Trigger Cinematic Opening
            if ui and hasattr(ui, "show_cinematic_opening"):
                ui.show_cinematic_opening()

            # 3. Pre-warm engine in parallel
            if aura and hasattr(aura, "warmup_engine"):
                aura.warmup_engine()

            # 4. Formulate Butler Greeting
            import datetime
            hour = datetime.datetime.now().hour
            if 5 <= hour < 12:
                tod = "Good morning"
            elif 12 <= hour < 17:
                tod = "Good afternoon"
            else:
                tod = "Good evening"

            pm = self.presence_manager
            has_catchup = False
            catchup_summary = ""
            if pm and pm.pending_count > 0:
                has_catchup = True
                catchup_summary = pm.get_brief_summary()

            if has_catchup and catchup_summary:
                speech_text = f"{tod}, sir. Welcome home. All systems are operational. While you were away: {catchup_summary}. Say 'read details' to hear everything."
                directive = (
                    f"[SYSTEM NOTIFICATION - ACT AS AURA BUTLER]: A double-clap has woken you. Greet the user with: '{tod}, sir. Welcome home.' "
                    f"Report that systems are operational, and summarize their missed events: {catchup_summary}. Offer to read details."
                )
            else:
                speech_text = f"{tod}, sir. Welcome home. All systems are online and operational. How may I assist you?"
                directive = (
                    f"[SYSTEM NOTIFICATION - ACT AS AURA BUTLER]: A double-clap has woken you. Greet the user warmly: '{tod}, sir. Welcome home.' "
                    f"Confirm that all systems are operational and ready for their command."
                )

            # Display card & speak
            if ui and hasattr(ui, "_win") and hasattr(ui._win, "_presence_card_sig"):
                ui._win._presence_card_sig.emit("[SYSTEMS ONLINE]", speech_text, "WAKE", 5000)
            if ui and hasattr(ui, "show_background_reply"):
                ui.show_background_reply(speech_text)

            if aura and hasattr(aura, "speak_presence_reply"):
                aura.speak_presence_reply(speech_text, butler_instruction=directive)
            elif aura and hasattr(aura, "speak_catchup"):
                aura.speak_catchup(speech_text)
        except Exception as exc:
            _LOG.exception("Error handling double clap: %s", exc)

    def shutdown(self, app=None) -> None:
        """Gracefully terminate background workers and close databases."""
        _LOG.info("Shutting down FeatureRegistry...")
        if self.presence_manager:
            try:
                if app:
                    self.presence_manager.stop_monitoring(app)
            except Exception as exc:
                _LOG.warning("Error stopping presence monitoring: %s", exc)
            self.presence_manager = None

        if self.clap_detector:
            try:
                self.clap_detector.stop()
            except Exception:
                pass
            self.clap_detector = None

        if self.chat_memory_manager:
            try:
                self.chat_memory_manager.close()
            except Exception:
                pass
            self.chat_memory_manager = None

        self._initialized = False
        _LOG.info("FeatureRegistry shutdown complete.")


def get_feature_registry() -> FeatureRegistry:
    """Return the global FeatureRegistry singleton."""
    global _REGISTRY_INSTANCE
    if _REGISTRY_INSTANCE is None:
        _REGISTRY_INSTANCE = FeatureRegistry()
    return _REGISTRY_INSTANCE
