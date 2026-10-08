import json
import asyncio
import sys
import tempfile
import threading
import types
import io
import zipfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import Mock, patch

from core import llm_client
from core.local_brain import (
    LocalBrain, _request_needs_tools, _validate_arguments,
    should_fallback_to_local,
)
from core.stt import VoskSTT
from core.tts import EdgeTTSEngine, ElevenLabsTTSEngine, KokoroONNXTTSEngine
from core import vosk_models
from memory import config_manager


class LocalAIConfigTests(unittest.TestCase):
    def test_tool_intent_gate_skips_schemas_for_chat_and_keeps_voice_actions(self):
        self.assertFalse(_request_needs_tools("Hello, how are you?"))
        self.assertFalse(_request_needs_tools("Tell me a short joke."))
        self.assertTrue(_request_needs_tools("Open the calculator"))
        self.assertTrue(_request_needs_tools("What time is it?"))

    def test_local_chat_does_not_send_tool_schemas_for_chitchat(self):
        async def execute(_name, _args):
            self.fail("Chit-chat should not call a tool.")

        async def say(_text):
            return None

        brain = LocalBrain(
            "short system prompt",
            [{
                "name": "open_application",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"name": {"type": "STRING"}},
                    "required": ["name"],
                },
            }],
            execute,
            say,
            lambda _message: None,
        )
        events = iter([
            {"type": "sentence", "text": "I am well, sir."},
            {"type": "done", "content": "I am well, sir.", "tool_calls": []},
        ])
        with patch("core.local_brain.call_llm_stream", return_value=events) as stream:
            reply = asyncio.run(brain.ask("Hello, how are you?"))
        self.assertEqual(reply, "I am well, sir.")
        self.assertEqual(stream.call_args.kwargs["tools"], [])

    def test_local_turn_releases_microphone_and_marks_audio_complete(self):
        from main import AuraLive

        class Brain:
            async def ask(self, _text):
                return "Ready, sir."

        aura = object.__new__(AuraLive)
        aura._local_turn_lock = asyncio.Lock()
        aura._local_brain = Brain()
        aura._local_turn_active = threading.Event()
        aura._turn_done_event = asyncio.Event()
        aura._local_turn_task = None
        aura._local_turn_started_at = 0.0
        aura._local_first_audio_pending = False
        aura._local_tts_timing_reported = False
        aura._interrupted = False
        aura._is_speaking = False
        aura._session_log = []
        aura._asst_name = "A.U.R.A."
        aura.ui = Mock()
        aura.ui.muted = False

        asyncio.run(aura._local_turn("Hello"))

        self.assertTrue(aura._turn_done_event.is_set())
        self.assertFalse(aura._local_turn_active.is_set())
        self.assertIsNone(aura._local_turn_task)

    def test_wrapped_gemini_keepalive_timeout_triggers_local_fallback(self):
        class WrappedFailure(Exception):
            def __init__(self):
                super().__init__("unhandled errors in a TaskGroup")
                self.exceptions = (TimeoutError("keepalive ping timeout"),)

        self.assertTrue(
            should_fallback_to_local("fallback", WrappedFailure())
        )
        self.assertFalse(
            should_fallback_to_local("local", WrappedFailure())
        )
        self.assertTrue(
            should_fallback_to_local(
                "fallback",
                ExceptionGroup(
                    "unhandled errors in a TaskGroup",
                    [TimeoutError("timed out while closing connection")],
                ),
            )
        )

    def test_initialization_modes_require_the_right_configuration(self):
        local_only = {
            "os_system": "windows",
            "local_llm": {"mode": "local", "model": "qwen2.5:3b"},
        }
        self.assertTrue(config_manager.is_setup_configured(local_only))
        self.assertFalse(config_manager.is_setup_configured({
            **local_only, "local_llm": {"mode": "local", "model": ""}
        }))
        self.assertTrue(config_manager.is_setup_configured({
            "os_system": "windows",
            "gemini_api_key": "gemini-secret",
            "local_llm": {"mode": "fallback", "model": "qwen2.5:3b"},
        }))
        self.assertFalse(config_manager.is_setup_configured({
            "os_system": "windows",
            "local_llm": {"mode": "fallback", "model": "qwen2.5:3b"},
        }))
        self.assertTrue(config_manager.is_setup_configured({
            "os_system": "windows", "gemini_api_key": "gemini-secret"
        }))

    def test_backward_compatible_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api_keys.json"
            path.write_text(json.dumps({
                "gemini_api_key": "kept",
                "os_system": "windows",
                "assistant_name": "AURA",
            }), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", path), \
                    patch.object(config_manager, "CONFIG_DIR", Path(folder)):
                self.assertEqual(config_manager.get_local_llm_config()["mode"], "gemini")
                config_manager.save_local_llm_config({
                    "mode": "local",
                    "provider": "ollama",
                    "model": "qwen2.5:3b",
                    "voice_by_model": {"qwen2.5:3b": "af_heart"},
                    "tts_voice_by_engine": {"kokoro": "af_heart"},
                })
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["gemini_api_key"], "kept")
            self.assertEqual(saved["assistant_name"], "AURA")
            self.assertEqual(saved["local_llm"]["voice_by_model"]["qwen2.5:3b"], "af_heart")
            self.assertEqual(
                saved["local_llm"]["tts_voice_by_engine"]["kokoro"],
                "af_heart",
            )
            self.assertEqual(saved["local_llm"]["stt_engine"], "vosk")
            self.assertNotIn("stt_model", saved["local_llm"])
            self.assertEqual(
                saved["local_llm"]["vosk_model"],
                "vosk-model-small-en-us-0.15",
            )

    def test_legacy_whisper_settings_migrate_to_vosk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api_keys.json"
            path.write_text(json.dumps({
                "local_llm": {
                    "mode": "local",
                    "stt_engine": "whisper",
                    "stt_model": "base.en",
                }
            }), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", path), \
                    patch.object(config_manager, "CONFIG_DIR", Path(folder)):
                config = config_manager.get_local_llm_config()
                self.assertEqual(config["stt_engine"], "vosk")
                self.assertNotIn("stt_model", config)
                self.assertEqual(
                    config["vosk_model"],
                    "vosk-model-small-en-us-0.15",
                )
                config_manager.save_local_llm_config(config)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["local_llm"]["stt_engine"], "vosk")
            self.assertNotIn("stt_model", saved["local_llm"])

    def test_local_voice_override_defaults_off_and_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api_keys.json"
            path.write_text(json.dumps({"local_llm": {"mode": "fallback"}}), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", path), \
                    patch.object(config_manager, "CONFIG_DIR", Path(folder)):
                self.assertFalse(config_manager.get_local_llm_config()["local_voice_override"])
                config_manager.save_local_llm_config({"local_voice_override": True})
                self.assertTrue(config_manager.get_local_llm_config()["local_voice_override"])

    def test_vosk_finalizes_short_utterance(self):
        recognizer = Mock()
        recognizer.FinalResult.return_value = json.dumps({"text": "hey aura"})
        vosk = types.SimpleNamespace(
            Model=Mock(return_value=object()),
            KaldiRecognizer=Mock(return_value=recognizer),
        )
        with patch.dict(sys.modules, {"vosk": vosk}):
            stt = VoskSTT()
        self.assertEqual(stt.finalize(), "hey aura")
        recognizer.Reset.assert_called_once_with()

    def test_vosk_stream_feed_skips_partial_transcription_work(self):
        recognizer = Mock()
        recognizer.AcceptWaveform.side_effect = [False, True]
        recognizer.Result.return_value = json.dumps({"text": "hello aura"})
        vosk = types.SimpleNamespace(
            Model=Mock(return_value=object()),
            KaldiRecognizer=Mock(return_value=recognizer),
        )
        with patch.dict(sys.modules, {"vosk": vosk}):
            stt = VoskSTT()

        self.assertEqual(stt.feed_audio(b"first audio block"), ("", False))
        self.assertEqual(
            stt.feed_audio(b"next audio block"), ("hello aura", True)
        )
        recognizer.PartialResult.assert_not_called()
        self.assertEqual(recognizer.AcceptWaveform.call_count, 2)

    def test_vosk_model_is_downloaded_once_and_cached(self):
        model_name = "vosk-model-small-en-us-0.15"
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr(f"{model_name}/am/final.mdl", b"model")
            archive.writestr(f"{model_name}/conf/mfcc.conf", b"config")
        archive_bytes = archive_buffer.getvalue()

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(vosk_models, "VOSK_MODELS_DIR", Path(folder)):
            with patch.object(
                vosk_models.urllib.request, "urlopen",
                return_value=io.BytesIO(archive_bytes),
            ) as open_url:
                model_path = Path(vosk_models.ensure_vosk_model(model_name))
                self.assertTrue((model_path / "am" / "final.mdl").is_file())
                self.assertTrue((model_path / "conf" / "mfcc.conf").is_file())
                self.assertEqual(
                    vosk_models.ensure_vosk_model(model_name), str(model_path)
                )
            open_url.assert_called_once()

    def test_vosk_catalog_has_multilingual_cpu_models(self):
        model_ids = {model_id for _label, model_id in vosk_models.VOSK_MODEL_OPTIONS}
        self.assertGreaterEqual(len(model_ids), 30)
        self.assertIn("vosk-model-small-en-in-0.4", model_ids)
        self.assertIn("vosk-model-small-hi-0.22", model_ids)
        self.assertIn("vosk-model-en-us-0.22-lgraph", model_ids)
        self.assertEqual(
            set(vosk_models._MODEL_URLS), model_ids
        )

    def test_kokoro_onnx_british_voice_synthesizes_audio(self):
        root = Path(__file__).resolve().parents[1] / "models"
        model_path = root / "kokoro-v1.0.onnx"
        voices_path = root / "voices-v1.0.bin"
        if not model_path.is_file() or not voices_path.is_file():
            self.skipTest("Kokoro ONNX model files are not installed.")
        if find_spec("kokoro_onnx") is None:
            self.skipTest("kokoro-onnx is not installed.")
        engine = KokoroONNXTTSEngine(
            voice="bm_george",
            speed=1.1,
            lang="en-gb",
            model_path=str(model_path),
            voices_path=str(voices_path),
        )
        audio = next(engine.audio_chunks("Good morning, sir."))
        self.assertGreater(audio.size, 0)

    def test_edge_tts_streams_provider_audio(self):
        class FakeCommunicate:
            def __init__(self, text, voice):
                self.text = text
                self.voice = voice

            async def stream(self):
                yield {"type": "audio", "data": b"audio-"}
                yield {"type": "WordBoundary", "data": b"ignored"}
                yield {"type": "audio", "data": b"bytes"}

        with patch.dict(sys.modules, {
            "edge_tts": types.SimpleNamespace(Communicate=FakeCommunicate)
        }):
            engine = EdgeTTSEngine("en-US-GuyNeural")
            audio = asyncio.run(engine.synthesise("hello"))
        self.assertEqual(audio, b"audio-bytes")

    def test_elevenlabs_tts_uses_selected_voice_and_checks_http_response(self):
        response = Mock()
        response.content = b"mp3-audio"
        with patch("requests.post", return_value=response) as post:
            audio = ElevenLabsTTSEngine("test-key", "voice-123").synthesise("hello")
        self.assertEqual(audio, b"mp3-audio")
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(
            post.call_args.args[0],
            "https://api.elevenlabs.io/v1/text-to-speech/voice-123",
        )
        self.assertEqual(post.call_args.kwargs["headers"]["xi-api-key"], "test-key")

    def test_local_stream_applies_requested_token_limit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_lines.return_value = [
            json.dumps({"message": {"content": "Ready."}, "done": True}).encode()
        ]
        with patch("core.llm_client.get_llm_provider", return_value="ollama"), \
                patch("core.llm_client.get_llm_settings",
                      return_value=("http://127.0.0.1:11434", "qwen2.5:1.5b")), \
                patch("core.llm_client.requests.post", return_value=response) as post:
            list(llm_client.call_llm_stream([], max_tokens=96))

        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], 96)

    def test_tool_schema_conversion_normalizes_nested_types(self):
        converted = llm_client.gemini_tools_to_openai([{
            "name": "sample",
            "description": "Sample action",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING"},
                    "count": {"type": "INTEGER"},
                },
                "required": ["query"],
            },
        }])
        self.assertEqual(converted[0]["type"], "function")
        self.assertEqual(converted[0]["function"]["parameters"]["type"], "object")
        self.assertEqual(
            converted[0]["function"]["parameters"]["properties"]["query"]["type"],
            "string",
        )

    @patch("core.llm_client.requests.get")
    def test_provider_model_list_parsing(self, get):
        get.return_value = Mock(
            json=lambda: {"models": [{"name": "qwen2.5:3b"}, {"name": "llama3.2:3b"}]},
            raise_for_status=Mock(),
        )
        self.assertEqual(
            llm_client.list_models("ollama", "http://127.0.0.1:11434"),
            ["qwen2.5:3b", "llama3.2:3b"],
        )
        self.assertTrue(get.call_args.args[0].endswith("/api/tags"))

    @patch("core.llm_client.requests.get")
    def test_lm_studio_model_list_uses_v1_endpoint(self, get):
        get.return_value = Mock(
            json=lambda: {"data": [{"id": "local-model"}]},
            raise_for_status=Mock(),
        )
        self.assertEqual(
            llm_client.list_models("lmstudio", "http://127.0.0.1:1234/v1"),
            ["local-model"],
        )
        self.assertEqual(
            get.call_args.args[0], "http://127.0.0.1:1234/v1/models"
        )

    def test_fallback_state_selection(self):
        self.assertTrue(should_fallback_to_local("fallback", "Gemini quota exhausted"))
        self.assertTrue(should_fallback_to_local("fallback", "API key not valid"))
        self.assertFalse(should_fallback_to_local("gemini", "network connection failed"))
        self.assertFalse(should_fallback_to_local("fallback", "malformed tool schema"))

    def test_tool_arguments_are_validated_before_dispatch(self):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        self.assertTrue(_validate_arguments({"count": 3}, schema))
        self.assertFalse(_validate_arguments({"count": "three"}, schema))
        self.assertFalse(_validate_arguments({}, schema))

    def test_local_brain_streams_and_executes_validated_tool(self):
        sentences = []
        dispatched = []
        timings = []

        async def execute(name, args):
            dispatched.append((name, args))
            return "completed"

        async def on_sentence(text):
            sentences.append(text)

        brain = LocalBrain(
            "system",
            [{
                "name": "lookup",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"query": {"type": "STRING"}},
                    "required": ["query"],
                },
            }],
            execute,
            on_sentence,
            lambda _message: None,
            on_timing=timings.append,
        )
        stream_calls = [
            iter([{
                "type": "done",
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "function": {
                        "name": "lookup",
                        "arguments": {"query": "weather"},
                    },
                }],
            }]),
            iter([
                {"type": "sentence", "text": "It is clear."},
                {"type": "done", "content": "It is clear.", "tool_calls": []},
            ]),
        ]
        with patch("core.local_brain.call_llm_stream",
                   side_effect=lambda *_args, **_kwargs: stream_calls.pop(0)) as stream:
            reply = asyncio.run(brain.ask("Check the weather"))
        self.assertEqual(reply, "It is clear.")
        self.assertEqual(stream.call_args.kwargs["max_tokens"], 96)
        self.assertEqual(dispatched, [("lookup", {"query": "weather"})])
        self.assertEqual(sentences, ["It is clear."])
        self.assertEqual(len(timings), 1)
        self.assertIn("Local LLM first sentence", timings[0])


if __name__ == "__main__":
    unittest.main()
