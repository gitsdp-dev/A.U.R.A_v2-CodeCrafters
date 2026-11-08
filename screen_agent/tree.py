"""Accessibility-tree compaction, relevance ranking, and token budgeting."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

_ROLE_NAMES = {
    "button": "btn", "checkbox": "check", "combobox": "combo",
    "dialog": "dialog", "link": "link", "listitem": "item",
    "menuitem": "menu", "radio": "radio", "tab": "tab",
    "textbox": "text", "textfield": "text", "treeitem": "tree",
    "window": "window", "option": "option", "slider": "slider",
    "scrollbar": "scroll",
}
_DECORATIVE = {
    "image", "separator", "unknown", "graphic", "decoration", "tooltip",
}
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_REF_RE = re.compile(r"^@[^:\s]+:e\d+$")


def estimate_tokens(text: str) -> int:
    """Use a conservative, dependency-free estimate for English prompt text."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4), len(_WORD_RE.findall(text)))


@dataclass(frozen=True)
class Element:
    """One compressed, actionable accessibility element."""

    number: int
    role: str
    name: str
    value: str
    ref: str
    states: tuple[str, ...] = ()


@dataclass(frozen=True)
class Observation:
    """Model-safe text and the private local lookup maps for one snapshot."""

    text: str
    refs: dict[int, str]
    elements: dict[int, Element]
    fingerprint: str
    app: str = ""
    window_id: str = ""


def _walk(tree: Any):
    if not isinstance(tree, dict):
        return
    yield tree
    children = tree.get("children")
    if isinstance(children, list):
        for child in children:
            yield from _walk(child)


def _node_text(node: dict[str, Any], key: str) -> str:
    value = node.get(key)
    return " ".join(str(value).split()) if value is not None else ""


def _role(node: dict[str, Any]) -> str:
    raw = _node_text(node, "role").casefold()
    return _ROLE_NAMES.get(raw, raw[:10] or "node")


def _rank(node: dict[str, Any], goal_words: set[str]) -> int:
    name = _node_text(node, "name").casefold()
    value = _node_text(node, "value").casefold()
    role = _node_text(node, "role").casefold()
    words = set(_WORD_RE.findall(f"{name} {value}"))
    score = 4 * len(words & goal_words)
    if any(word in name for word in goal_words):
        score += 2
    if role in {"button", "textbox", "textfield", "combobox", "menuitem", "link"}:
        score += 1
    if node.get("focused") or "focused" in node.get("states", []):
        score += 3
    return score


def _fingerprint(tree: Any) -> str:
    rows = []
    for node in _walk(tree):
        rows.append((
            _node_text(node, "role"),
            _node_text(node, "name"),
            _node_text(node, "value"),
            tuple(sorted(str(state).casefold() for state in node.get("states", []) if isinstance(state, str))),
        ))
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compress_snapshot(
    envelope: dict[str, Any],
    goal: str,
    *,
    token_budget: int = 900,
    system_prompt: str = "",
    max_elements: int = 32,
) -> Observation:
    """Reduce an agent-desktop envelope to ranked, numbered actionable lines."""
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise ValueError("A successful agent-desktop snapshot envelope is required.")
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("tree"), dict):
        raise TypeError("Snapshot data must contain an accessibility tree.")
    tree = data["tree"]
    goal_words = set(_WORD_RE.findall(goal.casefold()))
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()
    order = 0
    for node in _walk(tree):
        role_name = _node_text(node, "role").casefold()
        role = _role(node)
        name = _node_text(node, "name")
        value = _node_text(node, "value")
        ref = _node_text(node, "ref_id") or _node_text(node, "ref")
        states = node.get("states", [])
        state_names = {str(state).casefold() for state in states} if isinstance(states, list) else set()
        if (
            not _REF_RE.fullmatch(ref)
            or not name
            or role_name in _DECORATIVE
            or state_names & {"offscreen", "invisible", "hidden"}
            or node.get("visible") is False
        ):
            continue
        actions = node.get("available_actions")
        interactive = bool(actions) or role_name in {
            "button", "checkbox", "combobox", "link", "menuitem", "radio",
            "slider", "tab", "textbox", "textfield", "treeitem", "option",
        }
        if not interactive:
            continue
        identity = (role_name, name.casefold(), value.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        order += 1
        candidates.append((_rank(node, goal_words), -order, {
            "role": role,
            "name": name[:40],
            "value": value[:40],
            "ref": ref,
            "states": tuple(sorted(state_names)),
        }))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    candidates = candidates[:max(1, min(40, max_elements))]
    budget = max(32, int(token_budget))
    static_and_goal = f"{system_prompt}\nGoal: {goal}\n"
    used = estimate_tokens(static_and_goal) + estimate_tokens("<OBS>\n</OBS>")
    if used > budget:
        raise ValueError("The goal exceeds the configured Screen Agent token budget.")
    selected: list[dict[str, Any]] = []
    for _, _, item in candidates:
        number = len(selected) + 1
        line = f"{number}|{item['role']}|{item['name']}"
        if item["value"]:
            line += f"|{json.dumps(item['value'], ensure_ascii=True)}"
        line_cost = estimate_tokens(line) + 1
        if used + line_cost > budget:
            continue
        selected.append(item)
        used += line_cost

    lines: list[str] = []
    refs: dict[int, str] = {}
    elements: dict[int, Element] = {}
    for number, item in enumerate(selected, start=1):
        line = f"{number}|{item['role']}|{item['name']}"
        if item["value"]:
            line += f"|{json.dumps(item['value'], ensure_ascii=True)}"
        lines.append(line)
        element = Element(
            number, item["role"], item["name"], item["value"], item["ref"], item["states"]
        )
        refs[number] = element.ref
        elements[number] = element
    text = "<OBS>\n" + "\n".join(lines) + "\n</OBS>"
    return Observation(
        text=text,
        refs=refs,
        elements=elements,
        fingerprint=_fingerprint(tree),
        app=_node_text(data, "app"),
        window_id=str(data.get("window_id") or ""),
    )
