"""GitHub-specific code: recognising a merged/closed PR and tearing down
that issue's worktree. This is the *code host* side (where PRs live) - a
different axis from sources/ (the *issue tracker*), and deliberately kept
separate: the tracker can change (Jira -> GitHub Issues) without touching
this, and the code host could change without touching sources/.
"""
import hashlib
import hmac
import logging
import os

import git_ops
import state

log = logging.getLogger("orchestrator.hosts.github")

WEBHOOK_SECRET = os.environ.get("GH_WEBHOOK_SECRET", "").encode()


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    if not WEBHOOK_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def handle_pull_request_event(payload: dict, repo_by_full_name) -> None:
    """Tears down the worktree+branch for a merged or closed PR. Silently
    no-ops for anything else (PR opened, review comments, etc.) - this
    webhook only needs the one event. `repo_by_full_name` is
    Orchestrator.repo_by_full_name, passed in rather than imported to avoid
    this module depending on core's internal repo storage.
    """
    if payload.get("action") != "closed":
        return

    pr = payload.get("pull_request") or {}
    branch = (pr.get("head") or {}).get("ref", "")
    if not branch.startswith("agent/"):
        return
    issue_key = branch.removeprefix("agent/")

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    repo = repo_by_full_name(repo_full_name)
    if repo is None:
        log.warning("PR-closed webhook for unmapped repo %s", repo_full_name)
        return

    git_ops.teardown_worktree(repo.slug, issue_key)
    state.delete_issue(issue_key)
