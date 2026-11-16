"""Produce STL, OBJ, and GLB copies from one validated STL mesh."""

from __future__ import annotations

from pathlib import Path

from aura_cad.config import MAX_MESH_BYTES
from aura_cad.errors import ExportError


def export_meshes(stl_path: Path, output_dir: Path, mesh=None) -> dict[str, Path]:
    """Export the two interchange formats using the shared trimesh backend."""
    try:
        import trimesh

        if mesh is None:
            mesh = trimesh.load(str(stl_path), force="mesh", process=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = {
            "stl": output_dir / "model.stl",
            "obj": output_dir / "model.obj",
            "glb": output_dir / "model.glb",
        }
        if stl_path.resolve() != outputs["stl"].resolve():
            mesh.export(str(outputs["stl"]), file_type="stl")
        mesh.export(str(outputs["obj"]), file_type="obj")
        mesh.export(str(outputs["glb"]), file_type="glb")
        for path in outputs.values():
            if not path.is_file() or path.stat().st_size == 0:
                raise OSError(f"{path.suffix.upper()} export is empty")
            if path.stat().st_size > MAX_MESH_BYTES:
                raise OSError(f"{path.suffix.upper()} export exceeded the size limit")
        return outputs
    except Exception as exc:
        raise ExportError("The model could not be exported to STL, OBJ, and GLB.") from exc
