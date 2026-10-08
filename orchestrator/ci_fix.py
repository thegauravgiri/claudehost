"""Feeds GitHub Actions CI results into pr_autofix's combine-and-fix engine.

Listens for `check_suite` (action=completed) rather than individual
`check_run` events: a check suite only completes once every job GitHub
Actions ran for that commit has finished, so it's naturally "CI's final
word on this commit" - one event, not one per job, and nothing to
debounce on this side the way pr_autofix.offer_feedback debounces between
CodeRabbit and CI.
"""
import logging
import os
import time
from datetime import datetime, timezone

from hosts import github as gh_host
from pr_autofix import is_fork_pr, offer_feedback

log = logging.getLogger("orchestrator.ci_fix")

# The GitHub App slug for Actions-created check suites/runs. Other apps
# (CodeQL, third-party checks) also post check suites, but this module can
# only fetch logs through the Actions jobs/logs endpoint, so only these are
# useful here.
CI_APP_SLUG = os.environ.get("CI_CHECK_APP_SLUG", "github-actions")

# Per failing job - keeps the prompt bounded on a noisy job. The tail is
# what matters (that's where the actual failure output is), so this keeps
# the end of the log, not the start.
MAX_LOG_CHARS = 20000

# How long an Actions run can sit queued/in_progress before test_plan_for_commit
# gives up on it and cancels it outright - this is what happened the one time
# CI ran for 6 hours straight without finishing. Generous relative to this
# project's own ~9-10 minute suite, but short enough to not waste a fix
# round's time waiting on something that's genuinely hung.
STUCK_CI_SECONDS = int(os.environ.get("CI_STUCK_SECONDS", str(15 * 60)))


async def handle_check_suite(payload: dict, orchestrator) -> None:
    """Entry point for a `check_suite` webhook (action=completed)."""
    if payload.get("action") != "completed":
        return

    suite = payload.get("check_suite") or {}
    if (suite.get("app") or {}).get("slug") != CI_APP_SLUG:
        return

    conclusion = suite.get("conclusion")
    if conclusion not in ("success", "failure"):
        # neutral/cancelled/timed_out/stale/action_required: ambiguous
        # enough (not clearly "fixable", not clearly "fine") to leave alone.
        return

    pull_requests = suite.get("pull_requests") or []
    if not pull_requests:
        # Also empty for fork PRs (GitHub doesn't associate a fork's check
        # suites back to the PR here) - conveniently filters those out too,
        # since we couldn't push fixes to a fork anyway.
        return

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    repo = orchestrator.repo_by_full_name(repo_full_name)
    if repo is None:
        return

    pr_number = pull_requests[0].get("number")
    head_sha = suite.get("head_sha", "")
    if pr_number is None or not head_sha:
        return

    try:
        pr = await gh_host.get_pr(repo_full_name, pr_number)
    except Exception:
        log.exception("failed to fetch %s#%s for a completed check suite", repo.slug, pr_number)
        return

    if is_fork_pr(pr):
        return

    current_head_sha = (pr.get("head") or {}).get("sha", "")
    if current_head_sha and current_head_sha != head_sha:
        log.info(
            "check suite for %s#%s is for commit %s, PR is now at %s - stale, ignoring",
            repo.slug, pr_number, head_sha, current_head_sha,
        )
        return

    head_ref = (pr.get("head") or {}).get("ref", "")
    author_login = (pr.get("user") or {}).get("login", "")

    feedback_text = ""
    if conclusion == "failure":
        try:
            feedback_text = await _collect_failures(repo_full_name, head_sha)
        except Exception:
            log.exception("failed to collect CI failure logs for %s#%s", repo.slug, pr_number)
            return
        if not feedback_text:
            return

    await offer_feedback(
        repo, repo_full_name, pr_number, head_ref, author_login,
        head_sha, "ci", feedback_text, orchestrator,
    )


async def current_ci_feedback(repo_full_name: str, head_sha: str) -> str | None:
    """CI's result for this commit right now, rather than waiting for a
    check_suite webhook - used by "/agent go" to catch up on a CI run that
    already finished before the PR was armed, since GitHub never re-sends a
    webhook for something that already happened.

    None means "nothing to report yet" (no Actions runs for this commit, or
    some are still in progress) - distinct from "" (every run finished and
    none failed), since the caller must not treat "still running" as clean.
    """
    check_runs = await gh_host.list_check_runs_for_ref(repo_full_name, head_sha)
    actions_runs = [r for r in check_runs if (r.get("app") or {}).get("slug") == CI_APP_SLUG]
    if not actions_runs or any(r.get("status") != "completed" for r in actions_runs):
        return None
    if any(r.get("conclusion") == "failure" for r in actions_runs):
        return await _collect_failures(repo_full_name, head_sha)
    return ""


def _parse_gh_time(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _stuck_runs(runs: list[dict]) -> list[dict]:
    """Every run in `runs` that's queued/in_progress and has been for longer
    than STUCK_CI_SECONDS. Shared by has_stuck_ci (read-only, for the sweep
    that decides whether to dispatch at all) and test_plan_for_commit (which
    actually cancels them) so the two can't disagree about what "stuck"
    means.
    """
    now = time.time()
    unfinished = [r for r in runs if r.get("status") != "completed"]
    return [
        r for r in unfinished
        if (started := _parse_gh_time(r.get("run_started_at") or r.get("created_at"))) is not None
        and now - started > STUCK_CI_SECONDS
    ]


async def has_stuck_ci(repo_full_name: str, head_sha: str) -> bool:
    """Read-only: is there an Actions run for this commit that's been
    queued/in_progress past STUCK_CI_SECONDS? Used by sweep_stuck_ci to
    decide whether to dispatch a round at all - the actual cancellation
    happens exactly once, inside test_plan_for_commit, when that round
    actually runs. Checking (and thus cancelling) here too would make that
    later check see an already-cancelled run and lose the "stuck" signal
    before the prompt ever reflects it.
    """
    try:
        runs = await gh_host.list_workflow_runs_for_sha(repo_full_name, head_sha)
    except Exception:
        log.exception("failed to list workflow runs for %s@%s", repo_full_name, head_sha[:7])
        return False
    return bool(_stuck_runs(runs))


async def test_plan_for_commit(repo_full_name: str, head_sha: str) -> tuple[str, bool]:
    """What the next fix round should do about its own local test run, based
    on this commit's current Actions state - called once per round, right
    before the agent prompt is built, so a fix round never repeats testing
    CI has already covered, and never waits forever on a run that's hung.

    Returns (note, skip_browser):
    - note: one sentence to fold into the round's prompt (and its "picked
      up" comment) about CI's state - "" when there's nothing worth saying
      (no Actions runs for this commit, or one's in progress but still
      within a normal runtime).
    - skip_browser: True only when every Actions run for this exact commit
      has already completed and passed - the round can skip re-running the
      slow browser/E2E suite locally and still trust it's covered, since CI
      will check the new commit this round is about to push anyway. Any
      other state (failing, still running, not configured, or a run just
      cancelled for being stuck) means run the full suite, browser tests
      included - never skip on anything less than a confirmed pass.
    """
    try:
        runs = await gh_host.list_workflow_runs_for_sha(repo_full_name, head_sha)
    except Exception:
        log.exception("failed to list workflow runs for %s@%s", repo_full_name, head_sha[:7])
        return "", False

    if not runs:
        return "", False

    unfinished = [r for r in runs if r.get("status") != "completed"]
    stuck = _stuck_runs(runs)

    if stuck:
        cancelled = []
        for run in stuck:
            try:
                await gh_host.cancel_workflow_run(repo_full_name, run["id"])
                cancelled.append(run.get("name") or f"run {run['id']}")
            except Exception:
                log.exception("failed to cancel stuck CI run %s for %s", run.get("id"), repo_full_name)
        if cancelled:
            note = (
                f"CI ({', '.join(cancelled)}) was still running after over "
                f"{STUCK_CI_SECONDS // 60} minutes with no result, so it was just cancelled "
                "automatically - mention this in your summary, then run the full test suite "
                "yourself (browser tests included), since CI never finished checking this commit."
            )
            return note, False

    if unfinished:
        # Still running, but not stuck yet - say nothing and don't guess at
        # its outcome; run everything locally as usual.
        return "", False

    if all(r.get("conclusion") == "success" for r in runs):
        return (
            "CI already passed for this exact commit, so the browser/E2E suite doesn't need to "
            "be repeated locally this round - still run lint and the unit/feature tests to "
            "validate whatever you change. (CI will check the commit you're about to push, same "
            "as always.)"
        ), True

    return "", False


async def _collect_failures(repo_full_name: str, head_sha: str) -> str:
    """Every failing GitHub Actions job for this commit, each with its log
    tail, formatted as one feedback block.
    """
    check_runs = await gh_host.list_check_runs_for_ref(repo_full_name, head_sha)
    failing = [
        run for run in check_runs
        if run.get("conclusion") == "failure" and (run.get("app") or {}).get("slug") == CI_APP_SLUG
    ]
    if not failing:
        return ""

    blocks = []
    for run in failing:
        name = run.get("name", "unknown job")
        try:
            log_text = await gh_host.get_job_log(repo_full_name, run["id"])
        except Exception:
            log.exception("failed to fetch logs for job %s (%s)", run.get("id"), name)
            continue
        tail = log_text[-MAX_LOG_CHARS:]
        blocks.append(f"### CI job failed: {name}\n```\n{tail}\n```")
    return "\n\n".join(blocks)
