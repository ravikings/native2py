"""SQLite schema and tiny helpers for the console app.

No ORM — plain stdlib ``sqlite3``. The database path is controlled by the
``NGATE_CONSOLE_DB`` env var and defaults to ``console/data/console.db``
(relative to the repo root / current working directory).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "console/data/console.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    github_id TEXT UNIQUE,
    -- UNIQUE, not just NOT NULL: both get_or_create_local_user() and
    -- get_or_create_github_user() below rely on this for a race-safe
    -- INSERT OR IGNORE + re-select. GitHub logins are already
    -- globally unique per account, and "local" is the only username
    -- NGATE_AUTH=none ever creates, so this loses no legitimate case.
    username TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    language TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (owner_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects (id)
);

CREATE TABLE IF NOT EXISTS service_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    method TEXT,
    path TEXT,
    tool TEXT,
    status INTEGER,
    duration_ms REAL,
    request_id TEXT,
    build_id INTEGER,
    image_digest TEXT,
    received_at TEXT NOT NULL,
    -- The tailer re-reads an overlapping window of `docker logs` after every
    -- restart (see console/calls.py), so the same line is offered more than
    -- once by design; this constraint is what makes that replay idempotent.
    UNIQUE(project_id, request_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_service_calls_project_ts
    ON service_calls (project_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_service_calls_project_build
    ON service_calls (project_id, build_id);
"""


def get_db_path() -> Path:
    """Resolve the console database path from ``NGATE_CONSOLE_DB``."""
    raw = os.environ.get("NGATE_CONSOLE_DB", DEFAULT_DB_PATH)
    return Path(raw)


def get_db() -> sqlite3.Connection:
    """Return a new connection with ``row_factory`` set to ``sqlite3.Row``.

    Ensures the parent directory of the database file exists before
    connecting.
    """
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create all tables if they do not already exist."""
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


MAX_PROJECTS_PER_USER = 5


def get_or_create_local_user() -> sqlite3.Row:
    """Return the single local user used when NGATE_AUTH=none, creating it if needed.

    INSERT OR IGNORE, not check-then-insert: two concurrent requests (e.g.
    two tabs loading at once) both seeing no existing row and both
    proceeding to INSERT would, without the `username` UNIQUE constraint
    this relies on, silently create two different "local" user rows —
    after which project ownership checks made against one id would make
    projects created under the other id invisible, with no error raised
    anywhere. OR IGNORE + re-select is race-safe: whichever insert wins,
    both callers end up re-selecting the same row.
    """
    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO users (username) VALUES ('local')")
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE username = 'local'").fetchone()
    finally:
        conn.close()


def count_projects_for_owner(owner_id: int) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


def create_project(slug: str, owner_id: int, name: str, language: str | None = None) -> int:
    """Insert a new project row. Raises sqlite3.IntegrityError on duplicate slug."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO projects (slug, owner_id, name, language, status) "
            "VALUES (?, ?, ?, ?, 'new')",
            (slug, owner_id, name, language),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        conn.close()


def get_project(slug: str) -> sqlite3.Row | None:
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    finally:
        conn.close()


def list_projects(owner_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_db()
    try:
        if owner_id is None:
            return conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM projects WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()
    finally:
        conn.close()


def update_project_status(slug: str, status: str, language: str | None = None) -> None:
    conn = get_db()
    try:
        if language is not None:
            conn.execute(
                "UPDATE projects SET status = ?, language = ? WHERE slug = ?",
                (status, language, slug),
            )
        else:
            conn.execute(
                "UPDATE projects SET status = ? WHERE slug = ?", (status, slug)
            )
        conn.commit()
    finally:
        conn.close()


def create_build(project_id: int) -> int:
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO builds (project_id, status) VALUES (?, 'pending')",
            (project_id,),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        conn.close()


def get_build(build_id: int) -> sqlite3.Row | None:
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
    finally:
        conn.close()


def list_builds(project_id: int) -> list[sqlite3.Row]:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM builds WHERE project_id = ? ORDER BY id DESC", (project_id,)
        ).fetchall()
    finally:
        conn.close()


def get_latest_successful_build_id(project_id: int) -> int | None:
    """Id of the newest successful build for a project, or None if there is none."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM builds WHERE project_id = ? AND status = 'success' "
            "ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return row["id"] if row is not None else None
    finally:
        conn.close()


def append_build_log(build_id: int, line: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE builds SET log = log || ? WHERE id = ?", (line + "\n", build_id)
        )
        conn.commit()
    finally:
        conn.close()


def set_build_status(build_id: int, status: str, finished: bool = False) -> None:
    conn = get_db()
    try:
        if finished:
            conn.execute(
                "UPDATE builds SET status = ?, finished_at = datetime('now') WHERE id = ?",
                (status, build_id),
            )
        else:
            conn.execute("UPDATE builds SET status = ? WHERE id = ?", (status, build_id))
        conn.commit()
    finally:
        conn.close()


_CALL_FIELDS = (
    "ts",
    "kind",
    "method",
    "path",
    "tool",
    "status",
    "duration_ms",
    "request_id",
    "build_id",
    "image_digest",
)


class MalformedCallEntry(ValueError):
    """A call-log entry missing the fields that make it a call record."""


def record_service_call(project_id: int, entry: dict) -> bool:
    """Persist one parsed call-log entry. Returns False if it was a duplicate.

    Raises MalformedCallEntry if the entry lacks a usable ``ts`` or ``kind``.

    INSERT OR IGNORE against ``UNIQUE(project_id, request_id, ts)``: the
    supervised tailer deliberately re-reads an overlapping slice of container
    stdout whenever it restarts, so re-offering an already-stored line is the
    normal case, not an error.

    Entries from older service images lack the newer provenance keys
    (``build_id``/``image_digest``) — those store as NULL rather than raising,
    so upgrading the console never requires redeploying every service first.
    """
    values = [entry.get(field) for field in _CALL_FIELDS]
    # Reject rather than substitute. A placeholder ts also left request_id
    # NULL, and SQLite treats NULL as distinct under a UNIQUE constraint, so
    # every malformed line inserted a fresh row that could never dedupe —
    # letting a burst of garbage inflate the call count in a signed evidence
    # pack. The caller (console/calls.py) logs the rejection.
    ts = values[0]
    if not isinstance(ts, str) or not ts.strip():
        raise MalformedCallEntry(f"call entry has no usable ts: {entry!r}")
    kind = values[1]
    if not isinstance(kind, str) or not kind.strip():
        raise MalformedCallEntry(f"call entry has no usable kind: {entry!r}")

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO service_calls "
            "(project_id, ts, kind, method, path, tool, status, duration_ms, "
            " request_id, build_id, image_digest, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (project_id, *values),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _call_filters(
    project_id: int,
    since: str | None,
    until: str | None,
    build_id: int | None,
) -> tuple[str, list]:
    where = ["project_id = ?"]
    params: list = [project_id]
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts <= ?")
        params.append(until)
    if build_id is not None:
        where.append("build_id = ?")
        params.append(build_id)
    return " AND ".join(where), params


def get_service_calls(
    project_id: int,
    *,
    since: str | None = None,
    until: str | None = None,
    build_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Newest-first page of a project's recorded calls, as plain dicts.

    ``since``/``until`` are ISO8601 strings compared against ``ts``.
    """
    clause, params = _call_filters(project_id, since, until, build_id)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, ts, kind, method, path, tool, status, duration_ms, "
            "request_id, build_id, image_digest, received_at "
            f"FROM service_calls WHERE {clause} "
            "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def count_service_calls(
    project_id: int,
    *,
    since: str | None = None,
    until: str | None = None,
    build_id: int | None = None,
) -> int:
    clause, params = _call_filters(project_id, since, until, build_id)
    conn = get_db()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM service_calls WHERE {clause}", tuple(params)
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


def get_or_create_github_user(github_id: str, username: str) -> sqlite3.Row:
    """Return the user row for a GitHub account, creating or updating it as needed.

    Used by NGATE_AUTH=github (see console/auth.py). Keeps ``username`` in
    sync in case the GitHub account was renamed since the last login.

    INSERT OR IGNORE, not check-then-insert: a double-clicked "Sign in with
    GitHub" (or a retried slow callback) can fire two near-simultaneous
    callbacks for the same brand-new github_id — both would see no existing
    row and both attempt to INSERT, and the second would previously hit the
    UNIQUE constraint on github_id and raise an uncaught IntegrityError
    (500) instead of just returning the row the other request created.
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO users (github_id, username) VALUES (?, ?)",
                (github_id, username),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE github_id = ?", (github_id,)
            ).fetchone()

        if row["username"] != username:
            conn.execute(
                "UPDATE users SET username = ? WHERE github_id = ?",
                (username, github_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE github_id = ?", (github_id,)
            ).fetchone()
        return row
    finally:
        conn.close()


def delete_project(project_id: int) -> None:
    """Delete a project and its build history.

    Purely a DB operation — the caller (console/routes/pages.py) is
    responsible for stopping the running container, removing the built
    image, and deleting the on-disk workspace *before* calling this, since
    those aren't reachable once the row is gone.
    """
    conn = get_db()
    try:
        conn.execute("DELETE FROM service_calls WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM builds WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()
