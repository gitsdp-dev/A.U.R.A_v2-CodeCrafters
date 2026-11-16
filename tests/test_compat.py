import logging

import pytest

from compat import check_python_version, get_python_executable


@pytest.mark.parametrize("version", [(3, 10), (3, 11), (3, 12), (3, 13), (3, 14)])
def test_supported_python_versions_do_not_warn(version, caplog):
    with caplog.at_level(logging.WARNING, logger="aura.compat"):
        assert check_python_version(version)
    assert not caplog.records


def test_python_315_warns_and_continues(caplog):
    with caplog.at_level(logging.WARNING, logger="aura.compat"):
        assert check_python_version((3, 15))
    assert "best-effort" in caplog.text


def test_python_below_minimum_reports_and_exits():
    messages = []
    with pytest.raises(SystemExit) as exc:
        check_python_version((3, 9), log=messages.append)
    assert exc.value.code == 1
    assert "minimum Python 3.10" in messages[0]


def test_project_venv_is_preferred_when_running_elsewhere(tmp_path, monkeypatch, caplog):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    monkeypatch.setattr("compat.sys.executable", r"C:\Python\python.exe")
    with caplog.at_level(logging.INFO, logger="aura.compat"):
        assert get_python_executable(tmp_path) == str(venv_python)
    assert "project virtual-environment" in caplog.text
