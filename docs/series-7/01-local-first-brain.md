# Why I Built a Local-First DevSecOps Brain Instead of Using Glean or Notion AI

**Tags:** `DevSecOps` `SRE` `RAG` `Local LLM` `Knowledge Management` `Open Source`

---

> Our team's institutional knowledge lives in four systems that don't talk to each other. Glean wants $40/user/month and our data in their cloud. So I built **teammate** — an open-source assistant that turns a private git repo into a queryable team brain. This is part 1 of a 7-article series.

---

## The Problem

The week I started thinking about this, I sat through three incidents in five days. Each one followed the same pattern:

1. Alert fires. On-call engineer joins the channel.
2. "Has anyone seen this before?"
3. Silence, then someone vaguely remembers an incident 4 months ago.
4. 20 minutes of grep through Slack threads.
5. Someone finds the Confluence page. It's stale and links to a Jira ticket that's been closed.
6. The Jira ticket references a commit on a repo nobody currently maintains.
7. The actual fix is in someone's DM history.

Total MTTR for this kind of incident: 47 minutes. Of that, maybe 8 minutes was the actual fix. The other 39 was archaeology.

The knowledge isn't missing. It's **scattered**. We have:

- ~16,000 Jira issues across 29 projects, going back five years
- ~5,000 Confluence pages across five spaces
- Issues, PRs, and READMEs across ~40 GitHub repos
- Slack messages in ~30 channels, with retention varying by channel

The numbers add up to roughly **25,000 documents** that nobody can search at once.

I started looking at the obvious options.

---

## What I Considered (and Rejected)

### Glean

Glean is the market leader. It's good. It also costs roughly $40 per engineer per month at our team size, and the cost is the smallest issue. The bigger issues:

- **Data sovereignty**: our compliance posture doesn't allow indexing internal docs in a third-party cloud, full stop. This isn't a technology question; it's a contract question that no amount of "we're SOC 2" handwaving solves.
- **Vendor lock-in**: once you've Gleaned 25,000 docs, you're hostage to their pricing forever.
- **Customization ceiling**: I want the agent to do more than chat. I want it to create K8s Jobs, run my CronJobs, and write postmortems. Glean is a search tool. It will never do these things.

### Notion AI

Same data sovereignty problem. Also, it indexes Notion content well but is mediocre at unifying external sources.

### Mem.ai

Personal-knowledge focused. Doesn't unify team sources. Laptop-bound.

### LangChain + Pinecone (build it ourselves)

Tempting, but:

- 50+ MB of dependencies for a tool that should be <10 MB
- Cloud vector DB defeats the local-first premise
- LangChain has its own opinions about agent orchestration that I'd fight

### A self-hosted Glean clone

Considered. Rejected because operating Elasticsearch + a custom indexer + a custom UI is six months of work before I have a single useful query.

---

## The Constraints That Drove the Design

I wrote them down explicitly:

1. **Data stays on infrastructure we control.** No third-party cloud for index or embeddings.
2. **Engineers can use it offline.** On a plane, on a bad VPN day, the brain must answer.
3. **Onboarding cost ≤ 10 minutes per engineer.** No "configure Terraform first" rituals.
4. **Storage ≤ 5 GB per engineer.** Local indexes are fine. Multi-GB downloads are not.
5. **Sources are git-tracked.** The brain itself is a private repo. PR-review every change.
6. **The agent must do, not just search.** Trigger K8s Jobs, create Confluence pages, post to Slack.
7. **OSS-able.** I want other teams to use this. Means no proprietary dependencies, no internal APIs.

Constraint 5 is the one nobody else in the market satisfies. Glean ingests Confluence and gives you search. It does **not** give you a git repo of markdown that you can `git diff`. Without git, there's no audit trail. Without an audit trail, the team's brain has no version history. That's a non-starter for a SOC 2 audit and for sanity.

---

## The Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │ Team's PRIVATE git repository (the brain)    │
                    │                                              │
                    │ CLAUDE.md                                    │
                    │ docs/runbooks/  docs/architecture/  ADRs     │
                    │ knowledge/people.md  knowledge/services.md   │
                    │ archive/jira/...  archive/confluence/...     │
                    │ archive/github/... archive/slack/...         │
                    └────────────────────┬─────────────────────────┘
                                         │
                                         │ git clone / git pull
                                         ▼
                    ┌──────────────────────────────────────────────┐
                    │ Each engineer's laptop (the derived state)   │
                    │                                              │
                    │ Ollama (LLM + embeddings, local)             │
                    │ sqlite-vec index (~1-5 GB)                   │
                    │ Claude Code + teammate MCP server            │
                    │ Obsidian (optional, opens markdown natively) │
                    └──────────────────────────────────────────────┘
```

Five components, each chosen for a specific reason:

### Git as the federation layer

Every team already has a private git host. Adding "the brain" as another repo is zero infrastructure. `git log` becomes the audit trail. `git blame` answers "who wrote this and when." `git diff` shows what changed last week.

This is also the OSS angle: there's no "teammate cloud" because there can't be. The team's brain is on the team's git host. Forever.

### Markdown as the format

Diff-friendly. Code-review-friendly. Opens in Obsidian for engineers who like the journaling workflow. Renders in GitHub. Survives the next decade.

Compare to PDFs, Notion blocks, Confluence storage XHTML, Slack JSON — markdown beats them all on durability.

### Ollama for the local LLM

Ollama is the standard now. Zero API keys. Runs offline. Installs in 30 seconds. We use:

- `nomic-embed-text` for embeddings (768-d, ~270 MB)
- `llama3.2:3b` for answering (~2 GB)

Total disk: ~3 GB for both models. Both run fine on a MacBook Air.

### sqlite-vec for the vector store

I considered pgvector, Qdrant, Chroma. For a personal-laptop store with ~5 GB upper bound:

| Choice | Verdict |
|---|---|
| pgvector | Requires Postgres running on every laptop. No. |
| Qdrant | Daemon process, gRPC port, overkill for laptop. |
| Chroma | Smaller community, less proven. |
| sqlite-vec | Single file. Polyglot. ~1 MB extension. **Yes.** |

The index becomes one `.sqlite` file. Easy to ship as a GitHub Release artifact for fast onboarding (engineer downloads pre-built index in 30 seconds instead of embedding for 10 minutes).

### Claude Code as the agent layer

I considered building agent orchestration in-house. Then I realized Claude Code already does this — it's the agent, with proper tool use, file editing, and a mature plugin model. teammate doesn't reinvent it. teammate exposes the brain to Claude Code via MCP.

This decision saved me ~3 months of work. LangChain, LlamaIndex, AutoGen — all skipped.

---

## The Implementation, in Three Commands

The team lead runs once:

```bash
pip install claude-teammate
teammate scaffold ~/team-brain --team-name "my-team"
cd ~/team-brain
git init && git add . && git commit -m "initial brain"
git remote add origin git@github.com:my-org/my-brain.git
git push -u origin main
```

This creates a templated repo with `CLAUDE.md` (team rules), `docs/runbooks/`, `docs/architecture/`, `knowledge/`, plus a CI workflow that validates structure on every PR.

Each engineer runs once per laptop:

```bash
git clone git@github.com:my-org/my-brain.git ~/work/brain
cd ~/work/brain
teammate init       # builds the sqlite-vec index, ~10s on a MacBook
```

Daily use:

```bash
teammate ask "how do I rotate the RDS master password?"
```

The query flow:

1. Embed the question (Ollama, ~50ms)
2. Top-K nearest-neighbor search in sqlite-vec (~5ms)
3. Pull 6 most relevant chunks
4. Construct prompt: *"Answer using ONLY these chunks: …"*
5. Stream answer from Ollama with `[file path]` citations

A typical question returns in 2-3 seconds, with citations to specific runbook files. No data leaves the laptop.

---

## The Confidence Guards That Made It Production-Ready

A RAG system that confidently bullshits is worse than no system. teammate has four guards:

### 1. Score threshold

Below 0.5 cosine similarity, `teammate ask` returns:

```
I don't know. The closest match I found was:
  docs/runbooks/auth-deploy.md (score 0.42)
Try rewording your question, or re-index if this file is new.
```

Better to admit ignorance than to invent.

### 2. Citation guard

Every paragraph in the answer must cite at least one file path in `[brackets]`. Uncited paragraphs are stripped before display. If the model can't ground a claim in retrieved content, the user doesn't see the claim.

### 3. Audit log

Every retrieval logs to `.teammate-cache/audit.jsonl`:

```json
{
  "ts": "2026-05-15T07:14:22Z",
  "query": "how do I rotate RDS password?",
  "top_chunks": [
    {"path": "docs/runbooks/rds-rotation.md", "score": 0.81},
    {"path": "docs/runbooks/secrets-rotation.md", "score": 0.74}
  ],
  "answer_chars": 1242
}
```

Rotates weekly. Lets you audit what the agent has been asked and what it surfaced.

### 4. Per-action confidence floor

```toml
[confidence.action_floors]
ask                = 0.50
weekly_digest      = 0.60
pr_migration_plan  = 0.65
execute            = 0.85   # reserved — not yet wired
```

Higher-stakes actions require higher confidence. A digest can be a little speculative; an `execute` action (when we add it) cannot.

---

## When Sources Disagree

This is the second-most-common RAG failure mode after hallucination. If your knowledge base has both *"Auth runs on PostgreSQL 13"* (from a 2-year-old runbook) and *"All services migrated to PostgreSQL 16"* (from a recent ADR), the LLM will either pick one at random or hallucinate a blend.

teammate detects contradictions heuristically:

```
**Two sources disagree on this:**

- `[docs/runbooks/auth-pg.md]` says: "Auth runs on PostgreSQL 13."
- `[decisions/2026-03-pg-upgrade.md]` says: "All services migrated to PG 16."

Resolve manually before acting.
```

Phase 1 (heuristic, default-on) catches direct numeric or named-entity contradictions. Phase 2 (LLM judge, opt-in) catches semantic ones. The point is to **surface the conflict** rather than smooth it over.

---

## Results

After ~3 months of running:

| Metric | Before | After |
|---|---|---|
| MTTR for "have we seen this?" incidents | 47 min | 12 min |
| Onboarding-to-first-PR | 6 weeks | 2 weeks (anecdotal) |
| Cost per engineer / month | $0 (Glean rejected) | $0 |
| Index size on laptop | — | 1.4 GB |
| Query latency P50 | — | 1.8 s |
| Query latency P99 | — | 4.2 s |
| Sources unified | 0 | 4 (Jira, Confluence, GitHub, Slack) |
| Documents indexed | 0 | 24,853 |

The MTTR number is the one I care about most. The 35-minute reduction isn't because the AI is smart. It's because **the AI doesn't have to play archaeologist**. The runbook is already in the brain. The Jira ticket is already in the brain. The Slack thread is already in the brain. The agent just needs to find them.

---

## What's Not in This Article

teammate has grown beyond local-first. In the next 6 articles I'll cover:

- **Part 2**: Real-time triggers via Slack Socket Mode — agents that run on team requests, not just cron
- **Part 3**: How I imported 25k documents from 4 sources, idempotently
- **Part 4**: Moving from per-pod SQLite to a k8s-native Qdrant deployment
- **Part 5**: MTTD-first design: similarity search over your incident corpus
- **Part 6**: War-rooms that aren't blank — auto-pre-loaded incident response
- **Part 7**: What worked, what I cut, what I'd do differently

If any of these match a problem you're solving right now, jump ahead — the OSS repo has the code today.

---

## Try It Yourself

```bash
pip install claude-teammate
teammate scaffold ~/team-brain --team-name "my-team"
cd ~/team-brain
teammate init
teammate ask "what does this team build?"
```

Source: https://github.com/JIUNG9/teammate

If you build something with it, I'd love to hear what worked and what didn't.

---

*This is part 1 of "Building teammate: a DevSecOps Brain." [Next: Slack Socket Mode for real-time Claude triggers →](./02-slack-socket-mode.md)*
