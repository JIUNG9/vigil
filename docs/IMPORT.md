# Bulk corpus import — `vigil import`

`vigil import` is a family of CLI commands that pull content from Jira, Confluence, GitHub, and Slack into the brain repo's `archive/` directory, formatted as markdown with standardised YAML frontmatter.

Each importer is **idempotent** (re-running won't duplicate), **incremental** (uses a watermark per source), and **resumable** (a killed run picks up near where it died via checkpoints every 100 items).

---

## Install

```bash
pip install 'vigil[importers]'
```

This adds: `httpx`, `slack-sdk`, `pyyaml`.

---

## Output layout

All imports write to `<brain_root>/archive/<source>/...`:

```
brain/
├── archive/
│   ├── jira/<PROJECT>/<KEY>.md            # one file per issue
│   ├── confluence/<SPACE>/<page-id>-<slug>.md
│   ├── github/<owner>/<repo>/{readme,issues,pull_requests}/
│   └── slack/<channel>/YYYY/MM/DD-messages.md   # one daily rollup per channel
└── .vigil-sync/
    └── state.json                          # watermark per source
```

The watermark in `state.json` is what makes runs incremental. Delete the file (or just the relevant source key) to force a full re-scan.

---

## Frontmatter schema

Every imported file has this frontmatter:

```yaml
---
source: jira | confluence | github | slack
source_type: issue | page | pull_request | readme | channel_daily_rollup
source_id: <stable id from the source>
source_url: https://...
title: <human-readable title>
fetched_at: 2026-05-15T07:00:00Z
last_modified: 2026-05-14T22:30:00Z
author: <name or login>
labels: [...]
extra:
  # source-specific fields
  jira_status: "In Progress"
  jira_priority: "High"
---
<markdown body>
```

The indexer uses `source` and `extra.*` for Qdrant filter expressions.

---

## Jira

```bash
export ATLASSIAN_API_TOKEN="..."
export ATLASSIAN_EMAIL="you@your-org.com"
export JIRA_BASE_URL="https://your-org.atlassian.net"
export JIRA_PROJECTS="ENG,PLAT,INFRA"        # comma-separated project keys

vigil import jira
```

Uses the new `/rest/api/3/search/jql` endpoint (the legacy `/search` returns 410 Gone). Pagination via `nextPageToken`. `ORDER BY updated ASC` so watermark-resume works.

One file per issue at `archive/jira/<PROJECT>/<KEY>.md`. Comments are appended after the description (top 30, oldest first).

| Env var | Required | Description |
|---|---|---|
| `ATLASSIAN_API_TOKEN` | yes | Basic-auth password |
| `ATLASSIAN_EMAIL` | yes | Basic-auth username |
| `JIRA_BASE_URL` | yes | e.g. `https://your-org.atlassian.net` |
| `JIRA_PROJECTS` | yes | comma-separated project keys |

---

## Confluence

```bash
export ATLASSIAN_API_TOKEN="..."
export ATLASSIAN_EMAIL="you@your-org.com"
export CONFLUENCE_BASE_URL="https://your-org.atlassian.net/wiki"
export CONFLUENCE_SPACES="DOCS,ENG"          # comma-separated space keys

vigil import confluence
```

Uses `/rest/api/content/search` with CQL. **Quirks**:

- Space keys must be quoted in CQL: `space IN ("DOCS","ENG")` — bare keys return HTTP 400
- Date format must be `YYYY/MM/DD HH:MM` — ISO 8601 returns HTTP 400 "Could not parse cql"
- `ORDER BY lastmodified ASC` enables watermark-resume

One file per page at `archive/confluence/<SPACE>/<page-id>-<slug>.md`. Storage XHTML is converted to markdown via a small in-house converter; ~90% of pages convert cleanly.

---

## GitHub

```bash
export GITHUB_PAT="ghp_..."                  # classic PAT with `repo` scope
export GITHUB_ORG="your-org"
# optional:
export GITHUB_REPOS="repo-1,repo-2"          # restrict to specific repos

vigil import github
```

Pulls **READMEs + issues + pull requests + their comments** for every repo accessible to the PAT in `GITHUB_ORG`. No source code — that already lives in git.

Per-item layout:

```
archive/github/<owner>/<repo>/
├── README.md
├── issues/<num>-<slug>.md
└── pull_requests/<num>-<slug>.md
```

GitHub allows 5,000 req/hr per PAT; the first backfill of a large org takes ~2h. Subsequent nightly runs are much faster because of the `since` filter.

---

## Slack

```bash
export SLACK_BOT_TOKEN="xoxb-..."
# optional:
export SLACK_IMPORT_CHANNELS="devops,incidents"   # default: all channels bot is in
export SLACK_HISTORY_DAYS="30"                    # initial backfill window

vigil import slack
```

The bot must be a member of channels it imports. Required Bot Token scopes: `channels:read`, `groups:read`, `channels:history`, `groups:history`, `users:read`.

**Design choice**: one daily rollup per channel rather than one file per message. Reduces ~50k messages → ~30-100 daily rollup files while preserving substantive content. Messages with subtypes (joins, edits, bot-posts) and messages shorter than 30 characters are dropped as noise.

```
archive/slack/devops/2026/05/14-messages.md:

# #devops — 2026-05-14

- **09:23 · Alice Park**: heads up, we're rolling back PN-1834. RDS CPU was spiking.
- **09:24 · Min-jun Jo**: ack. I'll monitor replica lag.
- **09:31 · Alice Park**: replica lag back to <100ms. resolved.
```

---

## Run all four

```bash
vigil import all              # jira → confluence → github → slack, in sequence
vigil import all --dry-run    # render but don't write or advance watermark
```

A failure in one importer doesn't stop the rest.

---

## Secret redaction

Every body passes through a regex scrubber before being written to the archive. The patterns catch:

| Pattern | Replaced with |
|---|---|
| AWS access keys `AKIA…`, `ASIA…` | `[REDACTED-AWS-ACCESS-KEY]` |
| AWS 12-digit account IDs | `[REDACTED-AWS-ACCOUNT]` |
| Slack tokens `xoxb-…`, `xapp-…`, `xoxp-…` | `[REDACTED-SLACK-*]` |
| GitHub PATs `ghp_…`, `ghs_…`, `gho_…`, `ghu_…`, `ghr_…` | `[REDACTED-GITHUB-PAT]` |
| Atlassian API tokens `ATATT3…` | `[REDACTED-ATLASSIAN]` |
| Anthropic API keys `sk-ant-api03-…` | `[REDACTED-ANTHROPIC]` |
| OpenAI API keys `sk-…` | `[REDACTED-OPENAI]` |
| URL params `?token=` / `?key=` / `?secret=` / `?password=` | `[REDACTED-URL-PARAM]` |
| HTTP `Authorization: Bearer …` headers | `[REDACTED-BEARER]` |

This is **defense in depth**, not the only defense. Source-level access control (who can read which Jira project, who is in which Slack channel) is the primary control. Redaction catches accidental token paste.

Audit after each import:

```bash
grep -r "REDACTED" archive/ | wc -l
```

Custom patterns can be added by passing `custom_patterns=` to `vigil.importers.redact.redact()`.

---

## Kubernetes deployment

Production deployment runs each importer as a nightly CronJob, with the import script wrapped in a `commit → fetch → rebase → push` retry loop to handle concurrent pushes from sibling jobs.

See `examples/k8s/import-cronjobs/` for full manifests.

Key environment expectations:

- A K8s Secret named `vigil-credentials` with keys: `github-pat`, `atlassian-token`, `slack-bot-token`
- A ConfigMap `vigil-config` with a `brain_ref` key (typically `main`) — single control point for git ref across all CronJobs
- A ServiceAccount with `batch/cronjobs:get` + `batch/jobs:list,create` permissions (already required by the event-listener)

---

## Troubleshooting

**`fatal: not in a git directory`** when the import script tries to git-add
→ UID mismatch between init container (root) and main container (non-root). Add `chown -R 1000:1000 /etc/vigil/brain` in the init container, and `git config --global --add safe.directory $BRAIN` as the **first** command in the main container.

**Confluence returns `HTTP 400 Could not parse cql`**
→ Either the space keys aren't quoted, or the date isn't in `YYYY/MM/DD HH:MM` format. See the Confluence section above.

**Jira returns `HTTP 410 Gone`**
→ You're hitting the legacy `/rest/api/3/search` endpoint. Upgrade to `vigil >= 0.11.1` which uses `/rest/api/3/search/jql`.

**Importer never finishes within `activeDeadlineSeconds`**
→ Confirm `state.json` is being updated mid-run (every 100 items). If not, your CronJob may be wiping it via an emptyDir mount; persist it to the brain repo and commit.

**Push collisions: `! [rejected] HEAD -> main (fetch first)`**
→ Two import CronJobs are running concurrently and both pushed. Use the `commit → fetch → rebase → push` retry loop pattern (see `examples/k8s/import-cronjobs/`).
