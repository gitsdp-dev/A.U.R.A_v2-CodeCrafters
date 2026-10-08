from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aura.agent import tools
from aura.llm.provider import get_provider, normalize_chunk
from aura.voice.pipeline import SentenceChunker
from aura.voice.tts import KokoroSpeaker


class VoiceAgentTests(unittest.TestCase):
    def test_sentence_chunker_handles_abbreviations_decimals_and_cap(self):
        chunker = SentenceChunker(max_length=25)
        result = chunker.feed("Dr. Smith measured 3.5 cm. Ready? ")
        self.assertEqual(result, ["Dr. Smith measured 3.5 cm.", "Ready?"])
        incremental = SentenceChunker()
        self.assertEqual(incremental.feed("Value 3."), [])
        self.assertEqual(
            incremental.feed("5. Dr. Jones agrees. "),
            ["Value 3.5.", "Dr. Jones agrees."],
        )
        long_result = chunker.feed("This sentence is longer than twenty-five characters")
        self.assertTrue(long_result)
        self.assertTrue(all(len(item) <= 25 for item in long_result))

    def test_path_sandbox_blocks_parent_and_symlink_traversal(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            tools.configure_tools(folder, lambda _question: True)
            with self.assertRaises(ValueError):
                tools._within_workspace("..\\outside.txt")
            link = Path(folder) / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks are unavailable in this environment.")
            with self.assertRaises(ValueError):
                tools._within_workspace("escape\\secret.txt")

    def test_provider_factory_selects_explicit_and_auto_falls_back(self):
        config = {
            "llm": {
                "provider": "auto",
                "ollama": {"model": "kept-model"},
                "lmstudio": {"model": "auto"},
            }
        }
        with patch("aura.llm.ollama_provider.OllamaProvider.is_available", return_value=False), \
             patch("aura.llm.lmstudio_provider.LMStudioProvider.is_available", return_value=True):
            selected = get_provider(config)
            self.assertEqual(selected.provider_name, "lmstudio")
        with patch("aura.llm.ollama_provider.OllamaProvider.is_available", return_value=True), \
             patch("aura.llm.lmstudio_provider.LMStudioProvider.is_available", return_value=True):
            selected = get_provider(config, "ollama")
            self.assertEqual(selected.provider_name, "ollama")
            self.assertEqual(selected.model_name, "kept-model")

    def test_stream_normalization_for_both_backends(self):
        self.assertEqual(
            normalize_chunk("ollama", {"message": {"content": "hello"}}), "hello"
        )
        self.assertEqual(
            normalize_chunk(
                "lmstudio",
                {"choices": [{"delta": {"content": "there"}}]},
            ),
            "there",
        )
        self.assertEqual(
            normalize_chunk("lmstudio", {"choices": [{"delta": {"content": None}}]}),
            "",
        )

    def test_kokoro_synthesis_sentence_when_model_assets_are_available(self):
        root = Path(__file__).resolve().parents[1] / "models"
        model = root / "kokoro-v1.0.onnx"
        voices = root / "voices-v1.0.bin"
        if not model.is_file() or not voices.is_file():
            self.skipTest("Run scripts/download_models.py to enable the offline TTS test.")
        speaker = KokoroSpeaker(str(model), str(voices), voice="bm_george", speed=1.1)
        try:
            output, sample_rate = speaker._load_model().create(
                "Good morning, sir.", voice="bm_george", speed=1.1, lang="en-gb"
            )
            self.assertGreater(len(output), 0)
            self.assertGreater(sample_rate, 0)
        finally:
            speaker.close()


if __name__ == "__main__":
    unittest.main()
