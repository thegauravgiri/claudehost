"""Tracker-agnostic run lifecycle: worktree -> agent call -> comment back.

Nothing in this file knows what Jira is. A `sources/*.py` adapter turns a
webhook into a WorkItem and gets called back through `post_comment`; adding
GitHub Issues later means writing `sources/github_issues.py` against the same
two things, not touching this file.

Per-issue instructions run strictly in order (an in-process asyncio lock is
enough for that - only this process ever enqueues work), while different
issues run fully concurrently, each in its own worktree. The first
instruction for an issue starts a fresh Claude Code session in a freshly
provisioned worktree+branch; every instruction after that sends
X-Aicodebox-Continue against the *same* worktree, which is what lets a
follow-up Jira comment feel like a continued conversation - claudebox
resumes "the most recent session in this cwd", and the cwd is exactly this
issue's worktree, so there's no session id to track ourselves. (The OpenAI
response envelope this endpoint returns doesn't expose one anyway.)
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

import git_ops
import state

log = logging.getLogger("orchestrator.core")

REPOS_YAML = Path(__file__).parent / "repos.yaml"

CLAUDEBOX_TIMEOUT_SECONDS = 1800
HTTP_TIMEOUT_SECONDS = 1900

RESULT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "agent_result",
        "schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["done", "question", "failed", "blocked"],
                },
                "pr_url": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["status", "summary"],
            "additionalProperties": False,
        },
    },
}

PROMPT_TEMPLATE = """\
You are working on {repo_full_name} (GitHub, remote "origin"), on branch \
agent/{issue_key}, inside a git worktree that is already checked out for you.

Task from {source} issue {issue_key}:
{title}

{description}
{comments_section}
Latest instruction:
{instruction}

Rules:
1. First, assess the latest instruction and determine the appropriate action:
   - If the user is asking a QUESTION or having a discussion (e.g. "what language did you use?", "how should we design X?", "explain this file"):
     Do NOT make code changes or git commits. Answer the user clearly and completely in "summary", leave "pr_url" empty (""), and set "status" to "question".
   - If the task is AMBIGUOUS or you need critical clarification from the user before you can proceed:
     Ask your clarifying questions directly to the user in "summary", leave "pr_url" empty (""), and set "status" to "question". Do NOT guess or commit code until clarified.
   - If the instruction is a CODING or BUG FIX request (or user answering your prior questions):
     Proceed with the implementation steps below.

2. If implementing code changes:
   - Check `gh pr list --repo {repo_full_name} --head agent/{issue_key}` and `git log origin/{default_branch}..HEAD`. If a PR or prior commits exist, treat this as a continuation: make only the changes requested, don't redo prior work.
   - Install whatever dependencies the project needs and verify your change actually works (run its build/lint/test commands) before committing.
   - Commit your changes with a clear message.
   - Push before doing anything with `gh`: `git push -u origin agent/{issue_key}`.
   - If no PR exists yet for this branch, open one: `gh pr create --repo {repo_full_name} --head agent/{issue_key} --base {default_branch} --title "..." --body "..."`. If one already exists, do not create another - your push already updated it.
   - Set "status" to "done", set "pr_url" to the PR's URL, and describe what changed and why in "summary".

3. Finish by responding with a JSON object matching the schema:
   - "status": "done" (if code was written and PR is ready), "question" (if answering questions or asking for clarification), "blocked", or "failed".
   - "pr_url": the PR URL (or empty string "" if answering/asking questions without new PR changes).
   - "summary": your answer, question, or summary of code changes.
"""


@dataclass(frozen=True)
class WorkItem:
    source: str
    external_id: str
    repo_slug: str
    title: str
    description: str
    instruction: str
    comments_section: str = ""


@dataclass(frozen=True)
class RepoConfig:
    slug: str
    remote: str
    default_branch: str
    full_name: str


def load_repos() -> dict[str, RepoConfig]:
    if not REPOS_YAML.is_file():
        log.warning("no repos.yaml found at %s - see repos.yaml.example", REPOS_YAML)
        return {}
    raw = yaml.safe_load(REPOS_YAML.read_text()) or {}
    repos = {}
    for slug, cfg in raw.items():
        remote = cfg["remote"]
        full_name = remote.removeprefix("https://github.com/").removesuffix(".git")
        repos[slug] = RepoConfig(
            slug=slug,
            remote=remote,
            default_branch=cfg.get("default_branch", "main"),
            full_name=full_name,
        )
    return repos


_issue_locks: dict[str, asyncio.Lock] = {}


def _lock_for(issue_key: str) -> asyncio.Lock:
    if issue_key not in _issue_locks:
        _issue_locks[issue_key] = asyncio.Lock()
    return _issue_locks[issue_key]


class Orchestrator:
    def __init__(self, litellm_base_url: str, litellm_api_key: str):
        self._litellm_base_url = litellm_base_url
        self._litellm_api_key = litellm_api_key

    @property
    def _repos(self) -> dict[str, RepoConfig]:
        return load_repos()

    async def handle(self, item: WorkItem, post_comment) -> None:
        """Entry point for a source adapter. Returns immediately if a run
        for this issue is already in flight - the instruction is durably
        queued and will run as soon as the current one finishes.
        """
        lock = _lock_for(item.external_id)
        if lock.locked():
            state.enqueue_instruction(item.external_id, item.instruction)
            try:
                await post_comment(
                    item.external_id,
                    f"⏳ **Agent busy**: Queued instruction (will run once in-flight task completes):\n\n> {item.instruction}",
                )
            except Exception:
                log.exception("Failed to post queue comment for %s", item.external_id)
            return
        asyncio.create_task(self._drain(item, post_comment))

    async def _drain(self, item: WorkItem, post_comment) -> None:
        lock = _lock_for(item.external_id)
        async with lock:
            instruction = item.instruction
            while instruction is not None:
                await self._run_one(
                    WorkItem(
                        source=item.source,
                        external_id=item.external_id,
                        repo_slug=item.repo_slug,
                        title=item.title,
                        description=item.description,
                        instruction=instruction,
                    ),
                    post_comment,
                )
                instruction = state.pop_instruction(item.external_id)

    async def _run_one(self, item: WorkItem, post_comment) -> None:
        repo = self._repos.get(item.repo_slug)
        if repo is None:
            await post_comment(
                item.external_id,
                f"No repo configured for `{item.repo_slug}` in repos.yaml.",
            )
            return

        existing = state.get_issue(item.external_id)
        is_first_run = existing is None

        # Immediate acknowledgement comment
        action = "Started" if is_first_run else "Continuing"
        try:
            await post_comment(
                item.external_id,
                f"🤖 **Agent {action.lower()} work** on `{item.external_id}`:\n\n> {item.instruction}",
            )
        except Exception:
            log.exception("Failed to post acknowledgement comment for %s", item.external_id)

        try:
            worktree_path = await asyncio.to_thread(
                git_ops.provision_worktree,
                repo.slug, repo.remote, item.external_id, repo.default_branch,
            )
        except git_ops.GitOpsError as err:
            log.exception("provisioning failed for %s", item.external_id)
            await post_comment(
                item.external_id, f"Couldn't set up a workspace: {err}",
            )
            return

        state.upsert_issue(
            item.external_id, item.source, repo.slug,
            f"agent/{item.external_id}", str(worktree_path),
        )
        state.set_status(item.external_id, "running")
        state.set_current_instruction(item.external_id, item.instruction)

        prompt = PROMPT_TEMPLATE.format(
            repo_full_name=repo.full_name,
            issue_key=item.external_id,
            source=item.source,
            title=item.title,
            description=item.description,
            comments_section=item.comments_section,
            instruction=item.instruction,
            default_branch=repo.default_branch,
        )

        workspace_subpath = f"{repo.slug}/.worktrees/{item.external_id}"

        try:
            result = await self._call_agent(
                workspace_subpath, prompt, resume=not is_first_run,
            )
        except Exception as err:  # noqa: BLE001 - always report back to the issue
            log.exception("agent call failed for %s", item.external_id)
            state.set_status(item.external_id, "idle")
            state.set_current_instruction(item.external_id, None)
            await post_comment(item.external_id, f"Agent run failed: {err}")
            return

        state.mark_session_started(item.external_id)
        state.set_status(item.external_id, "idle")
        state.set_current_instruction(item.external_id, None)
        if result.get("pr_url"):
            state.set_pr_url(item.external_id, result["pr_url"])

        await post_comment(item.external_id, _format_result(result))

    def sweep_idle(self, ttl_seconds: float) -> list[str]:
        """Tears down worktrees for issues nobody has touched in a while -
        the PR-closed webhook is the normal teardown path, this is the
        backstop for issues that were abandoned before ever reaching a PR,
        or whose webhook was missed. Returns the issue keys collected, for
        logging.
        """
        swept = []
        for row in state.idle_since(ttl_seconds):
            issue_key = row["issue_key"]
            git_ops.teardown_worktree(row["repo_slug"], issue_key)
            state.delete_issue(issue_key)
            swept.append(issue_key)
        return swept

    def repo_by_full_name(self, full_name: str) -> RepoConfig | None:
        return next(
            (cfg for cfg in self._repos.values() if cfg.full_name == full_name), None,
        )

    def reconcile_and_resume(self, post_comment) -> list[str]:
        """Startup recovery: whatever was mid-run when the container last
        died has no live asyncio task anymore, so it never gets drained on
        its own. Move it back into the durable queue and kick off a drain
        task for it, exactly as if a fresh webhook had just arrived.
        Returns the issue keys that were requeued, for startup logging.
        """
        requeued = []
        for row in state.running_issues():
            if row["current_instruction"]:
                state.enqueue_instruction(row["issue_key"], row["current_instruction"])
                requeued.append(row["issue_key"])
            state.set_status(row["issue_key"], "idle")
            state.set_current_instruction(row["issue_key"], None)

        for row in state.all_issues():
            issue_key = row["issue_key"]
            if _lock_for(issue_key).locked():
                continue
            instruction = state.pop_instruction(issue_key)
            if instruction is None:
                continue
            item = WorkItem(
                source=row["source"], external_id=issue_key,
                repo_slug=row["repo_slug"], title="", description="",
                instruction=instruction,
            )
            asyncio.create_task(self._drain(item, post_comment))
        return requeued

    async def _call_agent(self, workspace: str, prompt: str, resume: bool) -> dict:
        headers = {
            "Authorization": f"Bearer {self._litellm_api_key}",
            "X-Aicodebox-Workspace": workspace,
            "X-Aicodebox-Timeout-Seconds": str(CLAUDEBOX_TIMEOUT_SECONDS),
        }
        if resume:
            headers["X-Aicodebox-Continue"] = "true"

        body = {
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": prompt}],
            "response_format": RESULT_SCHEMA,
        }

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{self._litellm_base_url}/v1/chat/completions",
                headers=headers,
                json=body,
            )
        resp.raise_for_status()
        envelope = resp.json()
        content = envelope["choices"][0]["message"]["content"]
        return json.loads(content)


def _format_result(result: dict) -> str:
    status = result.get("status", "unknown")
    pr_url = result.get("pr_url", "")
    summary = result.get("summary", "")

    if status == "question":
        header = "💬 **Agent Response**"
    elif status == "done":
        header = "✅ **Agent: done**"
    elif status == "blocked":
        header = "⛔ **Agent: blocked**"
    else:
        header = f"⚠️ **Agent: {status}**"

    lines = [header]
    if pr_url and status == "done":
        lines.append(pr_url)
    if summary:
        lines.append(summary)
    return "\n\n".join(lines)
