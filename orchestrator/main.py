"""FastAPI app: webhook endpoints, startup reconciliation, idle-worktree
sweep. All tracker/host-specific logic lives in sources/ and hosts/; this
file only wires HTTP in and out of core.Orchestrator.
"""
import asyncio
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request

import state
from core import Orchestrator
from hosts import github as gh_host
from sources import jira as jira_source

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("orchestrator.main")

IDLE_SWEEP_INTERVAL_SECONDS = 900
IDLE_TTL_SECONDS = int(os.environ.get("ORCHESTRATOR_IDLE_TTL_SECONDS", 60 * 60 * 24 * 3))

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


async def _idle_sweep_loop() -> None:
    while True:
        await asyncio.sleep(IDLE_SWEEP_INTERVAL_SECONDS)
        try:
            swept = await asyncio.to_thread(orchestrator.sweep_idle, IDLE_TTL_SECONDS)
            if swept:
                log.info("idle sweep collected %d issue(s): %s", len(swept), swept)
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the loop
            log.exception("idle sweep failed")


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
):
    body = await request.body()
    if not gh_host.verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad webhook signature")

    payload = await request.json()
    await asyncio.to_thread(
        gh_host.handle_pull_request_event, payload, orchestrator.repo_by_full_name,
    )
    return {"status": "ok"}
