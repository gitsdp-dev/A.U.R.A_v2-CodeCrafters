from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import requests

from aura.llm.provider import LLMProvider, normalize_chunk


class OllamaProvider(LLMProvider):
    provider_name = "ollama"

    def __init__(self, config: dict):
        llm = config.get("llm", {})
        options = llm.get("ollama", {})
        self.base_url = str(
            options.get("base_url", "http://localhost:11434")
        ).rstrip("/")
        self.timeout = float(llm.get("request_timeout", 120))
        self.max_tokens = int(llm.get("max_tokens", 256))
        configured_model = str(options.get("model", "")).strip()
        if not configured_model:
            from core.llm_client import get_llm_settings

            _, configured_model = get_llm_settings()
        self.model_name = configured_model
        self.unavailable_message = "Start Ollama."
        self._session = requests.Session()

    def list_models(self) -> list[str]:
        response = self._session.get(
            f"{self.base_url}/api/tags", timeout=min(self.timeout, 3)
        )
        response.raise_for_status()
        return [
            item["name"]
            for item in response.json().get("models", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]

    def is_available(self) -> bool:
        try:
            return self._session.get(
                f"{self.base_url}/api/tags", timeout=1.5
            ).ok
        except requests.RequestException:
            return False

    def stream_chat(
        self, messages: list[dict[str, str]], system_prompt: str
    ) -> Iterator[str]:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "stream": True,
            "options": {"num_ctx": 2048, "num_predict": self.max_tokens},
        }
        with self._session.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
            stream=True,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                record = json.loads(line)
                text = normalize_chunk("ollama", record)
                if text:
                    yield text
                if record.get("done"):
                    break

    def chat(self, messages: list[dict[str, str]], system_prompt: str) -> str:
        return "".join(self.stream_chat(messages, system_prompt))

    def agent_model(self) -> Any:
        from smolagents import LiteLLMModel

        return LiteLLMModel(
            model_id=f"ollama_chat/{self.model_name}",
            api_base=self.base_url,
        )

    def supports_tools(self) -> bool:
        try:
            response = self._session.post(
                f"{self.base_url}/api/show",
                json={"model": self.model_name},
                timeout=min(self.timeout, 3),
            )
            response.raise_for_status()
            capabilities = response.json().get("capabilities", [])
            return "tools" in capabilities
        except (requests.RequestException, ValueError):
            return False

    def close(self) -> None:
        self._session.close()
