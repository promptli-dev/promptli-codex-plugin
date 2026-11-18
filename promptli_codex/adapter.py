"""CodexAdapter: runs gateway turns in OpenAI Codex through the official Python SDK (`codex app-server`).

- One `codex app-server` process (via `openai_codex.AsyncCodex`) hosts one Codex *thread* per contact_key,
  so every person gets an isolated, persistent conversation. Thread ids are returned to the gateway, which
  persists them; after a restart or an idle close the next turn resumes the same thread.
- Inbound content is passed as an `ExternalMessage` (tool-level authority, below the owner's developer
  instructions), never as user input.
- Text is streamed from `item/agentMessage/delta` notifications, so live phone calls can start speaking
  before Codex finishes; `interrupt()` stops a running turn (barge-in).
- The gateway's tools are served in-process on a loopback MCP server (see toolserver.py) and wired into
  Codex as the `promptli` MCP server. Threads use approval policy `never` and a read-only sandbox by
  default (permissions.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, CodexError, ExternalMessage, Sandbox
from promptli_gateway.adapter import (
    AdapterCapabilities,
    GatewayContext,
    TextDelta,
    ToolActivity,
    TurnChunk,
    TurnDone,
    TurnError,
    TurnKind,
)
from promptli_gateway.config import home_dir
from promptli_gateway.tools import ToolSpec

from promptli_codex.permissions import harden, sandbox_for
from promptli_codex.prompts import developer_instructions, turn_preamble
from promptli_codex.toolserver import TOKEN_ENV, LocalToolServer

log = logging.getLogger("promptli.codex")

RUNTIME = "promptli-codex"
SERVER_NAME = "promptli"
PLUGIN_ID = "promptli-comms@promptli"  # our Codex plugin; its hosted MCP server is replaced in-process here
EXTERNAL_TOOL = "promptli_comms"


class CodexClient(Protocol):
    """The subset of openai_codex.AsyncCodex we use (a fake implements it in tests)."""

    async def __aenter__(self) -> Any: ...
    async def close(self) -> None: ...
    async def thread_start(self, **kw: Any) -> Any: ...
    async def thread_resume(self, thread_id: str, **kw: Any) -> Any: ...


ClientFactory = Callable[[CodexConfig], CodexClient]


def _default_factory(config: CodexConfig) -> CodexClient:
    client: Any = AsyncCodex(config=config)
    return client


def toml_value(v: Any) -> str:
    """A `-c key=value` value: JSON strings/bools/numbers/arrays are valid TOML."""
    return json.dumps(v)


def config_overrides(tool_server: dict[str, Any]) -> tuple[str, ...]:
    out = [f"mcp_servers.{SERVER_NAME}.{k}={toml_value(v)}" for k, v in tool_server.items()]
    # Our plugin also declares a `promptli` server (the hosted one, for interactive use). In background
    # threads the in-process server replaces it, so sends are seen by the gateway and not answered twice.
    # The plugin's skills stay enabled.
    out.append(f"plugins.{PLUGIN_ID}.mcp_servers.{SERVER_NAME}.enabled=false")
    return tuple(out)


def _item(payload: Any) -> Any:
    item = getattr(payload, "item", None)
    return getattr(item, "root", item)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return getattr(obj, name, default)


def _enum(v: Any) -> str:
    return str(getattr(v, "value", v) or "")


@dataclass
class _Session:
    key: str
    thread: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    turn: Any = None
    model: str | None = None

    @property
    def thread_id(self) -> str:
        return str(self.thread.id)


class CodexAdapter:
    name = "codex"
    capabilities = AdapterCapabilities(
        streams_text=True, native_tools=True, resumable_sessions=True, interruptible=True
    )

    def __init__(
        self,
        *,
        model: str | None = None,
        voice_model: str | None = None,
        sandbox: str | None = None,
        codex_bin: str | None = None,
        client_factory: ClientFactory | None = None,
        idle_close_s: float = 900,
        max_sessions: int = 32,
    ) -> None:
        self.model = model
        self.voice_model = voice_model  # a faster model for live calls; None = same as `model`
        self.sandbox: Sandbox = sandbox_for(sandbox)
        self.codex_bin = codex_bin  # None = the Codex runtime pinned by the openai-codex SDK
        self.factory = client_factory or _default_factory
        self.idle_close_s = idle_close_s
        self.max_sessions = max_sessions
        self.ctx: GatewayContext | None = None
        self.tools: list[ToolSpec] = []
        self.tool_server: LocalToolServer | None = None
        self.codex: CodexClient | None = None
        self._sessions: dict[str, _Session] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task[None] | None = None

    # ---- lifecycle ----------------------------------------------------------------------------
    async def start(self, ctx: GatewayContext, tools: list[ToolSpec]) -> None:
        self.ctx = ctx
        self.tools = tools
        self.tool_server = LocalToolServer(tools)
        await self.tool_server.start()
        self.codex = self.factory(self.codex_config())
        await self.codex.__aenter__()
        harden(self.codex)
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle())

    def codex_config(self) -> CodexConfig:
        assert self.tool_server is not None, "start() first"
        return CodexConfig(
            codex_bin=self.codex_bin,
            config_overrides=config_overrides(self.tool_server.codex_config()),
            cwd=self.workdir(),
            env={TOKEN_ENV: self.tool_server.token},
            client_name="promptli_codex",
            client_title="Promptli Comms for Codex",
        )

    async def close(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        self._sessions.clear()
        if self.codex is not None:
            with contextlib.suppress(Exception):
                await self.codex.close()
            self.codex = None
        if self.tool_server is not None:
            await self.tool_server.close()
            self.tool_server = None

    def workdir(self) -> str:
        if self.ctx and self.ctx.project_dir:
            return self.ctx.project_dir
        d = home_dir(RUNTIME) / "workspace"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def thread_options(self, session_key: str) -> dict[str, Any]:
        assert self.ctx is not None, "start() first"
        return {
            "approval_mode": ApprovalMode.deny_all,
            "sandbox": self.sandbox,
            "cwd": self.workdir(),
            "developer_instructions": developer_instructions(self.ctx, session_key),
            "model": self.model,
        }

    # ---- sessions -----------------------------------------------------------------------------
    async def _open(self, session_key: str, resume_id: str | None) -> _Session:
        assert self.codex is not None, "start() first"
        opts = self.thread_options(session_key)
        if resume_id:
            thread = await self.codex.thread_resume(resume_id, **opts)
        else:
            thread = await self.codex.thread_start(**opts)
        s = _Session(session_key, thread, model=self.model)
        self._sessions[session_key] = s
        self._evict_over_cap()
        return s

    async def _session(self, session_key: str, resume_id: str | None) -> _Session:
        s = self._sessions.get(session_key)
        if s is not None:
            return s
        lock = self._opening.setdefault(session_key, asyncio.Lock())
        async with lock:
            s = self._sessions.get(session_key)
            if s is not None:
                return s
            try:
                return await self._open(session_key, resume_id)
            except Exception:
                if not resume_id:
                    raise
                log.warning("could not resume thread %s for %s; starting fresh", resume_id, session_key)
                return await self._open(session_key, None)

    async def warm(self, session_key: str, resume_id: str | None) -> None:
        """A call is ringing: load this person's Codex thread now (no cold-start greeting)."""
        try:
            await self._session(session_key, resume_id)
        except Exception:
            log.warning("could not pre-load the Codex thread for %s", session_key, exc_info=True)

    def _evict_over_cap(self) -> None:
        idle = sorted((s for s in self._sessions.values() if not s.busy), key=lambda s: s.last_used)
        while len(self._sessions) > self.max_sessions and idle:
            self._sessions.pop(idle.pop(0).key, None)

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self.idle_close_s))
            now = time.monotonic()
            for s in list(self._sessions.values()):
                if not s.busy and now - s.last_used > self.idle_close_s:
                    self._sessions.pop(s.key, None)

    async def interrupt(self, session_key: str) -> None:
        s = self._sessions.get(session_key)
        if s is not None and s.busy and s.turn is not None:
            with contextlib.suppress(Exception):
                await s.turn.interrupt()

    async def reset_session(self, session_key: str) -> None:
        self._sessions.pop(session_key, None)

    def session_count(self) -> int:
        return len(self._sessions)

    # ---- turns --------------------------------------------------------------------------------
    async def run_turn(
        self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None
    ) -> AsyncIterator[TurnChunk]:
        try:
            s = await self._session(session_key, resume_id)
        except Exception as e:
            yield TurnError(f"could not start Codex: {e}", retryable=True)
            return
        async with s.lock:
            s.busy = True
            try:
                fast = kind in (TurnKind.VOICE, TurnKind.SUPERVISOR) and self.voice_model
                async for chunk in self._stream(s, turn_preamble(kind) + prompt, self._model_for(s, fast)):
                    yield chunk
            finally:
                s.busy = False
                s.turn = None
                s.last_used = time.monotonic()

    def _model_for(self, s: _Session, fast: Any) -> str | None:
        """The model override for this turn (None = keep the thread's current model)."""
        want = self.voice_model if fast else self.model
        if want is None or want == s.model:
            return None
        s.model = want
        return want

    async def _start_turn(self, s: _Session, prompt: str, model: str | None) -> Any:
        kw: dict[str, Any] = {"model": model} if model else {}
        message = ExternalMessage(tool_name=EXTERNAL_TOOL, namespace="promptli", content=prompt)
        try:
            return await s.thread.turn(message, **kw)
        except CodexError as e:  # a Codex runtime too old for external messages: plain input still works
            if "external" not in str(e).lower():
                raise
            log.warning("Codex runtime does not support external messages; sending plain input")
            return await s.thread.turn(prompt, **kw)

    async def _stream(self, s: _Session, prompt: str, model: str | None) -> AsyncIterator[TurnChunk]:
        messages: list[tuple[str, str]] = []  # (phase, text) of completed agent messages
        streamed: set[str] = set()
        current: str | None = None
        error: str | None = None
        try:
            s.turn = await self._start_turn(s, prompt, model)
            async for ev in s.turn.stream():
                method: str = getattr(ev, "method", "")
                payload = getattr(ev, "payload", None)
                if method == "item/agentMessage/delta":
                    item_id = str(_field(payload, "item_id", "") or "")
                    if item_id != current and (streamed or messages):
                        yield TextDelta("\n\n")
                    current = item_id
                    streamed.add(item_id)
                    yield TextDelta(str(_field(payload, "delta", "")))
                elif method == "item/started":
                    item = _item(payload)
                    kind = _field(item, "type")
                    if kind == "mcpToolCall":
                        yield ToolActivity(f"{_field(item, 'server')}.{_field(item, 'tool')}", "started")
                    elif kind in ("commandExecution", "fileChange", "webSearch"):
                        yield ToolActivity(str(kind), "started")
                elif method == "item/completed":
                    item = _item(payload)
                    if _field(item, "type") != "agentMessage":
                        continue
                    text = str(_field(item, "text", "") or "")
                    item_id = str(_field(item, "id", "") or "")
                    if text and item_id not in streamed:  # never streamed: send it whole
                        if streamed or messages:
                            yield TextDelta("\n\n")
                        yield TextDelta(text)
                        current = item_id
                    messages.append((_enum(_field(item, "phase")), text))
                elif method == "error":
                    err = _field(payload, "error")
                    if not _field(payload, "will_retry", False):
                        error = str(_field(err, "message", "") or "codex error")
                elif method == "turn/completed":
                    turn = _field(payload, "turn")
                    status = _enum(_field(turn, "status"))
                    if status == "failed":
                        detail = _field(_field(turn, "error"), "message") or error or "turn failed"
                        yield TurnError(f"codex: {detail}", retryable=False)
                        return
                    yield TurnDone(text=final_text(messages), session_id=s.thread_id)
                    return
        except Exception as e:
            log.exception("codex turn failed for %s", s.key)
            self._sessions.pop(s.key, None)
            yield TurnError(f"codex: {e}", retryable=True)
            return
        yield TurnError(f"codex: {error or 'the turn ended without completing'}", retryable=True)


def final_text(messages: list[tuple[str, str]]) -> str:
    """The final answer: the `final_answer` message, else the last phase-less one, else the last one."""
    for phase, text in reversed(messages):
        if phase == "final_answer":
            return text.strip()
    for phase, text in reversed(messages):
        if not phase:
            return text.strip()
    return messages[-1][1].strip() if messages else ""
