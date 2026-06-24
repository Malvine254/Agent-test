"""Best-effort sandboxed Python executor for the code-interpreter tool.

Design goals (in priority order):
  1. **Isolation** — run model-generated code in a *separate process* (spawn) so a
     crash, segfault, infinite loop or memory blow-up cannot take down the bot.
  2. **Hard limits** — wall-clock timeout (always) plus CPU/address-space limits
     where the OS supports them (POSIX / Linux production on Container Apps).
  3. **No ambient secrets** — the child scrubs credential-like environment
     variables before running, so injected code cannot read Key Vault keys,
     connection strings or the bot password from os.environ.
  4. **No network** — sockets are disabled in the child (best effort).
  5. **Scoped filesystem** — code runs inside a private temp workspace; any files
     it writes there are collected and returned to the caller as artifacts.

This is a *semi-trusted* sandbox: the code originates from the LLM acting on user
intent, not from an arbitrary attacker. It defends against buggy/runaway code and
casual credential exfiltration. It is NOT a substitute for OS-level isolation
against a determined adversary. On Linux production, resource limits apply; on
Windows (local dev) only the wall-clock timeout and process isolation apply.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import shutil
import tempfile
import textwrap
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Environment variables matching these (case-insensitive) substrings are removed
# from the child process before any user code runs.
_SECRET_ENV_MARKERS = (
    "key", "secret", "password", "passwd", "token", "connection", "conn_str",
    "connectionstring", "sas", "credential", "client_secret", "api",
)

# Files larger than this are not returned inline (path is still reported).
MAX_ARTIFACT_BYTES = int(os.environ.get("SANDBOX_MAX_ARTIFACT_BYTES", str(25 * 1024 * 1024)))
# Stdout/stderr captured from the child are truncated to keep prompts bounded.
MAX_CAPTURE_CHARS = int(os.environ.get("SANDBOX_MAX_CAPTURE_CHARS", "20000"))


@dataclass
class ExecResult:
    """Outcome of a sandboxed execution."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str = ""          # traceback / failure reason, empty on success
    timed_out: bool = False
    result_repr: str = ""    # repr() of a `result` variable if the code set one
    # Generated files: {filename: absolute_path_in_workspace}. The workspace is a
    # temp dir owned by the caller's process and must be consumed before cleanup().
    files: dict[str, str] = field(default_factory=dict)
    workspace: str = ""      # temp dir; caller calls cleanup() when done

    def cleanup(self) -> None:
        if self.workspace and os.path.isdir(self.workspace):
            shutil.rmtree(self.workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Child process entry point (must be top-level for spawn pickling)
# ---------------------------------------------------------------------------
def _child_main(code: str, workdir: str, result_path: str, memory_mb: int) -> None:
    import io
    import sys
    import traceback
    import contextlib

    # 1. Scrub credential-like environment variables.
    for k in list(os.environ.keys()):
        kl = k.lower()
        if any(m in kl for m in _SECRET_ENV_MARKERS):
            os.environ.pop(k, None)

    # 2. Apply OS resource limits where available (POSIX / Linux prod).
    try:
        import resource  # type: ignore

        if memory_mb and memory_mb > 0:
            limit = memory_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ValueError, OSError):
                pass
        # CPU seconds cap as a secondary guard against busy loops.
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
        except (ValueError, OSError):
            pass
    except Exception:
        # resource module absent on Windows — rely on parent's wall-clock timeout.
        pass

    # 3. Disable networking (best effort). We patch the *methods* rather than
    #    replacing socket.socket, because `ssl` and other stdlib modules subclass
    #    socket.socket at import time \u2014 clobbering the class breaks innocent imports
    #    (e.g. reportlab -> urllib -> ssl). Blocking connect() denies outbound traffic
    #    while keeping the class hierarchy intact.
    try:
        import socket

        def _no_net(*_a, **_k):
            raise OSError("Network access is disabled in the sandbox.")

        socket.socket.connect = _no_net          # type: ignore[assignment]
        socket.socket.connect_ex = _no_net        # type: ignore[assignment]
        socket.create_connection = _no_net        # type: ignore[assignment]
    except Exception:
        pass

    # 4. Run inside the private workspace.
    try:
        os.chdir(workdir)
    except Exception:
        pass

    # 5. Headless matplotlib so charts render without a display.
    os.environ["MPLBACKEND"] = "Agg"

    before = set(os.listdir(workdir)) if os.path.isdir(workdir) else set()

    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    error = ""
    result_repr = ""
    exec_globals: dict = {"__name__": "__sandbox__", "__builtins__": __builtins__}

    try:
        compiled = compile(textwrap.dedent(code), "<sandbox>", "exec")
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exec(compiled, exec_globals)  # noqa: S102 - intentional, isolated process
        # Capture an explicit `result` variable if the code defined one.
        if "result" in exec_globals:
            try:
                result_repr = repr(exec_globals["result"])[:4000]
            except Exception:
                result_repr = "<unrepresentable result>"
    except SystemExit:
        pass
    except BaseException:  # noqa: BLE001 - report everything back to the model
        error = traceback.format_exc(limit=8)

    after = set(os.listdir(workdir)) if os.path.isdir(workdir) else set()
    new_files = sorted(after - before)

    payload = {
        "stdout": stdout_buf.getvalue()[:MAX_CAPTURE_CHARS],
        "stderr": stderr_buf.getvalue()[:MAX_CAPTURE_CHARS],
        "error": error[:MAX_CAPTURE_CHARS],
        "result_repr": result_repr,
        "new_files": new_files,
    }
    try:
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_python(
    code: str,
    *,
    input_files: Optional[dict[str, bytes]] = None,
    timeout: float = 25.0,
    memory_mb: int = 768,
) -> ExecResult:
    """Execute ``code`` in an isolated child process.

    Args:
        code: Python source. May read ``input_files`` by name from the cwd and
            write output files (charts, .xlsx, .docx, .pdf, .zip, ...) to the cwd.
        input_files: {filename: raw_bytes} written into the workspace before run.
        timeout: Wall-clock limit in seconds; the process is killed if exceeded.
        memory_mb: Address-space cap (POSIX only).

    Returns:
        ExecResult. Caller MUST call ``result.cleanup()`` once any returned files
        in ``result.files`` have been consumed.
    """
    workdir = tempfile.mkdtemp(prefix="ci_sandbox_")
    result_path = os.path.join(workdir, "__result__.json")

    # Seed input files (e.g. an uploaded spreadsheet) into the workspace.
    input_names: set[str] = set()
    for name, data in (input_files or {}).items():
        safe = os.path.basename(str(name)) or "input.bin"
        try:
            with open(os.path.join(workdir, safe), "wb") as fh:
                fh.write(data)
            input_names.add(safe)
        except Exception as exc:  # pragma: no cover - disk issues
            logger.warning("sandbox: failed to seed input file %s: %s", safe, exc)
    input_names.add("__result__.json")

    ctx = mp.get_context("spawn")
    proc = ctx.Process(
        target=_child_main,
        args=(code, workdir, result_path, memory_mb),
        daemon=True,
    )
    proc.start()
    proc.join(timeout)

    timed_out = False
    if proc.is_alive():
        timed_out = True
        proc.terminate()
        proc.join(3)
        if proc.is_alive():  # pragma: no cover - stubborn process
            proc.kill()
            proc.join(2)

    payload = {}
    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            payload = {}

    # Collect generated artifacts (anything new that isn't an input/result file).
    files: dict[str, str] = {}
    try:
        for name in sorted(os.listdir(workdir)):
            if name in input_names:
                continue
            path = os.path.join(workdir, name)
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getsize(path) > MAX_ARTIFACT_BYTES:
                    logger.warning("sandbox: artifact %s exceeds size cap, skipping", name)
                    continue
            except OSError:
                continue
            files[name] = path
    except Exception:
        pass

    if timed_out:
        return ExecResult(
            ok=False,
            timed_out=True,
            error=f"Execution exceeded the {timeout:.0f}s time limit and was stopped.",
            stdout=str(payload.get("stdout", "")),
            files=files,
            workspace=workdir,
        )

    if not payload:
        return ExecResult(
            ok=False,
            error="The sandbox process exited without producing a result "
                  f"(exit code {proc.exitcode}). The code may have crashed or "
                  "exceeded the memory limit.",
            files=files,
            workspace=workdir,
        )

    error = str(payload.get("error", ""))
    return ExecResult(
        ok=not error,
        stdout=str(payload.get("stdout", "")),
        stderr=str(payload.get("stderr", "")),
        error=error,
        result_repr=str(payload.get("result_repr", "")),
        files=files,
        workspace=workdir,
    )
