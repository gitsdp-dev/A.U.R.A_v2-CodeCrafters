from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from pathlib import Path


class VoskListener:
    """Background 16 kHz mono Vosk microphone listener."""

    def __init__(self, model_path: str, device: int | None = None):
        self.model_path = Path(model_path)
        self.device = device
        self._audio: queue.Queue[bytes] = queue.Queue(maxsize=48)
        self._transcripts: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._stream = None
        self._thread: threading.Thread | None = None
        self.last_transcript_latency = 0.0
        self._available = self.model_path.is_dir()
        if not self._available:
            print(
                f"[STT] Vosk model not found at {self.model_path}. "
                "Download vosk-model-small-en-us-0.15 from "
                "https://alphacephei.com/vosk/models and extract it there."
            )

    def set_speaking(self, speaking: bool) -> None:
        if speaking:
            self._speaking.set()
            while True:
                try:
                    self._audio.get_nowait()
                except queue.Empty:
                    break
        else:
            self._speaking.clear()

    def start(self) -> None:
        if not self._available:
            raise RuntimeError("The configured Vosk model directory is missing.")
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="aura-vosk-listener", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            from vosk import KaldiRecognizer, Model
            import sounddevice as sd

            model = Model(str(self.model_path))
            recognizer = KaldiRecognizer(model, 16000)

            def callback(indata, _frames, _time_info, status) -> None:
                if status:
                    print(f"[STT] Microphone status: {status}")
                if not self._speaking.is_set() and not self._stop.is_set():
                    try:
                        self._audio.put_nowait(bytes(indata))
                    except queue.Full:
                        pass

            with sd.RawInputStream(
                samplerate=16000,
                blocksize=4000,
                device=self.device,
                channels=1,
                dtype="int16",
                callback=callback,
            ) as stream:
                self._stream = stream
                print("[STT] Listening.")
                utterance_started: float | None = None
                was_speaking = False
                while not self._stop.is_set():
                    speaking = self._speaking.is_set()
                    if speaking != was_speaking:
                        recognizer.Reset()
                        utterance_started = None
                        was_speaking = speaking
                    try:
                        chunk = self._audio.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if self._speaking.is_set():
                        utterance_started = None
                        continue
                    if utterance_started is None:
                        import time

                        utterance_started = time.perf_counter()
                    if recognizer.AcceptWaveform(chunk):
                        text = json.loads(recognizer.Result()).get("text", "").strip()
                        if text:
                            import time

                            self.last_transcript_latency = (
                                time.perf_counter() - (utterance_started or time.perf_counter())
                            )
                            self._transcripts.put(("transcript", text))
                        utterance_started = None
        except Exception as exc:
            if not self._stop.is_set():
                self._transcripts.put(("error", f"Microphone/Vosk error: {exc}"))
        finally:
            self._stream = None
            self._transcripts.put(("stopped", ""))

    def listen(self) -> Iterator[str]:
        self.start()
        while not self._stop.is_set():
            kind, value = self._transcripts.get()
            if kind == "error":
                raise RuntimeError(value or "Microphone/Vosk error.")
            if kind == "stopped":
                return
            if value:
                yield value

    def wait_for_transcript(self, timeout: float = 20) -> str | None:
        while not self._stop.is_set():
            try:
                kind, value = self._transcripts.get(timeout=timeout)
            except queue.Empty:
                return None
            if kind == "error":
                raise RuntimeError(value or "Microphone/Vosk error.")
            if kind == "transcript" and value:
                return value
            if kind == "stopped":
                return None
        return None

    def discard_pending_transcripts(self) -> None:
        while True:
            try:
                self._transcripts.get_nowait()
            except queue.Empty:
                return

    def stop(self) -> None:
        self._stop.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                print(f"[STT] Error closing microphone: {exc}")
        if self._thread is not None:
            self._thread.join(timeout=3)
