# teammate

> **Your team's brain in your team's git repo.** Local-LLM-queryable, Obsidian-friendly, git-federated. The Teamspace alternative for teams who can't put context in someone else's cloud.

[![CI](https://github.com/placen-org/teammate/actions/workflows/ci.yml/badge.svg)](https://github.com/placen-org/teammate/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What this is

A Claude Code plugin that turns a private git repository into your team's queryable brain.

- **The brain lives in git.** A normal private repo. `CLAUDE.md` at the root, `.claude/skills/`, `.claude/rules/`, `docs/`, `knowledge/`. Plain markdown — no proprietary format.
- **Each engineer has a local index.** `teammate init` indexes every markdown file into a sqlite-vec database on their laptop. ~10 seconds per brain.
- **Queries are local.** `teammate ask "what's our deploy procedure?"` streams an answer from a local LLM (Ollama) with citations to the markdown files it pulled facts from. No cloud round-trip.
- **Obsidian works out of the box.** Point Obsidian at the cloned repo and the team's brain becomes a beautiful linked notebook. Nothing to configure.
- **Sharing is just git.** When someone updates a runbook, they `git push`. When you want the latest brain, you `git pull`. The CI pipeline pre-builds the index as a release artifact for fast onboarding.

```
            ┌──────────────────────────────────────────┐
            │  Team's PRIVATE git repository           │
            │  (the source of truth)                   │
            │                                          │
            │  CLAUDE.md                               │
            │  .claude/skills/   .claude/rules/        │
            │  docs/   knowledge/                      │
            │  .github/workflows/brain-ci.yml          │
            └──────────────────────┬───────────────────┘
                                   │ git clone / pull
                                   ▼
            ┌──────────────────────────────────────────┐
            │  Each engineer's laptop                  │
            │  (the derived state)                     │
            │                                          │
            │  Ollama  +  sqlite-vec index             │
            │  Claude Code  +  teammate plugin         │
            │  Obsidian (optional)                     │
            │  gbrain (optional, auto-detected)        │
            └──────────────────────────────────────────┘
```

---

## Initial setup

Two flows. The team lead runs `scaffold` once. Each engineer runs `init` once per laptop.

### Flow A — TEAM LEAD: create the team-brain repo (one-time)

You're setting up the brain for your team. You'll do this once for the org.

**1. Install teammate.**

```bash
pip install claude-teammate
# or, if your team already uses Claude Code's plugin marketplace:
claude plugin install placen-org/teammate
```

**2. Scaffold an empty team-brain directory.**

```bash
teammate scaffold ~/team-brain --team-name "<your-team-name>"
cd ~/team-brain
```

You now have a templated repo with:

```
team-brain/
├── CLAUDE.md                              ← global team rules
├── .claude/
│   ├── skills/example-skill/SKILL.md      ← seed skill (replace)
│   └── rules/{commit,test}.md             ← split rules
├── docs/
│   ├── architecture/                      ← decision records
│   ├── runbooks/README.md                 ← on-call playbooks
│   └── onboarding/README.md               ← new-hire walkthrough
├── knowledge/
│   ├── people.md                          ← who owns what
│   ├── services.md                        ← what runs where
│   └── decisions/0001-use-team-brain.md   ← seed ADR
└── .github/workflows/brain-ci.yml         ← markdown lint + index build
```

The `TEAM-NAME` placeholder in every seed file is substituted with the value you passed to `--team-name`.

**3. Make it a git repo and push.**

```bash
git init -b main
git add -A
git commit -m "init: team-brain for <your-team>"
git remote add origin git@github.com:<your-org>/team-brain.git    # private repo
git push -u origin main
```

Done. The brain is now live. Anyone on the team can clone it.

**4. (Optional) Wire up the GitHub Actions pipeline.**

The bundled `.github/workflows/brain-ci.yml` does three things on every push to `main`:

- Lints all markdown files (markdownlint).
- Verifies internal markdown links resolve.
- Builds the sqlite-vec index in CI and attaches it as a Release artifact when you tag a version. Engineers can `teammate index pull` to skip local re-embedding.

If you tag your first release:

```bash
git tag v0.1.0
git push --tags
```

The workflow runs, builds `team-brain-index.sqlite`, attaches it to the GitHub Release. New hires download it instead of re-embedding locally — saves ~5 minutes on first setup.

**5. Tell your team to clone it.**

That's it on your side. Send the team-brain repo URL to the team. They each follow Flow B.

### Flow B — ENGINEER: set up teammate locally (per-laptop, one-time)

You're an engineer joining a team that already has a brain. Do this once per laptop.

**1. Install Claude Code if you don't already have it.**

```bash
# Mac/Linux
curl -fsSL https://claude.ai/install.sh | sh
```

**2. Install teammate.**

```bash
pip install claude-teammate
# or via the Claude Code plugin marketplace:
claude plugin install placen-org/teammate
```

**3. Clone the team-brain repo.**

```bash
git clone git@github.com:<your-org>/team-brain.git ~/team-brain
cd ~/team-brain
```

**4. Run `teammate init` from inside the brain.**

```bash
teammate init
```

This:

- Confirms `CLAUDE.md` is present (i.e., this is a team-brain repo).
- Detects whether Ollama is running. If not, prints the install hint.
- Detects whether `gbrain` is installed (auto-detected; optional).
- Indexes every markdown file in the brain into `.teammate-cache/vault.sqlite`. ~10 seconds for a typical brain (dozens to hundreds of markdown files).

**5. (Strongly recommended) Install Ollama for the local LLM.**

```bash
# Mac
brew install ollama
ollama serve &
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

Now `teammate ask` works:

```bash
teammate ask "what's our deploy procedure?"
teammate ask "who owns the auth service?"
teammate ask "why did we choose Postgres?"
```

You'll get streamed answers grounded in the team's own markdown, with citations to the source files. Everything happens on your laptop.

**6. (Optional) Set up Obsidian.**

```bash
# Open Obsidian, choose "Open folder as vault", point at ~/team-brain
```

Obsidian's graph view and backlinks work natively because the brain is plain markdown. No teammate-specific Obsidian plugin required.

**7. (Optional) Wire up the Claude Code MCP server.**

If you want Claude Code to be able to query the brain via MCP (recommended), add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "teammate-brain": {
      "command": "python",
      "args": ["-m", "teammate.mcp_server"],
      "env": {
        "TEAMMATE_BRAIN_ROOT": "/Users/<you>/team-brain"
      }
    }
  }
}
```

Now Claude Code can read `brain://CLAUDE.md`, `brain://skills/<name>`, `brain://docs/<path>`, etc., as MCP resources, and call the `brain.search(query, k)` tool when it needs to retrieve context.

---

## Daily use

```bash
# Whenever you're unsure
teammate ask "what's the on-call rotation?"
teammate ask "summarize ADRs from this quarter"

# When the team-brain repo has updates from teammates
cd ~/team-brain && git pull
teammate init    # re-runs the index (incremental — only re-embeds changed files)

# When YOU update something
echo "..." >> docs/runbooks/new-procedure.md
git commit -am "runbook: new procedure"
git push
# CI re-builds the index, your teammates get it on their next pull
```

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                          TEAMMATE PLUGIN                               │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Brain (read-only)                                               │  │
│  │   - iter_markdown(): yields every .md with frontmatter + body    │  │
│  │   - section(name): claude/skills/rules/docs/knowledge/other      │  │
│  │   - stats(): counts per section                                  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│         ▲                                  ▲                           │
│         │ reads                            │ reads                     │
│         │                                  │                           │
│  ┌──────┴──────────────┐         ┌─────────┴────────────────────┐     │
│  │  RAG layer          │         │  MCP server                  │     │
│  │   - Ollama HTTP     │         │   - resources/list, read     │     │
│  │   - sqlite-vec      │         │   - tools/call brain.search  │     │
│  │   - embeddings or   │         │   - stdio JSON-RPC           │     │
│  │     keyword fallback│         │                              │     │
│  └─────────────────────┘         └──────────────────────────────┘     │
│         ▲                                  ▲                           │
│         │ teammate ask                     │ Claude Code               │
│         │                                  │ (MCP client)              │
│         │                                                              │
│  ┌──────┴───────────────────────────────────────────────────────────┐  │
│  │  CLI                                                             │  │
│  │   teammate scaffold <dir>   — team lead, one-time per org        │  │
│  │   teammate init             — engineer, one-time per laptop      │  │
│  │   teammate ask "<query>"    — local-LLM Q&A with citations       │  │
│  │   teammate index [--rebuild] — refresh the local sqlite-vec      │  │
│  │   teammate stats            — show what's in the brain           │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### Why this architecture

| Choice | Why |
|---|---|
| **sqlite-vec** for the vector store | Single-file, ~1MB extension, polyglot, mature. The index is one `.sqlite` file — git-LFS-friendly, fits a GitHub Release artifact. |
| **Ollama** for the local LLM | Universal in 2026, no API key, runs offline, integrates without custom adapters. Default models: `llama3.2:3b`, `nomic-embed-text`. |
| **Claude Code itself as the agent layer** | We don't bring LangChain or LlamaIndex. Claude Code does the reasoning; teammate just exposes the brain as MCP resources + a search tool. Tiny dependency footprint. |
| **git as the federation layer** | The team already has private git. No new infrastructure. `git log` is the audit trail. `git blame` tells you who wrote what, when. |
| **Markdown as the format** | Obsidian opens it natively. Diff-friendly. Code-review-friendly. Lasts forever. |

### What we explicitly chose NOT to use

- **PGlite + pgvector** — heavier (~3MB + Node runtime), Python integration is weaker than sqlite-vec. Reconsider for v0.3 if the brain ever grows into a structured knowledge graph.
- **LangChain / LlamaIndex** — over-engineered when Claude Code is the agent. Adds ~50MB of deps for no value.
- **Cloud vector DBs** (Pinecone, Weaviate Cloud) — defeats the local-sovereign premise.
- **A teammate cloud service** — there isn't one. There never will be. The team's brain stays on the team's git host.

---

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `TEAMMATE_BRAIN_ROOT` | `cwd` | Override the brain root (useful when running the MCP server from a fixed path). |
| `TEAMMATE_FORCE_INIT` | `0` | Allow `init` to overwrite an existing pre-push hook. |
| `TEAMMATE_OVERRIDE` | `0` | Bypass guardrail hooks for one push (use sparingly). |

---

## Project structure

```
teammate/
  src/teammate/
    cli.py                   ← `teammate` entry point
    brain.py                 ← read-only Brain over a team-brain repo
    init.py                  ← scaffold + init orchestrators
    mcp_server.py            ← JSON-RPC MCP server
    rag/
      ollama.py              ← Ollama HTTP client
      index.py               ← sqlite-vec indexer
      ask.py                 ← retrieve + LLM stream
      gbrain.py              ← gbrain compatibility shim
  templates/team-brain-skeleton/   ← bundled team-brain template
  hooks/
    pre-push                 ← raw bash, optional
    pre-tool-use-guardrail.sh ← Claude Code PreToolUse hook, optional
  skills/
    init-teammate/SKILL.md   ← `/init-teammate` skill
    ask-vault/SKILL.md       ← `/ask-vault` skill
  .claude-plugin/plugin.json
  .github/workflows/
    ci.yml
    oss-hygiene.yml
  tests/                     ← 24 passing tests
  docs/
    OSS_HYGIENE.md
    QUICKSTART.md
  README.md
  pyproject.toml
  LICENSE
```

---

## Contributing

This is OSS for any team to adopt. The OSS hygiene workflow blocks any commit that hardcodes employer names, personal emails, or non-placeholder AWS account IDs in source code. See `docs/OSS_HYGIENE.md` for the policy.

Issues and PRs welcome.

## License

MIT — see [LICENSE](LICENSE).
