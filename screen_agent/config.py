"""Validated Screen Agent settings backed by A.U.R.A.'s plugin config store."""

from __future__ import annotations

from typing import Any

from memory.config_manager import get_plugin_config, save_plugin_config

NAMESPACE = "screen_agent"
_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "engine": "auto",
    "max_steps": 8,
    "token_budget": 900,
    "gemini_token_budget": 3000,
    "escalate_enabled": False,
    "escalation_after": 3,
    "headed_input": False,
    "allow_apps": [],
    "deny_apps": [],
    "cloud_opt_in": False,
}


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def _app_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def get_settings() -> dict[str, Any]:
    """Return sanitized settings with safe defaults for missing or invalid values."""
    raw = get_plugin_config(NAMESPACE)
    if not isinstance(raw, dict):
        raw = {}
    settings = dict(_DEFAULTS)
    settings.update(raw)
    settings["enabled"] = bool(settings["enabled"])
    engine = settings.get("engine")
    settings["engine"] = (
        engine if isinstance(engine, str) and engine in {"auto", "local", "gemini"}
        else "auto"
    )
    settings["max_steps"] = _bounded_int(settings["max_steps"], 8, 1, 20)
    settings["token_budget"] = _bounded_int(settings["token_budget"], 900, 128, 4096)
    settings["gemini_token_budget"] = _bounded_int(
        settings["gemini_token_budget"], 3000, 128, 8192
    )
    settings["escalation_after"] = _bounded_int(
        settings["escalation_after"], 3, 1, 20
    )
    settings["escalate_enabled"] = bool(settings["escalate_enabled"])
    settings["headed_input"] = bool(settings["headed_input"])
    settings["allow_apps"] = _app_list(settings["allow_apps"])
    settings["deny_apps"] = _app_list(settings["deny_apps"])
    settings["cloud_opt_in"] = bool(settings["cloud_opt_in"])
    return settings


def save_settings(values: dict[str, Any]) -> None:
    """Validate and persist form values without changing unrelated plugin settings."""
    if not isinstance(values, dict):
        raise TypeError("Screen Agent settings must be a mapping.")
    normalized = dict(values)
    normalized["engine"] = str(values.get("engine", "auto")).strip().lower()
    if normalized["engine"] not in {"auto", "local", "gemini"}:
        raise ValueError("Screen Agent engine must be auto, local, or gemini.")
    normalized["max_steps"] = _bounded_int(values.get("max_steps"), 8, 1, 20)
    normalized["token_budget"] = _bounded_int(
        values.get("token_budget"), 900, 128, 4096
    )
    normalized["gemini_token_budget"] = _bounded_int(
        values.get("gemini_token_budget"), 3000, 128, 8192
    )
    normalized["escalation_after"] = _bounded_int(
        values.get("escalation_after"), 3, 1, 20
    )
    normalized["enabled"] = bool(values.get("enabled", True))
    normalized["escalate_enabled"] = bool(values.get("escalate_enabled", False))
    normalized["headed_input"] = bool(values.get("headed_input", False))
    normalized["cloud_opt_in"] = bool(values.get("cloud_opt_in", False))
    normalized["allow_apps"] = _app_list(values.get("allow_apps"))
    normalized["deny_apps"] = _app_list(values.get("deny_apps"))
    save_plugin_config(NAMESPACE, normalized)


def settings_section() -> dict[str, Any]:
    """Return the existing plugin-settings overlay schema for this feature."""
    values = get_settings()
    display_values = dict(values)
    display_values["allow_apps"] = ", ".join(values["allow_apps"])
    display_values["deny_apps"] = ", ".join(values["deny_apps"])
    return {
        "plugin": NAMESPACE,
        "namespace": NAMESPACE,
        "title": "SCREEN AGENT — ACCESSIBILITY CONTROL",
        "fields": [
            {"key": "enabled", "label": "Enable Screen Agent", "type": "toggle", "default": True},
            {
                "key": "engine", "label": "Model engine", "type": "choice",
                "options": ["auto", "local", "gemini"], "default": "auto",
            },
            {"key": "max_steps", "label": "Maximum agent steps (1–20)", "type": "text", "default": 8},
            {"key": "token_budget", "label": "Local prompt token budget", "type": "text", "default": 900},
            {"key": "gemini_token_budget", "label": "Gemini prompt token budget", "type": "text", "default": 3000},
            {"key": "escalate_enabled", "label": "Allow escalation after local failures", "type": "toggle", "default": False},
            {"key": "escalation_after", "label": "Failed steps before escalation", "type": "text", "default": 3},
            {
                "key": "headed_input", "label": "Allow physical mouse/keyboard input (may interfere with what you are doing)",
                "type": "toggle", "default": False,
            },
            {"key": "allow_apps", "label": "Allowed apps (comma-separated; blank allows non-denied apps)", "type": "text"},
            {"key": "deny_apps", "label": "Additional denied apps (comma-separated)", "type": "text"},
            {
                "key": "cloud_opt_in",
                "label": "Allow accessibility text to be sent to Google Gemini",
                "type": "toggle", "default": False,
            },
        ],
        "values": display_values,
        "save": save_settings,
    }
