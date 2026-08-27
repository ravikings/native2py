"""Subprocess wrapper around the `ngate` CLI.

No project dependencies (no console.db, no console.app) — this module is
standalone so it can be reused/tested in isolation.

`ngate inspect <path>` does NOT emit JSON (confirmed by running it against
services/petro_api/native/petro_api.f90 and libraries/petro/cpp/include/*.hpp
in this repo, and by reading nativegate/cli.py's `inspect` command directly:
tools/nativegate/nativegate/cli.py, function `inspect`, ~line 465). It prints
a small human-readable tree via `click.echo`, e.g. for Fortran:

    module: petro_api (fortran)
      function pvt_set_fluid(api_gravity: float, gas_gravity: float, temp_f: float, icorr: int) -> void

and for C++ (with an extra leading "parser: ..." line, class/struct blocks):

    parser: clang AST (libclang 18.1.1)
    module: FluidModel (cpp)
      class FluidModel
        activate() -> None
        solution_gor(pressure: float) -> float
      struct PvtState
        bo: float
        ...
      function some_free_function(x: float) -> float

`inspect_ir` parses that text format into a dict (there is no --json/--format
flag on `ngate inspect` as of tools/nativegate/nativegate/cli.py).
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class NgateResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str


def run(
    *args: str,
    cwd: str | Path | None = None,
    timeout: int = 600,
    on_line: Callable[[str], None] | None = None,
) -> NgateResult:
    """Run `ngate <args>`. Never raises on nonzero exit.

    Without `on_line`: blocks silently and returns the full output only once
    the process exits — fine for a quick command like `detect`, but a
    multi-second-to-multi-minute step (`generate`, `build`) leaves the caller
    with nothing to show a user until it's already over.

    With `on_line`: streams stdout+stderr (merged, since the caller wants
    real-time progress, not a stdout/stderr split) line by line as the
    subprocess produces it, calling `on_line(line)` for each one as it
    arrives — the console's build page (console/jobs.py) uses this to push
    live lines onto the SSE log instead of the log pane sitting empty for the
    whole step. The returned `NgateResult.stdout` still carries the full
    combined output for anything that inspects it after the fact;
    `NgateResult.stderr` is empty in this mode since the two streams have
    already been merged into `stdout`/the `on_line` calls.
    """
    if on_line is not None:
        return _run_streaming(args, cwd=cwd, timeout=timeout, on_line=on_line)

    try:
        proc = subprocess.run(
            ["ngate", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        return NgateResult(ok=False, returncode=127, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return NgateResult(
            ok=False,
            returncode=-1,
            stdout=stdout,
            stderr=stderr + f"\n[ngate.run] timed out after {timeout}s",
        )
    return NgateResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _run_streaming(
    args: tuple[str, ...],
    *,
    cwd: str | Path | None,
    timeout: int,
    on_line: Callable[[str], None],
) -> NgateResult:
    try:
        proc = subprocess.Popen(
            ["ngate", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        )
    except FileNotFoundError as exc:
        return NgateResult(ok=False, returncode=127, stdout="", stderr=str(exc))

    # A blocking `for line in proc.stdout` can't be interrupted to check a
    # deadline, so read it on a separate thread and hand lines back through a
    # queue the main loop polls with a timeout — that's what makes the
    # overall `timeout` enforceable here the same way it is in the
    # subprocess.run() path above.
    line_queue: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line_queue.put(raw_line.rstrip("\n"))
        line_queue.put(None)  # sentinel: stream closed

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    lines: list[str] = []
    deadline = time.monotonic() + timeout
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            proc.kill()
            break
        try:
            line = line_queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        lines.append(line)
        on_line(line)

    if timed_out:
        proc.wait(timeout=5)
        msg = f"[ngate.run] timed out after {timeout}s"
        lines.append(msg)
        on_line(msg)
        return NgateResult(ok=False, returncode=-1, stdout="\n".join(lines), stderr="")

    returncode = proc.wait(timeout=5)
    return NgateResult(
        ok=returncode == 0, returncode=returncode, stdout="\n".join(lines), stderr=""
    )


def detect(path: str | Path) -> NgateResult:
    return run("detect", str(path))


_MODULE_RE = re.compile(r"^module:\s*(?P<name>\S+)\s*\((?P<lang>\w+)\)\s*$")
_CLASS_RE = re.compile(r"^  class (?P<name>\S+)\s*$")
_STRUCT_RE = re.compile(r"^  struct (?P<name>\S+)\s*$")
_TOP_FUNCTION_RE = re.compile(
    r"^  function (?P<name>\w+)\((?P<params>.*)\)\s*->\s*(?P<returns>\S+)\s*$"
)
_METHOD_RE = re.compile(
    r"^    (?P<name>\w+)\((?P<params>.*)\)\s*->\s*(?P<returns>\S+)\s*$"
)
_FIELD_RE = re.compile(r"^    (?P<name>\w+):\s*(?P<type>\S+)\s*$")


def _parse_params(raw: str) -> list[dict]:
    params = []
    raw = raw.strip()
    if not raw:
        return params
    for part in raw.split(", "):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, ptype = part.partition(":")
            params.append({"name": name.strip(), "type": ptype.strip()})
        else:
            params.append({"name": part, "type": None})
    return params


def _parse_ir_text(text: str) -> dict:
    """Parse the plain-text output of `ngate inspect` into a dict.

    Layout (see module docstring above / tools/nativegate/nativegate/cli.py):
      [parser: <description>]           (cpp only, optional leading line)
      module: <name> (<lang>)
      [  class <Name>
           <method>(<params>) -> <ret>
         ...]*
      [  struct <Name>
           <field>: <type>
         ...]*
      [  function <name>(<params>) -> <ret>]*
      [  <diagnostic / skipped notes>]*
      [  (nothing exposed ...)]
    """
    ir: dict = {
        "parser": None,
        "module": None,
        "language": None,
        "classes": [],
        "structs": [],
        "functions": [],
        "notes": [],
        "empty": False,
    }

    lines = text.splitlines()
    current_class: dict | None = None
    current_struct: dict | None = None

    for line in lines:
        if line.startswith("parser:"):
            ir["parser"] = line[len("parser:"):].strip()
            continue

        m = _MODULE_RE.match(line)
        if m:
            ir["module"] = m.group("name")
            ir["language"] = m.group("lang")
            current_class = current_struct = None
            continue

        m = _CLASS_RE.match(line)
        if m:
            current_class = {"name": m.group("name"), "methods": []}
            ir["classes"].append(current_class)
            current_struct = None
            continue

        m = _STRUCT_RE.match(line)
        if m:
            current_struct = {"name": m.group("name"), "fields": []}
            ir["structs"].append(current_struct)
            current_class = None
            continue

        m = _TOP_FUNCTION_RE.match(line)
        if m:
            ir["functions"].append(
                {
                    "name": m.group("name"),
                    "parameters": _parse_params(m.group("params")),
                    "returns": m.group("returns"),
                }
            )
            current_class = current_struct = None
            continue

        if current_class is not None:
            m = _METHOD_RE.match(line)
            if m:
                current_class["methods"].append(
                    {
                        "name": m.group("name"),
                        "parameters": _parse_params(m.group("params")),
                        "returns": m.group("returns"),
                    }
                )
                continue

        if current_struct is not None:
            m = _FIELD_RE.match(line)
            if m:
                current_struct["fields"].append(
                    {"name": m.group("name"), "type": m.group("type")}
                )
                continue

        if "(nothing exposed" in line:
            ir["empty"] = True
            continue

        stripped = line.strip()
        if stripped:
            ir["notes"].append(stripped)

    return ir


def inspect_ir(path: str | Path, functions: list[str] | None = None) -> dict:
    """Run `ngate inspect <path>` and parse its stdout into an IR dict.

    Raises RuntimeError if the command fails (nonzero exit).
    """
    args = ["inspect", str(path)]
    if functions:
        for fn in functions:
            args.extend(["--function", fn])
    result = run(*args)
    if not result.ok:
        raise RuntimeError(
            f"ngate inspect failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return _parse_ir_text(result.stdout)


def generate(
    service_name: str, cwd: str | Path, on_line: Callable[[str], None] | None = None
) -> NgateResult:
    return run("generate", service_name, cwd=cwd, on_line=on_line)


def build(
    service_name: str, cwd: str | Path, on_line: Callable[[str], None] | None = None
) -> NgateResult:
    return run("build", service_name, cwd=cwd, on_line=on_line)


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "services/petro_api/native/petro_api.f90"
    fns = sys.argv[2:] if len(sys.argv) > 2 else ["pvt_set_fluid"]
    print(f"detect({target}) ->", detect(target))
    ir = inspect_ir(target, fns)
    print(json.dumps(ir, indent=2))
