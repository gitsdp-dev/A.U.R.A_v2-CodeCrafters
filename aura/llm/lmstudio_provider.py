from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import requests

from aura.llm.provider import LLMProvider


class LMStudioProvider(LLMProvider):
    provider_name = "lmstudio"
    unavailable_message = (
        "Start the LM Studio local server (Developer tab, Start Server) and load a model."
    )

    def __init__(self, config: dict):
        llm = config.get("llm", {})
        options = llm.get("lmstudio", {})
        self.base_url = str(
            options.get("base_url", "http://localhost:1234/v1")
        ).rstrip("/")
        self.timeout = float(llm.get("request_timeout", 120))
        self.max_tokens = int(llm.get("max_tokens", 256))
        self.model_name = str(options.get("model", "auto")).strip()
        self._session = requests.Session()
        self._client: Any = None

    def list_models(self) -> list[str]:
        response = self._session.get(
            f"{self.base_url}/models", timeout=min(self.timeout, 3)
        )
        response.raise_for_status()
        records = response.json().get("data", [])
        models = [
            item["id"]
            for item in records
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if self.model_name.lower() in {"", "auto"}:
            if not models:
                raise RuntimeError("LM Studio reports no loaded models.")
            self.model_name = models[0]
            print(f"[LLM] LM Studio loaded model: {self.model_name}")
        return models

    def is_available(self) -> bool:
        try:
            response = self._session.get(
                f"{self.base_url}/models", timeout=1.5
            )
            if not response.ok:
                return False
            if self.model_name.lower() in {"", "auto"}:
                records = response.json().get("data", [])
                if not records:
                    return False
                self.model_name = str(records[0].get("id", ""))
                if not self.model_name:
                    return False
                print(f"[LLM] LM Studio loaded model: {self.model_name}")
            return True
        except (requests.RequestException, ValueError):
            return False

    def _openai_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self.base_url,
                api_key="lm-studio",
                timeout=self.timeout,
            )
        return self._client

    def stream_chat(
        self, messages: list[dict[str, str]], system_prompt: str
    ) -> Iterator[str]:
        records = [{"role": "system", "content": system_prompt}, *messages]
        stream = self._openai_client().chat.completions.create(
            model=self.model_name,
            messages=records,
            stream=True,
            max_tokens=self.max_tokens,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content
            if isinstance(text, str) and text:
                yield text

    def chat(self, messages: list[dict[str, str]], system_prompt: str) -> str:
        return "".join(self.stream_chat(messages, system_prompt))

    def agent_model(self) -> Any:
        from smolagents import OpenAIServerModel

        return OpenAIServerModel(
            model_id=self.model_name,
            api_base=self.base_url,
            api_key="lm-studio",
        )

    def supports_tools(self) -> bool:
        try:
            response = self._session.get(
                f"{self.base_url}/models", timeout=min(self.timeout, 3)
            )
            response.raise_for_status()
            for model in response.json().get("data", []):
                if model.get("id") == self.model_name:
                    capabilities = model.get("capabilities", [])
                    return bool(
                        model.get("supports_tools")
                        or "tools" in capabilities
                        or "tool_use" in capabilities
                    )
        except (requests.RequestException, ValueError):
            return False
        return False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._session.close()
