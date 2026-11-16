"""Cache admission and compatibility rules for generated part functions."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import logging
from datetime import datetime, timezone

from aura_cad.cache.models import CachedPart, ParameterSpec
from aura_cad.cache.signature import normalized_plan, plan_signature
from aura_cad.config import (
    MAX_BOUNDING_DIMENSION_MM,
    MAX_REPAIR_ATTEMPTS,
    PART_CACHE_VERSION_COMPATIBILITY,
)
from aura_cad.errors import SandboxError
from aura_cad.llm.prompts import PROMPT_VERSION
from aura_cad.models import PartPlan
from aura_cad.sandbox.validator import validate_script

LOGGER = logging.getLogger("aura.cad")
_INT_WORDS = {"teeth", "tooth", "holes", "cylinders", "storeys", "count"}


class PartCachePolicy:
    def __init__(
        self,
        backend_name: str | None = None,
        backend_version: str | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self.backend_name = backend_name or "build123d"
        self.backend_version = backend_version or self._backend_version()
        self.prompt_version = prompt_version

    def compatible(self, part: CachedPart) -> bool:
        return (
            part.backend_name == self.backend_name
            and self._version_compatible("backend", self.backend_version, part.backend_version)
            and self._version_compatible("prompt", self.prompt_version, part.prompt_version)
        )

    @staticmethod
    def _version_compatible(kind: str, current: str, stored: str) -> bool:
        if current == stored:
            return True
        allowed = PART_CACHE_VERSION_COMPATIBILITY.get(kind, {}).get(current, ())
        return stored in allowed

    @staticmethod
    def should_store(
        validated: bool, warnings: list[str] | tuple[str, ...], repairs_used: int
    ) -> bool:
        return bool(
            validated
            and not warnings
            and repairs_used < MAX_REPAIR_ATTEMPTS
        )

    @staticmethod
    def function_is_used(script: str, function_name: str) -> bool:
        try:
            tree = ast.parse(script)
        except SyntaxError:
            return False
        build = next(
            (
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "build"
            ),
            None,
        )
        return bool(
            build
            and any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == function_name
                for node in ast.walk(build)
            )
        )

    def create_part(
        self,
        plan: PartPlan,
        function_name: str,
        source: str,
        validated: bool,
        warnings: list[str] | tuple[str, ...] = (),
        repairs_used: int = 0,
    ) -> CachedPart | None:
        if not self.should_store(validated, warnings, repairs_used):
            return None
        normalized = normalized_plan(plan)
        function = self._extract_function(source, function_name)
        if function is None:
            return None
        if self._contains_non_docstring_strings(function):
            LOGGER.warning(
                "Skipping CAD cache entry %s with user-specific text literals",
                function_name,
            )
            return None
        try:
            validate_script(
                "import build123d as bd\nimport math\n"
                + function
                + "\n\ndef build():\n    return None\n"
            )
        except SandboxError:
            LOGGER.warning("Skipping CAD cache entry with non-reusable function %s", function_name)
            return None
        schema = self._parameter_schema(function, normalized.dimensions)
        if schema is None:
            return None
        signature = plan_signature(normalized)
        docstring = f"Parametric {signature.object_family} part."
        now = datetime.now(timezone.utc).isoformat()
        code_hash = hashlib.sha256(function.encode("utf-8")).hexdigest()
        return CachedPart(
            id=signature.value[:32],
            name=normalized.name[:80],
            object_family=signature.object_family,
            tags=signature.tags,
            signature=signature.value,
            param_schema=tuple(schema),
            source_code=function,
            docstring=docstring,
            backend_name=self.backend_name,
            backend_version=self.backend_version,
            prompt_version=self.prompt_version,
            code_sha256=code_hash,
            created_at=now,
            last_used_at=now,
            validated=True,
            function_name=function_name,
        )

    @staticmethod
    def _extract_function(script: str, function_name: str) -> str | None:
        try:
            tree = ast.parse(script)
        except SyntaxError:
            return None
        function = next(
            (
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == function_name
            ),
            None,
        )
        if function is None or function.decorator_list or function.args.defaults:
            return None
        if PartCachePolicy._calls_other_top_level_function(tree, function):
            return None
        clean = ast.Module(body=[function], type_ignores=[])
        PartCachePolicy._replace_docstring(clean, f"Parametric {function_name} part.")
        ast.fix_missing_locations(clean)
        return ast.unparse(clean).strip()

    @staticmethod
    def _calls_other_top_level_function(tree, target) -> bool:
        other_functions = {
            node.name for node in tree.body
            if isinstance(node, ast.FunctionDef) and node is not target
        }
        calls = {
            node.func.id for node in ast.walk(target)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        return bool(other_functions & calls)

    @staticmethod
    def _replace_docstring(module: ast.Module, docstring: str) -> None:
        for node in module.body:
            if not isinstance(node, ast.FunctionDef) or not node.body:
                continue
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    first.value.value = docstring
                    return
            node.body.insert(
                0,
                ast.Expr(value=ast.Constant(value=docstring)),
            )

    @staticmethod
    def _contains_non_docstring_strings(function: str) -> bool:
        node = ast.parse(function).body[0]
        if not isinstance(node, ast.FunctionDef) or not node.body:
            return True
        first = node.body[0]
        doc_location = (
            (first.value.lineno, first.value.col_offset)
            if isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            else None
        )
        return any(
            isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and (item.lineno, item.col_offset) != doc_location
            for item in ast.walk(node)
        )

    @staticmethod
    def _parameter_schema(
        function: str, dimensions: dict[str, float]
    ) -> list[ParameterSpec] | None:
        node = ast.parse(function).body[0]
        if not isinstance(node, ast.FunctionDef):
            return None
        arguments = node.args
        if (
            arguments.posonlyargs
            or arguments.vararg
            or arguments.kwonlyargs
            or arguments.kwarg
            or any(value is not None for value in arguments.kw_defaults)
        ):
            return None
        schema = []
        for argument in arguments.args:
            if argument.arg in {"self", "cls"}:
                continue
            annotation = argument.annotation
            kind = annotation.id if isinstance(annotation, ast.Name) else ""
            if kind not in {"int", "float"} or argument.arg not in dimensions:
                return None
            value = dimensions[argument.arg]
            minimum, maximum = PartCachePolicy._bounds(argument.arg, kind)
            schema.append(
                ParameterSpec(
                    argument.arg,
                    kind,
                    int(value) if kind == "int" else float(value),
                    minimum,
                    maximum,
                    "mm" if argument.arg.endswith("_mm") else "",
                )
            )
        return schema

    @staticmethod
    def _bounds(name: str, kind: str):
        if name in _INT_WORDS or any(term in name.split("_") for term in _INT_WORDS):
            return (6, 120) if "teeth" in name or "tooth" in name else (1, 1000)
        if "module" in name:
            return 0.2, 10.0
        if name.endswith("_mm"):
            return 0.1, MAX_BOUNDING_DIMENSION_MM
        return (0, 1000) if kind == "int" else (None, None)

    @staticmethod
    def _backend_version() -> str:
        names = ("build123d", "cadquery-ocp-novtk")
        versions = []
        for name in names:
            try:
                versions.append(f"{name}={importlib.metadata.version(name)}")
            except importlib.metadata.PackageNotFoundError:
                versions.append(f"{name}=missing")
        return ";".join(versions)
