# The vmlab CLI is vendored into each project as a zipapp

Skills live in agent-specific directories (`~/.claude/skills`, `~/.codex/skills`, …), but a **Regression suite** must run from cron or CI with no agent installed. So the skill copies a single-file zipapp to `.vmlab/vmlab.pyz` in the project, pinned by version, and `vmlab self-update` refreshes it from whichever skill copy is present.

This keeps saved **Scenarios** always matched to the API version sitting next to them and needs only `python3` offline. The cost is one committed binary-ish file per project and explicit updates.

## Considered options

- **Global install via pipx/uv** — one copy per machine, but CI needs network and a package index entry, and projects silently drift to a newer API.
- **Scripts reference the skill directory** — breaks on machines without the agent, and the path differs per agent.
