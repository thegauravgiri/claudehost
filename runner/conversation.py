"""Bridges stateless OpenAI-style chat history onto resumable Claude Code
sessions, and OpenAI tool calling onto a plain-text JSON protocol.

Why: every request used to flatten the whole message history into one `-p`
prompt on a brand-new session. The API only reuses a prompt-cache prefix at
content-block boundaries, so one giant text block never hit the cache (verified
in spend logs: zero cache reads, ~17k cache-write tokens per call, history up
to 126 messages). Resuming the session Claude Code already holds means only the
new messages are sent and the earlier turns are served from cache.
"""
import hashlib
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in (None, "text") and isinstance(p.get("text"), str):
                parts.append(p["text"])
            else:
                parts.append(json.dumps(p, sort_keys=True))
        return "".join(parts)
    return json.dumps(content, sort_keys=True)


def _call_sig(tc: dict) -> list:
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            pass
    return [fn.get("name", ""), args]


def normalize(msg: dict) -> dict:
    """Canonical form used for hashing, so a reply we returned hashes the
    same when the client echoes it back (tool-call ids are ignored)."""
    out = {"role": msg.get("role", ""), "content": content_text(msg.get("content"))}
    tcs = msg.get("tool_calls")
    if tcs:
        out["tool_calls"] = [_call_sig(tc) for tc in tcs if isinstance(tc, dict)]
    return out


def chain_hashes(msgs: list[dict], config_key: str) -> list[str]:
    """hashes[i] identifies the conversation made of the first i messages."""
    h = hashlib.sha256(config_key.encode())
    out = [h.hexdigest()]
    for m in msgs:
        h.update(json.dumps(m, sort_keys=True, separators=(",", ":")).encode())
        out.append(h.hexdigest())
    return out


@dataclass
class Entry:
    session_id: str
    workspace: str | None
    ts: float


class ConversationStore:
    """In-memory map from "conversation so far" to the Claude Code session
    holding it. Lost on restart, which only costs a fresh session."""

    def __init__(self, ttl_seconds: int, max_entries: int):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self._lock = threading.Lock()

    def _evict(self) -> None:
        cutoff = time.time() - self.ttl
        while self._entries:
            oldest = next(iter(self._entries.values()))
            if oldest.ts < cutoff or len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            else:
                break

    def take_longest(self, hashes: list[str], msgs: list[dict]) -> tuple[int, str, Entry] | None:
        """Pops the entry for the longest prefix that ends on an assistant
        message and leaves a non-empty remainder. Popped because resuming
        advances the session, so that prefix no longer describes it."""
        with self._lock:
            self._evict()
            for k in range(len(msgs) - 1, 0, -1):
                if msgs[k - 1]["role"] != "assistant":
                    continue
                entry = self._entries.pop(hashes[k], None)
                if entry is not None:
                    return k, hashes[k], entry
        return None

    def put(self, key: str, entry: Entry) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._evict()

    def __len__(self) -> int:
        return len(self._entries)


def render_messages(msgs: list[dict]) -> str:
    """Text form of messages sent to claude (everything except system)."""
    parts = []
    for m in msgs:
        role = m["role"]
        text = m["content"]
        if role == "system":
            continue
        if role == "assistant":
            if m.get("tool_calls"):
                calls = [{"name": n, "arguments": a} for n, a in m["tool_calls"]]
                text = (text + "\n" if text else "") + json.dumps({"tool_calls": calls})
            parts.append(f"Assistant: {text}")
        elif role == "tool":
            parts.append(f"Tool result: {text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def tools_prompt(tools: list[dict], tool_choice: Any = None) -> str:
    specs = []
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            specs.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
    if tool_choice == "required":
        rule = "Every reply MUST be a tool call; plain-text replies are not accepted. "
    elif isinstance(tool_choice, dict) and (tool_choice.get("function") or {}).get("name"):
        rule = f"Every reply MUST be a call to the tool {tool_choice['function']['name']!r}. "
    else:
        rule = ""
    return (
        f"{rule}You can call the following tools. You cannot run them yourself; the caller runs them and "
        "replies with 'Tool result:' messages.\n"
        f"{json.dumps(specs)}\n\n"
        "To call tools, reply with ONLY one JSON object and no other text, exactly in this shape:\n"
        '{"tool_calls":[{"name":"<tool name>","arguments":{...}}]}\n'
        "Arguments must follow the tool's parameter schema. Several calls may go in one array. "
        "When you need no tool, reply in plain text instead."
    )


_INVOKE = re.compile(r"<invoke\s+name=\"([^\"]+)\"\s*>(.*?)</invoke>", re.DOTALL)
_PARAM = re.compile(r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>", re.DOTALL)


def _coerce(value: str, schema: dict) -> Any:
    kind = schema.get("type")
    v = value.strip()
    try:
        if kind == "integer":
            return int(v)
        if kind == "number":
            return float(v)
        if kind == "boolean":
            return v.lower() == "true"
        if kind in ("array", "object"):
            return json.loads(v)
    except ValueError:
        pass
    return value.strip("\n") if kind == "string" else v


def parse_xml_calls(text: str, schemas: dict[str, dict]) -> list[dict] | None:
    """Models often fall back to <invoke name=..><parameter name=..> pseudo
    calls instead of the JSON protocol; accept them rather than forcing a
    wasted retry round trip."""
    out = []
    for name, body in _INVOKE.findall(text):
        if name not in schemas:
            return None
        props = (schemas[name] or {}).get("properties") or {}
        args = {k: _coerce(v, props.get(k) or {}) for k, v in _PARAM.findall(body)}
        out.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    return out or None


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def tool_schemas(tools: list[dict]) -> dict[str, dict]:
    return {
        t["function"]["name"]: t["function"].get("parameters") or {}
        for t in tools
        if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name")
    }


def parse_tool_calls(text: str, schemas: dict[str, dict]) -> list[dict] | None:
    """Extracts {"tool_calls": [...]} from the model's reply; None means it
    answered in plain text (or produced something invalid)."""
    tool_names = set(schemas)
    candidates = [text.strip()]
    candidates += _FENCE.findall(text)
    idx = text.find('{"tool_calls"')
    if idx != -1:
        candidates.append(text[idx:])
    decoder = json.JSONDecoder()
    for cand in candidates:
        start = cand.find("{")
        if start == -1:
            continue
        try:
            obj, _ = decoder.raw_decode(cand[start:])
        except json.JSONDecodeError:
            continue
        calls = obj.get("tool_calls") if isinstance(obj, dict) else None
        if not isinstance(calls, list) or not calls:
            continue
        out = []
        for c in calls:
            if not isinstance(c, dict) or c.get("name") not in tool_names:
                out = []
                break
            args = c.get("arguments", {})
            if not isinstance(args, dict):
                out = []
                break
            out.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(args)},
            })
        if out:
            return out
    return parse_xml_calls(text, schemas)
