# MCP integrations — Confluence, Jira, Slack, Web

> Status: shipped in v0.8. Four sync routines + the `vigil sync`
> CLI. The agent never auto-merges; humans review every staged file.

## The thesis

Most teams already have sources of truth that are not git: Confluence
runbooks, Jira decisions, pinned Slack messages, AWS / Kubernetes
documentation pages. Teammate's job is **not** to relocate those. The
team-brain repo is the *derived* canonical layer; the original sources
keep being authoritative for their own domain.

The sync routines exist to bridge that gap **on a slow loop, with PR
review**. We do not bidirectionally sync. We do not auto-merge. We
stage markdown drafts under `pending-imports/<routine>-<date>/`, and
a human turns those drafts into real `docs/runbooks/...` or
`docs/decisions/...` content via a normal pull request.

This is the same trust model the v0.5 colleague-agent shipped:
**routines stage drafts, the runner stages PRs, humans review.** The
agent itself holds no credentials. The runner — Anthropic-cloud
`/schedule`, a self-hosted GitHub Actions cron, or a vigil engineer
running `vigil sync` from the CLI — is the only thing with scoped
tokens to Atlassian / Slack / web sources.

## What ships

| Routine | Source | Output path | Dedup key |
| --- | --- | --- | --- |
| `confluence_sync` | Confluence pages (URL list or MCP-resolved) | `pending-imports/confluence-<date>/confluence-imports/<space>/<slug>.md` | `confluence_revision` |
| `jira_sync` | Jira issues (JQL or pre-resolved records) | `pending-imports/jira-<date>/jira-imports/<project>/<KEY>.md` | `jira_updated` |
| `slack_sync` | Pinned messages from declared channels | `pending-imports/slack-<date>/slack-imports/<channel>/pin-<ts>.md` | `slack_ts` |
| `web_pull` | Generic HTTPS URLs on a domain allowlist | `pending-imports/web-<date>/web-imports/<host>/<slug>.md` | `etag` (when present) |

Every output file carries a YAML frontmatter block recording
`source_url`, `last_synced`, and the source-specific revision key.
Re-syncing with the same revision is a no-op — the file isn't
rewritten and its mtime is preserved.

## Configuration schema

All four routines read from `.vigil/config.toml` under
`[sync.<name>]`. Adjacent CLI invocations like `vigil sync
confluence` consume the matching section; the `[sync]` keys are also
free-form so the runner / `/schedule` config can pass extras the OSS
schema doesn't model.

### Confluence

```toml
[sync.confluence]
# Either a list of pre-resolved page records (the runner already
# pulled them via Atlassian MCP):
pages = [
  { space = "ENG",
    title = "Deploy runbook",
    url   = "https://acme-corp.atlassian.net/wiki/spaces/ENG/pages/1",
    body  = "<h1>...</h1>",        # ADF-rendered HTML
    revision = "v17" },
]
# OR a list of URL-only entries the routine itself fetches via
# `httpx`:
# pages = [{ url = "https://acme-corp.atlassian.net/wiki/.../1" }]
```

### Jira

```toml
[sync.jira]
issues = [
  { key = "PLAT-123",
    project = "PLAT",
    summary = "Migrate to PG16",
    status  = "In Progress",
    description = "<p>...</p>",
    url     = "https://acme.atlassian.net/browse/PLAT-123",
    updated = "2026-05-01T10:00:00Z" },
]
```

The routine derives `project` from the issue key when it isn't
explicitly set (`PLAT-123` → `plat`).

### Slack

```toml
[sync.slack]
channels = ["#oncall", "#incidents"]
pins = [
  { channel  = "#oncall",
    ts       = "1714000000.000100",
    user     = "alice",
    text     = "Pinned: pager rotation table",
    permalink = "https://acme.slack.com/archives/C123/p1714000000000100" },
]
```

The `channels` list scopes which pins the routine accepts. Pins from
undeclared channels are refused; refusals surface in the routine's
summary as `refused=N`.

### Web

```toml
[sync.web]
urls = [
  "https://docs.aws.amazon.com/eks/latest/userguide/clusters.html",
  "https://kubernetes.io/docs/concepts/workloads/pods/",
]
allowlist_domains = ["docs.aws.amazon.com", "kubernetes.io"]
```

**Default-deny.** An empty `allowlist_domains` refuses every URL.
Suffix matching: `aws.amazon.com` admits `docs.aws.amazon.com` but
NOT `evil.aws.amazon.com.attacker`. The host-suffix check requires a
dot boundary so substring smuggling can't bypass the allowlist.

## CLI invocation

```bash
# Run a routine locally:
vigil sync confluence
vigil sync jira
vigil sync slack
vigil sync web

# Drop staged drafts somewhere else:
vigil sync confluence --out-dir /tmp/incoming

# Show what would happen, write nothing:
vigil sync web --dry-run
```

Each subcommand reads the matching `[sync.<name>]` section, dispatches
to the agent runner, and prints `[ok|warn|fail] <routine> — <summary>`
plus the artifacts list. The summary includes per-source counts:

```
[ok] confluence_sync — 12 page(s)  wrote=11  deduped=1
[warn] web_pull — 5 url(s)  wrote=3  deduped=0  refused=2
```

## Trust boundary

```
┌─────────────────────────────────────────────────────────────┐
│  RUNNER (the only thing with credentials)                   │
│   - Atlassian MCP server   ← Confluence / Jira tokens       │
│   - Slack MCP server       ← Slack token                    │
│   - httpx / WebFetch       ← public HTTP                    │
│                                                             │
│  Stages files via:                                          │
│    vigil sync <routine>  /  agent runner dispatch        │
└──────────────────────────┬──────────────────────────────────┘
                           │ pre-resolved records or URLs
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  AGENT (read-only filesystem; no tokens)                    │
│   - Convert HTML / ADF → markdown                           │
│   - Apply frontmatter                                       │
│   - Write under `out_dir/`                                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ staged files
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  PR — humans review                                         │
│   - Promote drafts into `docs/runbooks/`                    │
│   - Reject the ones that don't belong                       │
│   - Auto-merge: explicitly off                              │
└─────────────────────────────────────────────────────────────┘
```

The agent module has no idea what an Atlassian token looks like.
That's the point. The CLI hands the runner whatever's in the config
file, and a runner with MCP-aware fetchers swaps in a fetcher that
routes through the user's existing Atlassian MCP server. **Teammate
ships no MCP servers of its own** — it consumes whatever the user
already has configured.

## Cost / latency expectations

| Routine | Per-item cost | Per-item latency | Failure mode |
| --- | --- | --- | --- |
| `confluence_sync` | 1 MCP / HTTPS call per page | ~200 ms (MCP) / ~400 ms (HTTPS) | Atlassian rate-limit; the routine skips and warns |
| `jira_sync` | 1 MCP call per issue (description fetched once) | ~200 ms | Same |
| `slack_sync` | Zero — pins are pre-resolved by the runner | < 10 ms (file write only) | Refused channels surface as warnings |
| `web_pull` | 1 HTTPS GET per URL | varies | 5xx / fetch error; the routine skips and warns |

Throughput targets: a 50-page Confluence sync should finish under a
minute on a normal connection. Run weekly via `/schedule`; the
incremental cost is dominated by the MCP server, not the agent.

## How to add a new sync routine

1. Add `src/vigil/agent/<source>_sync.py`. Mirror the pattern in
   `confluence_sync.py`: a single `run(config, *, today=None,
   fetcher=None)` function, a per-routine output directory under
   `out_dir/<source>-imports/`, frontmatter that records the source's
   revision proxy, dedup via `_sync_common.write_doc`'s `revision_key`.
2. Register in `agent/runner.py` — add to `_REGISTRY`.
3. Wire to the CLI by appending the routine name to `_SYNC_ROUTINES`
   in `cli.py`.
4. Add tests under `tests/test_sync_routines.py`. Pattern: empty
   config → ok with no artifacts; one record → wrote=1; same revision
   re-sync → mtime preserved; injected fetcher captures the URL.

The shared HTML→markdown converter lives in
`vigil/agent/_sync_common.py`. Hand-rolled, regex-driven, no
BeautifulSoup. If your new source needs richer parsing, expand the
converter rather than depending on a third-party library — the
constraint is "OSS install must work without extras".

## What this is not

- **Not bidirectional.** Edits in the team-brain markdown do not flow
  back to Confluence / Jira / Slack. The original system stays
  authoritative; vigil is a downstream cache for retrieval.
- **Not real-time.** Sync is a slow loop — typically weekly via
  `/schedule`, on demand via `vigil sync`. Real-time would invite
  the credential-trust problems we explicitly avoided.
- **Not a search index over upstream.** The synced files live in the
  brain repo and feed the regular sqlite-vec index. `vigil ask`
  treats them like any other markdown.
