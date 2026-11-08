from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from screen_agent.agent import ScreenAgent, cloud_allowed
from screen_agent.client import AgentDesktopError, CommandResult
from screen_agent.fastpath import FastIntent, is_screen_agent_request, match_fast_path
from screen_agent.llm import (
    STATIC_SYSTEM_PROMPT,
    ScreenLLMError,
    validate_action,
)
from screen_agent.safety import confirmation_reason, sensitive_target
from screen_agent.tree import compress_snapshot, estimate_tokens


def _snapshot(*nodes: dict, app: str = "Notepad", snapshot_id: str = "snap") -> dict:
    children = []
    for index, node in enumerate(nodes, start=1):
        children.append({
            "role": node.get("role", "button"),
            "name": node.get("name", f"control {index}"),
            "value": node.get("value", ""),
            "ref_id": node.get("ref", f"@{snapshot_id}:e{index}"),
            "available_actions": node.get("available_actions", ["Click"]),
            "states": node.get("states", []),
            "visible": node.get("visible", True),
        })
    return {
        "version": "2.6",
        "ok": True,
        "command": "snapshot",
        "data": {
            "app": app,
            "snapshot_id": snapshot_id,
            "complete": True,
            "tree": {"role": "window", "name": "Document", "children": children},
        },
    }


class FakeClient:
    available = True

    def __init__(self, envelopes: list[dict] | None = None):
        self.envelopes = deque(envelopes or [_snapshot({"name": "Save"})])
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, command: str, *args: str, **_kwargs) -> CommandResult:
        self.calls.append((command, args))
        if command == "snapshot":
            envelope = self.envelopes[0]
            if len(self.envelopes) > 1:
                self.envelopes.popleft()
            return CommandResult(envelope["data"], envelope)
        if command == "find":
            return CommandResult({"match": None, "matches": []}, {"ok": True, "data": {}})
        return CommandResult({"action": command}, {"ok": True, "data": {"action": command}})


class FakeLLM:
    def __init__(self, *actions: dict):
        self.actions = deque(actions)

    def next_action(self, _goal: str, _observation):
        return self.actions.popleft()


def _settings(**overrides) -> dict:
    values = {
        "enabled": True,
        "engine": "local",
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
    values.update(overrides)
    return values


def test_tree_compression_ranks_goal_matches_and_drops_noise():
    envelope = _snapshot(
        {"name": "Search", "role": "textbox"},
        {"name": "Save", "role": "button"},
        {"name": "Save", "role": "button"},
        {"name": "decorative", "role": "image"},
        {"name": "Offscreen", "role": "button", "states": ["offscreen"]},
    )
    compressed = compress_snapshot(envelope, "click Save")

    assert compressed.text.splitlines()[1].endswith("|Save")
    assert len(compressed.elements) == 2
    assert all("decorative" not in line and "Offscreen" not in line for line in compressed.text.splitlines())
    assert len(compressed.refs) == len(compressed.elements)


def test_tree_token_budget_drops_low_ranked_complete_lines():
    envelope = _snapshot(*[
        {"name": f"control {index}", "role": "button"}
        for index in range(80)
    ] + [{"name": "Save", "role": "button"}])
    budget = 220
    compressed = compress_snapshot(
        envelope, "Save", token_budget=budget, system_prompt=STATIC_SYSTEM_PROMPT
    )
    complete_prompt = f"{STATIC_SYSTEM_PROMPT}\nGoal: Save\n{compressed.text}"

    assert estimate_tokens(complete_prompt) <= budget
    assert any("|Save" in line for line in compressed.text.splitlines())
    assert all(line.count("|") >= 2 for line in compressed.text.splitlines()[1:-1])


def test_tree_mapping_keeps_snapshot_qualified_refs_local():
    compressed = compress_snapshot(_snapshot({"name": "Save"}), "Save")

    assert compressed.refs == {1: "@snap:e1"}
    assert compressed.elements[1].ref == "@snap:e1"
    assert "@snap:e1" not in compressed.text


def test_tree_drops_bare_or_malformed_refs():
    compressed = compress_snapshot(_snapshot({"name": "Save", "ref": "@e1"}), "Save")

    assert compressed.refs == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a":"click","n":1,"t":null}', {"a": "click", "n": 1, "t": None}),
        ('{"a":"done","n":null,"t":null}', {"a": "done", "n": None, "t": None}),
        ('{"a":"key","n":null,"t":"CTRL+S"}', {"a": "key", "n": None, "t": "CTRL+S"}),
    ],
)
def test_model_action_schema_accepts_only_valid_actions(raw, expected):
    assert validate_action(raw, {1}) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"a":"shell","n":null,"t":"whoami"}',
        '{"a":"click","n":99,"t":null}',
        '{"a":"click","n":1,"t":"extra"}',
        '{"a":"done","n":null,"t":null,"explanation":"unsafe"}',
        '{"a":"type","n":1,"t":7}',
    ],
)
def test_model_action_schema_rejects_malformed_or_unmapped_output(raw):
    with pytest.raises(ScreenLLMError):
        validate_action(raw, {1})


@pytest.mark.parametrize(
    ("text", "action", "detail"),
    [
        ("AURA, open Notepad and type hello", "launch_type", "hello"),
        ("click Save in the current window", "click", "Save"),
        ("press Ctrl+S", "key", "CTRL+S"),
        ("scroll down", "scroll", "down"),
        ("maximize the current window", "maximize", ""),
        ("copy hello world to clipboard", "clipboard_set", "hello world"),
    ],
)
def test_fast_path_matches_common_intents(text, action, detail):
    match = match_fast_path(text)

    assert isinstance(match, FastIntent)
    assert match.action == action
    assert (match.text or match.label or match.key or match.direction).casefold() == detail.casefold()
    assert is_screen_agent_request(text)


def test_fast_path_ignores_non_screen_text():
    assert match_fast_path("tell me a joke") is None
    assert not is_screen_agent_request("tell me a joke")
    assert match_fast_path("open Notepad") is not None
    assert not is_screen_agent_request("open Notepad")
    assert not is_screen_agent_request("open https://example.com and type hello")


@pytest.mark.parametrize("error_code", ["STALE_REF", "AMBIGUOUS_TARGET"])
def test_safe_stale_errors_refresh_and_remap_the_target(error_code):
    old = compress_snapshot(_snapshot({"name": "Save"}, snapshot_id="old"), "click Save")
    fresh_envelope = _snapshot({"name": "Save", "ref": "@new:e4"}, snapshot_id="new")
    client = FakeClient([fresh_envelope])
    agent = ScreenAgent(settings=_settings(), client=client)
    action = {"a": "click", "n": 1, "t": None}

    retried = agent._retry_stale(
        AgentDesktopError(
            "stale", code=error_code, retry="safe",
            suggestion="Take a new snapshot.",
        ),
        action, old, "click Save",
    )

    assert retried is not None
    _, observation, updated = retried
    assert updated["n"] == 1
    assert observation.refs[1] == "@new:e4"


def test_stale_error_is_not_retried_without_safe_disposition():
    old = compress_snapshot(_snapshot({"name": "Save"}, snapshot_id="old"), "click Save")
    client = FakeClient([_snapshot({"name": "Save"}, snapshot_id="new")])
    agent = ScreenAgent(settings=_settings(), client=client)

    assert agent._retry_stale(
        AgentDesktopError("stale", code="STALE_REF", retry="unknown"),
        {"a": "click", "n": 1, "t": None},
        old,
        "click Save",
    ) is None
    assert not client.calls


def test_noop_detection_stops_after_two_consecutive_actions():
    client = FakeClient()
    llm = FakeLLM(
        {"a": "click", "n": 1, "t": None},
        {"a": "click", "n": 1, "t": None},
    )
    result = ScreenAgent(settings=_settings(max_steps=6), client=client, llm=llm).run(
        "complete this task"
    )

    assert "two consecutive" in result
    assert [name for name, _ in client.calls].count("click") == 2


def test_step_limit_stops_after_the_configured_number_of_actions():
    client = FakeClient()
    llm = FakeLLM({"a": "click", "n": 1, "t": None})
    result = ScreenAgent(settings=_settings(max_steps=1), client=client, llm=llm).run(
        "complete this task"
    )

    assert "1-step limit" in result
    assert [name for name, _ in client.calls].count("click") == 1


def test_safety_detects_destructive_password_and_unsaved_close_actions():
    password = SimpleNamespace(role="text", name="Password")
    delete_button = SimpleNamespace(role="button", name="Delete account")
    ordinary_button = SimpleNamespace(role="button", name="Save")

    assert sensitive_target(password)
    assert confirmation_reason(
        {"a": "type"}, goal="type", element=password
    ) == "Typing into a password-like field"
    assert confirmation_reason(
        {"a": "click"}, goal="click", element=delete_button
    ) == "A potentially destructive or irreversible action"
    assert confirmation_reason(
        {"a": "close-app"}, goal="close", element=ordinary_button,
        snapshot_text="You have unsaved changes",
    ) == "Closing an app that appears to contain unsaved changes"
    assert not confirmation_reason(
        {"a": "click"}, goal="click", element=ordinary_button
    )


def test_default_protected_apps_are_denied():
    from screen_agent.safety import app_allowed

    assert not app_allowed("Bitwarden", [], [])[0]
    assert not app_allowed("Example Bank", [], [])[0]
    assert not app_allowed("Notepad", ["Chrome"], [])[0]
    assert not app_allowed("Chrome Helper", ["Chrome"], [])[0]
    assert app_allowed("Notepad", [], [])[0]


def test_fast_path_action_verifies_changed_and_unchanged_snapshots():
    before = _snapshot({"name": "Save"})
    after = _snapshot({"name": "Saved"}, snapshot_id="after")
    client = FakeClient([before, after])
    agent = ScreenAgent(settings=_settings(), client=client)
    envelope, observation = agent._snapshot("click Save")

    assert agent._execute_fast_action(
        "click",
        [observation.elements[1].ref],
        "click Save",
        envelope,
        observation,
        action={"a": "click", "n": 1, "t": None},
    )

    unchanged_client = FakeClient([before])
    unchanged_agent = ScreenAgent(settings=_settings(), client=unchanged_client)
    unchanged_envelope, unchanged_observation = unchanged_agent._snapshot("click Save")
    assert not unchanged_agent._execute_fast_action(
        "click",
        [unchanged_observation.elements[1].ref],
        "click Save",
        unchanged_envelope,
        unchanged_observation,
        action={"a": "click", "n": 1, "t": None},
    )


def test_cloud_privacy_gate_requires_explicit_opt_in():
    assert not cloud_allowed({"cloud_opt_in": False})
    assert cloud_allowed({"cloud_opt_in": True})
