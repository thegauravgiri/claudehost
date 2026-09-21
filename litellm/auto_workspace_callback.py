"""Assigns a random claudebox workspace to any request that doesn't set
one, so stateless callers don't collide on the shared default workspace
and hit "409 workspace busy" under concurrent load. Requests that set
X-Aicodebox-Workspace themselves are left untouched.

Auto-assigned workspaces are single-use and deleted right after the call
finishes; this container shares the ./workspaces mount with claudebox so
the directory it created is visible here too.

Auto-assigned requests also get --exclude-dynamic-system-prompt-sections,
unless the client already sent its own X-Aicodebox-Extra-Args (adding a
second one would collide with LiteLLM's own header forwarding). Claude
Code's system prompt embeds the cwd, and a fresh random workspace on
every call means that block never hits Anthropic's prompt cache; this
flag moves it out of the cached prefix, verified to cut per-call cache
misses from ~8.6k tokens down to ~3.5k.

Auto-assigned requests also get X-Aicodebox-No-Tools (same client-header
guard as above), disabling Claude Code's internal Bash/Read/Write/Edit
tools. A workspace-less call has no real repo to act on, so those tools
can't do anything useful anyway - the directory is deleted right after
the call, before a caller could ever retrieve anything written to it.
Verified this drops total tokens for a plain chat call from ~24-33k to
~727, and does not affect caller-supplied OpenAI-style `tools`/
`tool_choice` (a separate mechanism from claudebox's internal tools;
claudebox already disables internal tools by default whenever the
caller sends its own `tools`, so this only changes plain, non-tool-call
requests).
"""
import json
import shutil
import uuid
from pathlib import Path
from typing import Literal

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import DualCache, UserAPIKeyAuth

WORKSPACE_HEADER = "X-Aicodebox-Workspace"
EXTRA_ARGS_HEADER = "X-Aicodebox-Extra-Args"
NO_TOOLS_HEADER = "X-Aicodebox-No-Tools"
CACHE_FLAG = "--exclude-dynamic-system-prompt-sections"
AUTO_PREFIX = "auto-"
WORKSPACES_ROOT = Path("/workspaces")


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
        # The client's raw header lands in data["headers"], not
        # data["extra_headers"] (a separate outbound-only mechanism) - and
        # forward_client_headers_to_llm_api forwards that raw header
        # regardless of what we set here, so we can only add a header
        # cleanly when the client didn't also send one themselves (else
        # both get sent and claudebox receives a comma-joined, invalid value).
        client_headers = data.get("headers") or {}
        has_workspace = any(k.lower() == WORKSPACE_HEADER.lower() for k in client_headers)
        has_extra_args = any(k.lower() == EXTRA_ARGS_HEADER.lower() for k in client_headers)
        has_no_tools = any(k.lower() == NO_TOOLS_HEADER.lower() for k in client_headers)

        if not has_workspace:
            extra = data.get("extra_headers") or {}
            extra[WORKSPACE_HEADER] = f"{AUTO_PREFIX}{uuid.uuid4()}"
            if not has_extra_args:
                extra[EXTRA_ARGS_HEADER] = json.dumps([CACHE_FLAG])
            if not has_no_tools:
                extra[NO_TOOLS_HEADER] = "true"
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
