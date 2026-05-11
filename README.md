# teammate

> **Your team's brain in your team's git repo.** Local-LLM-queryable, Obsidian-friendly, git-federated. The Teamspace alternative for teams who can't put context in someone else's cloud.

[![CI](https://github.com/JIUNG9/teammate/actions/workflows/ci.yml/badge.svg)](https://github.com/JIUNG9/teammate/actions/workflows/ci.yml)
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
claude plugin install JIUNG9/teammate
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
claude plugin install JIUNG9/teammate
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

### Event-driven invalidation

The brain is correct on Tuesday. Production changes on Wednesday. The
brain is now wrong, and nobody knows. v0.9 closes the loop with a
brain-invalidations event log: a sibling git repo of structured JSON
events, fed by CloudTrail (or terraform hooks, or `teammate impact emit`),
read by `teammate ask` at query time.

```bash
# Pre-apply hook — block if a recent HIGH event already touched these resources
teammate impact preview \
    --resource aws_vpc.shared \
    --resource aws_iam_role.deploy-bot \
    --severity high

# Post-apply hook — write an event the rest of the team will see
teammate impact emit \
    --resource aws_vpc.shared --action detach --severity high

# Read recent events
teammate impact list --since 24h
```

`teammate ask` prepends a banner when retrieved chunks reference a
recently-invalidated resource. Default: HIGH and above. Tunable via
`[invalidations] show_severity` in `.teammate/config.toml`. See
[`docs/IMPACT.md`](docs/IMPACT.md) for the full thesis, the no-daemon
argument, and the CloudTrail Lambda module shipped under
[`examples/infra/aws-cloudtrail-hook/`](examples/infra/aws-cloudtrail-hook).

### Mid-project adoption

Already have markdown scattered across `docs/`, `wiki/`, `runbooks/`?

```bash
teammate adopt              # dry-run, writes MIGRATION-PLAN.md
teammate adopt --apply      # fills template gaps; refuses dirty git tree
```

See [`docs/ADOPT.md`](docs/ADOPT.md) for discovery rules, plan format, and
the rationale behind the git-cleanliness gate.

### Shape-checking the brain

```bash
teammate validate           # exit 0 PASS, 1 FAIL, 2 WARN
teammate validate --json    # machine-readable for CI
```

Catches missing CLAUDE.md, dangling links, orphan files, binary blobs, and
unparseable frontmatter. See [`docs/VALIDATE.md`](docs/VALIDATE.md).

### Naming convention

Configure your team's repo / service naming via `.teammate-naming.toml`:

```bash
teammate naming init --template nexus-style    # write starter config
teammate naming check acme-infra-core-billing-tfmod
teammate validate --include-naming             # check brain dirs against the rules
```

See [`docs/NAMING.md`](docs/NAMING.md) for the full spec, the structural
pattern, and how to migrate from an unmanaged namespace.

### When something doesn't work

```bash
teammate doctor          # quick diagnostic — reachability, config, models, index, proxy
teammate doctor --json   # same, machine-readable for CI
```

For deployment behind a corporate proxy or with an internal Ollama mirror,
see [`docs/CORPORATE.md`](docs/CORPORATE.md).

### Colleague agent

CI shape-checks the brain on every push. The agent does the judgment
work — orphan triage, weekly digests, PR-time migration plans — that
CI can't.

```bash
teammate agent run weekly_digest --out-dir .teammate-agent
teammate agent run orphan_triage --out-dir .teammate-agent
teammate agent run pr_migration_plan --pr-number 42 --pr-files docs/runbooks/x.md
```

Routines stage markdown reports; a `/schedule` runner (Anthropic cloud
or self-hosted) is what posts them to Slack / opens issues / drops PR
comments. The agent itself never mutates the brain. See
[`docs/AGENT.md`](docs/AGENT.md).

### Personal-vs-team layout: the adapter

Your personal markdown lives somewhere idiosyncratic — `~/notes/runbooks/`,
`~/wiki/`, whatever. The team brain has a fixed canonical shape. The
adapter is the per-engineer translation layer:

```bash
teammate adapter init             # writes ~/.teammate-adapter.toml
teammate adapter show             # see the effective config
teammate adapter validate         # check that path globs still match files
```

MVP scope: path translation (personal globs → canonical brain paths) and
CLAUDE.md section precedence. Skill collisions and vocabulary aliases
land in v0.7. See [`docs/ADAPTER.md`](docs/ADAPTER.md).

### Confidence guards

`teammate ask` won't bluff. Four guards, all configurable in
`.teammate/config.toml`:

- **Score threshold** — below 0.5, we say "I don't know" instead of
  synthesising. Closest match is surfaced so you can decide whether to
  reword or re-index.
- **Citation guard** — every paragraph in the LLM's reply must cite a
  file path in `[brackets]`. Uncited paragraphs are stripped.
- **Audit JSONL** — every retrieval logs to
  `.teammate-cache/audit.jsonl`. Rotates weekly.
- **Per-action floor** — `ask` (0.5), agent routines (0.5–0.65),
  reserved `execute` (0.85). Tunable.

```bash
teammate audit --since 2026-05-01            # read recent retrievals
teammate audit --query-grep deploy           # regex filter
```

See [`docs/CONFIDENCE.md`](docs/CONFIDENCE.md).

### When sources disagree: contradiction detection

When two retrieved chunks contradict each other, `teammate ask` surfaces
the conflict instead of blending them into a half-truth:

```
**Two sources disagree on this:**
- `[runbooks/auth-pg.md]` says: "Auth runs on PostgreSQL 13."
- `[runbooks/db-policy.md]` says: "All services migrated to PostgreSQL 16."
Resolve manually before acting.
```

Phase 1 (heuristic) runs by default; Phase 2 (LLM judge) is opt-in via
`[contradiction] use_llm_judge`. See
[`docs/CONTRADICTION.md`](docs/CONTRADICTION.md).

### MCP integrations — Confluence, Jira, Slack, Web

Sources of truth that aren't git stay where they are. Teammate syncs
them into the brain on a slow loop, with PR review:

```bash
teammate sync confluence    # pulls Confluence pages → markdown
teammate sync jira          # pulls Jira issues → decision-record drafts
teammate sync slack         # pulls pinned messages from declared channels
teammate sync web           # generic HTTPS → markdown, with domain allowlist
```

Each routine reads `[sync.<name>]` from `.teammate/config.toml` and
stages markdown drafts under `pending-imports/<routine>-<date>/`.
The agent never auto-merges; a human turns drafts into real
`docs/runbooks/...` content via a normal PR. `web_pull` is
default-deny — an empty `allowlist_domains` refuses every URL. See
[`docs/MCP-INTEGRATIONS.md`](docs/MCP-INTEGRATIONS.md).

### Phase B — Ollama on EKS (opt-in)

Phase A (every engineer runs Ollama on their laptop) is the OSS
default. Once a team grows past five-ish engineers and laptop drift
becomes painful, the team-shared deployment in
`examples/infra/aws-eks-ollama/` graduates the inference plane onto
EKS:

- Terraform module for the durable primitives (namespace, PVC, SA)
- ArgoCD Application + raw k8s manifests for the workload
- Init Job pre-pulls `llama3.2:3b` + `nomic-embed-text` on first sync

See [`docs/PHASE-B-OLLAMA.md`](docs/PHASE-B-OLLAMA.md) for when to
graduate, the architecture rationale, and a step-by-step deployment
guide.

### Scale automation

The team-brain product wins at 3 AM by saying "I don't know" — and
v0.10 closes the loop by telling the right engineer when there's now
something to know. Four agent routines (`invalidation_digest`,
`targeted_radar`, `pr_review_assist`, `auto_pr_drafter`) scale with
**events**, not **engineers**: workload stays flat as the team grows
to 100+. None of them is a daemon — each is a finite cron job that
exits. None of them auto-mutates the brain — every PR is staged for
human review.

Plus a new morning-ritual CLI:

```bash
teammate brain-pulse              # what changed in YOUR scope last 24h
teammate brain-pulse --since 7d   # widen to a week
teammate brain-pulse --json       # machine-readable for scripts
```

`brain-pulse` aggregates targeted invalidations, brain page changes,
and pending PR-staged drafts into one screen. See
[`docs/SCALE-AUTOMATION.md`](docs/SCALE-AUTOMATION.md) for the full
architecture, the k8s-controller analogy, and the trust split between
the agent and the runner.

### Real-time event listener (Slack Socket Mode)

v0.11 adds a persistent WebSocket that replaces polling for Slack events.
The listener runs as a single-replica Deployment and triggers K8s Jobs
**in real time** — no public URL, no ALB, no ingress rule needed.

```
Slack workspace ──WebSocket (outbound)──▶ teammate-event-listener
                                                   │
                      ┌────────────────────────────┼─────────────────┐
                      ▼                            ▼                 ▼
               K8s Job: weekly_digest   K8s Job: brain_pulse   K8s Job: jira_sync
```

```bash
# Install with Socket Mode dependencies
pip install 'claude-teammate[listen]'

# Set tokens once (see docs/SOCKET-MODE.md for Slack app setup)
export SLACK_APP_TOKEN="xapp-..."
export SLACK_BOT_TOKEN="xoxb-..."
export TEAMMATE_SLACK_CHANNELS="ops-alerts"

# Start listening (Ctrl-C to stop)
teammate agent listen --no-fail-on-disconnect

# Say "brain pulse" in #ops-alerts → Job created instantly
```

Trigger keywords out of the box: `weekly digest`, `orphan triage`, `confluence sync`,
`jira sync`, `pr draft`, `brain pulse` / `reindex`. Edit `socket_listener.KEYWORD_ROUTES`
to add your own.

For K8s deployment: `examples/k8s/event-listener/`. For full setup: [`docs/SOCKET-MODE.md`](docs/SOCKET-MODE.md).

### Memory import / export

Personal `~/.claude/` memory accumulates facts the team could use —
service ownership, why we picked X, on-call quirks. Two flows:

```bash
# Active engineer — pull team-relevant facts into a review draft.
# Default for every entry is SKIP — opt-in per entry to import.
teammate memory-import --memory-root ~/.claude

# Departing engineer — dump team-relevant memory as a handover.
teammate memory-export --memory-root ~/.claude --user alice
```

Both commands are read-only on `~/.claude/`. The import flow has a
**reversed safety bias**: every checkbox in the draft starts unchecked,
even when the heuristic is confident an entry is team-relevant. See
[`docs/MEMORY-IMPORT.md`](docs/MEMORY-IMPORT.md) and
[`docs/MEMORY-EXPORT.md`](docs/MEMORY-EXPORT.md).

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
| `SLACK_APP_TOKEN` | — | `xapp-…` App-Level Token for Socket Mode. Required for `teammate agent listen`. |
| `SLACK_BOT_TOKEN` | — | `xoxb-…` Bot Token. Required for `teammate agent listen`. |
| `TEAMMATE_SLACK_CHANNELS` | all | Comma-separated channel names to watch. Empty = watch all channels. |
| `TEAMMATE_NAMESPACE` | `teammate-agent` | K8s namespace for Job creation (event-listener Deployment). |
| `ATLASSIAN_API_TOKEN` | — | Enables Jira/Confluence polling in the event listener. |
| `JIRA_BASE_URL` | — | e.g. `https://your-org.atlassian.net` |
| `CONFLUENCE_BASE_URL` | — | e.g. `https://your-org.atlassian.net/wiki` |
| `JIRA_WATCHER_JQL` | `labels = "architecture-decision" AND updated > -2m` | JQL filter for `jira_sync` triggers. |
| `CONFLUENCE_WATCHER_SPACES` | — | Comma-separated Confluence space keys (e.g. `DOCS,ENG`). |

---

## Project structure

```
teammate/
  src/teammate/
    cli.py                   ← `teammate` entry point
    brain.py                 ← read-only Brain over a team-brain repo
    init.py                  ← scaffold + init orchestrators
    mcp_server.py            ← JSON-RPC MCP server
    socket_listener.py       ← Slack Socket Mode WebSocket (teammate agent listen)
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
