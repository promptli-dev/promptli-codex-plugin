"""The Codex plugin package: manifest, bundled MCP server, skills and marketplace entry."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(rel: str) -> dict:  # type: ignore[type-arg]
    return json.loads((ROOT / rel).read_text())


def test_manifest_is_an_agent_plugin() -> None:
    m = load("plugin.json")
    assert m["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert re.fullmatch(r"[a-z0-9.-]+", m["name"]) and m["name"] == "promptli-comms"
    ext = m["extensions"]["com.openai"]
    assert (ROOT / ext["onboardingSkill"]).is_file()
    assert len(ext["interface"]["defaultPrompt"]) <= 3
    assert all(len(p) <= 512 for p in ext["interface"]["defaultPrompt"])


def test_versions_match() -> None:
    from promptli_codex import __version__

    assert load("plugin.json")["version"] == __version__
    py = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{__version__}"' in py


def test_mcp_json_uses_the_hosted_oauth_server_with_only_fields_codex_accepts() -> None:
    m = load("mcp.json")
    assert set(m) == {"$schema", "mcpServers"}
    server = m["mcpServers"]["promptli"]
    # Codex's Agent Plugins MCP parser denies unknown fields; OAuth is discovered from the server.
    assert server == {"type": "streamable-http", "url": "https://api.comms.promptli.com/mcp"}


def test_marketplace_points_at_this_plugin() -> None:
    mk = load(".agents/plugins/marketplace.json")
    assert mk["name"] == "promptli"
    (entry,) = mk["plugins"]
    assert entry["name"] == load("plugin.json")["name"]
    assert entry["source"] == {"source": "local", "path": "./"}
    assert entry["policy"]["installation"] in ("AVAILABLE", "INSTALLED_BY_DEFAULT", "NOT_AVAILABLE")
    assert entry["category"]


def test_skills_have_frontmatter_matching_their_folder() -> None:
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} == {"promptli-comms", "promptli-setup", "promptli-status"}
    for p in skills:
        text = p.read_text()
        fm = re.match(r"---\n(.*?)\n---\n", text, re.S)
        assert fm, p
        fields = dict(line.split(": ", 1) for line in fm.group(1).splitlines())
        assert fields["name"] == p.parent.name and len(fields["description"]) > 40
        assert "psk_" not in text and "sk-" not in text.replace("sk-test", "")


def test_installer_is_posix_sh_and_never_takes_a_key() -> None:
    sh = (ROOT / "install.sh").read_text()
    assert sh.startswith("#!/bin/sh")
    assert "API_KEY" not in sh
    assert "codex plugin marketplace add" in sh and "promptli-comms@promptli" in sh
