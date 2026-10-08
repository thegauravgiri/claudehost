"""FastAPI app: webhook endpoints, startup reconciliation, idle-worktree
sweep. All tracker/host-specific logic lives in sources/ and hosts/; this
file only wires HTTP in and out of core.Orchestrator.
"""
import asyncio
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request

import ci_fix
import pr_autofix
import state
from core import Orchestrator
from hosts import github as gh_host
from sources import jira as jira_source

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("orchestrator.main")

IDLE_SWEEP_INTERVAL_SECONDS = 900
IDLE_TTL_SECONDS = int(os.environ.get("ORCHESTRATOR_IDLE_TTL_SECONDS", 60 * 60 * 24 * 3))
# How long a PR auto-fix loop can sit in "awaiting_review" before it's
# flagged stalled - nothing confirmed the last push worked, or the webhook
# that would have never arrived.
CODERABBIT_STALL_SECONDS = 45 * 60
# How often to check for PRs that armed one of CodeRabbit/CI but are still
# waiting on the other, and for CI runs that have been going too long -
# independent of, and much more frequent than, the idle/stalled sweep above,
# since pr_autofix.COMBINE_WAIT_SECONDS (default 5 min) and ci_fix's stuck-CI
# threshold both need finer-grained polling than that sweep's 15-minute
# interval.
PENDING_SWEEP_INTERVAL_SECONDS = 20

app = FastAPI()
orchestrator = Orchestrator(
    litellm_base_url=os.environ["LITELLM_BASE_URL"],
    litellm_api_key=os.environ["LITELLM_API_KEY"],
)


@app.on_event("startup")
async def _startup() -> None:
    state.init()
    requeued = orchestrator.reconcile_and_resume(jira_source.post_comment)
    if requeued:
        log.warning("reconciled %d in-flight run(s) after restart: %s", len(requeued), requeued)
    asyncio.create_task(_idle_sweep_loop())
    asyncio.create_task(_pending_sweep_loop())


async def _idle_sweep_loop() -> None:
    while True:
        await asyncio.sleep(IDLE_SWEEP_INTERVAL_SECONDS)
        try:
            swept = await asyncio.to_thread(orchestrator.sweep_idle, IDLE_TTL_SECONDS)
            if swept:
                log.info("idle sweep collected %d issue(s): %s", len(swept), swept)
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the loop
            log.exception("idle sweep failed")
        try:
            stalled = await pr_autofix.sweep_stalled(
                CODERABBIT_STALL_SECONDS, orchestrator.repo_full_name_by_slug,
            )
            if stalled:
                log.warning("marked %d PR auto-fix loop(s) stalled: %s", len(stalled), stalled)
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the loop
            log.exception("pr_autofix stalled-loop sweep failed")


async def _pending_sweep_loop() -> None:
    while True:
        await asyncio.sleep(PENDING_SWEEP_INTERVAL_SECONDS)
        try:
            dispatched = await pr_autofix.sweep_pending(orchestrator)
            if dispatched:
                log.info("pending-combine sweep dispatched %d PR(s) solo: %s", len(dispatched), dispatched)
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the loop
            log.exception("pr_autofix pending-combine sweep failed")
        try:
            stuck = await pr_autofix.sweep_stuck_ci(orchestrator)
            if stuck:
                log.warning("stuck-CI sweep dispatched %d PR(s): %s", len(stuck), stuck)
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the loop
            log.exception("pr_autofix stuck-CI sweep failed")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhooks/jira")
async def jira_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    secret: str | None = None,
):
    token = x_webhook_secret or secret
    if not jira_source.verify_webhook(token):
        log.warning(
            "Jira webhook rejected: bad secret. Header: %r, Query secret: %r, All headers: %s",
            x_webhook_secret,
            secret,
            dict(request.headers),
        )
        raise HTTPException(status_code=401, detail="bad webhook secret")

    payload = await request.json()
    item = await jira_source.parse_webhook(payload)
    if item is None:
        log.info("Jira webhook ignored (did not match trigger conditions)")
        return {"status": "ignored"}

    log.info("Jira webhook accepted for issue %s; delegating to orchestrator", item.external_id)
    await orchestrator.handle(item, jira_source.post_comment)
    return {"status": "accepted"}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    body = await request.body()
    if not gh_host.verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad webhook signature")

    payload = await request.json()

    if x_github_event == "pull_request":
        await asyncio.to_thread(
            gh_host.handle_pull_request_event, payload, orchestrator.repo_by_full_name,
        )
        await pr_autofix.handle_pr_opened(payload, orchestrator)
    elif x_github_event == "pull_request_review":
        await pr_autofix.handle_review(payload, orchestrator)
    elif x_github_event == "issue_comment":
        await pr_autofix.handle_command(payload, orchestrator)
    elif x_github_event == "check_suite":
        await ci_fix.handle_check_suite(payload, orchestrator)
    else:
        log.info("Ignoring GitHub webhook event %r", x_github_event)
    return {"status": "ok"}
