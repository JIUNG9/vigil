# `vigil agent` — colleague-agent routines

The `agent` subcommand runs *judgment* work on the team brain. CI runs
`vigil validate` on every push: deterministic, read-only, blocks the
merge on shape regressions. The agent does the next thing — the messy
human work that sits between "the brain is structurally sound" and "the
brain is actually useful this week."

The agent is not the CI. It runs separately, on a schedule, and never
mutates the brain.

## The split

| | `vigil validate` (CI) | `vigil agent` (this) |
|---|---|---|
| When | every push | scheduled (weekly / nightly / on-PR) |
| Output | exit code + JSON | markdown reports under `out_dir/` |
| Determinism | bit-identical for same input | judgment heuristics (over time, may use an LLM) |
| Side effects | none | none in v0.5; the *runner* takes side effects |
| Trust surface | read-only on the brain | read-only on the brain; no tokens |

The runner — Anthropic-cloud `/schedule`, GitHub Actions cron, or a
self-hosted scheduler — picks routine names from a config like
[`examples/agent-routines.json`](../examples/agent-routines.json), runs
them, and distributes the resulting markdown files (Slack post, GitHub
issue, PR comment). Scoped tokens belong to the runner, never to the
agent itself.

## Routines

### `weekly_digest`

Calls `vigil validate --json` and `vigil doctor --json`,
aggregates a week of `git log`, counts files, flags an oversize
CLAUDE.md. Output: `weekly-digest-YYYY-MM-DD.md` with a
`<!-- POST TO SLACK START -->` / `<!-- POST TO SLACK END -->` chunk
the runner extracts for Slack.

```bash
vigil agent run weekly_digest --out-dir .vigil-agent
```

When it fires: weekly. The runner posts the Slack chunk to a team
channel.

### `orphan_triage`

Re-uses `validate`'s orphan-finder. For each orphan markdown file,
applies heuristics on filename, size, age, and outbound-link count
to propose `keep` / `move` / `archive`. Stages one GitHub-issue body
per orphan inside the report. Output: `orphan-triage-YYYY-MM-DD.md`.

```bash
vigil agent run orphan_triage --out-dir .vigil-agent
```

When it fires: nightly. The runner opens (or updates) one issue per
proposed action. The agent itself never opens issues; it only stages
the proposed body.

### `pr_migration_plan`

Runs `vigil adopt --dry-run` against the brain, then filters the
plan to entries whose path is in the PR's changed-files list. Output:
`pr-migration-plan-PR<N>.md`, ready to drop into a PR comment.

```bash
vigil agent run pr_migration_plan \
  --out-dir .vigil-agent \
  --pr-number 42 \
  --pr-files docs/runbooks/payments.md \
  --pr-files wiki/old-stuff.md
```

When it fires: on every PR open / synchronize. The runner posts the
file as a PR comment via `gh pr comment`.

## Trust surface

The agent has:

  * **Read-only** access to the brain filesystem.
  * **Write** access to its own `out_dir` (default `<brain>/.vigil-agent/`).

The agent does NOT have:

  * Any token.
  * Network access for distribution.
  * Permission to mutate the brain (no `git commit`, no `git push`).

The runner has:

  * The agent's report files.
  * A scoped Slack token, GitHub issue token, and PR-comment token.
  * Nothing else.

This split is the "agent never auto-mutates the brain" rule. Routines
stage drafts; the runner does the side effects with explicit, scoped
tokens. If the agent's heuristics are wrong, the worst case is a
useless Slack post or a closed-as-wontfix issue — never a corrupted
brain.

## Where the agent runs

Three places work:

1. **Anthropic-cloud `/schedule`** — recommended. The runner is
   managed; you point it at the routines config and forget it.
2. **GitHub Actions cron** — `actions/checkout` the brain, `pip install
   vigil`, run `vigil agent run <name>`, post artifacts.
3. **Local cron / launchd** — for paranoid teams. Same command, no
   cloud round-trip. Pair with `gh pr comment` / `slack-cli` for
   distribution.

See [`examples/agent-routines.json`](../examples/agent-routines.json)
for the schema each runner consumes.

## Why the agent never auto-mutates the brain

The brain's git history *is* the audit trail. If the agent fixes
something that was actually fine, you lose the trail of "we used to
believe this." If the agent gets it wrong on a sensitive page (an
on-call rotation, an ADR), the team loses trust in the agent and
starts ignoring its drafts.

Staging drafts and letting humans accept them keeps the agent useful
across the cases where it's right (most of the time) and the cases
where it's wrong (rare, but enough to matter). When teams are ready
for auto-apply, they can build the runner-side glue themselves —
the contract is "read this markdown file, take this action."
