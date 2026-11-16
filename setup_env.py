"""Create a project virtual environment and install A.U.R.A dependencies."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFERRED = ("3.12", "3.13", "3.11", "3.14", "3.10")


def _python_version(executable: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [
                executable, "-c",
                "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            ],
            capture_output=True, text=True, timeout=10, check=True,
        )
        major, minor = result.stdout.strip().split(".")
        version = int(major), int(minor)
        return version if (3, 10) <= version <= (3, 14) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _installed_pythons() -> dict[str, str]:
    found: dict[str, str] = {}
    if os.name == "nt" and shutil.which("py"):
        result = subprocess.run(
            ["py", "-0p"], capture_output=True, text=True, check=False
        )
        for line in result.stdout.splitlines():
            match = re.search(r"3\.(10|11|12|13|14).*?([A-Za-z]:\\.*?python(?:w)?\.exe)", line, re.I)
            if match:
                found[f"3.{match.group(1)}"] = match.group(2).strip()

    for version in PREFERRED:
        if version in found:
            continue
        commands = [f"python{version}", "python"]
        if os.name == "nt":
            commands.insert(0, f"py -{version}")
        for command in commands:
            executable = shutil.which(command)
            if not executable:
                if command.startswith("py -"):
                    try:
                        result = subprocess.run(
                            ["py", f"-{version}", "-c", "import sys; print(sys.executable)"],
                            capture_output=True, text=True, timeout=10, check=True,
                        )
                        executable = result.stdout.strip()
                    except (OSError, subprocess.SubprocessError):
                        continue
                else:
                    continue
            actual = _python_version(executable)
            if actual:
                found[f"{actual[0]}.{actual[1]}"] = executable
                break

    current = _python_version(sys.executable)
    if current:
        found.setdefault(f"{current[0]}.{current[1]}", sys.executable)
    return found


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _install(python: str, venv: Path) -> None:
    if not venv.exists():
        subprocess.run([python, "-m", "venv", str(venv)], check=True, cwd=ROOT)
    interpreter = _venv_python(venv)
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
        check=True, cwd=ROOT,
    )
    optional = ROOT / "requirements-optional.txt"
    if optional.exists():
        try:
            subprocess.run(
                [str(interpreter), "-m", "pip", "install", "-r", str(optional)],
                check=True, cwd=ROOT,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"Optional packages were not all installed: {exc}")


def main() -> int:
    installed = _installed_pythons()
    if not installed:
        print("Install Python 3.10–3.14, then rerun setup_env.py.")
        return 1
    selected_version = next(version for version in PREFERRED if version in installed)
    selected_python = installed[selected_version]
    venv = ROOT / ".venv"
    created_here = not venv.exists()

    try:
        _install(selected_python, venv)
    except (OSError, subprocess.CalledProcessError) as exc:
        if selected_version == "3.12" or "3.12" not in installed:
            print(f"Core dependency installation failed: {exc}")
            return 1
        if created_here and venv.exists():
            shutil.rmtree(venv)
        print(f"Setup failed with Python {selected_version}; retrying with Python 3.12.")
        try:
            _install(installed["3.12"], venv)
        except (OSError, subprocess.CalledProcessError) as retry_exc:
            print(f"Python 3.12 retry failed: {retry_exc}")
            return 1
    interpreter = _venv_python(venv)
    print("\nA.U.R.A environment is ready.")
    if os.name == "nt":
        print(f'Run: "{interpreter}" main.py')
    else:
        print(f'Run: "{interpreter}" main.py')
    print(f'Voice/model setup: "{interpreter}" setup.py')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
