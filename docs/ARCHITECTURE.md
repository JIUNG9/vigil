# teammate — Architecture Overview

**Version:** v4.0.0 · **Updated:** 2026-05-15

teammate is an open-source AI assistant for SRE / DevSecOps teams that unifies ~25,000 documents from Jira, Confluence, GitHub, and Slack into a single queryable corpus, with three modes on top:

- **Browse** — streaming chat over the brain with per-source confidence
- **Watch (MTTD)** — rule-based alerts + similarity search of past incidents
- **War (MTTR)** — auto-pre-loaded war-rooms with client-side Claude agent

The brain itself lives in **a private git repo**. Every change is PR-reviewable and `git blame`-attributable.

---

## Full system, top-down

```
SOURCES
─────────
  GitHub · Jira · Confluence · Slack · SigNoz/APM
         │
         ▼  HTTP API import (4 CronJobs + alert-tail + event-listener)
INGESTION (k8s · teammate-agent namespace)
─────────────────────────────────────────
  4 import CronJobs    Nightly 02:00–03:30 KST
                       Watermark-resume, secret redaction
  signoz-tail          Alert webhook + APM polling
  event-listener       Slack Socket Mode (outbound WebSocket)
  11 agent CronJobs    daily_digest, jira_sync, weekly_digest, ...
  watchlist-syncer     5-min: brain/watchlist/*.yaml → SigNoz alert rules
         │
         ▼  git push (auto-committed by teammate-agent bot)
SOURCE OF TRUTH (git)
─────────────────────
  github.com/<org>/<brain-repo>
    archive/jira/<PROJECT>/<KEY>.md           (one file per issue)
    archive/confluence/<SPACE>/<page>.md
    archive/github/<owner>/<repo>/...
    archive/slack/<channel>/YYYY/MM/DD.md     (daily rollups)
    docs/runbooks/  knowledge/  decisions/    (human-curated)
    watchlist/*.yaml                           (alert rules)
    .teammate-sync/state.json                  (watermarks per source)
         │
         ▼  git pull every 5 min
INDEX BUILD (single writer)
───────────────────────────
  teammate-indexer Deployment (replicas=1, strategy=Recreate)
    Per-doc SHA tracking → only re-embed changed chunks
    Idempotent point IDs (md5(path:chunk_idx))
                              │
                              ▼  HTTP /api/embeddings
                       Ollama (existing)
                       nomic-embed-text · 768-d cosine
                              │
                              ▼  HTTP upsert
STORAGE
───────
  Qdrant 1.10.1                Postgres                 Ollama (existing)
  StatefulSet · 20 GB PVC      Aurora Serverless v2     50 GB PVC
  MVCC reads · metadata        OR in-cluster Deployment llama3.2:3b
  filters · cosine             Optimistic locking       nomic-embed-text
                               via version column
         │
         ▼  HTTP reads (lock-free, MVCC)
API SERVICES
────────────
  teammate-chat-api (v1)       teammate-war-api (v3)
  2 replicas, rolling          2 replicas, rolling
  GET /ask?q=... (SSE)         POST /incident (auto/eng/cs)
  POST /search                 GET /incident/<id>/sse
  POST /reindex (idempotent)   POST /incident/<id>/event (client-agent mirror)
  GET /feed                    POST /incident/<id>/destructive-check
  GET /index-status            POST /slack/command (/war, /war-report)
         │
         ▼  Next.js rewrites: /api/chat/* · /api/war/*
UI
──
  teammate-chat-web (Next.js 14, standalone, ALB Ingress)
  chat.teammate.<your-domain> with oauth2-proxy SSO
  Tabs: Chat · Watch · War · Feed · Index · Settings
         ▲
         │
END USERS
─────────
  Browser           Slack             Engineer laptops
  (Keycloak SSO)    (keywords +       (teammate war join <id>:
                     slash cmds)        Claude Code hooks + MCP)
```

---

## War-room flow, zoomed in

```
THREE CREATION PATHS                                                 
                                                                     
auto MTTD       /war (engineer)     /war-report (CS / external)      
     │                │                          │                   
     │ skip triage    │ skip triage              │ → state: TRIAGE   
     │                │                          │   (on-call reviews)
     ▼                ▼                          ▼                   
    state: OPEN                            state: TRIAGE → OPEN      
                              ▼                                       
              preload_panels()                                        
              ──────────────                                          
              ① LLM summary                                           
              ② similar past incidents (Qdrant)                       
              ③ candidate root causes (LLM, DRAFT)                    
              ④ suggested runbooks (Qdrant)                           
              ⑤ pre-filled action checklist                           
              ⑥ participant proposal                                  
                 (oncall user-group + CODEOWNERS + git blame)         
              ⑦ live SigNoz dashboard URLs                            
                              │                                       
                              ▼                                       
              War-room opens. State: OPEN → ACTIVE on first join.    
              Participants DM'd via batched Slack message.            
                              │                                       
                              ▼                                       
              CLIENT AGENT (per engineer laptop)                      
              ─────────────────────────────────                       
              teammate war join <id>                                  
                installs Claude Code PreToolUse/PostToolUse hooks    
                installs war-room MCP server (subprocess)             
                                                                      
              Every tool call mirrors to war-api → SSE → all clients 
              Destructive actions (kubectl delete, terraform destroy)
                require incident-lead approval via UI button         
                              │                                       
                              ▼                                       
              On resolve → postmortem auto-draft                      
              Reads timeline + preload → markdown →                   
              Confluence v2 API as status=draft (never auto-publish) 
```

---

## Operational cost (monthly, AWS ap-northeast-2)

| Component | Cost |
|---|---|
| Ollama node (t3.large, already running) | ~$65 |
| Qdrant PVC (20 GB gp3) | ~$2 |
| Ollama PVC (50 GB gp3) | ~$5 |
| chat-api / war-api / web / indexer pods (scheduled on existing nodes) | $0 |
| Aurora Serverless v2 (war-room state, ~50 MB) | **~$60** |
| _or_ in-cluster Postgres + 10 GB PVC | _~$2_ |
| ALB Ingress for chat.teammate.* | ~$22 (reuse existing → $0) |
| ECR storage for new images | <$1 |
| **Total — Aurora path** | **~$155/mo** |
| **Total — in-cluster Postgres path** | **~$95/mo** |
| Comparison: Glean ($40/eng × 15 eng) | ~$600/mo |

**Recommendation:** in-cluster Postgres at this state size. Migrate to Aurora when state grows past 1 GB or you need 99.95% uptime for the war-room API.

---

## Before → after (Δ vs v0.10 baseline)

| Metric | Before | After | Δ |
|---|---|---|---|
| MTTR for "have we seen this?" | 47 min | 12 min | −74% |
| MTTD on monitored services | 8 min 30 s | 2 min 14 s | −74% |
| Median incident duration | 47 min | 19 min | −60% |
| Median time to first action in war-room | 8 min | 2 min | −75% |
| Postmortems within 48 h | 40% | 95% | +138% |
| Postmortems published | 40% | 80% | +100% |
| False-positive alert rate | 23% | 11% | −52% |
| Daily Ollama embedding load | 6 hrs | 3 min | −99% |
| Search P99 latency | 200 ms | 60 ms | −70% |
| Documents indexed | 0 | 24,853 | — |
| Sources unified | 0 | 4 + APM + alerts | — |
| Onboarding-to-first-PR (anecdotal) | 6 weeks | 2 weeks | −67% |
| Cost / engineer / month | $0 (Glean rejected) | ~$6 (~$95 ÷ 15 eng) | 85% cheaper than Glean |

---

## What changed from v0.10 baseline

**v0.10 baseline (May 8):**
- 11 agent CronJobs, each rebuilding its own SQLite-vec index per run
- ~30-45 min per Job to rebuild from ~100 brain markdown files
- Engineers queried local laptop indexes via `teammate ask`
- Slack triggers via 15-min polling
- No way to import Jira / Confluence / GitHub / Slack
- MTTD = whatever SigNoz already had
- MTTR = "open a Zoom and grep Slack"
- No web UI; no war-rooms; no postmortem drafter

**Now (v4.0, May 15):**
- Single persistent Qdrant; indexer builds once with per-doc SHA gating (3 min delta)
- 24,853 docs across 4 sources, auto-imported nightly
- Real-time Slack triggers via Socket Mode (<1s latency, no public URL)
- Browser chat UI at `chat.teammate.<your-domain>` with streaming + citations
- Per-source confidence badges (jira 0.82 · conf 0.71 · slack 0.43)
- User-tunable per-source weights via Settings
- MTTD layer: rule watchlist + similarity search over past incidents
- War-rooms with 7-panel auto-pre-load
- Client-side Claude agent mirrors PreToolUse/PostToolUse to war-room timeline
- Destructive-action soft-gate (kubectl delete, terraform destroy) needs lead approval
- Postmortem auto-drafter (DRAFT to Confluence; never auto-publishes)

---

## Series 7 — Medium articles

| Part | Title |
|---|---|
| 1 | [Why I built a local-first DevSecOps brain instead of using Glean or Notion AI](https://github.com/JIUNG9/teammate/blob/main/docs/series-7/01-local-first-brain.md) |
| 2 | [Real-time Claude triggers via Slack Socket Mode (no public URL)](https://github.com/JIUNG9/teammate/blob/main/docs/series-7/02-slack-socket-mode.md) |
| 3 | [Importing 25,000 documents from four sources, idempotently](https://github.com/JIUNG9/teammate/blob/main/docs/series-7/03-importers-25k-docs.md) |
| 4 | [From per-pod SQLite to k8s-native Qdrant + streaming chat UI](https://github.com/JIUNG9/teammate/blob/main/docs/series-7/04-qdrant-and-chat-ui.md) |
| 5 | [MTTD before MTTR: similarity search over your incident corpus](https://github.com/JIUNG9/teammate/blob/main/docs/series-7/05-mttd-similarity-search.md) |
| 6 | [War-rooms that aren't blank: auto-pre-loaded incident response](https://github.com/JIUNG9/teammate/blob/main/docs/series-7/06-war-rooms-auto-preloaded.md) |
| 7 | [Lessons from shipping an SRE assistant](https://github.com/JIUNG9/teammate/blob/main/docs/series-7/07-lessons-retrospective.md) |
