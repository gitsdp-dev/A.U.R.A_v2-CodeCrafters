"""Run already-validated CAD code in a bounded child process."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from aura_cad.config import (
    MAX_MESH_BYTES,
    MAX_OUTPUT_BYTES,
    SCRIPT_MEMORY_BYTES,
    SCRIPT_TIMEOUT_SECONDS,
)
from aura_cad.errors import BackendUnavailable, SandboxError, SandboxTimeout
from aura_cad.sandbox.validator import validate_script

LOGGER = logging.getLogger("aura.cad")
_WORKER = Path(__file__).with_name("worker.py")


class _WindowsJob:
    """Apply a process memory cap and reliably terminate the worker process tree."""

    def __init__(self, process: subprocess.Popen, memory_bytes: int, job_id: str) -> None:
        self._handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimit(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
                )]

            class ExtendedLimit(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimit),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            self._handle = handle
            limits = ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x100
            limits.ProcessMemoryLimit = memory_bytes
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            if not kernel32.AssignProcessToJobObject(
                handle, wintypes.HANDLE(int(process._handle))
            ):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except Exception as exc:
            LOGGER.warning(
                "[job=%s] Could not apply the CAD Windows memory cap: %s",
                job_id,
                type(exc).__name__,
            )
            self.close()

    def terminate(self, process: subprocess.Popen) -> None:
        if self._handle and os.name == "nt":
            try:
                import ctypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.TerminateJobObject.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint,
                ]
                kernel32.TerminateJobObject(self._handle, 1)
                return
            except (AttributeError, OSError):
                pass
        if process.poll() is None:
            process.kill()

    def close(self) -> None:
        if self._handle and os.name == "nt":
            try:
                import ctypes

                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            except (AttributeError, OSError):
                pass
            self._handle = None


class SandboxRunner:
    def __init__(
        self,
        timeout_seconds: float = SCRIPT_TIMEOUT_SECONDS,
        output_limit: int = MAX_OUTPUT_BYTES,
        memory_limit: int = SCRIPT_MEMORY_BYTES,
        worker_path: Path | None = None,
    ) -> None:
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.output_limit = max(1024, int(output_limit))
        self.memory_limit = max(256 * 1024 * 1024, int(memory_limit))
        self.worker_path = worker_path or _WORKER
        self._process: subprocess.Popen | None = None
        self._job: _WindowsJob | None = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        process = self._process
        if process is not None and process.poll() is None:
            if self._job is not None:
                self._job.terminate(process)
            else:
                process.kill()

    def run(self, script: str, mesh_path: Path, job_id: str = "") -> str:
        validate_script(script)
        if self._cancelled.is_set():
            raise SandboxError("CAD job cancelled.")
        with TemporaryDirectory(prefix="aura-cad-") as temp_name:
            temp = Path(temp_name)
            source_path = temp / "generated.py"
            source_path.write_text(script, encoding="utf-8")
            mesh_path.parent.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, "-I", str(self.worker_path), str(source_path), str(mesh_path)]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                cwd=temp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            self._process = process
            job = _WindowsJob(process, self.memory_limit, job_id)
            self._job = job
            captured = {"stdout": bytearray(), "stderr": bytearray()}
            overflow = threading.Event()
            readers = [
                threading.Thread(
                    target=self._capture,
                    args=(stream, captured[name], overflow, process, job),
                    daemon=True,
                )
                for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
                if stream is not None
            ]
            for reader in readers:
                reader.start()
            try:
                process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                job.terminate(process)
                process.wait()
                raise SandboxTimeout("CAD script exceeded its time limit.") from exc
            finally:
                self._process = None
                job.close()
                self._job = None
            for reader in readers:
                reader.join(timeout=2)
            if overflow.is_set():
                raise SandboxError("CAD worker output exceeded its size limit.")
            if self._cancelled.is_set():
                raise SandboxError("CAD job cancelled.")
            if not mesh_path.is_file() or mesh_path.stat().st_size <= 0:
                details = bytes(captured["stderr"]).decode("utf-8", "replace")
                stdout = bytes(captured["stdout"]).decode("utf-8", "replace")
                raise SandboxError(self._worker_error(stdout, details))
            if mesh_path.stat().st_size > MAX_MESH_BYTES:
                raise SandboxError("CAD mesh exceeded the output size limit.")
            return bytes(captured["stdout"]).decode("utf-8", "replace")[-self.output_limit:]

    def _capture(self, stream, target: bytearray, overflow: threading.Event,
                 process: subprocess.Popen, job: _WindowsJob) -> None:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            if len(target) + len(chunk) > self.output_limit:
                overflow.set()
                job.terminate(process)
                target.extend(chunk[:max(0, self.output_limit - len(target))])
            else:
                target.extend(chunk)

    @staticmethod
    def _worker_error(stdout: str, stderr: str) -> str:
        import json

        for line in reversed(stdout.splitlines()):
            try:
                status = json.loads(line)
            except (TypeError, ValueError):
                continue
            if status.get("ok") is False:
                return str(status.get("error") or "CAD script failed.")[:700]
        return (stderr or stdout or "CAD worker failed without a status message.")[-700:]


def ensure_backend_available() -> None:
    try:
        import build123d  # noqa: F401
        import trimesh  # noqa: F401
    except (ImportError, OSError) as exc:
        raise BackendUnavailable(
            "CAD support is not installed for this Python environment."
        ) from exc
