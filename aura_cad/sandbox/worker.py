"""Subprocess entry point; this is defense-in-depth, not a perfect sandbox."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
from pathlib import Path


class _LimitedOutput(io.TextIOBase):
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0

    def write(self, text: str) -> int:
        size = len(text.encode("utf-8", errors="replace"))
        self.used += size
        if self.used > self.maximum:
            raise RuntimeError("Generated code exceeded its output limit.")
        return len(text)


def _set_memory_limit() -> None:
    if sys.platform == "win32":
        return
    try:
        import resource

        maximum = 2 * 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))
    except (ImportError, OSError, ValueError):
        return


def _load_safe_namespace() -> dict:
    import build123d as bd
    import math

    safe_builtins = {
        name: getattr(__builtins__, name) if not isinstance(__builtins__, dict)
        else __builtins__[name]
        for name in (
            "__import__",
            "abs", "all", "any", "bool", "enumerate", "float", "int", "len",
            "list", "max", "min", "range", "round", "sum", "tuple", "zip",
        )
    }
    return {"__builtins__": safe_builtins, "bd": bd, "math": math}


def main() -> int:
    if len(sys.argv) != 3:
        print(json.dumps({"ok": False, "error": "Invalid worker arguments."}))
        return 2
    _set_memory_limit()
    source_path, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, mode="exec")
        namespace = _load_safe_namespace()
        with contextlib.redirect_stdout(_LimitedOutput(64 * 1024)):
            exec(compile(tree, str(source_path), "exec"), namespace, namespace)
            shape = namespace["build"]()
        if shape is None:
            raise ValueError("build() returned no shape.")
        from build123d import export_stl

        export_stl(shape, str(output_path))
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ValueError("The backend did not write an STL mesh.")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:600]}"}))
        return 1
    print(json.dumps({"ok": True, "size": output_path.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
