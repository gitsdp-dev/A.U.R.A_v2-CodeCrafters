from types import SimpleNamespace

import pytest

from core import startup


def test_startup_elevation_is_windows_only(monkeypatch):
    monkeypatch.setattr(startup.platform, "system", lambda: "Linux")
    startup.ensure_windows_administrator()


def test_startup_continues_when_already_administrator(monkeypatch):
    monkeypatch.setattr(startup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        startup.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: 1)),
        raising=False,
    )
    startup.ensure_windows_administrator()


def test_startup_relaunches_python_arguments_with_uac(monkeypatch):
    shell_execute_call = {}

    def shell_execute(*args):
        shell_execute_call["args"] = args
        return 33

    shell32 = SimpleNamespace(
        IsUserAnAdmin=lambda: 0,
        ShellExecuteW=shell_execute,
    )
    monkeypatch.setattr(
        startup.ctypes, "windll", SimpleNamespace(shell32=shell32), raising=False
    )
    monkeypatch.setattr(startup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(startup.sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(startup.sys, "argv", ["main.py", "--voice"])

    with pytest.raises(SystemExit) as raised:
        startup.ensure_windows_administrator()

    assert raised.value.code == 0
    assert shell_execute_call["args"] == (
        None,
        "runas",
        r"C:\Python\python.exe",
        "main.py --voice",
        str(startup.Path.cwd()),
        1,
    )
    assert shell_execute.argtypes == [
        startup.ctypes.c_void_p,
        startup.ctypes.c_wchar_p,
        startup.ctypes.c_wchar_p,
        startup.ctypes.c_wchar_p,
        startup.ctypes.c_wchar_p,
        startup.ctypes.c_int,
    ]


def test_startup_reports_cancelled_uac_and_exits(monkeypatch, capsys):
    shell32 = SimpleNamespace(
        IsUserAnAdmin=lambda: 0,
        ShellExecuteW=lambda *_args: 5,
    )
    monkeypatch.setattr(
        startup.ctypes,
        "windll",
        SimpleNamespace(
            shell32=shell32,
            kernel32=SimpleNamespace(GetLastError=lambda: 1223),
        ),
        raising=False,
    )
    monkeypatch.setattr(startup.platform, "system", lambda: "Windows")

    with pytest.raises(SystemExit) as raised:
        startup.ensure_windows_administrator()

    assert raised.value.code == 1
    assert "approval was cancelled" in capsys.readouterr().err
