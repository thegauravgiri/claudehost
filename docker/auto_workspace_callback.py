"""Auto-assigns a random claudebox workspace per request, so stateless
callers (using this like a plain Anthropic API) never collide on the
shared default workspace and hit "409 workspace busy" under concurrency -
see docs/ADVANCED.md#troubleshooting. Callers that already set
X-Aicodebox-Workspace themselves (real agentic/project sessions) are left
untouched, and only ever touched here for detection, never deletion.

Auto-assigned workspaces are single-use, so they're deleted again right
after the call finishes (success or failure) - this container shares the
same ./workspaces host mount as claudebox (see docker-compose.yml) so the
directory it created is visible here too.
"""
import shutil
import uuid
from pathlib import Path
from typing import Literal

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import DualCache, UserAPIKeyAuth

WORKSPACE_HEADER = "X-Aicodebox-Workspace"
AUTO_PREFIX = "auto-"
WORKSPACES_ROOT = Path("/workspaces")


def _cleanup(data: dict) -> None:
    # Best-effort: a failure here should never surface as a request error.
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
        # The client's own raw header (if any) shows up in data["headers"] at
        # this point - NOT data["extra_headers"], which is a separate outbound
        # mechanism. Check the former, write through the latter (confirmed by
        # testing to actually reach claudebox as a real HTTP header).
        client_headers = data.get("headers") or {}
        if not any(k.lower() == WORKSPACE_HEADER.lower() for k in client_headers):
            extra = data.get("extra_headers") or {}
            extra[WORKSPACE_HEADER] = f"{AUTO_PREFIX}{uuid.uuid4()}"
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
