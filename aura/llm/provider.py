from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any


class LLMProvider(ABC):
    """Small common interface for local chat providers."""

    model_name: str
    provider_name: str

    @abstractmethod
    def stream_chat(
        self, messages: list[dict[str, str]], system_prompt: str
    ) -> Iterator[str]:
        """Yield plain text pieces from a chat response."""

    @abstractmethod
    def chat(self, messages: list[dict[str, str]], system_prompt: str) -> str:
        """Return a complete chat response."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Return model names reported by the local inference server."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether the local server responds within a short timeout."""

    @abstractmethod
    def agent_model(self) -> Any:
        """Create the smolagents-compatible model object."""


def normalize_chunk(provider: str, chunk: dict[str, Any]) -> str:
    """Normalize native streaming records to a plain text fragment."""
    if provider == "ollama":
        message = chunk.get("message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else ""
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _configured_primary() -> str:
    try:
        from core.llm_client import get_llm_provider

        configured = get_llm_provider()
        return "lmstudio" if configured == "openai" else configured
    except (ImportError, AttributeError):
        return "ollama"


def get_provider(config: dict, provider_override: str | None = None) -> LLMProvider:
    """Create a configured backend; `auto` tries the existing primary first."""
    llm = config.get("llm", {})
    requested = (provider_override or llm.get("provider", "auto")).strip().lower()
    if requested not in {"ollama", "lmstudio", "auto"}:
        raise ValueError("provider must be 'ollama', 'lmstudio', or 'auto'")

    primary = _configured_primary()
    order = [primary, "lmstudio" if primary == "ollama" else "ollama"]
    if requested != "auto":
        order = [requested]

    from aura.llm.lmstudio_provider import LMStudioProvider
    from aura.llm.ollama_provider import OllamaProvider

    providers: dict[str, LLMProvider] = {
        "ollama": OllamaProvider(config),
        "lmstudio": LMStudioProvider(config),
    }
    for index, name in enumerate(order):
        selected = providers[name]
        if selected.is_available():
            if requested == "auto" and index:
                print(
                    f"[LLM] Configured provider '{primary}' is unavailable; "
                    f"falling back to {selected.provider_name}."
                )
            return selected
    if requested == "auto":
        raise RuntimeError(
            "Neither local backend is available. Start Ollama. Start the LM "
            "Studio local server (Developer tab, Start Server) and load a model."
        )
    raise RuntimeError(selected.unavailable_message)
