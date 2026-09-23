"""Bare-repo and git-worktree lifecycle for the Jira coding agent.

One bare clone lives per repo at REPOS_ROOT/<slug>.git, shared by every issue
on that repo. Each issue gets its own worktree at WORKSPACES_ROOT/<issue_key>,
checked out onto its own branch - this is what lets several issues on the same
repo run concurrently without colliding: claudebox 409s on a busy *workspace*,
and git itself refuses to check out one *branch* into two worktrees, so two
issues can never step on each other's files or history.

Both roots must be mounted at these exact paths in both this container and
claudebox: `git worktree add` bakes an absolute path into the worktree's
`.git` file (`gitdir: <bare>/worktrees/<name>`) and the matching
`<bare>/worktrees/<name>/gitdir` (pointing back). If a worktree is ever moved,
`git worktree repair <path>` rewrites both from what's actually on disk.

A plain `git clone --bare` creates no remote-tracking refs and no fetch
refspec - `origin/<branch>` will not resolve, and a later `git fetch` only
updates FETCH_HEAD. `--mirror` fixes that but goes further than wanted:
`remote.origin.mirror=true` makes a fetch prune anything not present
upstream, which would delete every agent/<issue-key> branch on the next
fetch. So the refspec is set by hand instead, after a bare clone, to get
origin/* without mirroring's pruning-of-local-refs behavior.

Concurrent git operations on one bare repo (two issues on the same repo,
fetching/creating worktrees at the same time) contend on ref locks and
packed-refs, and separately an auto-gc triggered by one agent's commit can
run while another is mid `worktree add`. Both are covered by an flock per
repo (_repo_lock), not an asyncio.Lock: the agents themselves mutate these
bare repos from inside claudebox, a separate process/container, which no
in-process lock could ever cover anyway.
"""
import fcntl
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

REPOS_ROOT = Path("/repos")
WORKSPACES_ROOT = Path("/workspace")
GIT_TIMEOUT = 120


class GitOpsError(RuntimeError):
    """A git operation needed to provision/tear down a worktree failed."""


def _run(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=True,
        )
    except subprocess.CalledProcessError as err:
        raise GitOpsError(
            f"{' '.join(args)} failed: {err.stderr.strip()}"
        ) from err
    except subprocess.TimeoutExpired as err:
        raise GitOpsError(f"{' '.join(args)} timed out") from err
    return result.stdout


@contextmanager
def _repo_lock(slug: str):
    lock_path = REPOS_ROOT / f"{slug}.lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _bare_repo_path(slug: str) -> Path:
    return REPOS_ROOT / f"{slug}.git"


def ensure_bare_repo(slug: str, remote_url: str) -> Path:
    """Clone the repo bare on first use, otherwise just fetch it.

    Must be called under _repo_lock. remote_url should be an https://
    GitHub URL - GH_TOKEN auth is applied via GIT_CONFIG_* env vars
    (url.insteadOf), not embedded in this URL, so it never persists in
    the bare repo's own config where `git remote -v` would show it.
    """
    bare_path = _bare_repo_path(slug)
    if not bare_path.is_dir():
        bare_path.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--bare", remote_url, str(bare_path)])
        _run(
            [
                "git", "config", "remote.origin.fetch",
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            cwd=bare_path,
        )
        _run(["git", "config", "gc.auto", "0"], cwd=bare_path)
    _run(["git", "fetch", "--prune", "origin"], cwd=bare_path)
    return bare_path


def provision_worktree(slug: str, remote_url: str, issue_key: str, default_branch: str) -> Path:
    """Create (or reset) the worktree+branch for one issue. Idempotent.

    Uses `-B` rather than `-b`: a retried/duplicated trigger on the same
    issue resets the branch to origin/<default> instead of erroring because
    the branch already exists.
    """
    worktree_path = WORKSPACES_ROOT / issue_key
    branch = f"agent/{issue_key}"
    with _repo_lock(slug):
        bare_path = ensure_bare_repo(slug, remote_url)
        if worktree_path.is_dir():
            _run(["git", "worktree", "prune", "--expire=now"], cwd=bare_path)
        if not worktree_path.is_dir():
            _run(
                [
                    "git", "worktree", "add", str(worktree_path),
                    "-B", branch, f"origin/{default_branch}",
                ],
                cwd=bare_path,
            )
    verify_worktree(worktree_path)
    return worktree_path


def verify_worktree(worktree_path: Path) -> None:
    """Raise if claudebox would find an empty dir instead of a real repo.

    claudebox creates the workspace directory itself with mkdir(parents=True,
    exist_ok=True) if it doesn't already exist - so a provisioning failure
    that left the directory missing or empty would otherwise go unnoticed
    until the agent runs and reports confused progress on a repo that isn't
    there.
    """
    try:
        _run(["git", "rev-parse", "--git-dir"], cwd=worktree_path)
    except GitOpsError as err:
        raise GitOpsError(
            f"worktree at {worktree_path} is not a usable git repo: {err}"
        ) from err


def has_open_pr_worth_of_commits(worktree_path: Path, default_branch: str) -> bool:
    """True if the branch has diverged from the default branch.

    Used by the agent-prompt guard, not by core.py directly, but kept here
    since it's a git-shaped question.
    """
    try:
        out = _run(
            ["git", "rev-list", "--count", f"origin/{default_branch}..HEAD"],
            cwd=worktree_path,
        )
    except GitOpsError:
        return False
    return int(out.strip() or "0") > 0


def teardown_worktree(slug: str, issue_key: str) -> None:
    """Remove a worktree and its branch. Best-effort past this point -
    called from PR-closed webhooks and TTL sweeps, where a half-successful
    cleanup shouldn't block/crash the caller (mirrors the request-path
    _cleanup pattern in litellm/auto_workspace_callback.py).
    """
    worktree_path = WORKSPACES_ROOT / issue_key
    if worktree_path.parent != WORKSPACES_ROOT:
        return
    branch = f"agent/{issue_key}"
    bare_path = _bare_repo_path(slug)
    with _repo_lock(slug):
        try:
            if worktree_path.is_dir():
                _run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=bare_path,
                )
        except GitOpsError:
            shutil.rmtree(worktree_path, ignore_errors=True)
            try:
                _run(["git", "worktree", "prune", "--expire=now"], cwd=bare_path)
            except GitOpsError:
                pass
        try:
            _run(["git", "branch", "-D", branch], cwd=bare_path)
        except GitOpsError:
            pass


def list_prunable(slug: str) -> list[str]:
    """Worktree paths git itself considers stale (gitdir points nowhere).

    Used at orchestrator startup to reconcile against the state DB before
    trusting anything on disk.
    """
    bare_path = _bare_repo_path(slug)
    if not bare_path.is_dir():
        return []
    with _repo_lock(slug):
        out = _run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=bare_path,
        )
    prunable = []
    current_path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
        elif line == "prunable" and current_path:
            prunable.append(current_path)
    return prunable
