from __future__ import annotations

import queue
import threading
from pathlib import Path

_MODEL_CACHE = {}
_MODEL_CACHE_LOCK = threading.Lock()


class KokoroSpeaker:
    """Lazy Kokoro ONNX voice with independent synthesis/playback workers."""

    def __init__(
        self,
        model_path: str,
        voices_path: str,
        voice: str = "bm_george",
        speed: float = 1.1,
        lang: str = "en-gb",
        microphone_state=None,
    ):
        self.model_path = Path(model_path)
        self.voices_path = Path(voices_path)
        self.voice = voice
        self.speed = speed
        self.lang = lang
        self._model_key = (self.model_path, self.voices_path)
        self._microphone_state = microphone_state
        self._synthesis: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._playback: queue.Queue[tuple[object, int, int] | None] = queue.Queue()
        self._model = None
        self._lock = threading.Lock()
        self._busy = 0
        self._generation = 0
        self._busy_lock = threading.Lock()
        self.first_audio_callback = None
        self._synth_thread = threading.Thread(
            target=self._synth_worker, name="aura-kokoro-synth", daemon=True
        )
        self._play_thread = threading.Thread(
            target=self._play_worker, name="aura-audio-playback", daemon=True
        )
        self._synth_thread.start()
        self._play_thread.start()

    def _load_model(self):
        with self._lock:
            if self._model is None:
                if not self.model_path.is_file() or not self.voices_path.is_file():
                    raise FileNotFoundError(
                        "Kokoro files are missing. Run scripts/download_models.py "
                        "or configure voice.model_path and voice.voices_path."
                    )
                with _MODEL_CACHE_LOCK:
                    self._model = _MODEL_CACHE.get(self._model_key)
                    if self._model is None:
                        from kokoro_onnx import Kokoro

                        self._model = Kokoro(
                            str(self.model_path), str(self.voices_path)
                        )
                        _MODEL_CACHE[self._model_key] = self._model
        return self._model

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._busy_lock:
            self._busy += 1
            generation = self._generation
        if self._microphone_state:
            self._microphone_state(True)
        self._synthesis.put((text, generation))

    def _synth_worker(self) -> None:
        while True:
            request = self._synthesis.get()
            if request is None:
                return
            text, generation = request
            try:
                samples, sample_rate = self._load_model().create(
                    text, voice=self.voice, speed=self.speed, lang=self.lang
                )
                with self._busy_lock:
                    current = generation == self._generation
                if current:
                    self._playback.put((samples, sample_rate, generation))
                else:
                    self._mark_finished(generation)
            except Exception as exc:
                print(f"[TTS] Kokoro synthesis failed: {exc}")
                with self._busy_lock:
                    if generation == self._generation and self._busy <= 1:
                        self.first_audio_callback = None
                self._mark_finished(generation)

    def _play_worker(self) -> None:
        while True:
            clip = self._playback.get()
            if clip is None:
                return
            try:
                import sounddevice as sd

                samples, sample_rate, generation = clip
                with self._busy_lock:
                    current = generation == self._generation
                if not current:
                    continue
                callback, self.first_audio_callback = self.first_audio_callback, None
                if callback:
                    callback()
                sd.play(samples, sample_rate)
                sd.wait()
            except Exception as exc:
                print(f"[TTS] Audio playback failed: {exc}")
            finally:
                self._mark_finished(clip[2])

    def _mark_finished(self, generation: int) -> None:
        with self._busy_lock:
            if generation == self._generation:
                self._busy = max(0, self._busy - 1)
            speaking = self._busy > 0
        if self._microphone_state:
            self._microphone_state(speaking)

    def stop(self) -> None:
        with self._busy_lock:
            self._generation += 1
            self._busy = 0
        self.first_audio_callback = None
        if self._microphone_state:
            self._microphone_state(False)
        while True:
            try:
                request = self._synthesis.get_nowait()
                if request is not None:
                    self._mark_finished(request[1])
            except queue.Empty:
                break
        while True:
            try:
                clip = self._playback.get_nowait()
                if clip is not None:
                    self._mark_finished(clip[2])
            except queue.Empty:
                break
        try:
            import sounddevice as sd

            sd.stop()
        except Exception as exc:
            print(f"[TTS] Error stopping playback: {exc}")

    def preload(self) -> None:
        self._load_model()

    def is_speaking(self) -> bool:
        with self._busy_lock:
            return self._busy > 0

    def close(self) -> None:
        self.stop()
        self._synthesis.put(None)
        self._playback.put(None)
        self._synth_thread.join(timeout=3)
        self._play_thread.join(timeout=3)
