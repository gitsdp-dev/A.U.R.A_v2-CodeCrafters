"""Local, deterministic parsing for common system-control voice commands."""

from __future__ import annotations

import re


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "minimum": 0, "minimums": 0, "maximum": 100, "maximums": 100,
    "max": 100, "minimum": 0,
}


def _number(text: str) -> int | None:
    match = re.search(r"\b(\d{1,3})\s*(?:%|percent)?\b", text)
    if match:
        return min(225, max(0, int(match.group(1))))
    words = re.findall(r"\b[a-z]+\b", text)
    for word in words:
        if word in _NUMBER_WORDS:
            return _NUMBER_WORDS[word]
    return None


def match_system_control_intent(text: str) -> dict | None:
    """Return an action dictionary, or None when the text is not a local command."""
    normalized = re.sub(r"[^a-z0-9%._ -]+", " ", str(text).casefold())
    normalized = " ".join(normalized.split())
    if not normalized:
        return None
    if any(phrase in normalized for phrase in (
        "open system settings", "open quick settings", "show system controls",
        "open system controls",
    )):
        return {"operation": "open_panel"}
    if "close system settings" in normalized or "close system controls" in normalized:
        return {"operation": "close_panel"}
    if "open wi-fi settings" in normalized or "open wifi settings" in normalized:
        return {"operation": "open_panel", "page": "wifi"}
    if "open accessibility settings" in normalized:
        return {"operation": "open_panel", "page": "accessibility"}
    if normalized in {"yes", "yes confirm", "confirm", "do it"}:
        return {"operation": "confirm", "confirmed": True}
    if normalized in {"no", "cancel", "no cancel"}:
        return {"operation": "confirm", "confirmed": False}

    if any(term in normalized for term in ("which speaker", "which audio output", "current speaker")):
        return {"operation": "status", "control_id": "output_devices"}
    if "which wi-fi" in normalized or "which wifi" in normalized:
        return {"operation": "status", "control_id": "wifi"}
    if "what is the volume" in normalized or "volume level" in normalized:
        return {"operation": "status", "control_id": "volume"}
    if normalized.startswith("is ") or normalized.startswith("are "):
        status_aliases = {
            "night light": "night_light", "wifi": "wifi", "wi-fi": "wifi",
            "airplane mode": "airplane_mode", "energy saver": "energy_saver",
            "battery saver": "energy_saver", "live captions": "live_captions",
            "narrator": "narrator", "magnifier": "magnifier",
            "sticky keys": "sticky_keys", "high contrast": "high_contrast",
            "mono audio": "mono_audio",
        }
        for phrase, control_id in status_aliases.items():
            if phrase in normalized:
                return {"operation": "status", "control_id": control_id}

    if "list wi-fi" in normalized or "list wifi" in normalized or "scan wi-fi" in normalized:
        return {"operation": "wifi_scan"}
    if "disconnect wi-fi" in normalized or "disconnect wifi" in normalized:
        return {"operation": "wifi_disconnect"}
    match = re.search(r"\bconnect to\s+(.+)$", normalized)
    if match:
        return {"operation": "wifi_connect", "network": match.group(1).strip()}
    if "switch audio output to " in normalized:
        return {
            "operation": "output_switch",
            "device": normalized.split("switch audio output to ", 1)[1].strip(),
        }

    toggle_phrases = (
        ("airplane mode", "airplane_mode"),
        ("energy saver", "energy_saver"),
        ("battery saver", "energy_saver"),
        ("live captions", "live_captions"),
        ("night light", "night_light"),
        ("wi-fi", "wifi"),
        ("wifi", "wifi"),
    )
    for phrase, control_id in toggle_phrases:
        if phrase in normalized and any(
            verb in normalized for verb in ("turn ", "switch ", "enable ", "disable ")
        ):
            enabled = not any(term in normalized for term in (" off", " disable", " turn off"))
            return {"operation": "set", "control_id": control_id, "value": enabled}

    access = {
        "on-screen keyboard": "onscreen_keyboard", "onscreen keyboard": "onscreen_keyboard",
        "narrator": "narrator", "magnifier": "magnifier", "sticky keys": "sticky_keys",
        "filter keys": "filter_keys", "toggle keys": "toggle_keys",
        "high contrast": "high_contrast", "mono audio": "mono_audio",
        "color filters": "color_filters",
    }
    for phrase, control_id in access.items():
        if phrase in normalized and any(
            verb in normalized for verb in ("turn on", "turn off", "enable", "disable")
        ):
            enabled = not any(term in normalized for term in ("turn off", "disable"))
            return {"operation": "set", "control_id": control_id, "value": enabled}

    for phrase, control_id in (("volume", "volume"), ("brightness", "brightness"),
                               ("text size", "text_size")):
        if phrase in normalized and any(term in normalized for term in (
            "set ", " to ", "increase", "decrease", "minimum", "maximum",
        )):
            if "increase" in normalized or "decrease" in normalized:
                amount = _number(normalized) or 10
                return {
                    "operation": "adjust", "control_id": control_id,
                    "delta": amount if "increase" in normalized else -amount,
                }
            number = _number(normalized)
            if number is not None:
                maximum = 225 if control_id == "text_size" else 100
                if control_id == "text_size" and any(
                    phrase in normalized for phrase in ("maximum", "max")
                ):
                    number = 225
                return {
                    "operation": "set", "control_id": control_id,
                    "value": min(maximum, max(100 if control_id == "text_size" else 0, number)),
                }
    if "volume " in normalized and normalized.split()[-1].isdigit():
        return {
            "operation": "set", "control_id": "volume",
            "value": min(100, max(0, int(normalized.split()[-1]))),
        }
    if "night light" in normalized and "strength" in normalized:
        strength = _number(normalized)
        if strength is not None:
            return {
                "operation": "set", "control_id": "night_light_strength",
                "value": min(100, max(0, strength)),
            }
    if "mute" in normalized or "unmute" in normalized:
        return {"operation": "set", "control_id": "mute", "value": "unmute" not in normalized}
    if "max volume" in normalized:
        return {"operation": "set", "control_id": "volume", "value": 100}
    return None
