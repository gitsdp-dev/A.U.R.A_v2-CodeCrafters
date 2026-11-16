"""AST allowlist before running any model-generated Python code."""

from __future__ import annotations

import ast

from aura_cad.config import MAX_SCRIPT_BYTES
from aura_cad.errors import SandboxError

_IMPORTS = {"build123d", "math"}
_BUILTINS = {
    "abs", "all", "any", "bool", "enumerate", "float", "int", "len",
    "list", "max", "min", "range", "round", "sum", "tuple", "zip",
}
BUILD123D_API = frozenset({
    "Align", "Axis", "Box", "Circle", "Cone", "Cylinder", "Mode", "Part",
    "Plane", "Pos", "Rectangle", "Rot", "Sphere", "chamfer", "extrude",
    "fillet",
})
MATH_CONSTANTS = frozenset({"e", "inf", "nan", "pi", "tau"})
MATH_FUNCTIONS = frozenset({
    "acos", "acosh", "asin", "asinh", "atan", "atan2", "atanh", "ceil",
    "copysign", "cos", "cosh", "degrees", "dist", "erf", "erfc", "exp",
    "expm1", "fabs", "floor", "fmod", "frexp", "fsum", "gamma", "hypot",
    "isclose", "isfinite", "isinf", "isnan", "ldexp", "lgamma", "log",
    "log10", "log1p", "log2", "modf", "nextafter", "pow", "radians",
    "remainder", "sin", "sinh", "sqrt", "tan", "tanh", "trunc", "ulp",
})
MATH_API = MATH_CONSTANTS | MATH_FUNCTIONS
_DENIED_CALLS = {
    "__import__", "breakpoint", "compile", "eval", "exec", "getattr", "globals",
    "help", "input", "locals", "open", "setattr", "vars",
}
_ALLOWED_NODES = (
    ast.Module, ast.Import, ast.ImportFrom, ast.alias, ast.FunctionDef, ast.arguments,
    ast.arg, ast.Return, ast.Expr, ast.Call, ast.Name, ast.Load, ast.Store,
    ast.Constant, ast.Attribute, ast.BinOp, ast.UnaryOp, ast.operator,
    ast.unaryop, ast.Compare, ast.cmpop, ast.BoolOp, ast.boolop, ast.If, ast.For,
    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.List, ast.Tuple, ast.Dict,
    ast.Set, ast.Subscript, ast.Slice, ast.keyword, ast.Pass, ast.Break,
    ast.Continue, ast.IfExp, ast.ListComp, ast.comprehension,
)


class _Validator(ast.NodeVisitor):
    def __init__(self, function_names: set[str]) -> None:
        self._function_names = function_names

    def generic_visit(self, node):
        if not isinstance(node, _ALLOWED_NODES):
            raise SandboxError(f"Unsupported Python syntax: {type(node).__name__}.")
        super().generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name not in _IMPORTS or alias.asname not in (None, "bd", "math"):
                raise SandboxError(f"Import is not allowed: {alias.name}.")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module not in _IMPORTS or node.level:
            raise SandboxError("Only build123d and math imports are allowed.")
        raise SandboxError("Use `import build123d as bd`; direct imports are disabled.")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise SandboxError("Private and dunder names are not allowed.")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise SandboxError("Private and dunder attribute access is not allowed.")
        root = node.value.id if isinstance(node.value, ast.Name) else None
        allowed = BUILD123D_API if root == "bd" else MATH_API if root == "math" else set()
        if node.attr not in allowed:
            raise SandboxError(f"Attribute is not allowed: {node.attr}.")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if (
                node.func.id in _DENIED_CALLS
                or node.func.id not in _BUILTINS | self._function_names
            ):
                raise SandboxError(f"Call is not allowed: {node.func.id}.")
        elif not isinstance(node.func, ast.Attribute):
            raise SandboxError("Dynamic function calls are not allowed.")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        raise SandboxError("Unbounded while-loops are not allowed.")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name.startswith("_") or node.decorator_list or node.args.defaults:
            raise SandboxError("Private, decorated, or default-argument functions are not allowed.")
        if node.name != "build" and not node.name.isidentifier():
            raise SandboxError("Invalid function name.")
        self.generic_visit(node)


def validate_script(script: str) -> ast.Module:
    """Parse and reject code outside the CAD-only allowlist."""
    if not isinstance(script, str) or not script.strip():
        raise SandboxError("The generated CAD script is empty.")
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise SandboxError("The generated CAD script exceeded the size limit.")
    try:
        tree = ast.parse(script, mode="exec")
    except SyntaxError as exc:
        raise SandboxError("The generated CAD script has invalid Python syntax.") from exc
    function_names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    _Validator(function_names).visit(tree)
    if "build" not in function_names:
        raise SandboxError("The generated CAD script must define build().")
    return tree
