import plistlib
from pathlib import Path
from types import SimpleNamespace

import platform_bg


def test_linux_autostart_writes_user_desktop_entry(monkeypatch, tmp_path):
    config_home = tmp_path / "xdg config"
    monkeypatch.setattr(platform_bg.sys, "platform", "linux")
    monkeypatch.setattr(platform_bg, "APP_ROOT", tmp_path / "AURA project")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(platform_bg.sys, "frozen", False, raising=False)

    platform_bg.autostart_enable()

    entry = config_home / "autostart" / "aura.desktop"
    contents = entry.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in contents
    assert "X-GNOME-Autostart-enabled=true" in contents
    assert 'Exec="' in contents
    assert '"--background"' in contents
    assert platform_bg.autostart_is_enabled()

    platform_bg.autostart_disable()
    assert not entry.exists()


def test_macos_autostart_writes_and_loads_launch_agent(
    monkeypatch, tmp_path
):
    home = tmp_path / "user"
    monkeypatch.setattr(platform_bg.sys, "platform", "darwin")
    monkeypatch.setattr(platform_bg, "APP_ROOT", tmp_path / "AURA")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(platform_bg.os, "getuid", lambda: 501, raising=False)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(platform_bg.subprocess, "run", run)
    monkeypatch.setattr(platform_bg.sys, "frozen", False, raising=False)

    platform_bg.autostart_enable()

    plist = home / "Library" / "LaunchAgents" / "com.codecrafters.aura.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == "com.codecrafters.aura"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is False
    assert data["ProgramArguments"][-1] == "--background"
    assert [command[1] for command in commands] == ["bootout", "bootstrap"]
    assert platform_bg.autostart_is_enabled()

    platform_bg.autostart_disable()
    assert not plist.exists()


def test_open_command_launches_a_visible_app_when_no_instance_exists(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(platform_bg.sys, "platform", "linux")
    monkeypatch.setattr(platform_bg, "APP_ROOT", tmp_path)
    launched = []
    monkeypatch.setattr(
        platform_bg.subprocess,
        "Popen",
        lambda arguments, **kwargs: launched.append((arguments, kwargs)),
    )
    monkeypatch.setattr(platform_bg.shutil, "which", lambda _name: None)
    monkeypatch.setattr(platform_bg.sys, "frozen", False, raising=False)

    platform_bg.launch_app("AURA")

    assert launched[0][0][-1] == "--open"
    assert str(tmp_path / "main.py") in launched[0][0]
    assert "--background" not in launched[0][0]
