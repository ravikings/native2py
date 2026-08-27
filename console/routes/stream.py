"""SSE endpoint for streaming a build's logs.

Uses ``sse-starlette`` (already present in the active environment —
``pip show sse-starlette`` reports 3.4.8 — and not a new dependency added by
this file) rather than hand-rolling a ``StreamingResponse``. Consumed by a
plain browser ``EventSource``, so no htmx SSE extension is needed: bare
``text/event-stream`` with unnamed ``data:`` events (default event name
``message``) for log lines, plus one final ``event: done`` carrying the
terminal build status.
"""

from __future__ import annotations

import queue
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from .. import db, jobs
from ..auth import get_current_user

router = APIRouter()

_TERMINAL_STATUSES = {"success", "failed"}
_POLL_INTERVAL_S = 1.0


@router.get("/p/{slug}/build/{build_id}/stream")
async def stream_build_log(
    slug: str, build_id: int, user: sqlite3.Row = Depends(get_current_user)
) -> EventSourceResponse:
    # Ownership check up front, not inside the generator: a project/build
    # mismatch should 404 before the SSE response even starts, the same as
    # any other project-scoped route (console/routes/pages.py's
    # `_owned_project`) — otherwise any authenticated user who knows/guesses
    # a numeric build_id could read another user's build log.
    project = db.get_project(slug)
    if project is None or project["owner_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="project not found")
    build = db.get_build(build_id)
    if build is None or build["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="build not found")

    async def event_generator():
        build = db.get_build(build_id)
        if build is None:
            yield {"event": "done", "data": "not_found"}
            return

        # Subscribe BEFORE reading the DB snapshot, not after: a line
        # published in the gap between "read snapshot" and "subscribe"
        # would previously be persisted to the DB but never queued for any
        # subscriber (jobs._log persists then publishes; publish() only
        # reaches subscribers that already exist), so it silently never
        # reached this stream — a build log that looks stalled or like it
        # skipped a step, for no visible reason. Subscribing first can
        # instead double-emit a line that lands in both the snapshot and
        # the queue (same line published right as the snapshot is read) —
        # a harmless duplicate on-screen, and far preferable to a silent
        # gap since jobs.run_build's single writer guarantees log lines
        # arrive in the same order in both places.
        q = jobs.bus.subscribe(build_id)
        try:
            # 1. Replay whatever is already persisted, line by line.
            build = db.get_build(build_id)
            existing_log = (build["log"] or "") if build else ""
            for line in existing_log.splitlines():
                yield {"data": line}

            # If the build already finished before the client connected,
            # there's nothing more to stream — emit done immediately.
            if build is None or build["status"] not in ("running", "pending"):
                yield {"event": "done", "data": build["status"] if build else "not_found"}
                return

            # 2. Stream new lines as they arrive. We also poll the DB status
            # every ~1s as a belt-and-suspenders check in case a publish is
            # ever missed (e.g. process restart mid-build), so the stream
            # always terminates instead of hanging forever.
            last_poll = time.monotonic()
            while True:
                try:
                    line = q.get(timeout=_POLL_INTERVAL_S)
                except queue.Empty:
                    line = "__timeout__"

                if line is None:
                    # Sentinel published by run_build on completion.
                    current = db.get_build(build_id)
                    status = current["status"] if current else "unknown"
                    yield {"event": "done", "data": status}
                    return

                if line != "__timeout__":
                    yield {"data": line}
                    continue

                now = time.monotonic()
                if now - last_poll >= _POLL_INTERVAL_S:
                    last_poll = now
                    current = db.get_build(build_id)
                    if current is not None and current["status"] in _TERMINAL_STATUSES:
                        yield {"event": "done", "data": current["status"]}
                        return
        finally:
            jobs.bus.unsubscribe(build_id, q)

    return EventSourceResponse(
        event_generator(),
        # X-Accel-Buffering is a no-op without an nginx-style proxy in front
        # (docs/console-design.md's MVP topology has none), but it's a single
        # header and cheap insurance against a byte-buffering intermediary
        # silently turning "streamed" back into "arrives all at once".
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
