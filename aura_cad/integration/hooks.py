"""The one integration point for CAD actions and UI callbacks."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

from aura_cad.errors import BackendUnavailable, CadError
from aura_cad.models import CadResult, JobState
from aura_cad.service import CadService
from aura_cad.strings import (
    CAD_CACHE_CLEAR_ACTION,
    CAD_CACHE_CLEAR_CONFIRM_DETAIL,
    CAD_CACHE_CLEAR_CONFIRM_TITLE,
    CAD_CACHE_CLEAR_FAILED,
    CAD_CACHE_CLEARED,
    CAD_CACHE_DISABLED,
    CAD_CACHE_ENABLED,
    CAD_CACHE_ENABLED_LABEL,
    CAD_CACHE_ENTRY_COUNT_LABEL,
    CAD_CACHE_LOADING,
    CAD_CACHE_OTHER_CONFIRMATION,
    CAD_CACHE_REFRESH_ACTION,
    CAD_CACHE_SETTING_FAILED,
    CAD_CACHE_SIZE_LABEL,
    CAD_CACHE_TITLE,
    cache_status,
    cache_size_label,
)
from aura_cad.viewer.panel import CadViewerPanel

LOGGER = logging.getLogger("aura.cad")
_LOCK = threading.RLock()
_HOOKS = None
_INSTALL_PENDING = False


class CadHooks:
    def __init__(self, player=None) -> None:
        self.player = player
        self.service = CadService()
        self.viewer = CadViewerPanel()
        self._progress_lock = threading.Lock()
        self._progress_values: dict[str, int] = {}
        self.service.on_progress = self._progress
        threading.Thread(
            target=self._refresh_cache_stats,
            name="aura-cad-cache-stats",
            daemon=True,
        ).start()

    def part_cache_settings_section(self) -> dict:
        """Return the cache settings schema; values are served from memory only."""
        cache = self.service.part_cache
        stats = cache.stats_snapshot()
        entries = str(stats.entry_count) if stats.loaded else CAD_CACHE_LOADING
        size = cache_size_label(stats.size_bytes) if stats.loaded else CAD_CACHE_LOADING

        def save(values: dict) -> None:
            enabled = bool(values.get("enabled", True))
            cache.set_enabled_snapshot(enabled)
            threading.Thread(
                target=self._persist_cache_enabled,
                args=(enabled,),
                name="aura-cad-cache-settings",
                daemon=True,
            ).start()

        return {
            "namespace": "cad_part_cache",
            "title": CAD_CACHE_TITLE,
            "values": {
                "enabled": cache.enabled_snapshot(),
                "entries": entries,
                "size": size,
            },
            "fields": [
                {
                    "key": "enabled",
                    "type": "toggle",
                    "label": CAD_CACHE_ENABLED_LABEL,
                    "default": True,
                },
                {
                    "key": "entries",
                    "type": "readonly",
                    "label": CAD_CACHE_ENTRY_COUNT_LABEL,
                    "default": entries,
                },
                {
                    "key": "size",
                    "type": "readonly",
                    "label": CAD_CACHE_SIZE_LABEL,
                    "default": size,
                },
            ],
            "save": save,
            "actions": [
                {"label": CAD_CACHE_REFRESH_ACTION, "run": self._refresh_cache_action},
                {"label": CAD_CACHE_CLEAR_ACTION, "run": self._clear_cache_action},
            ],
        }

    def _refresh_cache_stats(self) -> None:
        try:
            self.service.part_cache.refresh_stats()
        except (OSError, ValueError):
            LOGGER.exception("Could not load CAD cache statistics")

    def _persist_cache_enabled(self, enabled: bool) -> tuple[bool, str]:
        try:
            self.service.part_cache.set_enabled(enabled)
            return True, CAD_CACHE_ENABLED if enabled else CAD_CACHE_DISABLED
        except (OSError, ValueError):
            LOGGER.exception("Could not persist CAD cache setting")
            if self.player is not None:
                self.player.write_log(f"ERR: {CAD_CACHE_SETTING_FAILED}")
            return False, CAD_CACHE_SETTING_FAILED

    def _refresh_cache_action(self, _values: dict) -> tuple[bool, str]:
        stats = self.service.part_cache.refresh_stats()
        return True, cache_status(stats.entry_count, stats.size_bytes)

    def _clear_cache_action(self, _values: dict) -> tuple[bool, str]:
        from core.confirm import pending_title, request

        if pending_title():
            return False, CAD_CACHE_OTHER_CONFIRMATION

        def clear() -> str:
            try:
                self.service.part_cache.clear()
            except (OSError, ValueError) as exc:
                LOGGER.exception("Could not clear CAD part cache")
                if self.player is not None:
                    self.player.write_log(f"ERR: {CAD_CACHE_CLEAR_FAILED}")
                raise RuntimeError(CAD_CACHE_CLEAR_FAILED) from exc
            return CAD_CACHE_CLEARED

        message = request(
            "clear-cad-part-cache",
            CAD_CACHE_CLEAR_CONFIRM_TITLE,
            CAD_CACHE_CLEAR_CONFIRM_DETAIL,
            clear,
        )
        return not message.startswith(("Could not ask", "I cannot confirm")), message

    def handle_text(self, text: str, speak=None) -> bool:
        if not self.service.should_handle(text):
            return False
        self._process(text, speak=speak)
        return True

    def handle_voice_text(self, text: str) -> bool:
        if not self.service.is_busy or not re.search(
            r"\b(?:cancel|stop cad)\b", text or "", re.I
        ):
            return False
        threading.Thread(
            target=self.service.cancel,
            name="aura-cad-voice-cancel",
            daemon=True,
        ).start()
        return True

    def handle_tool(self, parameters: dict, player=None, speak=None) -> str:
        if player is not None:
            self.player = player
        request = str(parameters.get("request", "")).strip()
        if not request:
            return "Tell me what CAD model you want to make."
        result = self._process(request)
        if result is None:
            return "That does not look like a CAD model request."
        return result.message

    def _process(self, text: str, speak=None):
        player = self.player
        if player is not None and callable(getattr(player, "hide_quiz", None)):
            player.hide_quiz()
        if player is not None:
            hide_question = getattr(player, "hide_cad_question", None)
            if callable(hide_question):
                hide_question()
        try:
            if re.search(r"\bundo(?:\s+that)?(?:\s+change)?\b", text or "", re.I):
                from core.undo import peek, undo_last

                if peek() == "CAD model change":
                    message = undo_last()
                    if player is not None:
                        player.show_content("CAD DESIGN", message)
                    return CadResult(JobState.COMPLETE, message)
            result = self.service.process(text)
            if result is None:
                return None
            if result.state.value == "complete":
                self._finish_progress(self.service.last_job_id, completed=True)
            elif result.state.value == "failed" or (
                result.state.value == "cancelled" and not self.service.is_busy
            ):
                self._finish_progress(self.service.last_job_id)
            if result.state.value in {"clarifying", "confirming"} and player is not None:
                details = result.details
                question = str(details.get("question") or result.message)
                options = list(details.get("options") or [])
                show_question = getattr(player, "show_cad_question", None)
                if callable(show_question):
                    show_question("CAD DESIGN", question, options)
                if callable(speak):
                    ordinal_names = ("first", "second", "third", "fourth", "fifth")
                    spoken_options = ", ".join(
                        f"say {ordinal_names[index]} option for {option}"
                        for index, option in enumerate(options[:len(ordinal_names)])
                    )
                    spoken_question = question
                    if spoken_options:
                        spoken_question += f" You can tap an option or {spoken_options}."
                    try:
                        speak(spoken_question)
                    except Exception:
                        LOGGER.exception("Could not speak the CAD clarification")
            elif result.state.value == "complete":
                self._open_result(result)
            elif result.state.value == "cancelled" and player is not None:
                player.write_log(f"SYS: {result.message}")
            return result
        except BackendUnavailable as exc:
            self._finish_progress(self.service.last_job_id)
            return CadResult(JobState.FAILED, self._request_install(exc))
        except CadError as exc:
            self._finish_progress(self.service.last_job_id)
            LOGGER.exception(
                "[job=%s] CAD operation failed",
                self.service.last_job_id or "dispatch",
            )
            if player is not None:
                player.write_log(f"ERR: {exc}")
            return CadResult(JobState.FAILED, str(exc))
        except Exception:
            self._finish_progress(self.service.last_job_id)
            LOGGER.exception(
                "[job=%s] Unexpected CAD integration failure",
                self.service.last_job_id or "dispatch",
            )
            if player is not None:
                player.write_log("ERR: CAD design failed. Details were saved to the log.")
            return CadResult(
                JobState.FAILED, "CAD design failed. Please try again."
            )

    def _open_result(self, result) -> None:
        player = self.player
        if player is None:
            return
        try:
            self.viewer.open(player, result.details["stl"])
        except Exception:
            LOGGER.exception(
                "[job=%s] Could not open CAD viewer",
                result.details.get("job_id", "viewer"),
            )
            player.write_log("ERR: The model is saved, but the in-app viewer could not open it.")
        self._register_undo(result.project_id, result.version)
        player.show_content("CAD DESIGN", result.message)

    def _register_undo(self, project_id: str, version: int) -> None:
        if version <= 1:
            return

        def restore() -> str:
            with self.service._lock:
                previous = self.service._store.set_active_version(project_id, version - 1)
                self.service._script = Path(previous.script_path).read_text(encoding="utf-8")
                self.service._project_id = project_id
            self.viewer.open(self.player, previous.stl_path)
            return f"Restored CAD version {previous.version}."

        from core.undo import push_undo

        push_undo("CAD model change", restore)

    def _request_install(self, error: BackendUnavailable):
        global _INSTALL_PENDING
        from core.confirm import pending_title, request

        if pending_title():
            return "CAD support is unavailable; an install confirmation is already open."
        with _LOCK:
            if _INSTALL_PENDING:
                return "CAD support installation is already in progress."

        def install() -> str:
            global _INSTALL_PENDING
            with _LOCK:
                _INSTALL_PENDING = True
            try:
                requirements = Path(__file__).resolve().parents[2] / "requirements-cad.txt"
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
                    check=True,
                    timeout=900,
                    cwd=str(requirements.parent),
                    capture_output=True,
                    text=True,
                )
                return "CAD dependencies installed. Ask me to build the model again."
            except (OSError, subprocess.SubprocessError) as exc:
                LOGGER.exception("[job=install] CAD dependency installation failed")
                raise RuntimeError("CAD dependencies could not be installed.") from exc
            finally:
                with _LOCK:
                    _INSTALL_PENDING = False

        message = request(
            "install-cad-support",
            "Install CAD design support?",
            f"{error} Install build123d and trimesh for local CPU-only CAD work. "
            "The packages are free; pip will download them from the configured package index.",
            install,
        )
        if message.startswith(("Could not ask for confirmation", "I cannot confirm")):
            with _LOCK:
                _INSTALL_PENDING = False
        if self.player is not None:
            self.player.write_log(f"SYS: {message}")
        return message

    def _progress(self, job_id: str, message: str) -> None:
        if self.player is not None:
            self.player.write_log(f"SYS: {message}")
            show_progress = getattr(self.player, "show_cad_progress", None)
            if callable(show_progress):
                show_progress(
                    job_id, self._estimate_progress(job_id, message), message
                )

    def _finish_progress(self, job_id: str, completed: bool = False) -> None:
        if not job_id:
            return
        with self._progress_lock:
            self._progress_values.pop(job_id, None)
        if self.player is None:
            return
        finish = getattr(self.player, "finish_cad_progress", None)
        if callable(finish):
            finish(job_id, completed=completed)

    def _estimate_progress(self, job_id: str, message: str) -> int:
        normalized = message.casefold()
        if "model complete" in normalized:
            estimate = 100
        elif "saving the validated model" in normalized:
            estimate = 94
        elif "preparing stl" in normalized:
            estimate = 84
        elif "checking dimensions" in normalized:
            estimate = 72
        elif "geometry generated" in normalized:
            estimate = 62
        elif "generating model geometry" in normalized:
            estimate = 48
        elif "repairing model" in normalized:
            match = re.search(r"attempt\s+(\d+)", normalized)
            attempt = int(match.group(1)) if match else 1
            estimate = min(58, 38 + attempt * 5)
        elif "writing the cad script" in normalized or "updating the model" in normalized:
            estimate = 30
        elif "planning model parts" in normalized:
            estimate = 14
        else:
            estimate = 4
        with self._progress_lock:
            estimate = max(estimate, self._progress_values.get(job_id, 0))
            self._progress_values[job_id] = estimate
            if estimate >= 100:
                self._progress_values.pop(job_id, None)
        return estimate


def register_cad_hooks(player) -> CadHooks:
    """Attach the existing UI proxy to the shared CAD integration object."""
    global _HOOKS
    with _LOCK:
        if _HOOKS is None:
            _HOOKS = CadHooks(player)
        else:
            _HOOKS.player = player
        return _HOOKS


def get_cad_hooks() -> CadHooks:
    with _LOCK:
        if _HOOKS is None:
            return register_cad_hooks(None)
        return _HOOKS


def run_cad_action(parameters: dict, player=None, speak=None) -> str:
    return get_cad_hooks().handle_tool(parameters, player, speak)
