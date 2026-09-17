"""Auto-assigns a random claudebox workspace per request, so stateless
callers (using this like a plain Anthropic API) never collide on the
shared default workspace and hit "409 workspace busy" under concurrency -
see docs/ADVANCED.md#troubleshooting. Callers that already set
X-Aicodebox-Workspace themselves (real agentic/project sessions) are left
untouched.
"""
import uuid
from typing import Literal

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import DualCache, UserAPIKeyAuth

WORKSPACE_HEADER = "X-Aicodebox-Workspace"


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
            extra[WORKSPACE_HEADER] = f"auto-{uuid.uuid4()}"
            data["extra_headers"] = extra
        return data


proxy_handler_instance = AutoWorkspaceHandler()
