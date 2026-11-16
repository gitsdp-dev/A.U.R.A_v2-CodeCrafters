"""CAD backend boundary for future engines."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class CadBackend(Protocol):
    def run_script(self, script: str, mesh_path: Path, job_id: str = "") -> str:
        """Validate and execute one script, writing an STL mesh."""
