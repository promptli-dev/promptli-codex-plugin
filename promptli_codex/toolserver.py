"""Serve the gateway's in-process ToolSpecs to Codex over a loopback Streamable HTTP MCP server.

Codex runs as a separate `codex app-server` process, so it cannot call Python functions directly. The
gateway's tools must stay in this process: sends record "already replied" marks (so the gateway does not
auto-reply twice) and call tools talk to the live call. So we serve them on 127.0.0.1 at a random port,
guarded by a random bearer token that only the Codex child process receives (via an environment variable,
never argv or config files).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
import socket
from typing import Any

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from promptli_gateway.mcp_stdio import build_server
from promptli_gateway.tools import ToolSpec

log = logging.getLogger("promptli.codex.tools")

TOKEN_ENV = "PROMPTLI_CODEX_MCP_TOKEN"  # noqa: S105 (name of the env var Codex reads the bearer token from)
PATH = "/mcp"

Scope = dict[str, Any]


class LocalToolServer:
    """A loopback-only MCP server for one set of ToolSpecs. `url` and `token` are valid after start()."""

    def __init__(self, tools: list[ToolSpec], *, host: str = "127.0.0.1") -> None:
        self.tools = tools
        self.host = host
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._manager: StreamableHTTPSessionManager | None = None
        self._run: contextlib.AbstractAsyncContextManager[None] | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{PATH}"

    def authorized(self, headers: list[tuple[bytes, bytes]]) -> bool:
        want = f"Bearer {self.token}".encode()
        got = next((v for k, v in headers if k.lower() == b"authorization"), b"")
        return hmac.compare_digest(got, want)

    async def asgi(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        if scope.get("path", "").rstrip("/") != PATH:
            await _plain(send, 404, b"not found")
            return
        if not self.authorized(scope.get("headers") or []):
            await _plain(send, 401, b"unauthorized")
            return
        assert self._manager is not None
        await self._manager.handle_request(scope, receive, send)

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, 0))
        self.port = int(sock.getsockname()[1])
        hosts = [f"{self.host}:{self.port}", self.host, f"localhost:{self.port}"]
        self._manager = StreamableHTTPSessionManager(
            app=build_server(self.tools),
            stateless=True,
            json_response=True,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[]
            ),
        )
        self._run = self._manager.run()
        await self._run.__aenter__()
        config = uvicorn.Config(
            self.asgi, interface="asgi3", log_level="warning", lifespan="off", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(200):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.01)
        if not self._server.started:
            raise RuntimeError("the local Promptli tool server did not start")
        log.debug("promptli tools on %s", self.url)

    async def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)
        if self._run is not None:
            with contextlib.suppress(Exception):
                await self._run.__aexit__(None, None, None)
        self._server = self._task = self._run = None

    def codex_config(self, server_name: str = "promptli") -> dict[str, Any]:
        """The `mcp_servers.<name>` entry Codex needs (the token itself travels in TOKEN_ENV)."""
        return {
            "url": self.url,
            "bearer_token_env_var": TOKEN_ENV,
            "default_tools_approval_mode": "approve",  # our own tools never wait for a human
            "required": True,
        }


async def _plain(send: Any, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
