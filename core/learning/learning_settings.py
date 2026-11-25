"""
core/learning/learning_settings.py — Typed Settings for A.U.R.A Adaptive Intelligence.

Loads, validates, and persists learning settings through AURA's configuration manager.
All budget and retention limits are editable here.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

LOGGER = logging.getLogger("aura.learning.settings")


@dataclass
class LearningSettings:
    # Master switch
    enabled: bool = True

    # Retrieval and Context Injection limits
    retrieval_limit: int = 5
    max_injected_tokens: int = 400

    # Promotion & Confidence thresholds
    provisional_evidence_threshold: int = 3
    base_confidence_observation: float = 0.50
    base_confidence_execution: float = 0.70
    base_confidence_user_instruction: float = 0.95
    contradiction_penalty: float = 0.25
    demotion_threshold: float = 0.40

    # Ranking Weights (sum to 1.0)
    weight_relevance: float = 0.40
    weight_recency: float = 0.20
    weight_confidence: float = 0.20
    weight_scope: float = 0.10
    weight_success: float = 0.10
    recency_half_life_days: float = 14.0

    # Storage Caps (Optimizer targets)
    max_experiences: int = 5000
    max_preferences: int = 500
    max_skills: int = 200

    # LLM Reflection & Budget Gate (OFF by default)
    reflection_enabled: bool = False
    max_reflections_per_day: int = 10
    min_reflection_interval_sec: float = 60.0
    preferred_reflection_provider: str = "auto"  # "auto" | "local" | "gemini"

    # Phase 3 advanced feature flags
    strategy_optimization_enabled: bool = False

    def validate(self) -> LearningSettings:
        """Ensure numerical bounds and sensible defaults."""
        self.retrieval_limit = max(1, min(20, int(self.retrieval_limit)))
        self.max_injected_tokens = max(50, min(2000, int(self.max_injected_tokens)))
        self.provisional_evidence_threshold = max(1, min(10, int(self.provisional_evidence_threshold)))
        self.contradiction_penalty = max(0.05, min(0.90, float(self.contradiction_penalty)))
        self.demotion_threshold = max(0.10, min(0.80, float(self.demotion_threshold)))

        self.max_experiences = max(100, min(50000, int(self.max_experiences)))
        self.max_preferences = max(50, min(5000, int(self.max_preferences)))
        self.max_skills = max(20, min(2000, int(self.max_skills)))

        self.max_reflections_per_day = max(0, min(100, int(self.max_reflections_per_day)))
        self.min_reflection_interval_sec = max(5.0, min(3600.0, float(self.min_reflection_interval_sec)))
        return self


def load_settings() -> LearningSettings:
    """Load settings from config_manager with backwards-compatible defaults."""
    try:
        from memory.config_manager import load_api_keys
        cfg = load_api_keys().get("adaptive_learning")
        if isinstance(cfg, dict):
            # Filter keys to match dataclass fields
            valid_keys = LearningSettings.__annotations__.keys()
            filtered = {k: v for k, v in cfg.items() if k in valid_keys}
            return LearningSettings(**filtered).validate()
    except Exception as exc:
        LOGGER.warning("Failed to load learning settings from config; using defaults: %s", exc)
    return LearningSettings().validate()


def save_settings(settings: LearningSettings | dict[str, Any]) -> LearningSettings:
    """Save settings via config_manager."""
    if isinstance(settings, LearningSettings):
        data = asdict(settings.validate())
    else:
        # Validate through dataclass
        valid_keys = LearningSettings.__annotations__.keys()
        filtered = {k: v for k, v in settings.items() if k in valid_keys}
        obj = LearningSettings(**filtered).validate()
        data = asdict(obj)

    try:
        from memory.config_manager import CONFIG_FILE, ensure_config_dir, load_api_keys
        import json
        ensure_config_dir()
        current_config = load_api_keys()
        current_config["adaptive_learning"] = data
        CONFIG_FILE.write_text(json.dumps(current_config, indent=2), encoding="utf-8")
    except Exception as exc:
        LOGGER.error("Failed to save learning settings: %s", exc)

    return LearningSettings(**data)
