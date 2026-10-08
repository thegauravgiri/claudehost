import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("runner.executor")

ROOT_WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
# Only a fallback: the orchestrator always sends its own value via the
# X-Aicodebox-Timeout-Seconds header (see core.AGENT_TIMEOUT_SECONDS), so
# this matters only for a direct API call that omits that header.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "3600"))


class WorkspaceBusyError(Exception):
    """Raised when a workspace is already busy executing a prompt."""


@dataclass
class RunResult:
    text: str
    session_id: str
    usage: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0


class RunnerExecutor:
    def __init__(self, workspace_root: Path = ROOT_WORKSPACE):
        self.workspace_root = workspace_root
        self._busy_workspaces: set[str] = set()
        self._lock = asyncio.Lock()

    def resolve_workspace(self, subpath: str | None) -> Path:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if not subpath:
            return self.workspace_root
        p = Path(subpath)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Invalid workspace subpath: {subpath}")
        target = (self.workspace_root / p).resolve()
        if not str(target).startswith(str(self.workspace_root.resolve())):
            raise ValueError(f"Workspace path escapes root: {subpath}")
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def acquire_workspace(self, workspace_path: str) -> bool:
        async with self._lock:
            if workspace_path in self._busy_workspaces:
                return False
            self._busy_workspaces.add(workspace_path)
            return True

    async def release_workspace(self, workspace_path: str) -> None:
        async with self._lock:
            self._busy_workspaces.discard(workspace_path)

    async def execute(
        self,
        prompt: str,
        workspace_subpath: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        json_schema: dict | None = None,
        resume: bool = False,
        no_tools: bool = False,
        extra_args: list[str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> RunResult:
        workspace_path = self.resolve_workspace(workspace_subpath)
        workspace_str = str(workspace_path)

        if not await self.acquire_workspace(workspace_str):
            raise WorkspaceBusyError(f"Workspace {workspace_str} is currently busy")

        try:
            return await self._run_process(
                prompt=prompt,
                cwd=workspace_path,
                model=model,
                system_prompt=system_prompt,
                append_system_prompt=append_system_prompt,
                json_schema=json_schema,
                resume=resume,
                no_tools=no_tools,
                extra_args=extra_args,
                timeout_seconds=timeout_seconds,
            )
        finally:
            await self.release_workspace(workspace_str)

    async def _run_process(
        self,
        prompt: str,
        cwd: Path,
        model: str | None,
        system_prompt: str | None,
        append_system_prompt: str | None,
        json_schema: dict | None,
        resume: bool,
        no_tools: bool,
        extra_args: list[str] | None,
        timeout_seconds: int,
    ) -> RunResult:
        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
        ]

        if model:
            clean_model = model.removeprefix("claude-").removeprefix("openai/").removeprefix("claudebox/")
            cmd.extend(["--model", clean_model])

        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        if append_system_prompt:
            cmd.extend(["--append-system-prompt", append_system_prompt])

        if json_schema:
            cmd.extend(["--json-schema", json.dumps(json_schema)])

        if resume:
            cmd.append("--continue")
        # No "else": claude has no --no-continue flag (verified against
        # `claude --help` - passing it is a hard CLI error). Omitting
        # --continue already starts a fresh session, which is the
        # resume=False behavior this needs.

        if no_tools:
            # "" is the documented way to disable every tool (verified
            # against `claude --help`); --disallowedTools takes tool names,
            # not glob patterns, so "*" here never actually matched anything.
            cmd.extend(["--tools", ""])

        if extra_args:
            cmd.extend(extra_args)

        log.info("Executing claude in %s: %s", cwd, " ".join(cmd[:10]))

        loop = asyncio.get_running_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
            ),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                loop.run_in_executor(None, proc.communicate),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            # Deliberately not a second proc.communicate() call here: wait_for
            # timing out only gives up on *awaiting* the first one - it's a
            # blocking call running in its own worker thread, which keeps
            # reading the pipes regardless. A second concurrent communicate()
            # races the first one over the same stdout/stderr file
            # descriptors and crashes with "OSError: Bad file descriptor"
            # instead of ever raising this clean timeout (which is what
            # actually happened here: the crash surfaced as an opaque 500,
            # masking the real timeout and leaving the orchestrator to wait
            # out its own separate, unrelated client-side timeout instead).
            # proc.kill() alone is enough: it makes the pipes hit EOF, which
            # unblocks that still-running thread on its own - we don't need
            # its output anyway, we're raising a timeout regardless.
            raise TimeoutError(f"Claude execution timed out after {timeout_seconds}s")

        return self._parse_stream_json(stdout, proc.returncode, stderr)

    def _parse_stream_json(self, stdout: str, exit_code: int, stderr: str) -> RunResult:
        session_id = ""
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        result_text: str | None = None
        structured_output: Any = None

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "system" and event.get("subtype") == "init":
                sid = event.get("session_id", "")
                if isinstance(sid, str):
                    session_id = sid
                continue

            if etype == "assistant":
                msg = event.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                txt = block.get("text", "")
                                if txt:
                                    text_parts.append(txt)
                continue

            if etype == "result":
                res_usage = event.get("usage")
                if isinstance(res_usage, dict):
                    usage = dict(res_usage)
                # --json-schema runs answer through the engine's own
                # StructuredOutput tool rather than plain assistant text; the
                # validated object comes back on this field (verified against
                # the claude binary's zod schema - SDKResultMessage.structured_output).
                if "structured_output" in event:
                    structured_output = event.get("structured_output")
                res = event.get("result")
                if isinstance(res, str):
                    result_text = res
                continue

        if structured_output is not None:
            final_text = json.dumps(structured_output)
        elif result_text is not None:
            final_text = result_text
        else:
            final_text = "".join(text_parts).strip()

        if not final_text and exit_code != 0:
            final_text = f"Process exited with code {exit_code}: {stderr.strip()}"

        return RunResult(
            text=final_text,
            session_id=session_id,
            usage=usage,
            exit_code=exit_code,
        )


executor = RunnerExecutor()
