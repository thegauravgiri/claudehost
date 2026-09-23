"""Repo and git-worktree lifecycle for the Jira coding agent.

Each repo lives as a single workspace at WORKSPACES_ROOT/<slug>, shared by every
issue on that repo. Each issue gets its own worktree nested inside that repository
at WORKSPACES_ROOT/<slug>/.worktrees/<issue_key>, checked out onto its own branch.
This keeps /workspace clean (only one workspace directory per repository) while
allowing issues on the same repo to run concurrently in isolated worktrees:
claudebox 409s on a busy workspace path, and git itself refuses to check out one
branch into two worktrees, so two issues can never step on each other's files or
history.
"""
import fcntl
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

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


def _repo_path(slug: str) -> Path:
    return WORKSPACES_ROOT / slug


def _worktree_path(slug: str, issue_key: str) -> Path:
    return _repo_path(slug) / ".worktrees" / issue_key


@contextmanager
def _repo_lock(slug: str):
    lock_path = WORKSPACES_ROOT / f".{slug}.lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_repo(slug: str, remote_url: str, default_branch: str = "main") -> Path:
    """Clone the repo into WORKSPACES_ROOT/<slug> on first use, otherwise fetch it.

    Must be called under _repo_lock. remote_url should be an https://
    GitHub URL - GH_TOKEN auth is applied via GIT_CONFIG_* env vars
    (url.insteadOf), not embedded in this URL, so it never persists in
    the repo's own config where `git remote -v` would show it.
    """
    repo_dir = _repo_path(slug)
    if not (repo_dir / ".git").is_dir():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", remote_url, str(repo_dir)])
        _run(["git", "config", "gc.auto", "0"], cwd=repo_dir)

        # Ignore .worktrees directory so it doesn't show in git status
        exclude_file = repo_dir / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing_exclude = exclude_file.read_text() if exclude_file.is_file() else ""
        if ".worktrees" not in existing_exclude:
            with open(exclude_file, "a") as f:
                f.write("\n.worktrees/\n")

    _run(["git", "fetch", "--prune", "origin"], cwd=repo_dir)
    return repo_dir


def provision_worktree(slug: str, remote_url: str, issue_key: str, default_branch: str) -> Path:
    """Create (or reset) the worktree+branch for one issue inside the repo. Idempotent."""
    repo_dir = _repo_path(slug)
    worktree_path = _worktree_path(slug, issue_key)
    branch = f"agent/{issue_key}"
    with _repo_lock(slug):
        ensure_repo(slug, remote_url, default_branch)
        if worktree_path.is_dir():
            _run(["git", "worktree", "prune", "--expire=now"], cwd=repo_dir)
        if not worktree_path.is_dir():
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            _run(
                [
                    "git", "worktree", "add", str(worktree_path),
                    "-B", branch, f"origin/{default_branch}",
                ],
                cwd=repo_dir,
            )
    verify_worktree(worktree_path)
    return worktree_path


def verify_worktree(worktree_path: Path) -> None:
    """Raise if claudebox would find an empty dir instead of a real repo."""
    try:
        _run(["git", "rev-parse", "--git-dir"], cwd=worktree_path)
    except GitOpsError as err:
        raise GitOpsError(
            f"worktree at {worktree_path} is not a usable git repo: {err}"
        ) from err


def has_open_pr_worth_of_commits(worktree_path: Path, default_branch: str) -> bool:
    """True if the branch has diverged from the default branch."""
    try:
        out = _run(
            ["git", "rev-list", "--count", f"origin/{default_branch}..HEAD"],
            cwd=worktree_path,
        )
    except GitOpsError:
        return False
    return int(out.strip() or "0") > 0


def teardown_worktree(slug: str, issue_key: str) -> None:
    """Remove a worktree and its branch inside the repo."""
    repo_dir = _repo_path(slug)
    worktree_path = _worktree_path(slug, issue_key)
    branch = f"agent/{issue_key}"
    with _repo_lock(slug):
        try:
            if worktree_path.is_dir():
                _run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=repo_dir,
                )
        except GitOpsError:
            shutil.rmtree(worktree_path, ignore_errors=True)
            try:
                _run(["git", "worktree", "prune", "--expire=now"], cwd=repo_dir)
            except GitOpsError:
                pass
        try:
            _run(["git", "branch", "-D", branch], cwd=repo_dir)
        except GitOpsError:
            pass


def list_prunable(slug: str) -> list[str]:
    """Worktree paths git itself considers stale (gitdir points nowhere)."""
    repo_dir = _repo_path(slug)
    if not (repo_dir / ".git").is_dir():
        return []
    with _repo_lock(slug):
        out = _run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_dir,
        )
    prunable = []
    current_path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
        elif line == "prunable" and current_path:
            prunable.append(current_path)
    return prunable
