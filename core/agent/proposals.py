"""Safe proposal and validation path for new generated plugins."""

from __future__ import annotations

import ast
from pathlib import Path


class PluginProposalService:
    """Validate generated plugin code without touching core files."""

    def __init__(self, root: str | Path = "plugins/generated"):
        self.root = Path(root).resolve()

    def validate(self, name: str, code: str) -> Path:
        """Parse and compile a proposed plugin inside the generated directory."""
        safe_name = "".join(
            char for char in str(name) if char.isalnum() or char == "_"
        ).strip("_")
        if not safe_name:
            raise ValueError("plugin name is invalid")
        tree = ast.parse(code, filename=f"{safe_name}.py")
        if any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and any(alias.name in {"subprocess", "ctypes"} for alias in node.names)
            for node in ast.walk(tree)
        ):
            raise PermissionError(
                "generated plugins cannot import subprocess or ctypes"
            )
        compile(tree, f"{safe_name}.py", "exec")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{safe_name}.py"
        if self.root not in target.resolve().parents:
            raise PermissionError("plugin target escaped generated directory")
        return target

    def save_after_confirmation(
        self, name: str, code: str, confirmed: bool = False
    ) -> str:
        """Save only confirmed, validated plugin code and report restart registration."""
        target = self.validate(name, code)
        if not confirmed:
            return f"CONFIRMATION_REQUIRED\n{target}\n{code[:12000]}"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(code, encoding="utf-8")
        temporary.replace(target)
        return f"Plugin proposal saved to {target}; restart to register it."
