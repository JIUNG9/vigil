# Changelog

## [0.11.0] — 2026-05-11

### Added — Slack Socket Mode event listener (real-time)

- `teammate agent listen` — persistent Slack Socket Mode WebSocket. Triggers K8s Jobs
  for teammate routines in real time (<1s latency). No public URL needed — outbound only.
- `src/teammate/socket_listener.py` — full Socket Mode implementation:
  - Heartbeat thread (writes `/tmp/teammate-heartbeat` every 30s; liveness probe target)
  - Jira/Confluence polling thread (configurable interval, fires `jira_sync`/`confluence_sync`)
  - K8s Job creation from CronJob template with concurrency guard
  - Fail-fast: exponential backoff, exits non-zero after 5 reconnect failures
- `listen` optional extras: `slack-sdk>=3.27`, `kubernetes>=29.0`, `httpx>=0.27`
- `docs/SOCKET-MODE.md` — full setup: Slack app config, local testing, K8s Deployment
- `examples/k8s/event-listener/deployment.yaml` — production Deployment with probes
- `teammate config init` now emits a `[listener]` section with Socket Mode defaults

### Keyword routes (edit `socket_listener.KEYWORD_ROUTES` to customize)
| Message | Routine |
|---|---|
| `weekly digest` | `weekly_digest` |
| `orphan triage` / `orphan` | `orphan_triage` |
| `confluence sync` | `confluence_sync` |
| `jira sync` | `jira_sync` |
| `pr draft` | `auto_pr_drafter` |
| `brain pulse` / `reindex` | `brain_pulse` |

```bash
pip install 'claude-teammate[listen]'
```

## [0.10.0] — 2026-05-09

### Added — scale automation (the agent-as-k8s-controller pattern)
- Four new agent routines that scale to 100+ colleagues without daemons:
  - `invalidation_digest` — daily per-engineer email targeted by git activity
  - `targeted_radar` — for HIGH-severity events, identifies 3-5 most-relevant engineers via git-history scoring
  - `pr_review_assist` — auto-comments on PRs touching brain-referenced resources
  - `auto_pr_drafter` — drafts auto-PRs for HIGH-severity events; never auto-merges
- `teammate brain-pulse` — engineer's morning ritual CLI: targeted invalidations, brain changes, pending PR drafts, recommended actions. `--json` for scripting.
- `docs/SCALE-AUTOMATION.md` — full architecture + the k8s-controller analogy.
- 36+ new tests; total now 480+.

### The architectural property of v0.10
- Total routines: 11 (3 from v0.5 + 4 from v0.8 sync + 4 from v0.10 scale).
- Each is a finite cron job — exits when done. NO daemon.
- Workload scales linearly with #events, NOT with #engineers.
- Agent never auto-mutates the brain; PRs staged for human review.

### What this completes
v0.10 closes the loop:
- v0.3-v0.7: substrate, scoring, naming convention, adapter MVP — the foundation.
- v0.8: MCP integrations (Confluence/Jira/Slack/web) + Phase B Ollama — sources + hosting.
- v0.9: event-driven invalidation — the freshness layer.
- v0.10: scale automation — making the freshness layer reach the right engineer at the right time, without daemons.

The team-brain product that wins at 3 AM is the one willing to say "I don't know" — and v0.10 ships the smart agent that ensures the brain knows what it doesn't know, and tells the right person.

## [0.9.0] — 2026-05-09

### Added
- **Event-driven invalidation** — the missing freshness layer for brain-vs-infra drift:
  - `teammate impact preview` — pre-terraform hook; finds brain pages that reference touched resources, blocks if recent HIGH-severity events exist.
  - `teammate impact emit` — post-terraform hook; writes a structured invalidation event to the brain-invalidations repo.
  - `teammate impact list` — read recent events as a table.
- `src/teammate/invalidations.py` — runtime integration: `teammate ask` now prepends a banner when retrieved chunks reference recently invalidated resources. Default: only HIGH severity surfaces (configurable).
- `examples/infra/aws-cloudtrail-hook/` — Lambda + EventBridge module that catches CloudTrail events (VPC/IAM/RDS/SG mutations) and commits invalidation events to the brain-invalidations repo. Includes the Lambda Python source and tests.
- `templates/team-brain-skeleton/hooks/pre-push` — optional client-side gate. Installed via `teammate init --install-pre-push`.
- `[invalidations]` config section.
- `docs/IMPACT.md` — full thesis + integration guide.
- 49+ new tests; total now 435+.

### Notes
- The invalidations repo is the SINGLE SOURCE OF TRUTH for events. Slack / email / PagerDuty notifications are derived artifacts triggered by GitHub Actions on the repo, not the source of truth.
- `teammate ask` lazy-fetches the invalidations repo at command time (not a daemon). Cached 60s within a session.
- The agent never auto-mutates the brain in response to invalidation events. If the team wants auto-PR-drafting for HIGH severity, that's v0.10's `auto_pr_drafter` routine.
- Resource extraction is regex-based heuristic — covers the common AWS resource patterns. Custom patterns can be added via `[invalidations.extra_patterns]` (future v0.10).
- The CloudTrail Terraform variable was named `event_filter_arn` in the spec but the values are CloudTrail event NAMES, not ARNs — renamed to `event_name_patterns` in this release.
- The bundled pre-push hook lives at `templates/team-brain-skeleton/hooks/pre-push`. `teammate init --install-pre-push` copies it into `.git/hooks/`. Storing it in the source tree under `.git/` would have been clobbered by `git init`.

## [0.8.0] — 2026-05-09

### Added
- **MCP integrations** — 4 new agent routines:
  - `confluence_sync` — pulls Confluence pages, stages PR drafts
  - `jira_sync` — pulls Jira issues by JQL, stages decision-record drafts
  - `slack_sync` — pulls pinned messages from declared channels
  - `web_pull` — generic HTTP→markdown with domain allowlist
- `teammate sync confluence|jira|slack|web` — CLI invocation for the routines
- **Phase B Ollama infrastructure** — `examples/infra/aws-eks-ollama/`:
  - Terraform module (Namespace + PVC + ServiceAccount)
  - ArgoCD Application manifest
  - Raw k8s manifests for `kubectl apply` path (Deployment + Service + HPA + init Job)
  - Step-by-step deployment README
- `docs/MCP-INTEGRATIONS.md`, `docs/PHASE-B-OLLAMA.md`
- `examples/sync-routines.json` — sample `/schedule` runner config for the four routines
- 50+ new tests; total now 370+

### Notes
- All sync routines are PR-staging only. Agent never auto-merges. Humans review.
- `web_pull` enforces a domain allowlist by default; refuses URLs outside the list. Empty allowlist refuses everything (default-deny).
- Phase B Ollama is opt-in — Phase A (laptop Ollama) remains the OSS default. The infra is shipped as examples for teams who want centralized hosting.
- Atlassian MCP / Slack MCP integration uses your existing MCP server config; teammate doesn't ship its own MCP servers.
- `httpx` is lazy-imported in the sync routines so OSS users on the core install (`pip install claude-teammate`) can still load the agent runner without the `[rag]` extra.

## [0.7.0] — 2026-05-08

### Added
- **Configurable naming convention** — `.teammate-naming.toml` declares per-team vocabularies for prefix, category, domain, service, submodule, type. Pattern: `{prefix}-{category}-{domain}-{service}[-{submodule}]-{type}` with kebab-case charset, type-token-last, no-duplicate-tokens, length cap.
- `teammate naming check <name>` — validate one name (or stdin batch with `-`). Exit 0 / 1 / 2.
- `teammate naming list` — print effective convention.
- `teammate naming init [--template ...] [--force]` — write a starter config. Templates: `nexus-style`, `small-team`, `monorepo-only`, `strict-iac`.
- `teammate validate --include-naming` — integrates naming check into the existing shape report. Off by default; opt-in via flag or `[validate] include_naming = true` in main config.
- Korean locale for validator messages — `[locale] language = "ko"` in the naming TOML. Faithful translations of the validator's failure-reason strings.
- `examples/naming/{nexus-style,small-team,monorepo-only,strict-iac}.toml` — 4 starter configs.
- `templates/team-brain-skeleton/.teammate-naming.toml` — bundled minimal default.
- `docs/NAMING.md` — full spec, philosophy, migration guidance.
- 30+ new tests; total now 300+.

### Notes
- The shipped convention is pattern-only — every vocabulary token is team-defined. The OSS repo ships no proprietary prefixes, domain codes, or service names.
- Length-over-max is WARN, not FAIL — matches reference validator semantics.
- Naming check is OFF by default in `validate` to avoid surprising existing v0.6 users; opt in per-team.

## [0.6.0] — 2026-05-08

### Added
- **Adapter pattern (MVP)** — `.teammate-adapter.toml` per laptop; maps personal paths → canonical brain paths; handles CLAUDE.md section precedence when personal + team CLAUDE.md both exist. Skill collisions / vocabulary aliases deferred to v0.7 (the design needs real adopter patterns first).
- `teammate adapter show / init / validate` — CLI for the adapter config.
- **Contradiction detector** — top-k pair check (heuristic Phase 1 free; LLM Phase 2 only when flagged). Conflicts surface in `teammate ask` output as "two sources disagree on this:" prefix instead of synthesizing a half-truth.
- **Four confidence guards** (the v0.4 promise, now realized):
  - Score threshold — refuse synthesis below 0.5; respond with "I don't know" + closest match.
  - Citation guard — every claim cites a file path in [brackets]; uncited claims stripped.
  - Audit JSONL — `.teammate-cache/audit.jsonl`; one line per retrieval; weekly rotation.
  - Per-action confidence floor — different floors for ask / weekly_digest / orphan_triage / pr_migration_plan.
- `teammate audit` — read recent retrievals; `--query-grep` filter.
- `docs/ADAPTER.md`, `docs/CONTRADICTION.md`, `docs/CONFIDENCE.md`.
- `examples/adapter-personal-overlay.toml`, `examples/audit-log-sample.jsonl`.
- 78 new tests; total now 268 passing.

### Notes
- Adapter MVP is path translation + CLAUDE.md section precedence ONLY. Per advisor: design without real adopters is a strawman; v0.7 expands once we have 2-3 real teams' patterns.
- Contradiction Phase 2 (LLM) is opt-in via `[contradiction] use_llm_judge` to keep cost predictable. Phase 1 (heuristic) runs by default and catches the obvious cases.
- Score threshold is meaningful only in embedding mode. Keyword-fallback scores are unbounded and density-normalised; the gate is disabled in that path and the audit line records `retrieval_mode: "keyword"`. Documented in `docs/CONFIDENCE.md`.
- Audit log rotation is lazy — rename happens on the first append in a new ISO week. No daemon. A 3-week-quiet brain still gets exactly one rotation when it wakes up.
- Citation guard buffers per-paragraph. Short answers without a closing `\n\n` are flushed at end-of-stream with the same check, so single-paragraph replies don't vanish silently.
- Confidence guards realize the "At 3 AM, 'I don't know' is the most important output your AI can give you" thesis from the launch article. The team-brain product that wins is the one willing to say "I don't know."

## [0.5.0] — 2026-05-07

### Added
- **Colleague agent** — `src/teammate/agent/` package with 3 routines:
  - `weekly_digest` — runs `validate` + `doctor`, generates Slack-ready report.
  - `orphan_triage` — classifies orphan markdown files (keep / move / archive).
  - `pr_migration_plan` — `adopt --dry-run` against a PR diff for posting as a PR comment.
- `teammate agent run <name>` — local invocation; primarily called by `/schedule` runners.
- `teammate memory-import` — harvest team-relevant facts from `~/.claude/` memory into a review draft. **REVERSED safety bias**: every entry defaults to SKIP; opt-in to import. Read-only on `~/.claude/`.
- `teammate memory-export` — departing-engineer flow; dumps team-relevant memory as a handover artifact.
- `docs/AGENT.md`, `docs/MEMORY-IMPORT.md`, `docs/MEMORY-EXPORT.md`.
- `templates/team-brain-skeleton/.gitignore` — excludes `pending-imports/` and `.teammate-agent/` by default.
- `examples/agent-routines.json`, `examples/memory-import-draft.md`, `examples/handover-template.md`.
- 64 new tests; total now 188 passing.

### Notes
- Agent NEVER auto-mutates the brain. Routines stage drafts; the runner (Anthropic-cloud `/schedule` or self-hosted) opens issues / posts to Slack with scoped tokens.
- Memory-import never modifies `~/.claude/`. Redaction pre-pass flags emails / internal hostnames / employer-name patterns; user confirms per entry. The `[ ] IMPORT THIS` box stays unchecked even when the heuristic flags an entry as obviously team-relevant — opt-in is the only path.
- `memory-import` discovers Claude Code's nested layout: when `<root>/MEMORY.md` is absent, it digs into `<root>/projects/<id>/memory/MEMORY.md`. Multi-project users should pass `--memory-root` explicitly to pick the right one.
- `memory-import --interactive` is reserved for v0.6 (no per-entry CLI prompts in v0.5). The default `--non-interactive` flow — write a draft, edit by hand, commit — is the only path that ships. The safety property is identical either way: every checkbox starts unchecked.

## [0.4.0] — 2026-05-05

### Added
- `teammate adopt` — mid-project file migration. Walk an existing project, classify markdown files (KEEP / MOVE_SUGGESTED / REVIEW / ADD / SKIP_PER_ENGINEER), generate `MIGRATION-PLAN.md`. `--dry-run` default, `--apply` explicit.
- `teammate validate` — read-only shape checker. CLAUDE.md presence + size, link resolution, orphan files, non-canonical paths, binary files in brain, frontmatter parse. `--json` for CI use. Exit 0/1/2 on PASS/FAIL/WARN.
- `templates/team-brain-skeleton/.github/workflows/brain-ci.yml` — extended with `validate` on push, `adopt --dry-run` as PR comment, weekly artifact rebuild.
- `docs/ADOPT.md`, `docs/VALIDATE.md`.

### Notes
- `adopt --apply` refuses to run on a brain with uncommitted changes — commit or stash first. The brain's git history is the audit trail; CI must never auto-mutate it. Dry-run is unaffected and useful for previewing on dirty trees.
- `--apply` only adds template gap files; never moves existing content. Move suggestions are surfaced in the plan for human action.
- `brain-ci.yml` deliberately does NOT `curl | sh` Ollama in the artifact-build job. The CI Release artifact is a keyword-only index (engineers re-embed locally on `teammate init`).
- 64 new tests; total now 124 passing.

## [0.3.1] — 2026-05-04

### Added
- `teammate doctor` — diagnostic CLI: config source, LLM/embedding reachability with latency, model availability, index status (with version-stamp validation), proxy/CA env detection. `--json` flag for scripting / CI.
- `examples/configs/corporate-ollama.toml` — internal-mirror config example with proxy + custom-CA hints.
- `docs/CORPORATE.md` — corporate-environment deployment guide: proxy, CA bundles, air-gapped install, troubleshooting.
- `README.md`: pointer to `teammate doctor` and `docs/CORPORATE.md` for corporate adopters.

### Notes
- Patch release. No breaking changes from v0.3.0. Backward-compat shim in `rag/ollama` still works with `DeprecationWarning`.

## [0.3.0] — 2026-05-04

### Added
- Provider abstraction (`teammate.providers`) — `LLMProvider` and `EmbeddingProvider` ABCs.
- Config system: `.teammate/config.toml` (per-repo) → `~/.teammate/config.toml` (per-user) → env-var overrides.
- `teammate config show` and `teammate config init` CLI subcommands.
- Index versioning — `(provider, embedding_model, dim)` stamped at index time; mismatch is a hard error with a `--rebuild` hint.
- Auto-detection of available providers in `teammate init`.
- Example configs in `examples/configs/`.

### Changed
- `rag.ask.answer()` and `rag.index.index_paths()` now take provider objects (`LLMProvider`, `EmbeddingProvider`) instead of `OllamaClient`.
- `rag.ollama` is now a deprecation shim; import from `teammate.providers` instead.

### Backward-compat
- With no config present and Ollama running, behavior is identical to v0.2.
- `from teammate.rag.ollama import OllamaClient` still works (with `DeprecationWarning`).

### Roadmap (v0.4)
- Anthropic Claude API provider.
- OpenAI / Azure OpenAI provider.
- HTTP-generic provider (e.g. internal LLM gateways at corporate-VPN-only deployments).
