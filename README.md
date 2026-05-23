# Vigil  *(formerly **teammate**)*

> **A self-hosted DevSecOps command center.** Reliability ops, not chat. Six tabs over a git-backed corpus that auto-imports Jira / Confluence / GitHub / Slack. Engineers' Claude Code reads the brain locally; the dashboard is for the team's collective view. Private-VPC, no SaaS, no per-seat fee.

[![CI](https://github.com/JIUNG9/vigil/actions/workflows/ci.yml/badge.svg)](https://github.com/JIUNG9/vigil/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **About the rename**: this project was originally **teammate** (v0.10 → v4.0.0). Versions 1.0.0+ ship as **Vigil**. The GitHub repo redirects from `JIUNG9/teammate` to `JIUNG9/vigil`; legacy CLI names (`teammate`) keep working as aliases through the v1.x line. The story of the rename — and what we cut to get to v5 — is in [docs/series-7/](docs/series-7/).

## What Vigil is

A DevSecOps command center for teams that don't want to put their reliability data in someone else's cloud. Two surfaces:

| Surface | What it does | Who uses it |
|---|---|---|
| **Web dashboard** | 6 tabs: SLA / SLO / SLI / MTTD / MTTR / Extra. Multi-account (placen / shared / nw / dp pattern). Adaptive SigNoz watchlists, P0–P3 auto-classification with Slack ⇄ sync, incident analysis workbench with Slack-thread import. | The whole team, plus on-call |
| **Local sync agent** | A 30-line shell script that clones your team brain repo to `~/.vigil/brain` and `git pull --rebase`s every 60 min. Your existing Claude Code session reads from there directly — no API call needed. | Every engineer on their laptop |

### Three layers, one source of truth

```
            ┌──────────────────────────────────────────┐
            │  Layer 0 — Brain (private git repo)      │
            │                                          │
            │  archive/{jira,confluence,github,slack}/ │
            │  (auto-imported every 3 h)               │
            │  docs/runbooks/ knowledge/ decisions/    │
            │  watchlist/*.yaml (MTTD rules)           │
            └─────┬─────────────────────────────┬──────┘
                  │ git pull (cluster, 5 min)    │ git pull (laptop, 60 min)
                  ▼                              ▼
   ┌──────────────────────────────┐   ┌────────────────────────────┐
   │  Layer 1 — Cluster (in-VPC)  │   │  Layer 2 — Engineer laptop │
   │                              │   │                            │
   │  Qdrant + Ollama             │   │  ~/.vigil/brain (markdown) │
   │  Indexer (single writer)     │   │  Claude Code reads it      │
   │  Dashboard backend           │   │  via ~/.claude/CLAUDE.md   │
   │  war-api (MTTR)              │   │  Optional: vigil-incident  │
   │  Postgres (incident state)   │   │   client-agent (off by     │
   │  SigNoz APM (existing)       │   │   default; opt-in per inc) │
   └──────────────────────────────┘   └────────────────────────────┘
```

Trust flows one way: Layer 2 → 1 → 0. A compromised laptop can poison one war-room timeline but can't tamper with the corpus or the index.

### Why this and not chat-based brains?

We had a chat panel in v4 (then named *teammate*). Engineers ignored it. They already had Claude Code open; context-switching to a browser tab for a *query* didn't add value. v5 cut the chat tab and re-allocated the engineer-facing surface:

- **Collective views** (oncall, severity dashboards, SLO burn) → web dashboard
- **Individual reasoning** (an engineer asking "what runbook covers this") → local Claude Code reading `~/.vigil/brain` directly

The brain repo is the same in both. The surfaces are different.

---

## Initial setup

Two flows. The team lead runs `scaffold` once. Each engineer runs `init` once per laptop.

### Flow A — TEAM LEAD: create the team-brain repo (one-time)

You're setting up the brain for your team. You'll do this once for the org.

**1. Install Vigil.**

```bash
pip install vigil-devsecops
# or, if your team already uses Claude Code's plugin marketplace:
claude plugin install JIUNG9/vigil
```

**2. Scaffold an empty team-brain directory.**

```bash
vigil scaffold ~/team-brain --team-name "<your-team-name>"
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
- Builds the sqlite-vec index in CI and attaches it as a Release artifact when you tag a version. Engineers can `vigil index pull` to skip local re-embedding.

If you tag your first release:

```bash
git tag v0.1.0
git push --tags
```

The workflow runs, builds `team-brain-index.sqlite`, attaches it to the GitHub Release. New hires download it instead of re-embedding locally — saves ~5 minutes on first setup.

**5. Tell your team to clone it.**

That's it on your side. Send the team-brain repo URL to the team. They each follow Flow B.

### Flow B — ENGINEER: set up Vigil locally (per-laptop, one-time)

You're an engineer joining a team that already has a brain. Do this once per laptop.

**1. Install Claude Code if you don't already have it.**

```bash
# Mac/Linux
curl -fsSL https://claude.ai/install.sh | sh
```

**2. Install Vigil.**

```bash
pip install vigil-devsecops
# or via the Claude Code plugin marketplace:
claude plugin install JIUNG9/vigil
```

**3. Clone the team-brain repo.**

```bash
git clone git@github.com:<your-org>/team-brain.git ~/team-brain
cd ~/team-brain
```

**4. Run `vigil init` from inside the brain.**

```bash
vigil init
```

This:

- Confirms `CLAUDE.md` is present (i.e., this is a team-brain repo).
- Detects whether Ollama is running. If not, prints the install hint.
- Detects whether `gbrain` is installed (auto-detected; optional).
- Indexes every markdown file in the brain into `.vigil-cache/vault.sqlite`. ~10 seconds for a typical brain (dozens to hundreds of markdown files).

**5. (Strongly recommended) Install Ollama for the local LLM.**

```bash
# Mac
brew install ollama
ollama serve &
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

Now `vigil ask` works:

```bash
vigil ask "what's our deploy procedure?"
vigil ask "who owns the auth service?"
vigil ask "why did we choose Postgres?"
```

You'll get streamed answers grounded in the team's own markdown, with citations to the source files. Everything happens on your laptop.

**6. (Optional) Set up Obsidian.**

```bash
# Open Obsidian, choose "Open folder as vault", point at ~/team-brain
```

Obsidian's graph view and backlinks work natively because the brain is plain markdown. No Vigil-specific Obsidian plugin required.

**7. (Optional) Wire up the Claude Code MCP server.**

If you want Claude Code to be able to query the brain via MCP (recommended), add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "vigil-brain": {
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
vigil ask "what's the on-call rotation?"
vigil ask "summarize ADRs from this quarter"

# When the team-brain repo has updates from teammates
cd ~/team-brain && git pull
vigil init    # re-runs the index (incremental — only re-embeds changed files)

# When YOU update something
echo "..." >> docs/runbooks/new-procedure.md
git commit -am "runbook: new procedure"
git push
# CI re-builds the index, your vigils get it on their next pull
```

### Event-driven invalidation

The brain is correct on Tuesday. Production changes on Wednesday. The
brain is now wrong, and nobody knows. v0.9 closes the loop with a
brain-invalidations event log: a sibling git repo of structured JSON
events, fed by CloudTrail (or terraform hooks, or `vigil impact emit`),
read by `vigil ask` at query time.

```bash
# Pre-apply hook — block if a recent HIGH event already touched these resources
vigil impact preview \
    --resource aws_vpc.shared \
    --resource aws_iam_role.deploy-bot \
    --severity high

# Post-apply hook — write an event the rest of the team will see
vigil impact emit \
    --resource aws_vpc.shared --action detach --severity high

# Read recent events
vigil impact list --since 24h
```

`vigil ask` prepends a banner when retrieved chunks reference a
recently-invalidated resource. Default: HIGH and above. Tunable via
`[invalidations] show_severity` in `.vigil/config.toml`. See
[`docs/IMPACT.md`](docs/IMPACT.md) for the full thesis, the no-daemon
argument, and the CloudTrail Lambda module shipped under
[`examples/infra/aws-cloudtrail-hook/`](examples/infra/aws-cloudtrail-hook).

### Mid-project adoption

Already have markdown scattered across `docs/`, `wiki/`, `runbooks/`?

```bash
vigil adopt              # dry-run, writes MIGRATION-PLAN.md
vigil adopt --apply      # fills template gaps; refuses dirty git tree
```

See [`docs/ADOPT.md`](docs/ADOPT.md) for discovery rules, plan format, and
the rationale behind the git-cleanliness gate.

### Shape-checking the brain

```bash
vigil validate           # exit 0 PASS, 1 FAIL, 2 WARN
vigil validate --json    # machine-readable for CI
```

Catches missing CLAUDE.md, dangling links, orphan files, binary blobs, and
unparseable frontmatter. See [`docs/VALIDATE.md`](docs/VALIDATE.md).

### Naming convention

Configure your team's repo / service naming via `.vigil-naming.toml`:

```bash
vigil naming init --template nexus-style    # write starter config
vigil naming check acme-infra-core-billing-tfmod
vigil validate --include-naming             # check brain dirs against the rules
```

See [`docs/NAMING.md`](docs/NAMING.md) for the full spec, the structural
pattern, and how to migrate from an unmanaged namespace.

### When something doesn't work

```bash
vigil doctor          # quick diagnostic — reachability, config, models, index, proxy
vigil doctor --json   # same, machine-readable for CI
```

For deployment behind a corporate proxy or with an internal Ollama mirror,
see [`docs/CORPORATE.md`](docs/CORPORATE.md).

### Colleague agent

CI shape-checks the brain on every push. The agent does the judgment
work — orphan triage, weekly digests, PR-time migration plans — that
CI can't.

```bash
vigil agent run weekly_digest --out-dir .vigil-agent
vigil agent run orphan_triage --out-dir .vigil-agent
vigil agent run pr_migration_plan --pr-number 42 --pr-files docs/runbooks/x.md
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
vigil adapter init             # writes ~/.vigil-adapter.toml
vigil adapter show             # see the effective config
vigil adapter validate         # check that path globs still match files
```

MVP scope: path translation (personal globs → canonical brain paths) and
CLAUDE.md section precedence. Skill collisions and vocabulary aliases
land in v0.7. See [`docs/ADAPTER.md`](docs/ADAPTER.md).

### Confidence guards

`vigil ask` won't bluff. Four guards, all configurable in
`.vigil/config.toml`:

- **Score threshold** — below 0.5, we say "I don't know" instead of
  synthesising. Closest match is surfaced so you can decide whether to
  reword or re-index.
- **Citation guard** — every paragraph in the LLM's reply must cite a
  file path in `[brackets]`. Uncited paragraphs are stripped.
- **Audit JSONL** — every retrieval logs to
  `.vigil-cache/audit.jsonl`. Rotates weekly.
- **Per-action floor** — `ask` (0.5), agent routines (0.5–0.65),
  reserved `execute` (0.85). Tunable.

```bash
vigil audit --since 2026-05-01            # read recent retrievals
vigil audit --query-grep deploy           # regex filter
```

See [`docs/CONFIDENCE.md`](docs/CONFIDENCE.md).

### When sources disagree: contradiction detection

When two retrieved chunks contradict each other, `vigil ask` surfaces
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
vigil sync confluence    # pulls Confluence pages → markdown
vigil sync jira          # pulls Jira issues → decision-record drafts
vigil sync slack         # pulls pinned messages from declared channels
vigil sync web           # generic HTTPS → markdown, with domain allowlist
```

Each routine reads `[sync.<name>]` from `.vigil/config.toml` and
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
vigil brain-pulse              # what changed in YOUR scope last 24h
vigil brain-pulse --since 7d   # widen to a week
vigil brain-pulse --json       # machine-readable for scripts
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
Slack workspace ──WebSocket (outbound)──▶ vigil-event-listener
                                                   │
                      ┌────────────────────────────┼─────────────────┐
                      ▼                            ▼                 ▼
               K8s Job: weekly_digest   K8s Job: brain_pulse   K8s Job: jira_sync
```

```bash
# Install with Socket Mode dependencies
pip install 'vigil[listen]'

# Set tokens once (see docs/SOCKET-MODE.md for Slack app setup)
export SLACK_APP_TOKEN="xapp-..."
export SLACK_BOT_TOKEN="xoxb-..."
export TEAMMATE_SLACK_CHANNELS="ops-alerts"

# Start listening (Ctrl-C to stop)
vigil agent listen --no-fail-on-disconnect

# Say "brain pulse" in #ops-alerts → Job created instantly
```

Trigger keywords out of the box: `weekly digest`, `orphan triage`, `confluence sync`,
`jira sync`, `pr draft`, `brain pulse` / `reindex`. Edit `socket_listener.KEYWORD_ROUTES`
to add your own.

**What is — and is not — real-time:**

| Source | Latency | Mechanism |
|---|---|---|
| Slack messages | <1s | Socket Mode WebSocket |
| Brain docs `git push` | ~30-60s | GitHub Actions webhook (separate workflow) |
| Jira issue updates | ~60s | HTTP polling thread |
| Confluence page edits | ~60s | HTTP polling thread |
| Polling fallback | 15 min | `brain_pulse` CronJob |

**Fail-fast on disconnect.** The listener writes `/tmp/vigil-heartbeat` every 30s.
The K8s liveness probe restarts the pod if the heartbeat is more than 90s stale.
After 5 reconnect failures, the process exits — Kubernetes restarts it.

For K8s deployment: `examples/k8s/event-listener/`. For full setup: [`docs/SOCKET-MODE.md`](docs/SOCKET-MODE.md).

### Memory import / export

Personal `~/.claude/` memory accumulates facts the team could use —
service ownership, why we picked X, on-call quirks. Two flows:

```bash
# Active engineer — pull team-relevant facts into a review draft.
# Default for every entry is SKIP — opt-in per entry to import.
vigil memory-import --memory-root ~/.claude

# Departing engineer — dump team-relevant memory as a handover.
vigil memory-export --memory-root ~/.claude --user alice
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
│         │ vigil ask                     │ Claude Code               │
│         │                                  │ (MCP client)              │
│         │                                                              │
│  ┌──────┴───────────────────────────────────────────────────────────┐  │
│  │  CLI                                                             │  │
│  │   vigil scaffold <dir>   — team lead, one-time per org        │  │
│  │   vigil init             — engineer, one-time per laptop      │  │
│  │   vigil ask "<query>"    — local-LLM Q&A with citations       │  │
│  │   vigil index [--rebuild] — refresh the local sqlite-vec      │  │
│  │   vigil stats            — show what's in the brain           │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### Why this architecture

| Choice | Why |
|---|---|
| **sqlite-vec** for the vector store | Single-file, ~1MB extension, polyglot, mature. The index is one `.sqlite` file — git-LFS-friendly, fits a GitHub Release artifact. |
| **Ollama** for the local LLM | Universal in 2026, no API key, runs offline, integrates without custom adapters. Default models: `llama3.2:3b`, `nomic-embed-text`. |
| **Claude Code itself as the agent layer** | We don't bring LangChain or LlamaIndex. Claude Code does the reasoning; vigil just exposes the brain as MCP resources + a search tool. Tiny dependency footprint. |
| **git as the federation layer** | The team already has private git. No new infrastructure. `git log` is the audit trail. `git blame` tells you who wrote what, when. |
| **Markdown as the format** | Obsidian opens it natively. Diff-friendly. Code-review-friendly. Lasts forever. |

### What we explicitly chose NOT to use

- **PGlite + pgvector** — heavier (~3MB + Node runtime), Python integration is weaker than sqlite-vec. Reconsider for v0.3 if the brain ever grows into a structured knowledge graph.
- **LangChain / LlamaIndex** — over-engineered when Claude Code is the agent. Adds ~50MB of deps for no value.
- **Cloud vector DBs** (Pinecone, Weaviate Cloud) — defeats the local-sovereign premise.
- **A vigil cloud service** — there isn't one. There never will be. The team's brain stays on the team's git host.

---

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `TEAMMATE_BRAIN_ROOT` | `cwd` | Override the brain root (useful when running the MCP server from a fixed path). |
| `TEAMMATE_FORCE_INIT` | `0` | Allow `init` to overwrite an existing pre-push hook. |
| `TEAMMATE_OVERRIDE` | `0` | Bypass guardrail hooks for one push (use sparingly). |
| `SLACK_APP_TOKEN` | — | `xapp-…` App-Level Token for Socket Mode. Required for `vigil agent listen`. |
| `SLACK_BOT_TOKEN` | — | `xoxb-…` Bot Token. Required for `vigil agent listen`. |
| `TEAMMATE_SLACK_CHANNELS` | all | Comma-separated channel names to watch. Empty = watch all channels. |
| `TEAMMATE_NAMESPACE` | `vigil-agent` | K8s namespace for Job creation (event-listener Deployment). |
| `ATLASSIAN_API_TOKEN` | — | Enables Jira/Confluence polling in the event listener. |
| `JIRA_BASE_URL` | — | e.g. `https://your-org.atlassian.net` |
| `CONFLUENCE_BASE_URL` | — | e.g. `https://your-org.atlassian.net/wiki` |
| `JIRA_WATCHER_JQL` | `labels = "architecture-decision" AND updated > -2m` | JQL filter for `jira_sync` triggers. |
| `CONFLUENCE_WATCHER_SPACES` | — | Comma-separated Confluence space keys (e.g. `DOCS,ENG`). |

---

## Project structure

```
vigil/
  src/vigil/
    cli.py                   ← `vigil` entry point
    brain.py                 ← read-only Brain over a team-brain repo
    init.py                  ← scaffold + init orchestrators
    mcp_server.py            ← JSON-RPC MCP server
    socket_listener.py       ← Slack Socket Mode WebSocket (vigil agent listen)
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
    init-vigil/SKILL.md   ← `/init-vigil` skill
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
