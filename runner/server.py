import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import asyncio
import shutil

import conversation as conv
from executor import (
    DEFAULT_TIMEOUT_SECONDS, ROOT_WORKSPACE, QueueTimeoutError, RateLimitedError,
    UnknownAccountError, WorkspaceBusyError, executor,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("runner.server")

API_TOKEN = os.environ.get("RUNNER_API_TOKEN") or os.environ.get("CLAUDEBOX_API_MODE_TOKEN")

app = FastAPI(title="Claude Host Runner", version="1.0.0")

# Resume the Claude Code session behind a stateless conversation instead of
# re-sending (and re-caching) the whole history every turn. 0 disables.
CONVERSATION_TTL_SECONDS = int(os.environ.get("CONVERSATION_TTL_SECONDS", "3600"))
CONVERSATION_MAX_ENTRIES = int(os.environ.get("CONVERSATION_MAX_ENTRIES", "500"))
AUTO_PREFIX = "auto-"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
# Hidden thinking can run away (one real call spent 128k output tokens on a
# 250-token answer and took 459s, past the caller's 300s client timeout, and
# LiteLLM does not cancel upstream calls when its client gives up). So
# stateless calls get a hard deadline and an output-token ceiling.
STATELESS_TIMEOUT_SECONDS = int(os.environ.get("STATELESS_TIMEOUT_SECONDS", "240"))
STATELESS_MAX_OUTPUT_TOKENS = int(os.environ.get("STATELESS_MAX_OUTPUT_TOKENS", "32000"))
# The CLI fails (rather than truncating) when output, including hidden
# thinking, passes its cap, so a caller's small max_tokens (e.g. 16) is never
# handed to it directly: the CLI gets at least this much and the reply is
# truncated to the caller's limit afterwards, as the OpenAI API does.
# Stateless turns arrive seconds apart, so 5-minute cache writes (~1.25x input
# price) beat the CLI's default 1-hour ones (~2x). Agent sessions keep the
# default since their turns can be separated by long test runs.
STATELESS_CACHE_TTL = os.environ.get("STATELESS_CACHE_TTL", "5m")
VALID_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
MIN_CLI_OUTPUT_TOKENS = int(os.environ.get("MIN_CLI_OUTPUT_TOKENS", "4096"))
CHARS_PER_TOKEN = 4
# Identical requests arriving while one is already running share its result
# instead of each spending quota on the same prompt (client retries, fan-out).
_inflight: dict[str, asyncio.Future] = {}
store = conv.ConversationStore(CONVERSATION_TTL_SECONDS, CONVERSATION_MAX_ENTRIES)


class ClientDisconnected(Exception):
    pass


class Message(BaseModel):
    role: str
    content: str | list[Any] | None = ""
    tool_calls: list[Any] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "claude-sonnet"
    messages: list[Message]
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    thinking: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    reasoning: dict[str, Any] | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None


def _sse_chunks(
    chat_id: str, created: int, model: str, text: str, tool_calls: list | None = None,
    finish_reason: str = "stop",
):
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
    if tool_calls:
        yield frame({"tool_calls": [{"index": i, **tc} for i, tc in enumerate(tool_calls)]}, None)
        yield frame({}, "tool_calls")
        yield "data: [DONE]\n\n"
        return
    if text:
        yield frame({"content": text}, None)
    yield frame({}, finish_reason)
    yield "data: [DONE]\n\n"


def _fail_leader(fut: asyncio.Future | None, err: BaseException) -> None:
    if fut is not None and not fut.done():
        fut.set_exception(err)
        fut.exception()  # mark retrieved so a follower-less future doesn't warn


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
    return {"status": "ok", "service": "runner", **executor.stats(), "inflight": len(_inflight)}


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
    x_aicodebox_account: str | None = Header(default=None, alias="X-Aicodebox-Account"),
):
    verify_auth(authorization)

    try:
        account = executor.resolve_account(x_aicodebox_account)
    except UnknownAccountError as err:
        raise HTTPException(status_code=400, detail=str(err))

    # Workspace
    workspace = x_aicodebox_workspace or x_agent_workspace

    # Continue / Resume
    continue_val = (x_aicodebox_continue or x_agent_continue or "").lower()
    resume = continue_val in ("1", "true", "yes")

    # A stateless caller with no workspace must not share the default one, or
    # concurrent requests collide on it (409) instead of being processed.
    client_set_workspace = workspace is not None
    if workspace is None and not resume:
        workspace = f"{AUTO_PREFIX}{uuid.uuid4()}"

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

    # Timeout - only a fallback for a call that omits the header.
    timeout = DEFAULT_TIMEOUT_SECONDS
    if x_aicodebox_timeout_seconds:
        try:
            timeout = int(x_aicodebox_timeout_seconds)
        except ValueError:
            pass
    elif not resume and workspace.startswith(AUTO_PREFIX):
        timeout = min(timeout, STATELESS_TIMEOUT_SECONDS)

    stateless = not resume and workspace.startswith(AUTO_PREFIX)
    requested_cap = req.max_completion_tokens or req.max_tokens
    out_cap = requested_cap
    if stateless:
        out_cap = min(out_cap or STATELESS_MAX_OUTPUT_TOKENS, STATELESS_MAX_OUTPUT_TOKENS)
    if out_cap:
        out_cap = max(out_cap, MIN_CLI_OUTPUT_TOKENS)
    call_env: dict[str, str] = {}
    if out_cap:
        call_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(out_cap)
    if stateless and STATELESS_CACHE_TTL:
        call_env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = STATELESS_CACHE_TTL

    msgs = [conv.normalize(m.model_dump()) for m in req.messages]
    system_parts = [m["content"] for m in msgs if m["role"] == "system"]
    convo = [m for m in msgs if m["role"] != "system"]

    tools = req.tools if req.tool_choice != "none" else None
    schemas = conv.tool_schemas(tools or [])
    tool_names = set(schemas)
    if tool_names:
        system_parts.append(conv.tools_prompt(tools, req.tool_choice))
    # OpenAI-style reasoning effort maps to the CLI's --effort. The LiteLLM
    # callback does this too, but only for the four standard model names.
    effort = (req.reasoning or {}).get("effort") or req.reasoning_effort
    if isinstance(effort, str) and effort.lower() in VALID_EFFORT_LEVELS and "--effort" not in extra_args:
        extra_args = [*extra_args, "--effort", effort.lower()]
    # No CLI flag turns thinking off entirely; the closest is the lowest effort
    # level (measured ~30% fewer thinking tokens), used only if the caller
    # asked for no thinking and didn't pick an effort itself.
    if req.thinking and req.thinking.get("type") == "disabled" and "--effort" not in extra_args:
        extra_args = [*extra_args, "--effort", "low"]
    # A caller that names no workspace wants a plain completion, not a coding
    # agent. Without these defaults Claude Code loads its full tool set and
    # system prompt (~18k tokens per call instead of ~700). This used to live
    # only in the LiteLLM callback, which skips any model name it doesn't know,
    # such as per-account names like "mcm-claude-sonnet".
    stateless_call = not client_set_workspace and not resume
    if stateless_call and not system_parts:
        system_parts.append(DEFAULT_SYSTEM_PROMPT)
    if stateless_call and "--safe-mode" not in extra_args:
        extra_args = [*extra_args, "--safe-mode"]
    system_prompt = "\n\n".join(system_parts) if system_parts else None

    # JSON Schema
    json_schema = None
    if req.response_format:
        if req.response_format.get("type") == "json_schema":
            json_schema = req.response_format.get("json_schema", {}).get("schema")
        elif req.response_format.get("type") == "json_object":
            json_schema = {"type": "object"}

    if x_aicodebox_no_tools is not None:
        no_tools = x_aicodebox_no_tools.lower() in ("1", "true", "yes")
    else:
        no_tools = stateless_call

    # Only stateless callers (no explicit workspace, or a throwaway auto-
    # one) get conversation resume; stateful callers manage their own.
    use_store = (
        CONVERSATION_TTL_SECONDS > 0
        and not resume
        and (workspace is None or workspace.startswith(AUTO_PREFIX))
    )
    config_key = json.dumps(
        [req.model, system_prompt, x_aicodebox_append_system_prompt, json_schema, no_tools, extra_args, account],
        sort_keys=True,
    )
    hashes = conv.chain_hashes(convo, config_key) if use_store else []

    async def run(prompt: str, ws: str | None, session_id: str | None):
        task = asyncio.ensure_future(_execute(prompt, ws, session_id))
        # The client's own timeout (often 5 min) can fire while we're queued
        # or running; stop then instead of spending quota on an unread answer.
        while True:
            done, _ = await asyncio.wait({task}, timeout=1.0)
            if done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise ClientDisconnected()

    async def _execute(prompt: str, ws: str | None, session_id: str | None):
        return await executor.execute(
            prompt=prompt,
            workspace_subpath=ws,
            model=req.model,
            system_prompt=system_prompt,
            append_system_prompt=x_aicodebox_append_system_prompt,
            json_schema=json_schema,
            resume=resume,
            no_tools=no_tools,
            extra_args=extra_args,
            timeout_seconds=timeout,
            session_id=session_id,
            env=call_env,
            account=account,
        )

    def drop_auto_workspace(ws: str | None) -> None:
        if ws and ws.startswith(AUTO_PREFIX) and "/" not in ws:
            shutil.rmtree(ROOT_WORKSPACE / ws, ignore_errors=True)

    full_key = conv.chain_hashes(convo, config_key)[-1] if use_store else None
    leader_future: asyncio.Future | None = None
    follower_future: asyncio.Future | None = None
    if full_key is not None:
        follower_future = _inflight.get(full_key)
        if follower_future is None:
            leader_future = asyncio.get_running_loop().create_future()
            _inflight[full_key] = leader_future

    try:
        if follower_future is not None:
            log.info("Joining identical in-flight request")
            result, used_ws = await asyncio.shield(follower_future)
            leader = False
        else:
            leader = True
            hit = store.take_longest(hashes, convo) if use_store else None
            result = None
            used_ws = workspace
            if hit is not None:
                k, hit_key, entry = hit
                used_ws = entry.workspace
                log.info("Resuming session %s with %d new message(s)", entry.session_id, len(convo) - k)
                try:
                    result = await run(conv.render_messages(convo[k:]), entry.workspace, entry.session_id)
                except (RateLimitedError, QueueTimeoutError, ClientDisconnected):
                    store.put(hit_key, entry)
                    raise
                if result.failed:
                    if result.rate_limited:
                        store.put(hit_key, entry)
                    elif "No conversation found" in result.text:
                        log.warning("Session %s is gone; retrying fresh", entry.session_id)
                        result = None
                    else:
                        log.warning("Resumed turn on %s failed: %s", entry.session_id, result.text[:200])
                drop_auto_workspace(entry.workspace)
            if result is None:
                used_ws = workspace
                result = await run(conv.render_messages(convo), workspace, None)
            if leader_future is not None:
                leader_future.set_result((result, used_ws))
    except ClientDisconnected:
        _fail_leader(leader_future, QueueTimeoutError("the identical request this one joined was cancelled"))
        log.info("Client disconnected; cancelled its request")
        raise HTTPException(status_code=499, detail="client closed request")
    except RateLimitedError as err:
        _fail_leader(leader_future, err)
        raise HTTPException(
            status_code=429, detail=str(err), headers={"Retry-After": str(err.retry_after)},
        )
    except QueueTimeoutError as err:
        _fail_leader(leader_future, err)
        log.error("Queue timeout: %s", err)
        raise HTTPException(status_code=503, detail=str(err), headers={"Retry-After": "30"})
    except WorkspaceBusyError as err:
        _fail_leader(leader_future, err)
        log.warning("Workspace busy: %s", err)
        raise HTTPException(status_code=409, detail=str(err))
    except ValueError as err:
        _fail_leader(leader_future, err)
        log.error("Invalid request: %s", err)
        raise HTTPException(status_code=400, detail=str(err))
    except TimeoutError as err:
        _fail_leader(leader_future, err)
        log.error("Execution timeout: %s", err)
        raise HTTPException(status_code=504, detail=str(err))
    except Exception as err:
        _fail_leader(leader_future, err)
        log.exception("Execution error: %s", err)
        raise HTTPException(status_code=500, detail=f"Runner execution failed: {err}")
    finally:
        if leader_future is not None:
            _inflight.pop(full_key, None)
            _fail_leader(leader_future, RuntimeError("leading request was cancelled"))

    if result.failed:
        log.error(
            "Claude reported failure (exit=%s, api_status=%s): %s",
            result.exit_code, result.api_error_status, result.text[:300],
        )
        if result.rate_limited:
            headers = {}
            if result.retry_after_seconds:
                headers["Retry-After"] = str(result.retry_after_seconds)
            raise HTTPException(status_code=429, detail=result.text, headers=headers)
        raise HTTPException(status_code=502, detail=result.text or "Claude execution failed")

    tool_calls = conv.parse_tool_calls(result.text, schemas) if tool_names else None
    text = result.text
    finish_reason = "tool_calls" if tool_calls else "stop"
    if (
        requested_cap and not tool_calls and not json_schema
        and len(text) > requested_cap * CHARS_PER_TOKEN
    ):
        text = text[: requested_cap * CHARS_PER_TOKEN]
        finish_reason = "length"
    reply = {"role": "assistant", "content": "" if tool_calls else text}
    if tool_calls:
        reply["tool_calls"] = tool_calls
    if use_store and leader and result.session_id:
        key = conv.chain_hashes([*convo, conv.normalize(reply)], config_key)[-1]
        store.put(key, conv.Entry(result.session_id, used_ws, time.time()))
    elif leader and workspace:
        drop_auto_workspace(workspace)

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if req.stream:
        return StreamingResponse(
            _sse_chunks(chat_id, created, req.model, text, tool_calls, finish_reason),
            media_type="text/event-stream",
        )

    # Build standard OpenAI response
    raw = result.usage or {}
    cache_write = raw.get("cache_creation_input_tokens") or 0
    cache_read = raw.get("cache_read_input_tokens") or 0
    prompt_tokens = (raw.get("input_tokens") or 0) + cache_write + cache_read
    completion_tokens = raw.get("output_tokens") or 0
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "completion_tokens_details": {
            "reasoning_tokens": (raw.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
        },
        "prompt_tokens_details": {"cached_tokens": cache_read},
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": (
                    {"role": "assistant", "content": None, "tool_calls": tool_calls}
                    if tool_calls
                    else {"role": "assistant", "content": text}
                ),
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
