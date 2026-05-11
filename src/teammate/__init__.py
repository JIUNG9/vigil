"""teammate — your team's brain in your team's git repo.

A team-context-sharing tool that treats a private git repository as the
canonical brain (CLAUDE.md, runbooks, ADRs, knowledge files) and lets every
engineer query it locally via a local LLM (Ollama) with sqlite-vec retrieval.
The Teamspace alternative for teams who can't put context in someone else's
cloud.

Architecture:

  - **brain.py** — read-only view over the team's markdown.
  - **rag/** — local LLM + sqlite-vec retrieval, gbrain-compatible.
  - **mcp_server.py** — exposes the brain as MCP resources to Claude Code.
  - **init.py** — `teammate scaffold <dir>` (team-lead one-shot) +
    `teammate init` (per-laptop setup).
  - **cli.py** — the `teammate` command.

Optional layers:

  - **Obsidian** — point it at the cloned repo. Markdown opens natively.
  - **gbrain** — auto-detected; if installed, registered as a source.

Local-first by design. No cloud round-trip. No API keys at install.
"""

__version__ = "0.11.0"
