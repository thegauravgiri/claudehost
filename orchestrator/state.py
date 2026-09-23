"""Durable per-issue state.

Must survive a container restart: an in-memory dict would forget every
worktree the moment the process dies, orphaning them on disk with nothing to
reconcile against. sqlite3 (stdlib, no new dependency) on a named volume is
enough for this - one row per issue, one small table for instructions queued
while a run is already in flight.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("/state/orchestrator.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    issue_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    repo_slug TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    pr_url TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    has_session INTEGER NOT NULL DEFAULT 0,
    -- Set right before an agent call starts, cleared once it returns. Lets
    -- startup reconciliation re-queue whatever was actually in flight when
    -- the container died mid-run, instead of just silently dropping it.
    current_instruction TEXT,
    created_at REAL NOT NULL,
    last_activity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key TEXT NOT NULL,
    instruction TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _cursor():
    conn = _connect()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _cursor() as cur:
        cur.executescript(SCHEMA)


def get_issue(issue_key: str) -> sqlite3.Row | None:
    with _cursor() as cur:
        cur.execute("SELECT * FROM issues WHERE issue_key = ?", (issue_key,))
        return cur.fetchone()


def upsert_issue(
    issue_key: str,
    source: str,
    repo_slug: str,
    branch: str,
    worktree_path: str,
) -> None:
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO issues
                (issue_key, source, repo_slug, branch, worktree_path,
                 status, created_at, last_activity)
            VALUES (?, ?, ?, ?, ?, 'idle', ?, ?)
            ON CONFLICT(issue_key) DO UPDATE SET last_activity = excluded.last_activity
            """,
            (issue_key, source, repo_slug, branch, worktree_path, now, now),
        )


def set_status(issue_key: str, status: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE issues SET status = ?, last_activity = ? WHERE issue_key = ?",
            (status, time.time(), issue_key),
        )


def set_current_instruction(issue_key: str, instruction: str | None) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE issues SET current_instruction = ? WHERE issue_key = ?",
            (instruction, issue_key),
        )


def running_issues() -> list[sqlite3.Row]:
    """Issues whose status was still 'running' at last write - i.e. the
    container died mid-run rather than finishing normally."""
    with _cursor() as cur:
        cur.execute("SELECT * FROM issues WHERE status = 'running'")
        return cur.fetchall()


def mark_session_started(issue_key: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE issues SET has_session = 1, last_activity = ? WHERE issue_key = ?",
            (time.time(), issue_key),
        )


def set_pr_url(issue_key: str, pr_url: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE issues SET pr_url = ?, last_activity = ? WHERE issue_key = ?",
            (pr_url, time.time(), issue_key),
        )


def delete_issue(issue_key: str) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM issues WHERE issue_key = ?", (issue_key,))
        cur.execute(
            "DELETE FROM pending_instructions WHERE issue_key = ?", (issue_key,),
        )


def enqueue_instruction(issue_key: str, instruction: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO pending_instructions (issue_key, instruction, created_at) "
            "VALUES (?, ?, ?)",
            (issue_key, instruction, time.time()),
        )


def pop_instruction(issue_key: str) -> str | None:
    """Pop the oldest queued instruction for an issue, if any."""
    with _cursor() as cur:
        cur.execute(
            "SELECT id, instruction FROM pending_instructions "
            "WHERE issue_key = ? ORDER BY id ASC LIMIT 1",
            (issue_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("DELETE FROM pending_instructions WHERE id = ?", (row["id"],))
        return row["instruction"]


def all_issues() -> list[sqlite3.Row]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM issues")
        return cur.fetchall()


def idle_since(cutoff_seconds: float) -> list[sqlite3.Row]:
    cutoff = time.time() - cutoff_seconds
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM issues WHERE status = 'idle' AND last_activity < ?",
            (cutoff,),
        )
        return cur.fetchall()
