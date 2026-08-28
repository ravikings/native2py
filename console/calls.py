"""Live tailing of a deployed service's REST/MCP call logs.

Every deployed container (see console/deploy.py, ``_container_name``) writes
one JSON object per line to stdout for each REST/MCP call, via the
``nativegate.<service>.access`` logger — see the parallel work adding that
logging to generated services. This module shells out to
``docker logs -f`` (mirroring console/jobs.py's ``_run_streamed``/
``BuildLogBus`` pattern) and parses each line as JSON, skipping anything that
doesn't parse (uvicorn's own access/startup lines are interleaved on the same
stdout and are not JSON).

Ownership of the tail lives with a *supervisor* here, driven by
console/orchestrator.py's monitor loop, not with an SSE connection: a
per-browser-connection tail meant call history existed only for as long as
someone happened to be watching the page, and vanished on console restart.
The supervisor keeps exactly one long-lived tailer per running project,
writing every parsed entry to SQLite (``db.record_service_call``) as well as
to the in-memory ring buffer the live SSE path still reads.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from . import db
from .deploy import _container_name

logger = logging.getLogger("console.calls")

# Ring buffer of the last N parsed call-log entries per project slug, so a
# freshly opened page can show recent history immediately without a query.
# The durable record now lives in the service_calls table; this buffer is a
# cache in front of it for the live SSE path, so it stays capped and lossy.
_HISTORY_SIZE = 200
_lock = threading.Lock()
_history: dict[str, deque] = {}


def _record(slug: str, entry: dict) -> None:
    with _lock:
        buf = _history.setdefault(slug, deque(maxlen=_HISTORY_SIZE))
        buf.append(entry)
    _publish(slug, entry)


def recent_calls(slug: str) -> list[dict]:
    with _lock:
        return list(_history.get(slug, ()))


# --- live fan-out to SSE readers -------------------------------------------

# One queue per SSE connection, fed by the single supervised tailer, mirroring
# console/jobs.py's BuildLogBus. An SSE connection must never start its own
# `docker logs -f`: N open tabs would mean N subprocesses on the same
# container plus a second parsing path that can drift from what gets
# persisted.
#
# Bounded because a subscriber whose client stopped reading (a backgrounded
# tab, a half-open TCP connection) would otherwise grow its queue without
# limit until the request is finally torn down; the live pane is a tail, so
# dropping the oldest is the right loss when a reader can't keep up.
_SUBSCRIBER_QUEUE_SIZE = 1000

_sub_lock = threading.Lock()
_subscribers: dict[str, list["queue.Queue[dict]"]] = {}


def subscribe(slug: str) -> "queue.Queue[dict]":
    q: "queue.Queue[dict]" = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
    with _sub_lock:
        _subscribers.setdefault(slug, []).append(q)
    return q


def unsubscribe(slug: str, q: "queue.Queue[dict]") -> None:
    with _sub_lock:
        subs = _subscribers.get(slug)
        if not subs:
            return
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            _subscribers.pop(slug, None)


def subscriber_count(slug: str) -> int:
    with _sub_lock:
        return len(_subscribers.get(slug, ()))


def _publish(slug: str, entry: dict) -> None:
    with _sub_lock:
        subs = list(_subscribers.get(slug, ()))
    for q in subs:
        try:
            q.put_nowait(entry)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(entry)
            except (queue.Empty, queue.Full):
                pass


def parse_line(line: str) -> dict | None:
    """Parse one stdout line as a call-log entry, or None if it isn't one.

    Tolerant by design: stdout also carries uvicorn's own non-JSON
    access/startup lines, and even a JSON line missing expected keys
    shouldn't blow up the tail — just skip it.

    ``kind`` and a non-empty string ``ts`` are both required, matching
    db.record_service_call's contract. A JSON line without a usable timestamp
    is not a call record: it cannot dedupe (its request_id is NULL too, and
    SQLite treats NULL as distinct under a UNIQUE constraint), so storing it
    let a burst of malformed lines inflate the counts shown in the UI and
    embedded in signed evidence packs. Rejected-but-JSON lines are logged
    rather than dropped silently, so this can't hide a real logging bug.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Presence alone is not enough: the DB requires a non-empty *string*, so
    # accepting kind="" here would put the entry on the live page over SSE
    # while the same entry was rejected from storage — the page and a signed
    # pack covering the same minute would then disagree, which is exactly the
    # guarantee the pack exists to make.
    kind = data.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        logger.warning("skipping call-log line with no usable kind: %.200s", line)
        return None
    ts = data.get("ts")
    if not isinstance(ts, str) or not ts.strip():
        logger.warning("skipping call-log line with no usable ts: %.200s", line)
        return None
    return data


def tail_calls(
    slug: str,
    on_entry,
    stop_event: threading.Event | None = None,
    since: str | None = "10m",
    tail: str | None = "50",
):
    """Run ``docker logs -f`` for the project's service container, blocking.

    Calls ``on_entry(dict)`` for every parsed call-log line (also recording
    it into the in-memory ring buffer). Returns when the subprocess exits
    (container stopped) or when ``stop_event`` is set (caller/SSE client
    disconnected) — a watcher thread kills the subprocess in that case since
    iterating ``proc.stdout`` otherwise blocks until the next log line.
    Safe to run on a background thread.

    ``since`` is passed straight through to ``docker logs --since`` so the
    supervisor can resume from where a previous tailer died; it defaults to
    the original relative window so existing callers are unaffected.

    ``tail`` bounds how many trailing lines docker replays, and must be None
    for a *resume*: ``--tail N`` caps the output at the last N lines
    regardless of ``--since``, so a service that logged more than N calls
    during a tailer outage would have everything older than the newest N
    silently dropped from the window the resume was supposed to cover — an
    incomplete evidence pack with no error anywhere. A *first* attach may
    legitimately bound its backfill, which is why the two cases pass
    different values instead of sharing one flag combination.
    """
    name = _container_name(slug)
    cmd = ["docker", "logs", "-f"]
    if tail is not None:
        cmd += ["--tail", tail]
    if since is not None:
        cmd += ["--since", since]
    cmd.append(name)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    watcher = None
    if stop_event is not None:
        def _watch() -> None:
            stop_event.wait()
            if proc.poll() is None:
                proc.terminate()

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            entry = parse_line(line)
            if entry is None:
                continue
            _record(slug, entry)
            on_entry(entry)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return proc


# --- supervised tailers ----------------------------------------------------

# How far back a restarted tailer re-reads. The overlap is deliberate: a
# tailer that died mid-line would otherwise lose whatever landed between its
# last recorded entry and the restart. Re-ingesting the overlap is free
# because record_service_call is idempotent on (project_id, request_id, ts).
_RESTART_OVERLAP_SECONDS = 30

# Pause before respawning `docker logs` after the previous one ended. Without
# it, a container that is gone for good would spin this thread hot.
_RESPAWN_DELAY_SECONDS = 5.0

_sup_lock = threading.Lock()
_tailers: dict[str, "_Tailer"] = {}


class _Tailer:
    """One supervised ``docker logs -f`` loop for a single project."""

    def __init__(self, slug: str, project_id: int) -> None:
        self.slug = slug
        self.project_id = project_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"calls-tailer-{slug}", daemon=True
        )
        # Wall clock, not the container's own `ts` field: a service whose
        # clock is skewed from the host's would otherwise make us ask docker
        # for a window that never contains its lines.
        self._resume_from = datetime.now(timezone.utc)
        self._attached = False

    def _since(self) -> str | None:
        # None on the first attach. `docker logs` applies --since as a hard
        # filter *before* --tail, so pairing them here would have clamped the
        # cold-start backfill to the last 30 seconds and silently dropped
        # everything a service logged before the console came up — history the
        # ledger, and any evidence pack drawn from it, would simply not have.
        # --tail alone bounds that first read; --since only governs a resume,
        # where the watermark is real.
        if not self._attached:
            return None
        start = self._resume_from - timedelta(seconds=_RESTART_OVERLAP_SECONDS)
        return start.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _on_entry(self, entry: dict) -> None:
        self._resume_from = datetime.now(timezone.utc)
        try:
            db.record_service_call(self.project_id, entry)
        except db.MalformedCallEntry as exc:
            # Loud, because the alternative was storing it: an entry with no
            # usable timestamp is not a call record, and letting it through
            # inflated the counts embedded in signed evidence packs.
            logger.warning("dropping malformed call entry for %s: %s", self.slug, exc)
        except Exception:  # noqa: BLE001 - a DB hiccup must not end the tail
            logger.exception("failed to persist call for %s", self.slug)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                # First attach bounds its backfill with --tail and no --since;
                # every respawn after that must read the whole --since window,
                # not just its last lines. Both read _attached, so resolve
                # them before flipping it.
                tail = "50" if not self._attached else None
                since = self._since()
                self._attached = True
                tail_calls(
                    self.slug,
                    self._on_entry,
                    self.stop_event,
                    since=since,
                    tail=tail,
                )
            except Exception:  # noqa: BLE001 - missing container, docker down, ...
                logger.warning("call tailer for %s failed; will retry", self.slug, exc_info=True)
            # `docker logs -f` also returns normally when the container stops;
            # either way, wait before respawning so stop_event can win.
            self.stop_event.wait(_RESPAWN_DELAY_SECONDS)


def ensure_tailer(slug: str, project_id: int) -> None:
    """Start the supervised tailer for ``slug`` unless one is already running.

    Idempotent under the lock: the monitor loop calls this every tick, and two
    tailers on one container would double-write every line (the UNIQUE
    constraint is a backstop for log replay, not a substitute for this).
    """
    with _sup_lock:
        existing = _tailers.get(slug)
        if existing is not None and existing.thread.is_alive():
            return
        tailer = _Tailer(slug, project_id)
        _tailers[slug] = tailer
        tailer.thread.start()


def stop_tailer(slug: str) -> None:
    """Stop ``slug``'s tailer if it has one. Safe to call for unknown slugs."""
    with _sup_lock:
        tailer = _tailers.pop(slug, None)
    if tailer is not None:
        tailer.stop_event.set()
    # Drop the ring buffer too. Keyed by slug and never reclaimed, it kept one
    # deque per project ever created for the life of the process — including
    # deleted ones, whose call history is exactly what should not linger.
    with _lock:
        _history.pop(slug, None)


def stop_all_tailers() -> None:
    """Signal every tailer to stop — called from the app's lifespan shutdown.

    Setting stop_event is what kills the `docker logs -f` subprocess (via
    tail_calls' watcher thread); without it the console can exit leaving
    orphaned subprocesses attached to the daemon.
    """
    with _sup_lock:
        tailers = list(_tailers.values())
        _tailers.clear()
    for tailer in tailers:
        tailer.stop_event.set()
    for tailer in tailers:
        tailer.thread.join(timeout=2.0)


def active_tailers() -> list[str]:
    with _sup_lock:
        return [slug for slug, t in _tailers.items() if t.thread.is_alive()]
