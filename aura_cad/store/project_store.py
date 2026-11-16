"""Atomic, versioned storage for generated CAD projects."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from aura_cad.config import PROJECTS_DIR
from aura_cad.models import CadSpec, ScriptVersion


class ProjectStore:
    def __init__(self, root: Path = PROJECTS_DIR) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def create_project(self, spec: CadSpec) -> str:
        project_id = uuid4().hex
        directory = self.root / project_id
        directory.mkdir(parents=True, exist_ok=False)
        self._atomic_json(directory / "spec.json", spec.as_dict())
        self._atomic_json(directory / "active.json", {"version": 0})
        return project_id

    def commit_version(
        self, project_id: str, script: str, spec: CadSpec, exports: dict[str, Path]
    ) -> ScriptVersion:
        with self._lock:
            project = self._project_dir(project_id)
            version = self.active_version(project_id) + 1
            version_dir = project / "versions" / f"v{version}"
            version_dir.mkdir(parents=True, exist_ok=False)
            try:
                script_path = version_dir / "model.py"
                script_path.write_text(script, encoding="utf-8")
                for name in ("stl", "obj", "glb"):
                    source = exports.get(name)
                    if source is None or not source.is_file():
                        raise OSError(f"Missing validated {name.upper()} export.")
                    shutil.copyfile(source, version_dir / f"model.{name}")
                self._atomic_json(project / "spec.json", spec.as_dict())
                self._atomic_json(project / "active.json", {"version": version})
            except Exception:
                shutil.rmtree(version_dir, ignore_errors=True)
                raise
            return self.get_version(project_id, version)

    def get_version(self, project_id: str, version: int | None = None) -> ScriptVersion:
        project = self._project_dir(project_id)
        selected = self.active_version(project_id) if version is None else int(version)
        directory = project / "versions" / f"v{selected}"
        if selected <= 0 or not directory.is_dir():
            raise FileNotFoundError("CAD project version does not exist.")
        return ScriptVersion(
            project_id,
            selected,
            str(directory / "model.py"),
            str(directory / "model.stl"),
            str(directory / "model.obj"),
            str(directory / "model.glb"),
        )

    def active_version(self, project_id: str) -> int:
        try:
            data = json.loads((self._project_dir(project_id) / "active.json").read_text(
                encoding="utf-8"
            ))
        except (OSError, ValueError) as exc:
            raise FileNotFoundError("CAD project state could not be read.") from exc
        version = data.get("version")
        if not isinstance(version, int) or version < 0:
            raise ValueError("CAD project version state is invalid.")
        return version

    def set_active_version(self, project_id: str, version: int) -> ScriptVersion:
        selected = int(version)
        self.get_version(project_id, selected)
        self._atomic_json(
            self._project_dir(project_id) / "active.json", {"version": selected}
        )
        return self.get_version(project_id, selected)

    def previous_version(self, project_id: str) -> ScriptVersion | None:
        current = self.active_version(project_id)
        if current <= 1:
            return None
        return self.get_version(project_id, current - 1)

    def _project_dir(self, project_id: str) -> Path:
        if not project_id or any(char not in "0123456789abcdef" for char in project_id):
            raise ValueError("Invalid CAD project identifier.")
        return self.root / project_id

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, path)
