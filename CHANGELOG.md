# Changelog

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
