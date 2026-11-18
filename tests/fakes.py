"""A scripted stand-in for openai_codex.AsyncCodex (the app-server never runs in unit tests)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
from openai_codex import CodexConfig, ExternalMessage

from promptli_codex.toolserver import TOKEN_ENV


def ev(method: str, **payload: Any) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=SimpleNamespace(**payload))


def agent_item(item_id: str, text: str, phase: str | None) -> SimpleNamespace:
    return SimpleNamespace(root=SimpleNamespace(type="agentMessage", id=item_id, text=text, phase=phase))


def completed(status: str, message: str | None = None) -> SimpleNamespace:
    err = SimpleNamespace(message=message) if message else None
    return ev("turn/completed", turn=SimpleNamespace(id="turn", status=status, error=err))


class FakeTurn:
    def __init__(self, thread: FakeThread, text: str) -> None:
        self.thread = thread
        self.text = text
        self.interrupted = False

    async def interrupt(self) -> None:
        self.interrupted = True

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool the way Codex does: JSON-RPC to the local MCP server with the bearer token."""
        cfg = self.thread.codex.config
        server = next(o for o in cfg.config_overrides if o.startswith("mcp_servers.promptli.url="))
        url = server.split("=", 1)[1].strip('"')
        token = (cfg.env or {})[TOKEN_ENV]
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-06-18",
        }
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json=body, headers=headers)
        return r.json()

    async def stream(self) -> AsyncIterator[Any]:
        n = len(self.thread.inputs)
        if "FAIL" in self.text:
            yield ev("error", error=SimpleNamespace(message="model overloaded"), will_retry=False)
            yield completed("failed", "model overloaded")
            return
        if "CRASH" in self.text:
            raise RuntimeError("app-server went away")
        if "USE_TOOL" in self.text:
            yield ev(
                "item/started",
                item=SimpleNamespace(
                    root=SimpleNamespace(type="mcpToolCall", server="promptli", tool="reply_email")
                ),
            )
            self.thread.tool_results.append(
                await self.call_tool("reply_email", {"message_id": "msg_1", "text": "hi"})
            )
        yield ev("item/agentMessage/delta", item_id="c1", delta="Checking. ")
        yield ev("item/completed", item=agent_item("c1", "Checking. ", "commentary"))
        if "SLOW" in self.text:
            await asyncio.sleep(0.05)
        reply = f"reply #{n}"
        for word in reply.split(" "):
            yield ev("item/agentMessage/delta", item_id="f1", delta=word + " ")
        yield ev("item/completed", item=agent_item("f1", reply, "final_answer"))
        yield completed("interrupted" if self.interrupted else "completed")


class FakeThread:
    def __init__(self, codex: FakeCodex, thread_id: str, opts: dict[str, Any]) -> None:
        self.codex = codex
        self.id = thread_id
        self.opts = opts
        self.inputs: list[Any] = []
        self.turn_kw: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []

    async def turn(self, input: Any, **kw: Any) -> FakeTurn:
        self.inputs.append(input)
        self.turn_kw.append(kw)
        text = input.content if isinstance(input, ExternalMessage) else str(input)
        return FakeTurn(self, str(text))


class FakeCodex:
    instances: ClassVar[list[FakeCodex]] = []
    fail_resume: ClassVar[bool] = False

    def __init__(self, config: CodexConfig) -> None:
        self.config = config
        self.threads: list[FakeThread] = []
        self.resumed: list[str] = []
        self.entered = False
        self.closed = False
        self._client = SimpleNamespace(
            _sync=SimpleNamespace(_approval_handler=lambda m, p: {"decision": "accept"})
        )
        FakeCodex.instances.append(self)

    async def __aenter__(self) -> FakeCodex:
        self.entered = True
        return self

    async def close(self) -> None:
        self.closed = True

    async def thread_start(self, **kw: Any) -> FakeThread:
        t = FakeThread(self, f"thr-{len(self.threads)}", kw)
        self.threads.append(t)
        return t

    async def thread_resume(self, thread_id: str, **kw: Any) -> FakeThread:
        if FakeCodex.fail_resume:
            raise RuntimeError(f"no rollout found for thread id {thread_id}")
        self.resumed.append(thread_id)
        t = FakeThread(self, thread_id, kw)
        self.threads.append(t)
        return t
