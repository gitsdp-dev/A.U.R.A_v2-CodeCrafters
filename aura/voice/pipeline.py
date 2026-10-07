from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path

from aura.llm.provider import LLMProvider, get_provider
from aura.voice.stt import VoskListener
from aura.voice.tts import KokoroSpeaker

VOICE_PROMPT = (
    "Reply concisely and conversationally. Use no markdown, code blocks, or emojis. "
    "Be a polite British butler and address the user as sir."
)
_ABBREVIATIONS = {
    "dr.", "mr.", "mrs.", "ms.", "prof.", "sr.", "jr.", "st.", "vs.",
    "etc.", "e.g.", "i.e.", "a.m.", "p.m.",
}
_MARKDOWN = re.compile(r"[*_`#>\[\]{}|~]")


class SentenceChunker:
    def __init__(self, max_length: int = 200):
        self.max_length = max_length
        self.buffer = ""

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        sentences: list[str] = []
        while self.buffer:
            boundary = self._sentence_boundary()
            if boundary:
                index, width = boundary
                value = self.buffer[:index].strip()
                self.buffer = self.buffer[index + width :]
                if value:
                    sentences.append(value)
                continue
            if len(self.buffer) > self.max_length:
                split = self.buffer.rfind(" ", 0, self.max_length + 1)
                if split <= 0:
                    split = self.max_length
                sentences.append(self.buffer[:split].strip())
                self.buffer = self.buffer[split:].lstrip()
                continue
            break
        return sentences

    def _sentence_boundary(self) -> tuple[int, int] | None:
        for index, char in enumerate(self.buffer):
            if char == "\n":
                return index, 1
            if char not in ".!?":
                continue
            if char == ".":
                if index + 1 == len(self.buffer):
                    continue
                before = self.buffer[: index + 1].lower()
                token = before.rsplit(None, 1)[-1]
                if token in _ABBREVIATIONS:
                    continue
                if index > 0 and index + 1 < len(self.buffer):
                    if self.buffer[index - 1].isdigit() and self.buffer[index + 1].isdigit():
                        continue
            if index + 1 == len(self.buffer) or self.buffer[index + 1].isspace():
                return index + 1, 1
        return None

    def flush(self) -> str:
        remaining, self.buffer = self.buffer.strip(), ""
        return remaining


def strip_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = _MARKDOWN.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def is_action_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b(open|launch|start|create|make|write|save|read|list|show|"
            r"check|find|look up|search|tell me the time|what time|"
            r"system info|system information|date|time)\b",
            text,
            re.IGNORECASE,
        )
    )


class ShortTermMemory:
    def __init__(self, history_length: int):
        self.history_length = max(1, history_length)
        self.messages: list[dict[str, str]] = []

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        self.messages = self.messages[-self.history_length * 2 :]


class VoicePipeline:
    def __init__(self, config: dict, provider_override: str | None = None):
        self.config = config
        self.provider: LLMProvider | None = None
        self.provider_error: str | None = None
        try:
            self.provider = get_provider(config, provider_override)
        except RuntimeError as exc:
            self.provider_error = str(exc)
        voice = config.get("voice", {})
        root = Path(config.get("_base_dir", Path.cwd()))
        quantized = voice.get("quantized_model_path", "models/kokoro-v1.0.int8.onnx")
        quantized_path = _resolve(root, quantized)
        model_path = (
            quantized_path
            if quantized_path.is_file()
            else _resolve(root, voice.get("model_path", "models/kokoro-v1.0.onnx"))
        )
        self.stt = VoskListener(
            str(_resolve(root, voice.get("vosk_model_path", "models/vosk-model-small-en-us-0.15"))),
            voice.get("mic_device_index"),
        )
        self.tts = KokoroSpeaker(
            str(model_path),
            str(_resolve(root, voice.get("voices_path", "models/voices-v1.0.bin"))),
            voice.get("voice", "bm_george"),
            float(voice.get("speed", 1.1)),
            voice.get("lang", "en-gb"),
            microphone_state=self.stt.set_speaking,
        )
        agent_config = config.get("agent", {})
        self.memory = ShortTermMemory(int(agent_config.get("history_length", 6)))
        self.wake_word = bool(voice.get("wake_word", False))
        self.wake_text = str(voice.get("wake_word_text", "hey aura")).lower()
        self.agent = None

    def _speak(self, text: str) -> None:
        clean = strip_for_speech(text)
        if not clean:
            return
        print(f"A.U.R.A.: {clean}")
        try:
            self.tts.speak(clean)
        except Exception as exc:
            print(f"[TTS] Speech unavailable; response printed instead: {exc}")

    def _speak_then_wait(self, text: str, timeout: float = 30) -> None:
        self._speak(text)
        deadline = time.monotonic() + timeout
        while self.tts.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _spoken_stream(self, messages: list[dict[str, str]]) -> str:
        assert self.provider is not None
        chunks = SentenceChunker()
        answer: list[str] = []
        started = time.perf_counter()
        first_token = True
        self.tts.first_audio_callback = lambda: print(
            f"[Timing] provider={self.provider.provider_name} "
            f"model={self.provider.model_name} first_audio="
            f"{time.perf_counter() - started:.3f}s"
        )
        for piece in self.provider.stream_chat(messages, VOICE_PROMPT):
            if first_token:
                first_token = False
                print(
                    f"[Timing] provider={self.provider.provider_name} "
                    f"model={self.provider.model_name} first_llm_token="
                    f"{time.perf_counter() - started:.3f}s"
                )
            answer.append(piece)
            for sentence in chunks.feed(piece):
                self._speak(sentence)
        tail = chunks.flush()
        if tail:
            self._speak(tail)
        return "".join(answer).strip()

    def _ensure_agent(self) -> bool:
        assert self.provider is not None
        if self.agent is not None:
            return True
        supports_tools = getattr(self.provider, "supports_tools", None)
        tool_policy = self.config.get("agent", {}).get("tool_calling", "auto")
        supported = callable(supports_tools) and supports_tools()
        if tool_policy is True:
            supported = True
        elif tool_policy is False:
            supported = False
        if not supported:
            warning = (
                f"Tool support could not be confirmed for {self.provider.provider_name} "
                f"model {self.provider.model_name}; agent actions are disabled. "
                "Choose a model with tool-calling support."
            )
            print(f"[Agent] {warning}")
            self._speak(warning)
            self.agent = False
            return False
        try:
            from aura.agent.agent import create_agent
        except ImportError as exc:
            warning = f"Agent dependencies are unavailable; using chat only: {exc}"
            print(f"[Agent] {warning}")
            self._speak(warning)
            self.agent = False
            return False

        self.agent = create_agent(self.provider, self.config, self._confirm)
        return True

    def _confirm(self, question: str) -> bool:
        self.stt.discard_pending_transcripts()
        self._speak(question + " Please say yes or no.")
        while self.tts.is_speaking():
            time.sleep(0.05)
        answer = self.stt.wait_for_transcript(timeout=20)
        accepted = bool(answer and answer.strip().lower() in {"yes", "yes please", "yeah", "yep"})
        print(f"[Agent] Confirmation {'granted' if accepted else 'not granted'}.")
        return accepted

    def _run_agent(self, request: str) -> str:
        assert self.provider is not None
        if not self._ensure_agent() or self.agent is False:
            return self._spoken_stream([*self.memory.messages, {"role": "user", "content": request}])
        started = time.perf_counter()
        assert self.provider is not None
        self.tts.first_audio_callback = lambda: print(
            f"[Timing] provider={self.provider.provider_name} "
            f"model={self.provider.model_name} first_audio="
            f"{time.perf_counter() - started:.3f}s"
        )
        task = (
            f"Recent conversation:\n{self.memory.messages}\n\n"
            f"{request}\n\nFor the final spoken response, be concise and conversational, "
            "use no markdown or emojis, and address the user as sir in a polite "
            "British-butler tone."
        )
        answer = str(self.agent.run(task))
        print(
            f"[Timing] provider={self.provider.provider_name} "
            f"model={self.provider.model_name} agent={time.perf_counter() - started:.3f}s"
        )
        steps = getattr(getattr(self.agent, "memory", None), "steps", [])
        max_steps = int(self.config.get("agent", {}).get("max_steps", 3))
        if len(steps) >= max_steps and self._confirm(
            f"This task reached the {max_steps}-step limit. May I continue?"
        ):
            answer = str(self.agent.run("Continue the previous task from its current state.", reset=False))
        self._speak(answer)
        return answer

    def run(self) -> None:
        try:
            self.tts.preload()
        except Exception as exc:
            print(f"[TTS] Kokoro unavailable; responses will be printed: {exc}")
        if self.provider is None:
            message = self.provider_error or "No local LLM provider is available."
            print(f"[LLM] {message}")
            self._speak_then_wait(message)
            self.close()
            return
        selected = (
            f"Using {self.provider.provider_name} with model {self.provider.model_name}."
        )
        print(f"[LLM] {selected}")
        self._speak(selected)
        if self.wake_word:
            print(f"[STT] Wake word enabled: {self.wake_text}")
        try:
            self._ensure_agent()
            for recognized in self.stt.listen():
                heard_at = time.perf_counter()
                print(
                    f"[Timing] provider={self.provider.provider_name} "
                    f"model={self.provider.model_name} STT="
                    f"{self.stt.last_transcript_latency:.3f}s"
                )
                text = recognized.strip()
                if self.wake_word:
                    if self.wake_text not in text.lower():
                        continue
                    text = re.sub(re.escape(self.wake_text), "", text, flags=re.IGNORECASE).strip()
                    if not text:
                        self._speak("Yes, sir?")
                        continue
                print(f"You: {text}")
                if not text:
                    continue
                self.memory.add("user", text)
                try:
                    answer = (
                        self._run_agent(text)
                        if is_action_request(text)
                        else self._spoken_stream(self.memory.messages)
                    )
                    self.memory.add("assistant", answer)
                except Exception as exc:
                    message = (
                        f"The {self.provider.provider_name} connection failed: {exc}. "
                        "Please retry, or restart with --provider to switch backend."
                    )
                    self._speak(message)
                elapsed = time.perf_counter() - heard_at
                print(
                    f"[Timing] provider={self.provider.provider_name} "
                    f"model={self.provider.model_name} turn={elapsed:.3f}s"
                )
        except KeyboardInterrupt:
            print("\n[Voice] Shutting down.")
        except Exception as exc:
            message = f"Voice mode stopped: {exc}"
            print(f"[Voice] {message}")
            self._speak_then_wait(message)
        finally:
            self.close()

    def close(self) -> None:
        self.stt.stop()
        self.tts.close()
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path
