---
name: promptli-status
description: Show the Promptli agent's address, number and whether the promptli-codex service is running, and diagnose problems. Use when the user asks about Promptli status, why the agent is not answering, or to run the Promptli doctor.
---

# Promptli status and doctor

- Status: run `promptli-codex status` and summarise it in two or three lines.
- Diagnose: run `promptli-codex doctor`. For every failing check, explain what it means and the exact fix.
  If everything passes, say so in one line.
- Logs: `promptli-codex logs -n 100` shows recent service logs. Never paste credentials from them.

If `promptli-codex` is not installed, say so and point to the `promptli-setup` skill.
