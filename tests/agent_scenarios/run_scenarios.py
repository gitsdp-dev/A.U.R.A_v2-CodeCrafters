"""Run the ten generalized-agent mock scenarios and print pass/fail results."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    """Run the scenario test module and return its exit code."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/agent_scenarios/test_scenarios.py",
    ]
    result = subprocess.run(command, check=False)
    print(f"agent_scenarios_exit={result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
