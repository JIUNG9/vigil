# Importing 25,000 Documents from Four Sources, Idempotently

**Tags:** `Kubernetes` `CronJobs` `Atlassian API` `GitHub API` `Slack API` `Idempotent ETL`

---

> Part 3 of "Building teammate." How we bulk-imported 16,259 Jira issues, 5,310 GitHub items, 3,247 Confluence pages, and 37 Slack daily rollups into a single git-tracked corpus — with watermarks, redaction, and a resume mechanism that survives 3-hour timeouts.

---

## Why Not Just Use Each Tool's Search?

teammate's pitch is "unified search across your knowledge sources." For that to mean anything, the corpus has to actually be unified — one filesystem, one format, one query layer.

The alternative is a federated query that fans out to four APIs per question. I rejected that because:

- **Latency**: even with parallel fan-out, you're bottlenecked by Atlassian's 200-300ms response times. Multiplied by 5+ queries per question, that's 1-2 seconds of wall time before you even start ranking.
- **Rate limits**: Confluence is 10 req/sec, Jira is similar. Five engineers querying simultaneously DDOSes you off your own knowledge base.
- **No ranking**: federated search gives you four ranked lists. Merging them is a distinct art.
- **No history**: a federated query is ephemeral. The team's knowledge graph has no version, no audit, no diff.

So: pull everything into markdown, into git, into one place. The boring answer is the right one.

The question is *how*.

---

## The Constraints

Same as the rest of the project:

- **Idempotent**: running an import twice should produce the same archive, not duplicates
- **Resumable**: a 3-hour cron timeout shouldn't lose 2h59m of progress
- **Source-of-truth in git**: every imported document is a PR-reviewable markdown file
- **Sanitization at boundary**: tokens, account IDs, and PII get scrubbed before write
- **Minimal cognitive load**: one CLI per source, same flags, same env-var conventions

---

## The Architecture

Five files, ~1,000 lines total:

```
src/teammate/importers/
├── __init__.py
├── base.py          # ImporterBase + frontmatter schema + watermark machinery
├── redact.py        # secret/PII regex scrubber
├── jira.py          # Jira issues + comments
├── confluence.py    # Confluence pages
├── github.py        # READMEs + issues + PRs + comments
└── slack.py         # daily rollups per channel
```

Plus four Kubernetes CronJobs that run them on schedule. We'll walk through each piece.

---

## The Base Class

Every importer subclasses `ImporterBase` and implements three methods:

```python
class ImporterBase(ABC):
    """Subclasses implement iterate() + render() + watermark()."""

    source_name: str = ""

    def __init__(self, brain_root, *, dry_run=False):
        self.brain_root = Path(brain_root)
        self.dry_run = dry_run
        self.archive_root = self.brain_root / "archive" / self.source_name
        self.state_path = self.brain_root / ".teammate-sync" / "state.json"

    @abstractmethod
    def iterate(self, since: Any) -> Iterator[dict]:
        """Yield raw items from the source, optionally filtered by `since`."""

    @abstractmethod
    def render(self, item: dict) -> tuple[str, dict, str]:
        """Return (relative_path, frontmatter_dict, markdown_body)."""

    @abstractmethod
    def watermark(self, item: dict) -> Any:
        """Return the watermark value of `item` (e.g. last_modified str)."""
```

The base class owns the run loop:

```python
CHECKPOINT_EVERY = 100

def run(self) -> ImportResult:
    state = self._load_state()
    since = state.get(self.source_name, {}).get("watermark")
    log.info("%s: starting from watermark=%r", self.source_name, since)

    result = ImportResult(source=self.source_name)
    max_watermark = since
    last_checkpoint_at = 0

    for item in self.iterate(since):
        rel_path, fm, body = self.render(item)
        wm = self.watermark(item)
        if wm and (max_watermark is None or str(wm) > str(max_watermark)):
            max_watermark = wm
        if self._write(rel_path, fm, body):
            result.written += 1
        else:
            result.skipped += 1

        # Incremental checkpoint — so a killed run resumes near where it died.
        written_or_skipped = result.written + result.skipped
        if (
            not self.dry_run
            and max_watermark is not None
            and written_or_skipped - last_checkpoint_at >= CHECKPOINT_EVERY
        ):
            state.setdefault(self.source_name, {})["watermark"] = str(max_watermark)
            self._save_state(state)
            last_checkpoint_at = written_or_skipped

    # Final save
    if not self.dry_run and max_watermark is not None:
        state.setdefault(self.source_name, {})["watermark"] = str(max_watermark)
        self._save_state(state)

    return result
```

Three properties matter:

### Watermark-driven incremental sync

`state.json` (in `.teammate-sync/`, git-tracked) carries the high-water mark per source:

```json
{
  "jira":       {"watermark": "2026-05-15T07:42:00.000+0900", "last_run": "..."},
  "confluence": {"watermark": "2026/05/14 22:30", "last_run": "..."},
  "github":     {"watermark": "2026-05-15T07:30:00Z", "last_run": "..."},
  "slack":      {"watermark": "2026-05-15T00:00:00+00:00", "last_run": "..."}
}
```

Every run reads its watermark and queries the source for items updated since. First run = full backfill. Every subsequent run = delta only.

### Checkpoint every 100 items

This one I had to learn the hard way. The first Confluence run hit `activeDeadlineSeconds: 3600`, got killed at 3h, and lost the entire watermark. The next run started from scratch and timed out again. Guaranteed infinite failure loop.

The fix: persist `state.json` after every 100 items, not just at the end. A killed run loses at most 100 items of progress.

### `ORDER BY ASC` is non-negotiable

For watermark-resume to actually skip already-imported items, the source query must return items in **ascending** order of the watermark field. If you order DESC, every run re-fetches the same newest items first and never makes progress through older ones.

```python
# Jira
jql = " AND ".join(clauses) + " ORDER BY updated ASC"

# Confluence
cql = " AND ".join(clauses) + " ORDER BY lastmodified ASC"
```

This took me an embarrassingly long time to notice, because the bug presents as "the importer always finishes within the timeout" — which seems good, until you check the archive and find only the last 500 items.

---

## The Frontmatter Schema

Every imported markdown file has a YAML frontmatter that tells the indexer where it came from:

```markdown
---
source: jira
source_type: issue
source_id: INFRA-2391
source_url: https://your-org.atlassian.net/browse/INFRA-2391
title: "dp-prod-rds storage autoscale ceiling hit"
fetched_at: 2026-05-13T07:11:49Z
last_modified: 2026-05-13T06:42:00.000+0900
author: "Alice Park"
labels: ["incident", "architecture-decision"]
extra:
  jira_status: "In Progress"
  jira_priority: "High"
  jira_issuetype: "Bug"
  jira_assignee: "Min-jun Jo"
---

# INFRA-2391 — dp-prod-rds storage autoscale ceiling hit

## Description
...

## Comments
...
```

Standard across all four importers. The `extra` block is free-form per source — Jira has `jira_status`, GitHub has `github_state`, Confluence has `confluence_space`, etc.

This lets the indexer build typed Qdrant filters at query time:

```python
qdrant.search(
    collection="brain",
    query_vector=embed(query),
    query_filter={
        "must": [
            {"key": "source",          "match": {"value": "jira"}},
            {"key": "extra.jira_status","match": {"value": "Done"}},
        ]
    },
)
```

---

## The Jira Importer

Jira deprecated `/rest/api/3/search` in 2025 — it returns `410 Gone` now. The new endpoint is `/rest/api/3/search/jql` with cursor-based pagination:

```python
def iterate(self, since: Any) -> Iterator[dict]:
    import httpx

    clauses = [f"project IN ({','.join(self.projects)})"]
    if since:
        dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        clauses.append(f'updated > "{dt.strftime("%Y-%m-%d %H:%M")}"')

    jql = " AND ".join(clauses) + " ORDER BY updated ASC"

    next_page_token = None
    with httpx.Client(auth=self.auth, timeout=30) as client:
        while True:
            params = {
                "jql": jql,
                "maxResults": 50,
                "fields": "*navigable,comment",
                "expand": "renderedFields",
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token

            resp = client.get(f"{self.base}/rest/api/3/search/jql", params=params)
            if resp.status_code != 200:
                log.error("jira search HTTP %d: %s", resp.status_code, resp.text[:300])
                return

            data = resp.json()
            issues = data.get("issues", [])
            if not issues:
                break
            for issue in issues:
                yield issue

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
```

Each issue's `renderedFields.description` is HTML, and `fields.comment.comments[].body` is ADF (Atlassian Document Format, a JSON tree). The renderer converts both to markdown with a small homemade HTML-to-MD pass — 80 lines, good enough for runbook-quality text.

Result on first run: **16,259 issues from 29 projects in 14 minutes**. Subsequent nightly runs typically take 30-90 seconds for the delta.

---

## The Confluence Importer

Confluence CQL has two non-obvious quirks I discovered the hard way:

### Quirk 1: Space keys must be quoted

```python
# This returns HTTP 400 "Could not parse cql":
"space IN (DEVOPS, NEXUS, INFRA) AND lastmodified > '...'"

# This works:
"space IN (\"DEVOPS\",\"NEXUS\",\"INFRA\") AND lastmodified > '...'"
```

The error message gives you nothing useful — just `BadRequestException: Could not parse cql:`. The quotes are mandatory for multi-space `IN` clauses.

### Quirk 2: Date format must be slashes, not ISO

```python
# Returns 400:
'lastmodified > "2026-05-12T09:00:00.000Z"'

# Works:
'lastmodified > "2026/05/12 09:00"'
```

CQL has its own date format. ISO 8601 is silently rejected.

After fixing both, the import works:

```python
def iterate(self, since: Any) -> Iterator[dict]:
    import httpx

    spaces_cql = ",".join(f'"{s}"' for s in self.spaces)
    clauses = [f"space IN ({spaces_cql})", "type = page"]
    if since:
        dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        clauses.append(f'lastmodified > "{dt.strftime("%Y/%m/%d %H:%M")}"')

    cql = " AND ".join(clauses) + " ORDER BY lastmodified ASC"

    with httpx.Client(auth=self.auth, timeout=30) as client:
        start = 0
        while True:
            resp = client.get(
                f"{self.base}/rest/api/content/search",
                params={
                    "cql": cql,
                    "limit": 25,
                    "start": start,
                    "expand": "body.storage,version,space",
                },
            )
            if resp.status_code != 200:
                log.error("confluence search HTTP %d: %s",
                          resp.status_code, resp.text[:300])
                return
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for page in results:
                yield page
            if len(results) < 25:
                break
            start += 25
```

The HTML-to-markdown conversion for Confluence storage format is gnarlier than Jira's because of custom Atlassian macros (`<ac:structured-macro>`, `<ac:rich-text-body>`, `<ac:image>`). The current implementation handles ~90% of pages cleanly; the rest end up with some literal tags in the markdown, which is acceptable for retrieval purposes.

Result: **3,247 pages imported** on first run. Slower than Jira because pages are larger and the storage→markdown conversion adds CPU time.

---

## The GitHub Importer

GitHub's API is the most polite of the four. Standard cursor pagination, generous rate limits with a PAT, predictable JSON shapes.

The non-obvious choice: the importer pulls **READMEs + issues + pull requests + their comments**, not the source code itself. The code lives in git; what's interesting for retrieval is the *prose* — what's in the README, what was discussed in PR reviews, what the issue tracker has accumulated.

```python
def _iter_repo(self, client, repo: str, since_iso: str | None) -> Iterator[dict]:
    owner_repo = f"{self.org}/{repo}"

    # 1. README
    r = client.get(f"{_API}/repos/{owner_repo}/readme")
    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data.get("content", "")).decode("utf-8")
        yield {"_kind": "readme", "_owner_repo": owner_repo, ...}

    # 2. Issues + PRs (same endpoint)
    for state in ("open", "closed"):
        page = 1
        while True:
            params = {"state": state, "per_page": 100, "page": page,
                      "sort": "updated", "direction": "asc"}
            if since_iso:
                params["since"] = since_iso
            r = client.get(f"{_API}/repos/{owner_repo}/issues", params=params)
            if r.status_code != 200:
                if r.status_code != 410:  # 410 = issues disabled on this repo
                    log.warning("github %s issues HTTP %d", owner_repo, r.status_code)
                break
            items = r.json()
            if not items:
                break
            for it in items:
                it["_owner_repo"] = owner_repo
                it["_kind"] = "pull_request" if it.get("pull_request") else "issue"
                # Fetch comments per item
                cr = client.get(it.get("comments_url", ""), params={"per_page": 100})
                it["_comments"] = cr.json() if cr.status_code == 200 else []
                yield it
            if len(items) < 100:
                break
            page += 1
```

The "fetch comments per item" pattern is a rate-limit risk — 5,310 issues × 1 comment fetch each = 5,310 extra API calls. GitHub allows 5,000 req/hr per PAT, so a fresh import takes ~2 hours wall time. After the first backfill, the watermark filter means daily runs touch only a handful of items.

Result: **5,310 README + issue + PR documents across the org's repos**.

---

## The Slack Importer

Slack is the most distinct: instead of one markdown file per message (would be ~50,000 files of noise), the importer creates **one daily rollup per channel**:

```
archive/slack/devops/2026/05/13-messages.md
archive/slack/devops/2026/05/14-messages.md
archive/slack/incident-room/2026/05/14-messages.md
```

Each file is a markdown list of substantive messages from that channel that day:

```markdown
# #devops — 2026-05-14

- **09:23 · Alice Park**: heads up, we're rolling back PN-1834. RDS CPU was spiking.
- **09:24 · Min-jun Jo**: ack. I'll monitor replica lag.
- **09:31 · Alice Park**: replica lag back to <100ms. resolved.
```

Filters:
- Only channels the bot is a member of (Slack's permission model)
- Skip message subtypes (joins, edits, bot-posts)
- Drop messages shorter than 30 characters (acks, thumbs-up, emoji-only)

This drops the corpus from a hypothetical ~500k messages to ~37 daily-rollup files, while preserving the substance. The "what was discussed?" signal is intact; the "thumbs up" noise is gone.

---

## The Redaction Layer

Every imported body passes through a regex scrubber **before** being written to the archive. This is the last line of defense between source content and a git-tracked file:

```python
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"),             "[REDACTED-AWS-ACCESS-KEY]"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"),             "[REDACTED-AWS-STS-KEY]"),
    (re.compile(r"(?<![\w-])([0-9]{12})(?![\w-])"),   "[REDACTED-AWS-ACCOUNT]"),
    (re.compile(r"\bxoxb-[A-Za-z0-9-]{10,}\b"),       "[REDACTED-SLACK-BOT]"),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"),       "[REDACTED-SLACK-APP]"),
    (re.compile(r"\bxoxp-[A-Za-z0-9-]{10,}\b"),       "[REDACTED-SLACK-USER]"),
    (re.compile(r"\b(ghp|ghs|gho|ghu|ghr)_[A-Za-z0-9]{30,}\b"),
                                                       "[REDACTED-GITHUB-PAT]"),
    (re.compile(r"\bATATT3[A-Za-z0-9_-]{10,}\b"),     "[REDACTED-ATLASSIAN]"),
    (re.compile(r"\bsk-ant-api03-[A-Za-z0-9_-]{30,}\b"),
                                                       "[REDACTED-ANTHROPIC]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{30,}\b"),          "[REDACTED-OPENAI]"),
    (re.compile(r"([?&](?:token|key|secret|password|api[_-]?key)=)([^&\s]+)",
                re.IGNORECASE),                        r"\1[REDACTED-URL-PARAM]"),
    (re.compile(r"(Authorization:\s*Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE),
                                                       r"\1[REDACTED-BEARER]"),
]

def redact(text: str) -> str:
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
```

This is **defense in depth**, not the only defense. Source-level access control (who can read the Jira project, who can read the Slack channel) is the primary control. Redaction catches the cases where someone pasted a token into a Jira comment by accident.

A useful audit step is to grep the archive for the redaction tokens after each import:

```bash
grep -r "REDACTED" archive/ | wc -l
# 47 — review these before pushing to the public-ish OSS repo
```

---

## The Kubernetes CronJob Pattern

Each importer gets a CronJob that runs nightly, commits changes back to the brain repo, and pushes:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: teammate-import-jira
  namespace: teammate-agent
spec:
  schedule: "0 17 * * *"   # 17:00 UTC = 02:00 KST
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      activeDeadlineSeconds: 3600
      backoffLimit: 1
      template:
        spec:
          serviceAccountName: teammate-agent
          initContainers:
          - name: clone-brain
            image: alpine/git:2.43.0
            command: ["/bin/sh", "-c"]
            args:
            - |
              set -eu
              git clone --branch "${BRAIN_REF}" \
                "https://x-access-token:${GITHUB_PAT}@github.com/${BRAIN_REPO}.git" \
                /etc/teammate/brain
              chown -R 1000:1000 /etc/teammate/brain
            env:
            - {name: BRAIN_REF, valueFrom: {configMapKeyRef: {name: teammate-config, key: brain_ref}}}
            - {name: BRAIN_REPO, value: "your-org/your-brain-repo"}
            - {name: GITHUB_PAT, valueFrom: {secretKeyRef: {name: teammate-credentials, key: github-pat}}}
            volumeMounts:
            - {name: brain, mountPath: /etc/teammate/brain}
          containers:
          - name: teammate
            image: your-registry/teammate:latest
            imagePullPolicy: Always
            command: ["/bin/sh", "-c"]
            args:
            - |
              set -eu
              BRAIN=/etc/teammate/brain
              git config --global --add safe.directory $BRAIN
              git -C $BRAIN config user.email "teammate-agent@example.com"
              git -C $BRAIN config user.name "teammate-agent"

              teammate import jira

              git -C $BRAIN add archive/ .teammate-sync/
              if ! git -C $BRAIN diff --cached --quiet; then
                git -C $BRAIN commit -m "import(jira): incremental sync $(date -u +%Y-%m-%dT%H:%MZ)"
                # Concurrent pushes from sibling import jobs collide; retry on rebase.
                for attempt in 1 2 3; do
                  git -C $BRAIN fetch "${REMOTE_URL}" "${BRAIN_REF}"
                  git -C $BRAIN rebase FETCH_HEAD || { git -C $BRAIN rebase --abort; sleep 5; continue; }
                  if git -C $BRAIN push "${REMOTE_URL}" "HEAD:${BRAIN_REF}"; then
                    break
                  fi
                  sleep 5
                done
              fi
            env:
              # ... ATLASSIAN_*, JIRA_BASE_URL, JIRA_PROJECTS, etc.
```

Three war stories from getting this right:

### War story 1: `fatal: not in a git directory`

After the import succeeds, `git -C $BRAIN status` fails with "not in a git directory" — even though `.git` clearly exists.

Cause: init container runs as `root` (alpine/git default UID 0), main container runs as UID 1000 (our Dockerfile's `USER brain`). Git 2.35+ enforces ownership checks (CVE-2022-24765 hardening): a repo owned by a different UID than the current process is rejected as "not in a git directory" — a misleading error.

Fix:
```bash
chown -R 1000:1000 /etc/teammate/brain          # in init container
git config --global --add safe.directory $BRAIN  # in main container, before any other git command
```

The `--global` is critical because per-repo `git config` itself needs the safe-directory check to pass first. Chicken and egg.

### War story 2: `.git/config: Permission denied`

After `chown` + `safe.directory`, the next error is when git tries to write `git config user.email`. The `.git/config` file is still owned by root (from `git clone`), and the user 1000 process can't write to it.

The `chown -R 1000:1000` in the init container fixes this — but ONLY if applied to the entire `.git/` subdirectory, not just the top-level brain dir.

### War story 3: Concurrent push failures

Three import CronJobs run within a 30-minute window each night. The first one pushes cleanly. The second one pushes against a now-different remote tip and gets `! [rejected] HEAD -> main (fetch first)`.

The naive fix is `git pull --rebase` before push. But pull-merge-push isn't atomic, so two siblings can race and one still loses. The retry loop above handles it: fetch, rebase, attempt push, retry up to 3 times.

This is concurrency control via convergent retries — fine at this scale (4 importers, ~30 commits/day total). At higher rates we'd need a proper queue.

---

## Results

After the first full backfill and 30 days of nightly runs:

| Source | Initial backfill | Time | Nightly delta |
|---|---|---|---|
| Jira | 16,259 issues | 14 min | 50-300 issues |
| Confluence | 3,247 pages | 47 min | 5-20 pages |
| GitHub | 5,310 README + issues + PRs | ~2 h (first time, rate-limited) | 30-100 items |
| Slack | 37 daily rollups | 21 s | 1 rollup / channel / day |
| **Total** | **24,853 docs** | **~3 h cold** | **<10 min warm** |

The brain repo grew from a handful of human-curated markdown files to ~250 MB of indexable content, all PR-reviewable, all `git blame`-attributable, all redacted at the boundary.

---

## What I'd Do Differently

1. **Start with the `--global safe.directory` line.** Would have saved 4 hours of debugging.

2. **Build the checkpoint mechanism on day 1.** The Confluence-times-out-forever loop was painful and entirely predictable.

3. **Skip GitHub comments on the initial backfill.** The per-item comment fetch is the rate-limit hot path. A two-phase backfill (issues without comments first, then comments for "important" issues) would have cut wall time in half.

4. **Bigger Confluence storage→markdown converter.** I built ~80 lines of regex; would benefit from a real HTML parser at this scale.

---

## Try It Yourself

```bash
pip install 'claude-teammate[importers]'

# Configure tokens
export GITHUB_PAT=ghp_...           GITHUB_ORG=my-org
export ATLASSIAN_API_TOKEN=...      ATLASSIAN_EMAIL=you@example.com
export JIRA_BASE_URL=https://my-org.atlassian.net  JIRA_PROJECTS=PROJ1,PROJ2
export CONFLUENCE_BASE_URL=https://my-org.atlassian.net/wiki  CONFLUENCE_SPACES=DOCS,ENG
export SLACK_BOT_TOKEN=xoxb-...     SLACK_IMPORT_CHANNELS=devops,incidents

cd ~/work/brain
teammate import all --dry-run     # see what would happen
teammate import all               # commit to it
```

Source: https://github.com/JIUNG9/teammate/tree/main/src/teammate/importers

CronJob manifests: https://github.com/JIUNG9/teammate/tree/main/examples/k8s

---

*Part 3 of "Building teammate." [← Part 2: Slack Socket Mode](./02-slack-socket-mode.md) · Next: From per-pod SQLite to k8s-native Qdrant (coming when v1 ships)*
