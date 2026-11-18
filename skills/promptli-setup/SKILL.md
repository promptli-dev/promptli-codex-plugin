---
name: promptli-setup
description: Set up Promptli Comms for Codex - log in to the hosted Promptli tools and optionally install the promptli-codex background service so people can email, text or call this agent. Use when the user installs the Promptli plugin or asks to set up Promptli, an agent email address or phone number.
---

# Set up Promptli Comms

1. Tools for this session: if the `promptli` MCP tools are missing or say "Not logged in", ask the user to
   run `codex mcp login promptli` in their own terminal. A browser opens; they pick or create the agent.
   Nothing is pasted into chat.
2. Check whether the background service is installed: run `promptli-codex version`. If it is missing and
   the user wants people to be able to email, text or call the agent, tell them to run this in their own
   terminal and stop:
   `curl -fsSL https://comms.promptli.com/install/codex.sh | sh`
3. Run `promptli-codex status --json`. If it is already configured, show the agent, address, number and
   service state, and offer `promptli-codex doctor`.
4. If it is not configured, ask the user to run `promptli-codex setup` in their own terminal. It shows a
   security warning they must accept, then opens their browser to log in and pick or create the agent
   (`--number` also offers a phone number, `--test` uses test mode). Never ask for an API key or a login
   code in chat and never put one on a command line.
5. Afterwards run `promptli-codex doctor` and explain any failing check with its exact fix.
