"""Jira-specific code, and only Jira-specific code: parsing `/agent <text>`
out of an inbound webhook, and rendering a result back into a comment. This
is the one file that changes if the tracker is ever swapped for GitHub
Issues - core.py knows nothing about Jira or ADF.
"""
import hmac
import os
import re

import httpx

from core import WorkItem

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
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(body)
    return "".join(parts)


def parse_webhook(payload: dict) -> WorkItem | None:
    """None means: not a comment event, not an /agent command, or a comment
    posted by the bot's own account (without this guard, our own result
    comment would re-trigger the webhook and the agent would loop on itself).
    """
    if payload.get("webhookEvent") not in ("comment_created", "comment_updated"):
        return None

    comment = payload.get("comment") or {}
    author = comment.get("author") or {}
    if JIRA_EMAIL and author.get("emailAddress", "").lower() == JIRA_EMAIL.lower():
        return None

    text = _extract_text(comment.get("body")).strip()
    match = TRIGGER_RE.match(text)
    if not match:
        return None

    issue = payload.get("issue") or {}
    issue_key = issue.get("key")
    fields = issue.get("fields") or {}
    project_key = (fields.get("project") or {}).get("key")
    if not issue_key or not project_key:
        return None

    return WorkItem(
        source="jira",
        external_id=issue_key,
        repo_slug=project_key,
        title=fields.get("summary") or "",
        description=_extract_text(fields.get("description")),
        instruction=match.group(1).strip(),
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
