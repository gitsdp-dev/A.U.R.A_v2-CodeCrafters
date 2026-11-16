from pathlib import Path

import pytest

import ui


def test_resolves_normal_desktop(monkeypatch, tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(ui.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert ui.get_desktop_dir() == desktop


def test_prefers_onedrive_redirected_desktop(monkeypatch, tmp_path):
    desktop = tmp_path / "OneDrive" / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(ui.platform, "system", lambda: "Windows")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ui, "_windows_known_desktop_dir", lambda: desktop)
    monkeypatch.setattr(ui, "_windows_registry_desktop_dir", lambda: None)
    assert ui.get_desktop_dir() == desktop


def test_creates_desktop_when_no_configured_folder_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(ui.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        ui.subprocess, "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": "", "returncode": 1})(),
    )
    desktop = ui.get_desktop_dir()
    assert desktop == tmp_path / "Desktop"
    assert desktop.is_dir()


def test_desktop_path_preserves_non_ascii_home(monkeypatch, tmp_path):
    home = tmp_path / "使用者-é"
    desktop = home / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(ui.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: home)
    assert ui.get_desktop_dir() == desktop


def test_shortcut_arguments_quote_absolute_script_path(tmp_path):
    script = tmp_path / "AURA 用户" / "main.py"
    args = ui.build_shortcut_arguments(script, ("--background",))
    assert str(script.resolve()) in args
    assert args.startswith('"')
    assert args.endswith('" --background')


def test_frozen_shortcut_arguments_omit_script(tmp_path):
    script = tmp_path / "main.py"
    assert ui.build_shortcut_arguments(script, ("--background",), frozen=True) == "--background"


def test_existing_desktop_shortcut_path_is_stable_and_overwritten(tmp_path):
    shortcut = tmp_path / "AURA.desktop"
    shortcut.write_text("old", encoding="utf-8")
    ui._write_desktop_entry(shortcut, "[Desktop Entry]\nName=AURA updated\n")
    assert shortcut.read_text(encoding="utf-8") == "[Desktop Entry]\nName=AURA updated\n"
    assert list(tmp_path.glob("AURA*")) == [shortcut]
