# Lessons From Shipping an SRE Assistant

**Tags:** `SRE` `Retrospective` `AI Agents` `Open Source` `Lessons Learned`

---

> Part 7 (final) of "Building teammate." A retrospective on the 7-day push that took teammate from "11 cron routines" to "fully-featured SRE assistant with war-rooms, MTTD layer, and a client-side agent that mirrors Claude Code actions." What worked, what I cut, what I'd do differently if I started today.

---

## The Compressed Timeline

```
2026-05-08   Brain repo baseline. 11 cron routines.
2026-05-11   v0.11: Slack Socket Mode listener (real-time triggers)
2026-05-12   v0.11.2: daily_digest routine, strict-quiet notifications
2026-05-13   v0.12: 4-source importers (24,853 docs)
2026-05-14   Resumable watermark fix (Confluence backfill survives timeouts)
2026-05-15   v1 + v2 + v3 + v4 all ship: Qdrant + chat UI + MTTD + war-rooms + client-agent + postmortems
```

That last day is the unrealistic-looking one. Realistically, v1-v4 take 4-6 weeks of focused work in production. The point of compressing it here is to **show the architecture as a coherent whole** — every component designed to fit a slot the next component will need.

---

## What Worked

### 1. The "brain as a git repo" decision was the right one

Nine months in, I haven't regretted it once. Engineers trust `git log`. They review brain changes in PRs the same way they review code. The audit trail is automatic. The disaster recovery story is "git is the backup."

The boring choice was the right one.

### 2. Local-first as a constraint, not a feature

I never marketed teammate as "the offline AI." But by designing for local-first — sqlite-vec on a laptop, Ollama on a laptop, no cloud dependencies — I was forced to keep the moving parts minimal. When we moved to Qdrant + cluster Ollama in v1, the local-first code path stayed as a fallback. Engineers can still use teammate on a plane.

Constraints clarify designs.

### 3. Slack Socket Mode beat Events API

The decision to use outbound WebSocket instead of inbound webhook saved us:

- Zero public attack surface for the agent triggers
- No DNS / cert-manager / ALB config to maintain
- No "Slack's IP ranges changed" outages

Single-replica + heartbeat liveness handles disconnects fine.

### 4. Resumable watermarks every 100 items

This was the small line in `base.py` that made the importers production-grade:

```python
if written_or_skipped - last_checkpoint_at >= CHECKPOINT_EVERY:
    state.setdefault(self.source_name, {})["watermark"] = str(max_watermark)
    self._save_state(state)
    last_checkpoint_at = written_or_skipped
```

Without it, the Confluence backfill (5,108 pages, 47+ minutes) failed forever in a 1-hour timeout loop. With it, a killed run loses at most 100 items.

The 1.5 hours I spent debugging the timeout taught me to bake checkpointing into every long-running batch job from day 1.

### 5. Strict-quiet notifications

I shipped verbose-mode first (`✅` reaction + thread reply per Job). The team's `#devops` channel was 60% bot replies by day 3. I rolled it back to "lifecycle only" — exactly one message on listener startup, exactly one on fatal disconnect, silence in between.

Engineer trust *went up*. Because when the bot does message you, it means something is actually broken.

> Counter-intuitive: for tools engineers live with daily, **fewer notifications = more trust**.

### 6. K8s itself as the concurrency primitive

I never deployed Redis, Postgres advisory locks, or Zookeeper for incident dedup, Job dedup, or rebuild dedup. The pattern was always:

```python
jobs = batch.list_namespaced_job(NS, label_selector="teammate-routine=reindex")
active = [j for j in jobs.items if j.status.active and j.status.active > 0]
if active:
    return existing_job
# else: create new
```

K8s API is the source of truth for "is this thing running?" — and the source of truth is also the lock.

### 7. The 7-panel war-room preload

The conceptual shift from "war-room is an empty room you walk into" to "war-room is a briefing already in progress when you walk in" was the single biggest UX win in the whole project. Median time-to-first-action dropped from 8 min to 2 min.

---

## What I Cut

### LLM-driven incident pattern prediction (tier 3 MTTD)

I built it. It's behind a feature flag (`MTTD_PATTERN_LAYER_ENABLED=false`). I never turned it on.

The industry pattern is clear: every observability vendor that shipped "AI predicts your incidents" has retired it within 24 months due to false-positive fatigue. Datadog, PagerDuty, BigPanda — all have the same story.

Similarity search over the past-incident corpus is the high-signal half. Pattern *prediction* is the dangerous half. Until I have a clear validation loop (which I don't), it stays off.

### Per-job Slack thread replies during incidents

The earliest war-room design had teammate auto-posting status updates every 10 minutes during an active incident:

> "🕐 incident-1834 — 10 min elapsed. CPU still > 80%. 3 participants active. Recent action: alice ran kubectl rollout undo."

This sounded useful. It was terrible. Engineers tuned out the messages by the 3rd one. The 10-minute interval was always wrong — too noisy if the incident was active, too quiet if engineers were stuck on a phone call.

I replaced it with **event-driven updates only**: a message arrives only when something *meaningful* changes (new alert, deploy detected, action checked off, participant joined). Same channel, ~6× less volume, much higher signal.

### Daily 10-minute postmortem-progress check-ins

Same lesson. Cut.

### A standalone Next.js app for the chat UI

I scoped this in. But after evaluating, the right place for the chat UI is **embedded as a tab in the existing DevSecOps dashboard** the team already runs. A standalone app would be its own login flow, its own deploy pipeline, its own page-load time. Embedded saves all of that.

The standalone web app remains in the OSS repo as `examples/web-app/` for teams that don't have an existing dashboard.

---

## What I'd Do Differently

### 1. Build the client-agent on day 1 of war-rooms, not last

I shipped the war-room UI (timeline, chat, action checklist) first, then added the client-agent two days later. For the first 48 hours, war-rooms were "yet another Slack thread." Engineers worked in their own terminals; nothing about the room reflected what was actually happening.

The client-agent — mirror + mediate via Claude Code hooks + MCP — is what made the war-room feel like a collaborative space. Should have shipped it as v3 phase 1, not phase 5.

### 2. Make LLM outputs visually weighted

Black text labeled "DRAFT" gets read as truth. Yellow-background blocks labeled "DRAFT" get read as drafts. Same words, different cognitive weight.

If I were starting over, every LLM-generated output (root cause candidates, postmortem drafts, watch-list recommendations) would have a colored border and an explicit "Generated by LLM — review required" badge. The current "DRAFT" text label is too easy to ignore.

### 3. Start with single-replica Postgres, not Aurora

I planned for Aurora from the start (multi-AZ, automated backups, 99.95% SLA). For the actual data volume (~50 MB of incident state, ~500k events), a single-replica Postgres Deployment with a 20 GB PVC would have been fine. Move to Aurora when there's actual load.

Premature production-grading slowed v3 by a day. Should have shipped with vanilla Postgres first.

### 4. Build the OSS sanitizer step as a git pre-push hook

I have a sanitization checklist (no `your-org.atlassian.net`, no real account IDs, no real channel names). I run it manually before each OSS push.

A git pre-push hook on the OSS repo would have caught the two times I almost pushed an internal email address. Should have automated this from session 1.

### 5. Treat the article series as a first-class deliverable

I wrote the articles after the code. For most of them, that worked — the code came first, articles documented what already existed. But for articles 5 (MTTD) and 6 (war-rooms), I caught design issues *while writing the article*. If I'd written the article (or at least a detailed outline) before coding, I'd have caught these earlier.

Write the article first. Then implement.

---

## Numbers Across the Whole Project

| | Before | After |
|---|---|---|
| Avg MTTR for "have we seen this?" incidents | 47 min | 12 min |
| Avg MTTD for monitored services | 8 min 30 s | 2 min 14 s |
| Median incident duration | 47 min | 19 min |
| Daily 1-on-1 questions about "where's the runbook for X?" | ~12 | ~2 |
| Onboarding-to-first-PR (anecdotal) | 6 weeks | 2 weeks |
| Per-pod index rebuild time | 30-45 min | 0 (read from Qdrant) |
| Postmortems written within 48 hours | 40% | 95% |
| Engineer complaints about bot noise | "yes, lots" | "zero, post strict-quiet" |
| Cost per engineer per month | $0 | $0 (vs $40 Glean) |

---

## What's Next

teammate v4 is the last planned milestone. The roadmap from here is governance, not features:

- Multi-tenancy (multiple teams sharing one cluster but separate brains)
- Per-team RBAC inside the chat-api
- Federated similarity search across brain instances (with consent gates)
- Mobile-friendly war-room UI

But these aren't planned for me. The OSS repo is the artifact. If you're at a team like mine — pre-Glean budget, post-Notion, sitting on years of scattered Jira/Confluence/Slack/GitHub knowledge — fork it and run.

---

## Try It Yourself

```bash
pip install 'claude-teammate[all]'
teammate scaffold ~/team-brain --team-name "my-team"
cd ~/team-brain && teammate init
teammate import all
teammate ask "what is our deploy procedure?"
```

```bash
# For the cluster path
helm upgrade --install qdrant qdrant/qdrant -n teammate-agent
kubectl apply -f examples/k8s/chat-api/deployment.yaml
# ... and so on
```

OSS: https://github.com/JIUNG9/teammate

---

## Acknowledgments

To everyone who let me ship in their channels, took my pre-strict-quiet noise without complaint, and pointed out the auto-pre-load idea over a coffee that became the war-room design — thank you.

To the SRE community that's been carrying the "share runbooks better" problem for a decade — I hope this is one more step.

---

*Part 7 of "Building teammate" — series complete. [← Part 6: War-rooms](./06-war-rooms-auto-preloaded.md) · [Series index](./README.md)*
