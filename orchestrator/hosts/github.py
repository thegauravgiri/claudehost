"""GitHub-specific code: recognising a merged/closed PR and tearing down
that issue's worktree, plus the REST calls and comment-parsing the
CodeRabbit auto-fix loop (coderabbit.py) needs. This is the *code host* side
(where PRs live) - a different axis from sources/ (the *issue tracker*), and
deliberately kept separate: the tracker can change (Jira -> GitHub Issues)
without touching this, and the code host could change without touching
sources/.
"""
import hashlib
import hmac
import logging
import os
import re

import httpx

import git_ops
import state

log = logging.getLogger("orchestrator.hosts.github")

WEBHOOK_SECRET = os.environ.get("GH_WEBHOOK_SECRET", "").encode()
GH_TOKEN = os.environ.get("GH_TOKEN", "")
CODERABBIT_LOGIN = os.environ.get("CODERABBIT_LOGIN", "coderabbitai[bot]")

API_BASE = "https://api.github.com"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GH_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}

# CodeRabbit wraps each actionable suggestion's machine-readable instructions
# in a collapsible "Prompt for AI Agents" section, as a fenced code block
# straight after it - this doesn't anchor to the exact surrounding HTML
# (<details>/<summary> wrapper), just the marker text and the next fence,
# since that's the part CodeRabbit has kept stable across format tweaks.
_PROMPT_MARKER_RE = re.compile(r"Prompt for AI Agents", re.IGNORECASE)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.DOTALL)


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    if not WEBHOOK_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def handle_pull_request_event(payload: dict, repo_by_full_name) -> None:
    """Tears down the worktree+branch for a merged or closed PR, and any
    CodeRabbit auto-fix loop state for it. Silently no-ops for anything else
    (PR opened, review comments, etc.) - this webhook only needs the one
    event. `repo_by_full_name` is Orchestrator.repo_by_full_name, passed in
    rather than imported to avoid this module depending on core's internal
    repo storage.
    """
    if payload.get("action") != "closed":
        return

    pr = payload.get("pull_request") or {}
    branch = (pr.get("head") or {}).get("ref", "")

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    repo = repo_by_full_name(repo_full_name)
    if repo is None:
        log.warning("PR-closed webhook for unmapped repo %s", repo_full_name)
        return

    pr_number = pr.get("number")
    if pr_number is not None:
        git_ops.teardown_pr_worktree(repo.slug, pr_number)
        state.delete_pr_loop(f"{repo.slug}#{pr_number}")

    if branch.startswith("agent/"):
        issue_key = branch.removeprefix("agent/")
        git_ops.teardown_worktree(repo.slug, issue_key)
        state.delete_issue(issue_key)


def _extract_prompt(body: str | None) -> str | None:
    if not body:
        return None
    marker = _PROMPT_MARKER_RE.search(body)
    if marker is None:
        return None
    fence = _FENCE_RE.search(body, marker.end())
    if fence is None:
        return None
    prompt = fence.group(1).strip()
    return prompt or None


def extract_ai_prompts(review_body: str | None, comments: list[dict]) -> list[str]:
    """Pulls every "Prompt for AI Agents" instruction out of a CodeRabbit
    review: one per actionable inline comment, plus the review body itself
    in case a prompt ever lands there instead. Order matches the order
    CodeRabbit posted them in; duplicates (same instruction attached to more
    than one comment) are dropped.
    """
    prompts: list[str] = []
    seen: set[str] = set()

    def add(text: str | None) -> None:
        if text and text not in seen:
            seen.add(text)
            prompts.append(text)

    add(_extract_prompt(review_body))
    for comment in comments:
        add(_extract_prompt(comment.get("body")))
    return prompts


async def get_pr(repo_full_name: str, pr_number: int) -> dict:
    url = f"{API_BASE}/repos/{repo_full_name}/pulls/{pr_number}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=API_HEADERS)
        resp.raise_for_status()
        return resp.json()


async def list_review_comments(repo_full_name: str, pr_number: int, review_id: int) -> list[dict]:
    url = f"{API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/reviews/{review_id}/comments"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=API_HEADERS, params={"per_page": 100})
        resp.raise_for_status()
        return resp.json()


async def list_reviews(repo_full_name: str, pr_number: int) -> list[dict]:
    """Oldest first (GitHub's own order) - callers wanting the latest take
    the last element, same as "/agent go" re-checking the most recent one.
    """
    url = f"{API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=API_HEADERS, params={"per_page": 100})
        resp.raise_for_status()
        return resp.json()


async def list_check_runs_for_ref(repo_full_name: str, ref: str) -> list[dict]:
    """Every check run GitHub has recorded against a commit - used to gather
    *all* currently-failing CI jobs for a push, not just whichever one's
    completion happened to fire the triggering webhook.
    """
    url = f"{API_BASE}/repos/{repo_full_name}/commits/{ref}/check-runs"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=API_HEADERS, params={"per_page": 100})
        resp.raise_for_status()
        return resp.json().get("check_runs", [])


async def get_job_log(repo_full_name: str, job_id: int) -> str:
    """Plain-text logs for one GitHub Actions job. A check run created by
    Actions has the same id as its underlying job, which is what makes this
    callable with a check_run's own "id" field directly. The API responds
    with a redirect to time-limited blob storage holding the actual text.
    """
    url = f"{API_BASE}/repos/{repo_full_name}/actions/jobs/{job_id}/logs"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=API_HEADERS)
        resp.raise_for_status()
        return resp.text


async def list_workflow_runs_for_sha(repo_full_name: str, head_sha: str) -> list[dict]:
    """Every Actions workflow run for this commit (the run as a whole, with
    timing/status) - distinct from list_check_runs_for_ref's check-runs,
    which don't carry a cancellable run id or a reliable start time. Used to
    detect a run that's been going for too long without finishing.
    """
    url = f"{API_BASE}/repos/{repo_full_name}/actions/runs"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=API_HEADERS, params={"head_sha": head_sha, "per_page": 50})
        resp.raise_for_status()
        return resp.json().get("workflow_runs", [])


async def cancel_workflow_run(repo_full_name: str, run_id: int) -> None:
    url = f"{API_BASE}/repos/{repo_full_name}/actions/runs/{run_id}/cancel"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=API_HEADERS)
        # 202 = accepted; 409 = already completed/already cancelling - both
        # fine, not worth failing the round over either.
        if resp.status_code not in (202, 409):
            resp.raise_for_status()


async def post_pr_comment(repo_full_name: str, pr_number: int, body: str) -> int:
    # PRs are issues in GitHub's API - this is the same "leave a general
    # conversation comment" endpoint issues use, not a review-comment one,
    # since these are status updates, not tied to a specific diff line.
    # Returns the new comment's id, so a caller that wants to edit it later
    # (see update_pr_comment) can remember it.
    url = f"{API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=API_HEADERS, json={"body": body})
        resp.raise_for_status()
        return resp.json()["id"]


async def update_pr_comment(repo_full_name: str, comment_id: int, body: str) -> None:
    url = f"{API_BASE}/repos/{repo_full_name}/issues/comments/{comment_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(url, headers=API_HEADERS, json={"body": body})
        resp.raise_for_status()
