"""Generate and refine bounded build123d scripts."""

from __future__ import annotations

import ast
import json

from aura_cad.cache.models import CacheAction, PartResolution
from aura_cad.llm.adapter import CadLLMAdapter, strip_code_fence
from aura_cad.llm.prompts import (
    CODEGEN_PROMPT,
    PART_CODEGEN_PROMPT,
    REFINE_PROMPT,
)
from aura_cad.models import CadSpec, PartPlan
from aura_cad.sandbox.validator import MATH_API, MATH_CONSTANTS, MATH_FUNCTIONS


class CadCodeGenerator:
    def __init__(self, llm: CadLLMAdapter) -> None:
        self._llm = llm

    def generate(self, spec: CadSpec, plan: list[PartPlan], job_id: str) -> str:
        plan_json = json.dumps(
            [
                {"name": part.name, "description": part.description,
                 "dimensions": part.dimensions}
                for part in plan
            ],
            ensure_ascii=True,
        )
        script = strip_code_fence(self._llm.generate(
            CODEGEN_PROMPT.format(
                spec=json.dumps(spec.as_dict(), ensure_ascii=True),
                plan=plan_json,
            ),
            job_id,
        ))
        return normalize_build123d_math(script)

    def generate_for_parts(
        self, spec: CadSpec, resolutions: list[PartResolution], job_id: str
    ) -> str:
        """Ask for new part functions only; cached functions remain verbatim."""
        if len(resolutions) == 1 and resolutions[0].part is not None:
            return self._single_cached_script(resolutions[0])
        generated = [item for item in resolutions if item.action == CacheAction.GENERATE]
        reusable = [item for item in resolutions if item.part is not None]
        prompt = PART_CODEGEN_PROMPT.format(
            spec=json.dumps(spec.as_dict(), ensure_ascii=True),
            generate_parts=json.dumps(
                [self._part_payload(item) for item in generated], ensure_ascii=True
            ),
            reusable_parts=json.dumps(
                [self._reuse_payload(item) for item in reusable], ensure_ascii=True
            ),
        )
        script = strip_code_fence(self._llm.generate(prompt, job_id))
        cached_sources = [
            item.part.source_code for item in reusable if item.part is not None
        ]
        combined = script.rstrip() + (
            "\n\n" + "\n\n".join(cached_sources) if cached_sources else ""
        )
        return normalize_build123d_math(combined)

    @staticmethod
    def _part_payload(item: PartResolution) -> dict:
        return {
            "function_name": item.function_name,
            "name": item.plan.name,
            "description": item.plan.description,
            "dimensions": item.plan.dimensions,
        }

    @staticmethod
    def _reuse_payload(item: PartResolution) -> dict:
        part = item.part
        return {
            "function_name": item.function_name,
            "name": part.name,
            "signature": part.signature,
            "parameters": item.parameters,
            "param_schema": [param.as_dict() for param in part.param_schema],
        }

    @staticmethod
    def _single_cached_script(item: PartResolution) -> str:
        part = item.part
        if part is None:
            raise ValueError("A cached part is required for deterministic assembly.")
        arguments = ", ".join(
            f"{name}={value!r}" for name, value in item.parameters.items()
        )
        call = f"{item.function_name}({arguments})"
        return (
            "import build123d as bd\nimport math\n\n"
            f"{part.source_code}\n\n"
            "def build():\n"
            f"    return {call}\n"
        )

    @staticmethod
    def apply_parameter_values(
        script: str, resolutions: list[PartResolution]
    ) -> str | None:
        """Replace only keyword arguments to cached functions in the build entry point."""
        updates = {
            item.function_name: item.parameters
            for item in resolutions
            if item.action == CacheAction.ADAPT and item.part is not None
        }
        if not updates:
            return None
        try:
            tree = ast.parse(script)
        except SyntaxError:
            return None
        build = next(
            (
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "build"
            ),
            None,
        )
        if build is None:
            return None
        transformer = _BuildCallParameterUpdater(updates)
        for statement in build.body:
            transformer.visit(statement)
        if not transformer.changed:
            return None
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    def refine(self, spec: CadSpec, script: str, request: str, job_id: str) -> str:
        refined = strip_code_fence(self._llm.generate(
            REFINE_PROMPT.format(
                spec=json.dumps(spec.as_dict(), ensure_ascii=True),
                script=script,
                request=request[:500],
            ),
            job_id,
        ))
        return normalize_build123d_math(refined)


def normalize_build123d_math(script: str) -> str:
    """Canonicalize approved math names while keeping the sandbox allowlist strict."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return script
    bound_names = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    bound_names.update(
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
    )
    bound_names.update(
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    imported_math = _imported_math_names(tree, bound_names)
    build_aliases = _import_module_aliases(
        tree, bound_names, "build123d"
    )
    math_aliases = _import_module_aliases(tree, bound_names, "math")
    transformer = _Build123dMathAlias(
        bound_names, imported_math, build_aliases, math_aliases
    )
    tree = transformer.visit(tree)
    uses_math = any(
        isinstance(node, ast.Name) and node.id == "math"
        for node in ast.walk(tree)
    )
    has_math_import = any(
        isinstance(node, ast.Import)
        and any(alias.name == "math" and alias.asname in (None, "math")
                for alias in node.names)
        for node in tree.body
    )
    if transformer.changed or (uses_math and not has_math_import):
        if not has_math_import:
            tree.body.insert(0, ast.Import(names=[ast.alias(name="math")]))
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)
    return script


def _imported_math_names(
    tree: ast.Module, bound_names: set[str]
) -> dict[str, str]:
    imported = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != "math" or node.level:
            continue
        names = {alias.name for alias in node.names}
        if (
            "*" in names
            or not names.issubset(MATH_API)
            or any((alias.asname or alias.name) in bound_names for alias in node.names)
        ):
            continue
        imported.update({
            alias.asname or alias.name: alias.name for alias in node.names
        })
    return imported


def _import_module_aliases(
    tree: ast.Module, bound_names: set[str], module: str
) -> set[str]:
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == module and (alias.asname or alias.name) not in bound_names
    }


class _Build123dMathAlias(ast.NodeTransformer):
    def __init__(
        self,
        bound_names: set[str],
        imported_math: dict[str, str],
        build_aliases: set[str],
        math_aliases: set[str],
    ) -> None:
        self.bound_names = bound_names
        self.imported_math = imported_math
        self.build_aliases = build_aliases
        self.math_aliases = math_aliases
        self.changed = False

    def visit_Import(self, node: ast.Import):
        names = []
        for alias in node.names:
            local_name = alias.asname or alias.name
            if alias.name == "build123d" and local_name in self.build_aliases:
                names.append(ast.alias(name="build123d", asname="bd"))
                self.changed |= local_name != "bd"
            elif alias.name == "math" and local_name in self.math_aliases:
                names.append(ast.alias(name="math"))
                self.changed |= local_name != "math"
            else:
                names.append(alias)
        node.names = names
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if (
            node.module == "math"
            and not node.level
            and all(
                (alias.asname or alias.name) in self.imported_math
                for alias in node.names
            )
        ):
            self.changed = True
            return None
        return node

    def visit_Call(self, node: ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in MATH_FUNCTIONS
            and node.func.id not in self.bound_names
        ):
            node.func = ast.Attribute(
                value=ast.Name(id="math", ctx=ast.Load()),
                attr=node.func.id,
                ctx=ast.Load(),
            )
            self.changed = True
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load) and node.id in self.build_aliases:
            if node.id != "bd":
                self.changed = True
                return ast.Name(id="bd", ctx=node.ctx)
        if isinstance(node.ctx, ast.Load) and node.id in self.math_aliases:
            if node.id != "math":
                self.changed = True
                return ast.Name(id="math", ctx=node.ctx)
        imported_name = self.imported_math.get(node.id)
        if imported_name and isinstance(node.ctx, ast.Load):
            self.changed = True
            return ast.Attribute(
                value=ast.Name(id="math", ctx=ast.Load()),
                attr=imported_name,
                ctx=ast.Load(),
            )
        if (
            isinstance(node.ctx, ast.Load)
            and node.id in MATH_CONSTANTS
            and node.id not in self.bound_names
        ):
            self.changed = True
            return ast.Attribute(
                value=ast.Name(id="math", ctx=ast.Load()),
                attr=node.id,
                ctx=ast.Load(),
            )
        return node

    def visit_Attribute(self, node: ast.Attribute):
        self.generic_visit(node)
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "bd"
            and node.attr in MATH_API
        ):
            node.value = ast.Name(id="math", ctx=ast.Load())
            self.changed = True
        return node


class _BuildCallParameterUpdater(ast.NodeTransformer):
    def __init__(self, updates: dict[str, dict[str, int | float]]) -> None:
        self.updates = updates
        self.changed = 0

    def visit_Call(self, node: ast.Call):
        self.generic_visit(node)
        if not isinstance(node.func, ast.Name) or node.func.id not in self.updates:
            return node
        parameters = self.updates[node.func.id]
        if node.args or {item.arg for item in node.keywords} != set(parameters):
            return node
        for keyword in node.keywords:
            value = parameters[keyword.arg]
            keyword.value = ast.Constant(value=value)
            self.changed += 1
        return node
