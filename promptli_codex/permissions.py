"""Permissions for unattended Codex threads: never escalate, decline anything that asks for a human.

Threads start with `ApprovalMode.deny_all` (approval policy `never`): commands run inside Codex's sandbox,
and anything that would need approval fails instead of asking. As a second line of defense, every approval
request the app-server still sends is declined here (the SDK's built-in handler would accept them). The
Promptli tools are approved through their own MCP server entry (`default_tools_approval_mode = "approve"`).
"""

from __future__ import annotations

import logging
from typing import Any

from openai_codex import Sandbox

log = logging.getLogger("promptli.codex")

# The owner chooses one of these in config.json (`extra.sandbox`). Full access is never used for strangers.
SANDBOXES = {"read-only": Sandbox.read_only, "workspace-write": Sandbox.workspace_write}
DEFAULT_SANDBOX = "read-only"


def sandbox_for(name: str | None) -> Sandbox:
    key = (name or DEFAULT_SANDBOX).strip().lower()
    if key not in SANDBOXES:
        raise ValueError(
            f"sandbox must be one of {', '.join(SANDBOXES)} (got {name!r}); "
            "full access is not allowed for sessions that strangers can start"
        )
    return SANDBOXES[key]


def decline(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Answer an app-server request that would otherwise wait for a human at the keyboard."""
    del params
    log.info("declined %s (nobody is at the keyboard to approve it)", method)
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "decline"}
    if method == "mcpServer/elicitation/request":
        return {"action": "decline", "content": None}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    return {}


def harden(codex: Any) -> bool:
    """Swap the SDK's accept-everything approval handler for `decline`. Returns False if the SDK changed."""
    sync = getattr(getattr(codex, "_client", None), "_sync", None)
    if sync is None or not hasattr(sync, "_approval_handler"):
        log.warning("could not install the decline-all approval handler; relying on approval policy 'never'")
        return False
    sync._approval_handler = decline
    return True
