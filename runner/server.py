import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from executor import DEFAULT_TIMEOUT_SECONDS, WorkspaceBusyError, executor

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("runner.server")

API_TOKEN = os.environ.get("RUNNER_API_TOKEN") or os.environ.get("CLAUDEBOX_API_MODE_TOKEN")

app = FastAPI(title="Claude Host Runner", version="1.0.0")


class Message(BaseModel):
    role: str
    content: str | list[Any] = ""


class ChatCompletionRequest(BaseModel):
    model: str = "claude-sonnet"
    messages: list[Message]
    response_format: dict[str, Any] | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


def _sse_chunks(chat_id: str, created: int, model: str, text: str):
    """Fakes an OpenAI streaming response as a single content delta: the
    executor already waits for Claude Code's full response (its subprocess
    is read via communicate(), not incrementally), so there's no real
    per-token stream to relay - only whether the client gets one chunk or
    none. Without this, a client that requests stream=true (the playground,
    most chat UIs) got a stream whose only delta was empty, since nothing
    upstream of this ever wrote the content into it - a 200 with no visible
    output and no error.
    """
    def frame(delta: dict, finish_reason: str | None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    yield frame({"role": "assistant"}, None)
    if text:
        yield frame({"content": text}, None)
    yield frame({}, "stop")
    yield "data: [DONE]\n\n"


def verify_auth(authorization: str | None) -> None:
    if not API_TOKEN:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "runner"}


@app.get("/openai/v1/models")
async def list_models(authorization: str | None = Header(default=None)):
    verify_auth(authorization)
    models = ["claude-haiku", "claude-sonnet", "claude-opus", "claude-opusplan", "haiku", "sonnet", "opus"]
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "claudehost",
            }
            for m in models
        ],
    }


@app.post("/openai/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_aicodebox_workspace: str | None = Header(default=None, alias="X-Aicodebox-Workspace"),
    x_agent_workspace: str | None = Header(default=None, alias="X-Agent-Workspace"),
    x_aicodebox_continue: str | None = Header(default=None, alias="X-Aicodebox-Continue"),
    x_agent_continue: str | None = Header(default=None, alias="X-Agent-Continue"),
    x_aicodebox_append_system_prompt: str | None = Header(default=None, alias="X-Aicodebox-Append-System-Prompt"),
    x_aicodebox_no_tools: str | None = Header(default=None, alias="X-Aicodebox-No-Tools"),
    x_aicodebox_timeout_seconds: str | None = Header(default=None, alias="X-Aicodebox-Timeout-Seconds"),
    x_aicodebox_extra_args: str | None = Header(default=None, alias="X-Aicodebox-Extra-Args"),
):
    verify_auth(authorization)

    # Workspace
    workspace = x_aicodebox_workspace or x_agent_workspace

    # Continue / Resume
    continue_val = (x_aicodebox_continue or x_agent_continue or "").lower()
    resume = continue_val in ("1", "true", "yes")

    # Extra arguments
    extra_args: list[str] = []
    if x_aicodebox_extra_args:
        try:
            parsed = json.loads(x_aicodebox_extra_args)
            if isinstance(parsed, list):
                extra_args = [str(x) for x in parsed]
            else:
                extra_args = [str(parsed)]
        except json.JSONDecodeError:
            extra_args = x_aicodebox_extra_args.split()

    # Timeout - only a fallback for a call that omits the header; the
    # orchestrator always sends one explicitly (see core.AGENT_TIMEOUT_SECONDS).
    timeout = DEFAULT_TIMEOUT_SECONDS
    if x_aicodebox_timeout_seconds:
        try:
            timeout = int(x_aicodebox_timeout_seconds)
        except ValueError:
            pass

    # Extract system prompt and user prompt
    system_parts = []
    prompt_parts = []

    for msg in req.messages:
        content_str = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        if msg.role == "system":
            system_parts.append(content_str)
        elif msg.role == "user":
            prompt_parts.append(content_str)
        elif msg.role == "assistant":
            prompt_parts.append(f"Assistant: {content_str}")

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    prompt = "\n\n".join(prompt_parts) if prompt_parts else ""

    # JSON Schema
    json_schema = None
    if req.response_format:
        if req.response_format.get("type") == "json_schema":
            json_schema = req.response_format.get("json_schema", {}).get("schema")
        elif req.response_format.get("type") == "json_object":
            json_schema = {"type": "object"}

    no_tools = (x_aicodebox_no_tools or "").lower() in ("1", "true", "yes")

    try:
        result = await executor.execute(
            prompt=prompt,
            workspace_subpath=workspace,
            model=req.model,
            system_prompt=system_prompt,
            append_system_prompt=x_aicodebox_append_system_prompt,
            json_schema=json_schema,
            resume=resume,
            no_tools=no_tools,
            extra_args=extra_args,
            timeout_seconds=timeout,
        )
    except WorkspaceBusyError as err:
        log.warning("Workspace busy: %s", err)
        raise HTTPException(status_code=409, detail=str(err))
    except ValueError as err:
        log.error("Invalid request: %s", err)
        raise HTTPException(status_code=400, detail=str(err))
    except TimeoutError as err:
        log.error("Execution timeout: %s", err)
        raise HTTPException(status_code=504, detail=str(err))
    except Exception as err:
        log.exception("Execution error: %s", err)
        raise HTTPException(status_code=500, detail=f"Runner execution failed: {err}")

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if req.stream:
        return StreamingResponse(
            _sse_chunks(chat_id, created, req.model, result.text),
            media_type="text/event-stream",
        )

    # Build standard OpenAI response
    usage = result.usage or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
