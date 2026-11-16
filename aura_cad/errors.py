"""Documented errors translated into short HUD messages by the integration."""


class CadError(RuntimeError):
    """Base exception for expected CAD failures."""


class SpecError(CadError):
    """The requested specification is incomplete or invalid."""


class CodegenError(CadError):
    """Planning or code generation failed."""


class SandboxError(CadError):
    """Generated code failed static or runtime safety checks."""


class SandboxTimeout(SandboxError):
    """Generated code exceeded its execution deadline."""


class ValidationError(CadError):
    """The generated mesh did not pass geometry checks."""


class ExportError(CadError):
    """A requested model format could not be exported."""


class BackendUnavailable(CadError):
    """The optional CAD backend is not installed or is incompatible."""
