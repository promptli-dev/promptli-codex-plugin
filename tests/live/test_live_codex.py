"""Live: the real `codex app-server` (needs `codex login`), with Promptli tools served locally.

Nothing is sent to anyone: the only tool is a local fake. Run with `uv run pytest -m live tests/live`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from promptli_gateway.adapter import GatewayContext, TextDelta, ToolActivity, TurnDone, TurnKind
from promptli_gateway.tools import ToolSpec, obj

from promptli_codex.adapter import CodexAdapter
from promptli_codex.cli import codex_cli

pytestmark = pytest.mark.live


def _logged_in() -> bool:
    cli = codex_cli()
    if cli is None:
        return False
    r = subprocess.run([cli, "login", "status"], capture_output=True, text=True, check=False)  # noqa: S603
    return r.returncode == 0


@pytest.fixture(autouse=True)
def _need_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not _logged_in():
        pytest.skip("Codex is not logged in (run `codex login`)")
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path))


async def test_live_turn_streams_resumes_and_calls_a_promptli_tool(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    async def lookup_contact(a: dict[str, Any]) -> dict[str, Any]:
        calls.append(a)
        return {"ok": True, "contact": {"name": "Jane Example", "memories": ["likes tea"]}}

    tools = [
        ToolSpec(
            "lookup_contact",
            "Find a saved contact by email.",
            obj({"email": {"type": "string"}}),
            lookup_contact,
        )
    ]
    ctx = GatewayContext("live-test", "live@example.com", None, str(tmp_path))
    a = CodexAdapter()
    await a.start(ctx, tools)
    try:
        prompt = "Use lookup_contact for jane@example.com, then answer with only her name."
        chunks = [c async for c in a.run_turn("ctc_jane", prompt, kind=TurnKind.CHANNEL, resume_id=None)]
        done = chunks[-1]
        assert isinstance(done, TurnDone) and "Jane" in done.text, chunks
        assert any(isinstance(c, TextDelta) for c in chunks)
        assert any(isinstance(c, ToolActivity) for c in chunks) and calls
        thread_id = done.session_id
        await a.reset_session("ctc_jane")
        chunks = [
            c
            async for c in a.run_turn(
                "ctc_jane", "What name did you just give?", kind=TurnKind.CHANNEL, resume_id=thread_id
            )
        ]
        assert isinstance(chunks[-1], TurnDone) and chunks[-1].session_id == thread_id
        assert "Jane" in chunks[-1].text
    finally:
        await a.close()
