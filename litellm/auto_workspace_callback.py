"""This callback is registered globally in litellm_settings.callbacks, so it
fires for every model on this gateway - including ones that have nothing to
do with claudebox (e.g. a directly-configured Azure/OpenAI model). Every
change below only ever applies to the claudebox-backed model names in
CLAUDEBOX_MODELS; anything else returns untouched on the first line.

For claudebox models:

Assigns a random workspace to any request that doesn't set one, so
stateless callers don't collide on the shared default workspace and hit
"409 workspace busy" under concurrent load. Requests that set
X-Aicodebox-Workspace themselves are left untouched. Auto-assigned
workspaces are single-use and deleted right after the call finishes; this
container shares the ./workspaces mount with claudebox so the directory it
created is visible here too.

Auto-assigned requests also get X-Aicodebox-No-Tools and a minimal
placeholder system message if they didn't supply their own, plus
--safe-mode (disables CLAUDE.md/skills/plugins/hooks auto-loading).
Together this cuts total tokens for a plain call with no system prompt
of its own from ~24-33k down to ~150-160 (verified) by never letting
Claude Code's own agent framing - system prompt, internal tools, or
project-memory discovery - leak into a workspace-less call that has
nothing for any of that to act on anyway. A caller that supplies its
own system message, or sets a workspace, is left untouched on each of
these respectively. (--bare would disable CLAUDE.md discovery too, but
also forces API-key-only auth and drops OAuth/subscription auth
entirely - not usable here.)

--exclude-dynamic-system-prompt-sections was tried first here and is
NOT used: its own help text says it's ignored whenever --system-prompt
is set, and a system message (the caller's or our injected default) is
now always present for a workspace-less call, so it would always be a
no-op in this configuration.

Also translates the standard OpenAI `reasoning`/`reasoning_effort`
parameters into claudebox's `--effort` CLI flag via
X-Aicodebox-Extra-Args, for both workspace and workspace-less calls -
claudebox accepts but never wires those OpenAI-standard fields to
anything, so without this translation they're silently inert.

All of the X-Aicodebox-Extra-Args injections above (safe-mode, effort)
are skipped whenever the client already sent its own Extra-Args header:
forward_client_headers_to_llm_api forwards the client's raw header
regardless of what this hook sets in data["extra_headers"], so setting
both results in claudebox receiving an invalid, comma-joined value. Same
guard applies to X-Aicodebox-Workspace and X-Aicodebox-No-Tools.
"""
import json
import shutil
import uuid
from pathlib import Path
from typing import Literal

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import DualCache, UserAPIKeyAuth

CLAUDEBOX_MODELS = {"claude-haiku", "claude-sonnet", "claude-opus", "claude-opusplan"}

WORKSPACE_HEADER = "X-Aicodebox-Workspace"
EXTRA_ARGS_HEADER = "X-Aicodebox-Extra-Args"
NO_TOOLS_HEADER = "X-Aicodebox-No-Tools"
SAFE_MODE_FLAG = "--safe-mode"
AUTO_PREFIX = "auto-"
WORKSPACES_ROOT = Path("/workspaces")
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
VALID_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _extract_effort(data: dict) -> str | None:
    nested = data.get("reasoning")
    if isinstance(nested, dict):
        effort = nested.get("effort")
        if isinstance(effort, str) and effort.lower() in VALID_EFFORT_LEVELS:
            return effort.lower()
    flat = data.get("reasoning_effort")
    if isinstance(flat, str) and flat.lower() in VALID_EFFORT_LEVELS:
        return flat.lower()
    return None


def _cleanup(data: dict) -> None:
    try:
        workspace = (data.get("extra_headers") or {}).get(WORKSPACE_HEADER, "")
        if workspace.startswith(AUTO_PREFIX):
            path = WORKSPACES_ROOT / workspace
            if path.is_dir() and path.parent == WORKSPACES_ROOT:
                shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


class AutoWorkspaceHandler(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Literal[
            "completion", "text_completion", "embeddings",
            "image_generation", "moderation", "audio_transcription",
        ],
    ):
        if data.get("model") not in CLAUDEBOX_MODELS:
            return data

        # The client's raw header lands in data["headers"], not
        # data["extra_headers"] (a separate outbound-only mechanism).
        client_headers = data.get("headers") or {}
        has_workspace = any(k.lower() == WORKSPACE_HEADER.lower() for k in client_headers)
        has_extra_args = any(k.lower() == EXTRA_ARGS_HEADER.lower() for k in client_headers)
        has_no_tools = any(k.lower() == NO_TOOLS_HEADER.lower() for k in client_headers)

        extra_args_to_add = []
        if not has_workspace:
            extra_args_to_add.append(SAFE_MODE_FLAG)
        effort = _extract_effort(data)
        if effort:
            extra_args_to_add += ["--effort", effort]

        extra = data.get("extra_headers") or {}

        if not has_workspace:
            extra[WORKSPACE_HEADER] = f"{AUTO_PREFIX}{uuid.uuid4()}"
            if not has_no_tools:
                extra[NO_TOOLS_HEADER] = "true"

            messages = data.get("messages") or []
            has_system = any(
                isinstance(m, dict) and m.get("role") == "system" for m in messages
            )
            if not has_system:
                data["messages"] = [
                    {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                    *messages,
                ]

        if extra_args_to_add and not has_extra_args:
            extra[EXTRA_ARGS_HEADER] = json.dumps(extra_args_to_add)

        if extra:
            data["extra_headers"] = extra
        return data

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        _cleanup(data)
        return response

    async def async_post_call_failure_hook(
        self, request_data, original_exception, user_api_key_dict, traceback_str=None,
    ):
        _cleanup(request_data)


proxy_handler_instance = AutoWorkspaceHandler()
