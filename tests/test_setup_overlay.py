import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

import ui
from memory import config_manager
from ui import MainWindow, SetupOverlay


class SetupOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_initialise_button_submits_gemini_setup(self):
        overlay = SetupOverlay(config={})
        self.assertEqual(overlay._init_btn.text(), "▸  INITIALISE SYSTEMS")
        submitted = []
        overlay.done.connect(submitted.append)
        overlay._key_input.setText("AIza-test-key")

        overlay._init_btn.click()

        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["mode"], "gemini")
        self.assertEqual(submitted[0]["gemini_api_key"], "AIza-test-key")

        worker = overlay._model_worker
        if worker and worker.isRunning():
            worker.wait(3000)
        overlay.close()

    def test_saved_local_settings_persist_and_empty_gemini_key_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "api_keys.json"
            config_dir = Path(folder)
            initial = {
                "gemini_api_key": "old-gemini-key",
                "os_system": "windows",
                "local_llm": {
                    "mode": "fallback",
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11434",
                    "model": "qwen2.5:1.5b",
                },
            }
            config_path.write_text(json.dumps(initial), encoding="utf-8")
            with patch.object(ui, "API_FILE", config_path), \
                    patch.object(ui, "CONFIG_DIR", config_dir), \
                    patch.object(config_manager, "CONFIG_FILE", config_path), \
                    patch.object(config_manager, "CONFIG_DIR", config_dir):
                overlay = SetupOverlay(config=initial)
                self.assertEqual(overlay._init_btn.text(), "▸  SAVE CHANGES")
                overlay._mode_pills._buttons_by_value["local"].click()
                overlay._model.setEditText("llama3.2:1b")
                overlay._key_input.clear()
                saved = []
                fake_window = SimpleNamespace(
                    _ready=False,
                    on_brain_change=None,
                    _overlay=SimpleNamespace(begin_initialising=saved.append),
                    _apply_state=lambda _state: None,
                    _log=SimpleNamespace(append_log=lambda _message: None),
                )

                overlay.done.connect(
                    lambda settings:
                        MainWindow._on_setup_done(fake_window, settings)
                )
                overlay._init_btn.click()

                persisted = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn("gemini_api_key", persisted)
                self.assertEqual(persisted["local_llm"]["mode"], "local")
                self.assertEqual(persisted["local_llm"]["model"], "llama3.2:1b")

                reopened = SetupOverlay(config=persisted)
                self.assertEqual(reopened._selected_model(), "llama3.2:1b")
                self.assertEqual(reopened._key_input.text(), "")
                self.assertEqual(reopened._init_btn.text(), "▸  SAVE CHANGES")

                worker = overlay._model_worker
                if worker and worker.isRunning():
                    worker.wait(3000)
                worker = reopened._model_worker
                if worker and worker.isRunning():
                    worker.wait(3000)
                overlay.close()
                reopened.close()

    def test_close_button_dismisses_without_submitting(self):
        overlay = SetupOverlay(config={})
        submitted = []
        dismissed = []
        overlay.done.connect(submitted.append)
        overlay.finished.connect(lambda: dismissed.append(True))

        overlay._close_btn.click()

        self.assertEqual(dismissed, [True])
        self.assertEqual(submitted, [])
        worker = overlay._model_worker
        if worker and worker.isRunning():
            worker.wait(3000)
        overlay.close()

    def test_local_initialisation_is_saved_and_does_not_prompt_next_boot(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "api_keys.json"
            config_dir = Path(folder)
            with patch.object(ui, "API_FILE", config_path), \
                    patch.object(ui, "CONFIG_DIR", config_dir), \
                    patch.object(config_manager, "CONFIG_FILE", config_path), \
                    patch.object(config_manager, "CONFIG_DIR", config_dir):
                overlay = SetupOverlay(config={})
                saved = []
                fake_window = SimpleNamespace(
                    _ready=False,
                    on_brain_change=None,
                    _overlay=SimpleNamespace(
                        begin_initialising=lambda settings: saved.append(settings)
                    ),
                    _apply_state=lambda _state: None,
                    _log=SimpleNamespace(append_log=lambda _message: None),
                )
                overlay.done.connect(
                    lambda settings:
                        MainWindow._on_setup_done(fake_window, settings)
                )
                overlay._mode_pills._buttons_by_value["local"].click()
                self.assertTrue(overlay._local_voice_override.isHidden())
                overlay._mode_pills._buttons_by_value["fallback"].click()
                self.assertFalse(overlay._local_voice_override.isHidden())
                overlay._local_voice_override.setChecked(True)
                overlay._mode_pills._buttons_by_value["local"].click()
                self.assertFalse(overlay._vosk_model.isHidden())
                self.assertFalse(hasattr(overlay, "_stt"))
                self.assertFalse(hasattr(overlay, "_stt_model"))
                overlay._vosk_model.setCurrentIndex(1)
                overlay._tts.setCurrentIndex(overlay._tts.findData("elevenlabs"))
                self.assertTrue(overlay._tts_pills._buttons_by_value["elevenlabs"].isEnabled())
                self.assertFalse(overlay._elevenlabs_key_input.isHidden())
                overlay._elevenlabs_key_input.setText("test-elevenlabs-key")
                overlay._init_btn.click()

                self.assertEqual(len(saved), 1)
                self.assertTrue(fake_window._ready)
                self.assertTrue(MainWindow._check_config(fake_window))
                persisted = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn("gemini_api_key", persisted)
                self.assertEqual(persisted["local_llm"]["mode"], "local")
                self.assertEqual(persisted["local_llm"]["stt_engine"], "vosk")
                self.assertNotIn("stt_model", persisted["local_llm"])
                self.assertEqual(
                    persisted["local_llm"]["vosk_model"],
                    "vosk-model-small-en-in-0.4",
                )
                self.assertEqual(
                    persisted["local_llm"]["tts_engine"], "elevenlabs"
                )
                self.assertEqual(
                    persisted["local_llm"]["tts_voice_by_engine"]["elevenlabs"],
                    persisted["local_llm"]["tts_voice"],
                )
                self.assertEqual(
                    persisted["elevenlabs_api_key"], "test-elevenlabs-key"
                )
                self.assertTrue(persisted["local_llm"]["local_voice_override"])

                worker = overlay._model_worker
                if worker and worker.isRunning():
                    worker.wait(3000)
                overlay.close()

    def test_online_tts_choices_are_available_for_local_mode(self):
        overlay = SetupOverlay(config={"local_llm": {
            "model": "qwen2.5:1.5b",
            "tts_engine": "kokoro",
            "tts_voice": "af_heart",
            "voice_by_model": {"qwen2.5:1.5b": "af_heart"},
        }})
        overlay._mode_pills._buttons_by_value["local"].click()
        self.assertTrue(overlay._tts_pills._buttons_by_value["edgetts"].isEnabled())
        self.assertTrue(overlay._tts_pills._buttons_by_value["elevenlabs"].isEnabled())

        overlay._tts_pills._buttons_by_value["edgetts"].click()
        self.assertEqual(overlay._tts.currentData(), "edgetts")
        self.assertEqual(overlay._selected_voice(), "en-US-GuyNeural")
        self.assertTrue(overlay._elevenlabs_key_input.isHidden())

        overlay._tts_pills._buttons_by_value["elevenlabs"].click()
        self.assertEqual(overlay._tts.currentData(), "elevenlabs")
        self.assertFalse(overlay._elevenlabs_key_input.isHidden())
        overlay._tts_pills._buttons_by_value["edgetts"].click()
        self.assertEqual(overlay._selected_voice(), "en-US-GuyNeural")

        worker = overlay._model_worker
        if worker and worker.isRunning():
            worker.wait(3000)
        overlay.close()


if __name__ == "__main__":
    unittest.main()
