from __future__ import annotations

from pathlib import Path

import pytest

from core.plugin_loader import discover_plugins
from plugins.ide_agent import IDEAgentService
from plugins.ide_agent.patches import apply_unified_diff
from plugins.ide_agent.policy import ProjectPolicy


def _patch(name: str, old: str, new: str) -> str:
    return f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def test_explicit_root_and_project_tools_are_confined(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    service = IDEAgentService(tmp_path)
    assert service.root == tmp_path.resolve()
    assert service.read_file("app.py") == "value = 1"
    assert "app.py" in service.list_tree()
    with pytest.raises(PermissionError):
        service.read_file("../outside.txt")


def test_command_policy_rejects_shells_and_unapproved_tools(tmp_path: Path):
    policy = ProjectPolicy(tmp_path)
    with pytest.raises(PermissionError):
        policy.command("python -c x && del file")
    with pytest.raises(PermissionError):
        policy.command("del file")
    assert policy.command("python -m pytest -q")[:2] == ["python", "-m"]


def test_unified_patch_applies_and_invalid_context_does_not_change_file(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    policy = ProjectPolicy(tmp_path)
    apply_unified_diff(policy, _patch("app.py", "value = 1", "value = 2"))
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    with pytest.raises(ValueError):
        apply_unified_diff(policy, _patch("app.py", "wrong", "value = 3"))
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_unified_patch_rolls_back_when_second_replace_fails(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    patch = _patch("first.py", "one", "changed") + _patch("second.py", "two", "changed")
    from plugins.ide_agent import patches

    original_replace = patches.os.replace
    calls = 0

    def replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        return original_replace(source, destination)

    monkeypatch.setattr(patches.os, "replace", replace)
    with pytest.raises(OSError):
        apply_unified_diff(ProjectPolicy(tmp_path), patch)
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_ide_agent_plugin_is_discoverable(tmp_path: Path):
    messages = []
    registry = discover_plugins(
        Path(__file__).resolve().parents[1] / "plugins",
        set(),
        logger=messages.append,
    )
    assert registry.has("ide_agent")


def test_large_patch_uses_human_confirmation_gate(tmp_path: Path, monkeypatch):
    target = tmp_path / "app.py"
    second = tmp_path / "second.py"
    target.write_text("one\n", encoding="utf-8")
    second.write_text("alpha\n", encoding="utf-8")
    from core import confirm

    seen = {}

    def request(key, title, detail, operation):
        seen["key"] = key
        seen["detail"] = detail
        return "waiting for human confirmation"

    monkeypatch.setattr(confirm, "request", request)
    from plugins.ide_agent_plugin import run

    result = run(
        {
            "action": "apply_patch",
            "project_root": str(tmp_path),
            "patch": _patch("app.py", "one", "two")
            + _patch("second.py", "alpha", "beta"),
        }
    )
    assert result == "waiting for human confirmation"
    assert seen["key"] == "ide-agent"
    assert target.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "alpha\n"
