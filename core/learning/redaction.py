"""
core/learning/redaction.py — Secret and PII Sanitization for Learning Subsystem.

Defends against storing API keys, credentials, tokens, passwords, and high-entropy
secrets in the learning database.
"""
from __future__ import annotations

import math
import re
from typing import Any

# Match standard key and token patterns
_KNOWN_KEY_PATTERNS = [
    # Gemini / Google API keys: AIza...
    re.compile(r"AIza[0-9A-Za-z-_]{30,45}", re.ASCII),
    # OpenAI / generic bearer tokens: sk-...
    re.compile(r"\bsk-[a-zA-Z0-9_\-]{20,}\b", re.ASCII),
    # GitHub personal access tokens
    re.compile(r"\bgh[pousr]-[a-zA-Z0-9]{36,}\b", re.ASCII),
    # AWS access key ID
    re.compile(r"\bAKIA[0-9A-Z]{16}\b", re.ASCII),
    # Bearer tokens in headers or strings
    re.compile(r"(?i)\bBearer\s+[a-zA-Z0-9\._~+/-]{20,}=*", re.ASCII),
    # Generic password assignments in config/json/query strings
    re.compile(
        r"""(?i)(?:password|passwd|api_key|apikey|secret|access_token|private_key|auth_token)\s*[:=]\s*["']?([^\s"'\n\r]{6,})["']?""",
    ),
]

# Words that indicate assignment or context for secrets
_SECRET_KEYWORDS = re.compile(
    r"(?i)\b(password|passwd|api_key|apikey|secret|token|credential|private_key)\b"
)


def shannon_entropy(data: str) -> float:
    """Calculate Shannon entropy for a given string."""
    if not data:
        return 0.0
    entropy = 0.0
    length = len(data)
    frequencies: dict[str, int] = {}
    for char in data:
        frequencies[char] = frequencies.get(char, 0) + 1
    for count in frequencies.values():
        prob = count / length
        entropy -= prob * math.log2(prob)
    return entropy


def redact_secrets(text: str) -> str:
    """
    Scrub credentials, tokens, API keys, and high-entropy secret tokens from text.
    Returns cleaned text with sensitive fragments replaced by [REDACTED].
    """
    if not text or not isinstance(text, str):
        return "" if text is None else str(text)

    cleaned = text

    # 1. Known key and token patterns
    for pattern in _KNOWN_KEY_PATTERNS:
        # If pattern has a capturing group (like key=val), replace only the secret value
        if pattern.groups > 0:
            def _repl_group(match: re.Match) -> str:
                full = match.group(0)
                secret = match.group(1)
                return full.replace(secret, "[REDACTED]")
            cleaned = pattern.sub(_repl_group, cleaned)
        else:
            cleaned = pattern.sub("[REDACTED]", cleaned)

    # 2. High-entropy standalone word detector (> 20 chars, entropy > 3.4)
    words = re.findall(r"\b[A-Za-z0-9_\-+/=]{20,}\b", cleaned)
    for word in words:
        if word == "[REDACTED]":
            continue
        # Redact high entropy tokens (hex tokens have max entropy ~4.0, base64 ~6.0)
        if shannon_entropy(word) > 3.4:
            cleaned = cleaned.replace(word, "[REDACTED]")

    return cleaned


def redact_data_structure(data: Any) -> Any:
    """Recursively scrub secrets from dicts, lists, or strings."""
    if isinstance(data, str):
        return redact_secrets(data)
    if isinstance(data, dict):
        scrubbed = {}
        for k, v in data.items():
            if _SECRET_KEYWORDS.search(str(k)):
                scrubbed[k] = "[REDACTED]"
            else:
                scrubbed[k] = redact_data_structure(v)
        return scrubbed
    if isinstance(data, (list, tuple)):
        return [redact_data_structure(item) for item in data]
    return data
