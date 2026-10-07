"""
Speech-to-Text engines for A.U.R.A v2

Vosk     – offline streaming transcription
"""
import json

class VoskSTT:
    """Streaming transcription using Vosk."""

    def __init__(self, model_path: str | None = None, language: str = "en-us"):
        from vosk import Model, KaldiRecognizer
        print("[STT] Loading Vosk model…")
        if model_path:
            model = Model(model_path)
        else:
            lang  = language.strip().lower() if language and language.strip().lower() != "auto" else "en-us"
            model = Model(lang=lang)
        self._rec = KaldiRecognizer(model, 16000)
        print("[STT] Vosk ready.")

    def process_chunk(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Feed raw int16 LE PCM bytes. Returns (text, is_final)."""
        text, is_final = self.feed_audio(audio_bytes)
        if is_final:
            return text, True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False

    def feed_audio(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Feed audio without partial decoding; return completed text and status."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        return "", False

    def finalize(self) -> str:
        """Flush the last short utterance and reset the recognizer for the next one."""
        result = json.loads(self._rec.FinalResult())
        self._rec.Reset()
        return result.get("text", "")
