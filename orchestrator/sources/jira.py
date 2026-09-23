"""Jira-specific code, and only Jira-specific code: parsing `/agent <text>`
out of an inbound webhook, and rendering a result back into a comment. This
is the one file that changes if the tracker is ever swapped for GitHub
Issues - core.py knows nothing about Jira or ADF.
"""
import hmac
import logging
import os
import re

import httpx

from core import WorkItem

log = logging.getLogger("orchestrator.sources.jira")

TRIGGER_RE = re.compile(r"^/agent\s+(.+)", re.IGNORECASE | re.DOTALL)

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("JIRA_WEBHOOK_SECRET", "")


def verify_webhook(secret_header: str | None) -> bool:
    if not WEBHOOK_SECRET:
        return True
    return secret_header is not None and hmac.compare_digest(
        secret_header, WEBHOOK_SECRET,
    )


def _extract_text(body) -> str:
    """Jira sometimes hands back a plain string, sometimes an ADF doc
    (a nested {type, content: [...]} tree) - collect every "text" leaf
    either way rather than assuming one shape.
    """
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            if node_type == "text":
                parts.append(node.get("text", ""))
            elif node_type == "hardBreak":
                parts.append("\n")
            for child in node.get("content") or []:
                walk(child)
            if node_type in ("paragraph", "heading"):
                parts.append("\n\n")
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(body)
    return "".join(parts).strip()


def _format_comments_history(comments_data: dict, current_comment_text: str = "") -> str:
    comments = comments_data.get("comments") or []
    if not comments:
        return ""
    formatted = []
    for c in comments[-6:]:
        author = (c.get("author") or {}).get("displayName") or "User"
        body = _extract_text(c.get("body")).strip()
        if body and body != current_comment_text:
            formatted.append(f"- [{author}]: {body}")
    if not formatted:
        return ""
    return "\nRecent ticket comments / discussion:\n" + "\n".join(formatted) + "\n"


async def fetch_issue(issue_key: str) -> dict:
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}?fields=summary,description,comment"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        resp.raise_for_status()
        return resp.json()


async def parse_webhook(payload: dict) -> WorkItem | None:
    """None means: not a comment event, not an /agent command, or a comment
    posted by the bot's own account (without this guard, our own result
    comment would re-trigger the webhook and the agent would loop on itself).
    """
    log.info("Received Jira payload keys: %s", list(payload.keys()))
    event = payload.get("webhookEvent")
    log.info("Received Jira webhook event: %s", event)
    if event not in ("comment_created", "comment_updated"):
        log.info("Ignoring webhook: event %r is not comment_created or comment_updated", event)
        return None

    comment = payload.get("comment") or {}
    author = comment.get("author") or {}
    author_email = author.get("emailAddress", "")
    if JIRA_EMAIL and author_email.lower() == JIRA_EMAIL.lower():
        log.info("Ignoring webhook: author email %r matches JIRA_EMAIL %r", author_email, JIRA_EMAIL)
        return None

    text = _extract_text(comment.get("body")).strip()
    log.info("Extracted comment body: %r", text)
    match = TRIGGER_RE.match(text)
    if not match:
        log.info("Ignoring webhook: comment body does not match '^/agent <instruction>' pattern")
        return None

    issue = payload.get("issue") or {}
    issue_key = issue.get("key")
    fields = issue.get("fields") or {}
    project_key = (fields.get("project") or {}).get("key")
    if not issue_key or not project_key:
        log.warning("Ignoring webhook: missing issue_key (%r) or project_key (%r)", issue_key, project_key)
        return None

    title = fields.get("summary") or ""
    description = _extract_text(fields.get("description"))
    comments_section = ""

    # Fetch full issue details (description and recent comments) from Jira REST API
    if JIRA_BASE_URL and JIRA_API_TOKEN:
        try:
            issue_data = await fetch_issue(issue_key)
            fetched_fields = issue_data.get("fields") or {}
            title = fetched_fields.get("summary") or title
            if not description:
                description = _extract_text(fetched_fields.get("description"))
            comments_section = _format_comments_history(
                fetched_fields.get("comment") or {}, current_comment_text=text,
            )
            log.info("Fetched issue details and comments from Jira for %s", issue_key)
        except Exception:
            log.exception("Failed to fetch full issue details from Jira REST API for %s", issue_key)

    log.info(
        "Parsed WorkItem for issue %s (project %s):\n  Title: %r\n  Description: %r\n  Instruction: %r",
        issue_key,
        project_key,
        title,
        description,
        match.group(1).strip(),
    )
    return WorkItem(
        source="jira",
        external_id=issue_key,
        repo_slug=project_key,
        title=title,
        description=description,
        instruction=match.group(1).strip(),
        comments_section=comments_section,
    )


def _adf_from_text(text: str) -> dict:
    """Minimal ADF: one paragraph per blank-line-separated block, plain text
    only. Jira Cloud's REST v3 comment endpoint requires ADF rather than a
    plain string; an accurate plain-text comment is all this needs to be, so
    bold/links aren't worth the extra complexity here.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]}
        for p in paragraphs
    ]
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph"}]}


async def post_comment(issue_key: str, text: str) -> None:
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            json={"body": _adf_from_text(text)},
        )
        resp.raise_for_status()
