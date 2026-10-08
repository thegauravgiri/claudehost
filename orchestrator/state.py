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

-- One row per PR being auto-fixed against CodeRabbit's review and/or CI
-- failures (see pr_autofix.py, ci_fix.py). key is "<repo_slug>#<pr_number>"
-- - a PR, not an issue, so it gets its own table rather than overloading
-- `issues`.
CREATE TABLE IF NOT EXISTS pr_loops (
    key TEXT PRIMARY KEY,
    repo_slug TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_ref TEXT NOT NULL,
    author TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    -- running | awaiting_review | done | gave_up | stalled
    status TEXT NOT NULL DEFAULT 'running',
    -- The last CodeRabbit review id acted on, so a redelivered webhook for
    -- the same review doesn't start a second iteration.
    last_review_id INTEGER,
    -- Set by a "/agent stop" PR comment, cleared by "/agent go". While set,
    -- CodeRabbit reviews on this PR are ignored entirely.
    paused INTEGER NOT NULL DEFAULT 0,
    -- The head sha pr_autofix.sweep_stuck_ci last dispatched a round for,
    -- so a commit whose CI is (still, or again) stuck doesn't get a new
    -- round dispatched every sweep interval - only once per commit.
    last_ci_sha TEXT,
    -- Combine-and-wait bookkeeping (see pr_autofix.offer_feedback): the
    -- commit these pending results are for, and each source's result for
    -- it - NULL means "hasn't reported in yet", "" means "reported back
    -- clean", anything else is feedback text to fix. A push to a new
    -- commit resets both to NULL against the new pending_sha.
    pending_sha TEXT,
    pending_coderabbit TEXT,
    pending_ci TEXT,
    pending_since REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
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
        # pr_loops already existed (without `paused`) on any deployment
        # running before the /agent stop|go feature - CREATE TABLE IF NOT
        # EXISTS above is a no-op against it, so add the column by hand.
        # Harmless to re-run: sqlite errors on a column that's already there.
        for ddl in (
            "ALTER TABLE pr_loops ADD COLUMN paused INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE pr_loops ADD COLUMN last_ci_sha TEXT",
            "ALTER TABLE pr_loops ADD COLUMN pending_sha TEXT",
            "ALTER TABLE pr_loops ADD COLUMN pending_coderabbit TEXT",
            "ALTER TABLE pr_loops ADD COLUMN pending_ci TEXT",
            "ALTER TABLE pr_loops ADD COLUMN pending_since REAL",
        ):
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError:
                pass


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


def get_pr_loop(key: str) -> sqlite3.Row | None:
    with _cursor() as cur:
        cur.execute("SELECT * FROM pr_loops WHERE key = ?", (key,))
        return cur.fetchone()


def get_pr_loop_by_head_ref(repo_slug: str, head_ref: str) -> sqlite3.Row | None:
    """Reverse lookup for a `push` webhook, which only gives a branch name,
    not a PR number - finds which (if any) armed PR that branch belongs to.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM pr_loops WHERE repo_slug = ? AND head_ref = ?",
            (repo_slug, head_ref),
        )
        return cur.fetchone()


def upsert_pr_loop(
    key: str,
    repo_slug: str,
    pr_number: int,
    head_ref: str,
    author: str,
    worktree_path: str,
) -> None:
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO pr_loops
                (key, repo_slug, pr_number, head_ref, author, worktree_path,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                head_ref = excluded.head_ref,
                worktree_path = excluded.worktree_path,
                updated_at = excluded.updated_at
            """,
            (key, repo_slug, pr_number, head_ref, author, worktree_path, now, now),
        )


def set_pr_loop_status(key: str, status: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET status = ?, updated_at = ? WHERE key = ?",
            (status, time.time(), key),
        )


def ensure_pr_loop(key: str, repo_slug: str, pr_number: int, head_ref: str, author: str) -> None:
    """Creates a row for a PR this loop hasn't touched yet (a "/agent
    stop|go|auto" comment can arrive before any CodeRabbit review has),
    without disturbing one that already exists - upsert_pr_loop's job once a
    real fix iteration runs, not this one's. A brand-new row always starts
    paused=1 (explicit, not relying on the column default) - "/agent go" is
    a one-shot check that must not accidentally arm continuous auto-react
    just because this is the first time the PR's been touched.
    """
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO pr_loops
                (key, repo_slug, pr_number, head_ref, author, worktree_path,
                 status, paused, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '', 'running', 1, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (key, repo_slug, pr_number, head_ref, author, now, now),
        )


def set_paused(key: str, paused: bool) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET paused = ?, updated_at = ? WHERE key = ?",
            (1 if paused else 0, time.time(), key),
        )


def set_pr_loop_review(key: str, review_id: int) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET last_review_id = ?, updated_at = ? WHERE key = ?",
            (review_id, time.time(), key),
        )


def clear_pr_loop_review(key: str) -> None:
    """Undoes set_pr_loop_review - used when a round that was dispatched to
    act on a review crashes before it can actually evaluate it (see
    run_fix_round's exception handler). The review gets marked "handled" the
    moment it's picked up, before the round even runs, so a crash leaves
    that marker stuck pointing at a review whose feedback was never actually
    applied - a later /agent go would see "already handled, nothing new" and
    wrongly declare the PR clean. Clearing it here means the next check
    re-evaluates CodeRabbit's current review fresh, regardless of whether
    it's the same one or a newer one.
    """
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET last_review_id = NULL, updated_at = ? WHERE key = ?",
            (time.time(), key),
        )


def set_last_ci_sha(key: str, sha: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET last_ci_sha = ?, updated_at = ? WHERE key = ?",
            (sha, time.time(), key),
        )


def reset_pending(key: str, sha: str) -> None:
    """Starts a fresh pending-combine window for `sha`, discarding whatever
    was pending for a previous (now-superseded) commit.
    """
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            """
            UPDATE pr_loops
            SET pending_sha = ?, pending_coderabbit = NULL, pending_ci = NULL,
                pending_since = ?, updated_at = ?
            WHERE key = ?
            """,
            (sha, now, now, key),
        )


def set_pending_coderabbit(key: str, text: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET pending_coderabbit = ?, updated_at = ? WHERE key = ?",
            (text, time.time(), key),
        )


def set_pending_ci(key: str, text: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET pending_ci = ?, updated_at = ? WHERE key = ?",
            (text, time.time(), key),
        )


def clear_pending(key: str) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            UPDATE pr_loops
            SET pending_sha = NULL, pending_coderabbit = NULL, pending_ci = NULL,
                pending_since = NULL, updated_at = ?
            WHERE key = ?
            """,
            (time.time(), key),
        )




def pending_pr_loops(wait_seconds: float) -> list[sqlite3.Row]:
    """PRs with a pending combine window older than `wait_seconds` that
    still hasn't heard from both sources - sweep_pending's candidates for a
    solo dispatch (see pr_autofix.sweep_pending).
    """
    cutoff = time.time() - wait_seconds
    with _cursor() as cur:
        cur.execute(
            """
            SELECT * FROM pr_loops
            WHERE pending_sha IS NOT NULL
              AND (pending_coderabbit IS NOT NULL OR pending_ci IS NOT NULL)
              AND pending_since < ?
              AND paused = 0
            """,
            (cutoff,),
        )
        return cur.fetchall()


def increment_pr_loop_iteration(key: str) -> int:
    with _cursor() as cur:
        cur.execute(
            "UPDATE pr_loops SET iteration = iteration + 1, updated_at = ? WHERE key = ?",
            (time.time(), key),
        )
        cur.execute("SELECT iteration FROM pr_loops WHERE key = ?", (key,))
        return cur.fetchone()["iteration"]


def delete_pr_loop(key: str) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM pr_loops WHERE key = ?", (key,))


def pr_loops_by_repo(repo_slug: str) -> list[sqlite3.Row]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM pr_loops WHERE repo_slug = ?", (repo_slug,))
        return cur.fetchall()


def active_pr_loops() -> list[sqlite3.Row]:
    """Every PR currently armed (not paused), across all repos - sweep_stuck_ci's
    candidates for a CI-stuck check, regardless of what status they're in.
    """
    with _cursor() as cur:
        cur.execute("SELECT * FROM pr_loops WHERE paused = 0")
        return cur.fetchall()


def stalled_pr_loops(cutoff_seconds: float) -> list[sqlite3.Row]:
    """pr_loops still waiting on a CodeRabbit re-review long after we asked
    for one - CodeRabbit never answered, or its webhook never arrived.
    """
    cutoff = time.time() - cutoff_seconds
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM pr_loops WHERE status = 'awaiting_review' AND updated_at < ?",
            (cutoff,),
        )
        return cur.fetchall()
