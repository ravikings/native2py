"""Build runner: shells out to ``ngate`` for one project and streams logs.

This module is meant to run ``run_build`` on a background thread (or via
FastAPI ``BackgroundTasks``, which itself runs the callable in a thread pool
for sync functions). ``run_build`` is fully synchronous/blocking by design so
it is safe to hand to ``threading.Thread(target=run_build, args=...)``.

Per docs/console-design.md §5, the build wall-clock budget is 10 minutes.
``ngate.run`` (and ``generate``/``build``) already default their subprocess
``timeout`` to 600s per *step*, which is the enforcement mechanism here — we
deliberately do NOT add a second, outer wall-clock timer around the whole
sequence of steps on top of that. (If a future revision needs a true
"10 minutes for the whole build" budget rather than "10 minutes per step",
that belongs in ``ngate.run``'s caller contract, not bolted on here.)
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from . import db, deploy, ngate

# Steps run in order. Each is either "generate", "build", or a ("run", arg)
# pair meaning ``ngate.run(arg, service_name, cwd=...)``.
_STEP_GENERATE = "generate"
_STEP_BUILD = "build"
_STEP_TEST = "test"
_STEP_VERIFY = "verify"
_STEP_DOCKER = "docker"


class BuildLogBus:
    """Tiny in-memory pub/sub so SSE readers don't have to poll the DB.

    One ``queue.Queue`` per subscriber per build. Not persisted, not shared
    across processes — fine for the single-process MVP topology described in
    docs/console-design.md §2.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[int, list[queue.Queue]] = {}

    def subscribe(self, build_id: int) -> "queue.Queue[str | None]":
        q: "queue.Queue[str | None]" = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(build_id, []).append(q)
        return q

    def unsubscribe(self, build_id: int, q: "queue.Queue[str | None]") -> None:
        with self._lock:
            subs = self._subscribers.get(build_id)
            if not subs:
                return
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                self._subscribers.pop(build_id, None)

    def publish(self, build_id: int, line: str | None) -> None:
        with self._lock:
            subs = list(self._subscribers.get(build_id, ()))
        for q in subs:
            q.put(line)


# Module-level singleton, imported by console/routes/stream.py.
bus = BuildLogBus()


def _log(build_id: int, line: str) -> None:
    """Append one line to the build's persisted log and fan it out live."""
    db.append_build_log(build_id, line)
    bus.publish(build_id, line)


def _log_step_marker(build_id: int, step_label: str) -> None:
    _log(build_id, f">>> ngate {step_label}")


def _log_step_outcome(build_id: int, step_label: str, result: ngate.NgateResult) -> None:
    if not result.ok:
        _log(build_id, f">>> ngate {step_label} failed (returncode={result.returncode})")


def _run_streamed(build_id: int, step_label: str, *args: str, cwd: Path) -> ngate.NgateResult:
    """Run an `ngate` subcommand, logging its output live as it's produced.

    Without this, the log pane sat empty for the whole step — `subprocess.run`
    blocks until exit and only then hands back output to log, so a `generate`
    (clang parsing) or `build` (cmake + pip wheel, easily a minute-plus) step
    looked like nothing was happening until it either finished or timed out.
    `ngate.run`'s `on_line` streams merged stdout/stderr line by line instead.
    """
    _log_step_marker(build_id, step_label)
    result = ngate.run(*args, cwd=cwd, on_line=lambda line: _log(build_id, line))
    _log_step_outcome(build_id, step_label, result)
    return result


def _log_step_result(build_id: int, step_label: str, result: ngate.NgateResult) -> None:
    """Marker + full output dump + failure line, for calls that were NOT
    streamed live (e.g. `_pip_install_wheel`, a plain `pip install` rather
    than an `ngate` subcommand) — everything at once, after the fact."""
    _log_step_marker(build_id, step_label)
    if result.stdout:
        for out_line in result.stdout.splitlines():
            _log(build_id, out_line)
    if result.stderr:
        for err_line in result.stderr.splitlines():
            _log(build_id, err_line)
    _log_step_outcome(build_id, step_label, result)


def run_build(project_slug: str, build_id: int, project_dir: Path) -> None:
    """Run the full generate -> build -> test -> verify pipeline, blocking.

    Safe to call from a background thread. Writes progress to the DB and
    publishes each appended line onto ``bus`` for live SSE streaming.

    Steps whose files/targets don't exist yet (e.g. a project with no golden
    tests wired up) are skipped gracefully: a step is considered "missing"
    (not failed) when the underlying ``NgateResult`` is not ok AND its stderr
    looks like a "nothing to do here" complaint from the CLI (no such
    file/target). We log the skip and continue rather than failing the whole
    build over an optional step.
    """
    db.set_build_status(build_id, "running")
    _log(build_id, f">>> build {build_id} for project {project_slug!r} starting")

    steps: list[tuple[str, bool]] = [
        (_STEP_GENERATE, False),
        (_STEP_BUILD, False),
        (_STEP_TEST, True),
        (_STEP_VERIFY, True),
        (_STEP_DOCKER, True),
    ]

    for step_name, optional in steps:
        if step_name == _STEP_GENERATE:
            result = _run_streamed(build_id, step_name, "generate", project_slug, cwd=project_dir)
        elif step_name == _STEP_BUILD:
            result = _run_streamed(build_id, step_name, "build", project_slug, cwd=project_dir)
            if result.ok:
                # `ngate build` only produces a wheel in dist/; `ngate test`
                # imports the *installed* package, so install it into this
                # interpreter's environment before moving on.
                install_result = _pip_install_wheel(project_slug, project_dir)
                _log_step_result(build_id, "pip install", install_result)
                if not install_result.ok:
                    db.set_build_status(build_id, "failed", finished=True)
                    _log(build_id, ">>> build failed at step 'pip install'")
                    bus.publish(build_id, None)
                    return
        elif step_name == _STEP_VERIFY:
            # `ngate verify` compares against a recorded golden.json baseline
            # and fails outright if one doesn't exist yet — correct behavior
            # for an established project, wrong for the very first build of a
            # brand-new one, where there is nothing to compare against yet.
            # `ngate golden record` is the documented way to establish that
            # baseline (tools/nativegate/docs/cli-reference.md, "ngate golden
            # record|verify|show"), so run it once, up front, if missing,
            # rather than treating "no baseline yet" as a build failure.
            golden_path = project_dir / "services" / project_slug / "golden.json"
            if not golden_path.exists():
                _run_streamed(build_id, "golden record", "golden", "record", project_slug, cwd=project_dir)
            result = _run_streamed(build_id, step_name, step_name, project_slug, cwd=project_dir)
        elif step_name == _STEP_DOCKER:
            # `ngate docker <name>` alone only writes the Dockerfile; --build
            # is required to actually produce the image `deploy.py` runs.
            result = _run_streamed(
                build_id, step_name, step_name, project_slug, "--build", cwd=project_dir
            )
        else:
            result = _run_streamed(build_id, step_name, step_name, project_slug, cwd=project_dir)

        if not result.ok:
            if optional and _looks_like_missing_step(result):
                _log(build_id, f">>> ngate {step_name} skipped (not configured for this project)")
                continue
            db.set_build_status(build_id, "failed", finished=True)
            _log(build_id, f">>> build failed at step {step_name!r}")
            bus.publish(build_id, None)
            return

    # Deploy is best-effort: a project that built and passed verification is
    # still a "success" build even if the local service container fails to
    # start (e.g. Docker not available in this environment). Surface the
    # failure in the log rather than the build's terminal status.
    try:
        info = deploy.start_service(project_slug)
        _log(build_id, f">>> deployed: {info['url']}  mcp: {info['mcp_url']}")
    except Exception as exc:  # noqa: BLE001 - deploy failure must not fail the build
        _log(build_id, f">>> deploy skipped: {exc}")

    db.set_build_status(build_id, "success", finished=True)
    _log(build_id, ">>> build succeeded")
    bus.publish(build_id, None)


def _pip_install_wheel(project_slug: str, project_dir: Path) -> ngate.NgateResult:
    """Install the wheel `ngate build` just produced, via plain pip (not ngate)."""
    import subprocess
    import sys

    dist_dir = project_dir / "services" / project_slug / "dist"
    # `ngate build` runs `pip wheel . -w dist`, which also downloads every
    # dependency wheel (fastapi, numpy, ...) into the same dist/ directory —
    # so this must match only the project's own wheel, not "any *.whl".
    wheel_prefix = project_slug.replace("-", "_")
    wheels = sorted(dist_dir.glob(f"{wheel_prefix}-*.whl")) if dist_dir.is_dir() else []
    if not wheels:
        return ngate.NgateResult(
            ok=False, returncode=1, stdout="", stderr=f"no wheel found in {dist_dir}"
        )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheels[-1])],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        return ngate.NgateResult(ok=False, returncode=1, stdout=exc.stdout or "", stderr=str(exc))
    return ngate.NgateResult(
        ok=proc.returncode == 0, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def _looks_like_missing_step(result: ngate.NgateResult) -> bool:
    """Best-effort heuristic: is this failure "there's nothing to test/verify"
    rather than a real error we should stop the pipeline for?

    ``ngate`` doesn't have a distinct exit code for "no golden tests defined
    yet" today, so we look for the common phrasing in stderr. This is
    intentionally conservative — any doubt, treat the failure as real.
    """
    text = (result.stderr or "") + (result.stdout or "")
    text = text.lower()
    markers = (
        "no such file",
        "not found",
        "no tests",
        "no golden",
        "nothing to do",
        "not configured",
    )
    return any(marker in text for marker in markers)


# ---------------------------------------------------------------------------
# Manual smoke test / usage example (not wired into app.py by this module).
#
# From a route handler, kick off a build without blocking the request:
#
#   import threading
#   from pathlib import Path
#   from console import db, jobs
#
#   @app.post("/p/{slug}/build")
#   def start_build(slug: str):
#       project = db.get_project(slug)
#       build_id = db.create_build(project["id"])
#       project_dir = Path("console/data/projects") / slug
#       threading.Thread(
#           target=jobs.run_build,
#           args=(slug, build_id, project_dir),
#           daemon=True,
#       ).start()
#       return {"build_id": build_id}
#
# Or, with FastAPI's BackgroundTasks (runs sync callables in a thread pool):
#
#   @app.post("/p/{slug}/build")
#   def start_build(slug: str, background_tasks: BackgroundTasks):
#       project = db.get_project(slug)
#       build_id = db.create_build(project["id"])
#       project_dir = Path("console/data/projects") / slug
#       background_tasks.add_task(jobs.run_build, slug, build_id, project_dir)
#       return {"build_id": build_id}
#
# To watch it live from a second thread/process during manual testing:
#
#   q = jobs.bus.subscribe(build_id)
#   while True:
#       line = q.get()
#       if line is None:
#           break
#       print(line)
#   jobs.bus.unsubscribe(build_id, q)
# ---------------------------------------------------------------------------
