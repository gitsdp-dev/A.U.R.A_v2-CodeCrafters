import subprocess
from pathlib import Path

import setup as aura_setup


def test_setup_uses_current_python_312(tmp_path):
    assert aura_setup._setup_python(
        tmp_path,
        current_version=(3, 12),
        current_executable="current-python-312",
    ) == "current-python-312"
    assert not (tmp_path / ".venv").exists()


def test_setup_creates_python_312_venv_for_other_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(aura_setup, "_find_python312", lambda: "base-python-312")
    monkeypatch.setattr(aura_setup, "_python_version", lambda _executable: (3, 12))

    def create_venv(_label, args):
        executable = aura_setup._venv_python(tmp_path)
        executable.parent.mkdir(parents=True)
        executable.touch()

    monkeypatch.setattr(aura_setup, "_run", create_venv)

    executable = aura_setup._setup_python(tmp_path, current_version=(3, 13))

    assert executable == str(aura_setup._venv_python(tmp_path))
    assert Path(executable).is_file()


def test_setup_refuses_to_replace_venv_with_another_python(tmp_path, monkeypatch):
    executable = aura_setup._venv_python(tmp_path)
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(aura_setup, "_find_python312", lambda: "base-python-312")
    monkeypatch.setattr(aura_setup, "_python_version", lambda _executable: (3, 11))

    try:
        aura_setup._setup_python(tmp_path, current_version=(3, 13))
    except RuntimeError as exc:
        assert "refusing to replace it" in str(exc)
    else:
        raise AssertionError("A non-3.12 venv must not be used or replaced.")


def test_all_requirement_files_are_attempted_even_if_optional_install_fails(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(aura_setup, "HERE", tmp_path)
    attempts = []

    def run(label, args):
        attempts.append(Path(args[-1]).name)
        if args[-1].endswith("requirements-optional.txt"):
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(aura_setup, "_run", run)

    aura_setup._install_requirements("python-312")

    assert attempts == [
        "requirements.txt",
        "requirements-optional.txt",
        "requirements-cad.txt",
    ]
    assert "optional integrations requirements" in capsys.readouterr().out


def test_core_requirements_include_background_assistant_dependencies():
    requirements_file = aura_setup.HERE / "requirements.txt"
    package_names = set()
    for line in requirements_file.read_text(encoding="utf-8").splitlines():
        requirement = line.split("#", 1)[0].split(";", 1)[0].strip()
        if requirement:
            package_names.add(requirement.split(">=", 1)[0].split("[", 1)[0].lower())

    assert {"pynput", "vosk"} <= package_names


def test_gui_command_prefers_pythonw_on_windows(tmp_path, monkeypatch):
    python_exe = tmp_path / "python.exe"
    pythonw_exe = tmp_path / "pythonw.exe"
    python_exe.touch()
    pythonw_exe.touch()
    monkeypatch.setattr(aura_setup, "OS", "Windows")

    assert aura_setup._gui_executable(str(python_exe)) == str(pythonw_exe)
