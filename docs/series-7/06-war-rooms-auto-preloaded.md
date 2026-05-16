# War-Rooms That Aren't Blank: Auto-Pre-Loaded Incident Response

**Tags:** `SRE` `MTTR` `Incident Response` `Claude Code` `Server-Sent Events` `MCP`

---

> Part 6 of "Building teammate." The hardest part of an incident isn't fixing it — it's deciding what to look at first. teammate war-rooms open with 7 panels already populated: similar past incidents, candidate root causes, suggested runbooks, action checklist, participant proposal, live data, summary. Plus a client-side agent that mirrors every engineer's Claude Code actions to the war-room in real time.

---

## The Blank Page Problem

War-rooms — whether Slack channels, Zoom calls, or PagerDuty incident pages — almost always start the same way:

1. Alert fires.
2. On-call joins the war-room.
3. "Have we seen this before?"
4. Silence.
5. Someone grep-searches Slack.
6. Someone opens 4 Confluence tabs.
7. 10 minutes in, you finally have context.

The reason this happens isn't that engineers are slow. It's that **the war-room itself contains zero context** at the moment it opens. Every responder starts archaeology from scratch.

teammate flips this. By the time the war-room exists in the UI, it's already populated with 7 panels of pre-computed context. The on-call engineer's first 10 minutes go to *responding*, not *searching*.

---

## The 7 Panels

When an incident is created (regardless of source — auto MTTD, engineer /war, CS /war-report), a preload pipeline runs:

```python
def preload_panels(incident):
    similar = similarity.find_similar(symptom, top_k=3)
    runbooks = _suggest_runbooks(symptom)
    actions = _derive_actions(similar)
    participants = propose_participants(incident)
    live_urls = _build_signoz_urls(incident)
    summary, causes = _llm_synthesize(incident, similar, runbooks)

    return PreloadResult(
        incident_id=incident.id,
        summary=summary,
        similar_incidents=[asdict(s) for s in similar],
        candidate_causes=causes,
        runbooks=runbooks,
        actions=actions,
        participants=participants,
        live_data_urls=live_urls,
    )
```

The result is stored in Postgres and surfaced in the war-room UI:

| Panel | What it shows | How it's computed |
|---|---|---|
| ① Summary | LLM-generated 2-sentence incident summary | Ollama, grounded in similar incidents |
| ② Similar past incidents | Top 3 with similarity scores + resolution notes | Qdrant search over `archive/jira/INCD/` |
| ③ Candidate root causes | LLM-ranked, all marked DRAFT | LLM analysis + similar-incident resolutions |
| ④ Suggested runbooks | Top 3 from `docs/runbooks/` | Qdrant search filtered to runbook path prefix |
| ⑤ Action checklist | Pre-filled from baseline + similar-incident resolutions | Static baseline + derived from panel ② |
| ⑥ Participant proposal | On-call user-group + CODEOWNERS + recent committers | Slack API + brain CODEOWNERS + git blame |
| ⑦ Live data URLs | SigNoz dashboard pre-zoomed to incident window | URL template + service name |

Panels ②, ④, ⑥ are deterministic. Panels ①, ③ involve LLM and are marked DRAFT.

---

## Three Creation Paths, One Pipeline

The same preload runs regardless of how the incident was declared:

```
AUTO ─┐                                       ┌─► OPEN ──► ACTIVE
       │                                       │   (engineer skips
ENG  ──┤── POST /incident ──► preload_panels ──┤    triage; DMs out;
       │                                       │    client-agents on)
CS   ──┘                                       └─► TRIAGE ──► OPEN
                                                   (on-call confirms
                                                    or dismisses)
```

The triage gate is important. CS-reported incidents (`/war-report "checkout 결제 안 됨"`) shouldn't auto-page on-call — they're customer reports, not telemetry. They land in a triage queue. On-call reviews → confirms (state moves to OPEN) or dismisses (state moves to DISMISSED, logged for false-positive analysis).

Engineer-declared incidents skip triage. They go straight to OPEN, and the on-call's first job is to confirm the participant proposal before DMs go out.

---

## Participant Selection

The hardest part of participant selection is **not over-paging**. The naive approach — DM the whole on-call rotation — is the fastest path to alert fatigue.

teammate's selector ranks candidates:

```python
def propose_participants(incident):
    candidates = []
    # 1. Current on-call (Slack user-group)
    for u in slack_usergroup_members("@oncall-devsecops"):
        candidates.append({"user": u, "source": "oncall", "priority": 1})

    # 2. CODEOWNERS for the affected service
    if incident.affected_service:
        for u in codeowners_for(incident.affected_service):
            candidates.append({"user": u, "source": "codeowners", "priority": 2})

    # 3. Recent committers (last 14 days) on the service
    for u in recent_committers(incident.affected_service, days=14):
        candidates.append({"user": u, "source": "git-blame", "priority": 3})

    return candidates[:8]  # Cap to prevent notification spam
```

The output is a proposal — the engineer who declared the incident can edit it before DMs go out. The DMs themselves are **batched**: one Slack message in a thread, mentioning `@user1 @user2 @user3 ...`, with a single "join war-room" link. Not N separate DMs.

---

## The Client-Side Agent: Mirror + Mediate

This is the part of the design that took the longest to land. Originally I built war-rooms as just "Postgres rows + an SSE UI." Engineers' work happened in their own terminals, invisible to anyone else in the room. The war-room had a chat box but nobody used it.

The pivot: every engineer in a war-room runs `teammate war join <incident>`. That CLI installs Claude Code hooks and an MCP server on their laptop for the duration of the incident.

```
Engineer's Claude Code session
       │
       │ every tool call
       ▼
PreToolUse hook ──HTTPS POST──► war-api /event
       │                          │
       │                          ├─► Postgres incident_events (audit)
       │                          ├─► SSE fanout to all subscribers
       │                          └─► (if destructive: lead approval gate)
       ▼
Tool runs (or blocks)
       │
       ▼
PostToolUse hook ──HTTPS POST──► war-api /event (with result)
       │
       ▼
Claude continues
```

The war-room UI shows a live timeline that interleaves:

- Chat messages from participants
- New alerts from SigNoz
- **Every engineer's Claude tool calls, in real time**
- Action checklist toggles
- State changes

Suddenly the war-room knows what each engineer is *doing*, not just what they're saying. When alice's Claude runs `kubectl get pods`, jiung's Claude doesn't need to run it separately — the result is already on the screen.

### Mediation via MCP

The client agent also adds a war-room MCP server to the engineer's Claude Code:

```python
# Pseudo-MCP server, runs as subprocess for duration of war-room session
tools = {
    "warroom_context": lambda: get_current_incident_state(incident_id),
    "warroom_post": lambda msg: post_event(incident_id, msg),
    "warroom_suggest_runbook": lambda symptom: similarity_search(symptom, kind="runbook"),
}
```

When the engineer asks Claude "what's the procedure for rolling back PN-1834?", Claude can call `mcp__teammate_war__warroom_context()` to get the current incident state, then `mcp__teammate_war__warroom_suggest_runbook(...)` to find relevant docs — all without leaving the conversation.

The brain isn't somewhere over there. It's a tool Claude reaches for as naturally as `Bash` or `Read`.

### The destructive-action soft gate

The hooks have one more responsibility. Before a destructive action runs — `kubectl delete`, `terraform destroy`, `aws s3 rm`, `git push --force` — the PreToolUse hook checks with the war-api:

```python
DESTRUCTIVE_PATTERNS = [
    "kubectl delete",
    "terraform destroy",
    "aws s3 rm",
    "aws iam delete",
    "aws rds delete",
    "git push --force",
    # ...
]

if _is_destructive(cmd):
    if not _approved(war_url, incident_id, user, cmd):
        print("⚠️ BLOCKED: requires incident-lead approval", file=sys.stderr)
        return 1
```

The incident lead sees the pending request in the war-room UI:

> 🔴 **jiung's Claude wants to run**: `kubectl delete pod dp-prod-rds-1`
> [Approve] [Deny]

If approved, the original tool call succeeds. If denied, the engineer sees the block message and can ask in the war-room chat for clarification.

This isn't about distrust. It's about **two pairs of eyes on high-blast-radius operations during a stressful situation**, with zero friction outside the war-room context.

---

## Postmortem Drafter

When the incident resolves, a final pipeline runs:

```python
def draft_postmortem(incident_id):
    timeline = fetch_timeline(incident_id)      # from incident_events
    preload = fetch_preload(incident_id)        # from incident_preload
    markdown = compose_markdown(incident_id, timeline, preload)
    confluence_page_id = push_to_confluence(incident_id, markdown)  # as DRAFT
    return {"status": "drafted", "confluence_page_id": confluence_page_id}
```

The output is a DRAFT Confluence page under the team's postmortem parent. It contains:

- Summary (from preload panel ①)
- Timeline (every event from `incident_events`)
- Candidate root causes (panel ③, marked DRAFT)
- Action items (from the checklist, with completion state)
- Lessons section (empty, "please fill in")
- Related incidents (from panel ②)

The page is **never auto-published**. The drafter sets `status: draft` in the Confluence v2 API. An engineer reviews, edits the Lessons section, possibly corrects the LLM-drafted root causes, then explicitly publishes.

The LLM is wrong often enough about root causes that auto-publishing would erode trust quickly. But the drafter saves the 60-90 minutes of mechanical work — gathering the timeline, formatting markdown, listing similar incidents — that nobody enjoys doing the morning after a 2 AM incident.

---

## Results

After 30 days running:

| Metric | Before | After |
|---|---|---|
| Median time to first action in an incident | 8 min | 2 min |
| Median incident duration | 47 min | 19 min |
| Number of `kubectl get pods` runs per incident (across all engineers) | 8 | 2 (deduplicated via mirror) |
| Destructive actions blocked by soft-gate | n/a | 4 (all subsequently approved by lead) |
| Postmortems written within 48 hours | 40% | 95% |
| Postmortems published (vs drafted-and-abandoned) | 40% | 80% |

The destructive-gate block number is interesting. 4 blocks in 30 days, all eventually approved. None were "stop, this is wrong" — all were "yes, do it, here's why." The value isn't in the blocks themselves; it's in the **30-second pause** that forces a second look before a high-blast-radius command runs.

---

## What I'd Do Differently

1. **Build the client agent on day 1 of v3.** I shipped the war-room UI without it; engineers used the chat box once and went back to their terminals. The agent is what made war-rooms feel collaborative.

2. **Default the destructive list shorter.** I started with 12 patterns; engineers complained about the false-positive blocks on routine `kubectl delete pod` operations. Cut to 6. Better to miss a block than to train engineers to ignore blocks.

3. **Mark LLM outputs as DRAFT visually, with color.** Black text labeled "DRAFT" gets read as truth. Yellow background with a "DRAFT" badge gets read as a draft. Same words, different cognitive weight.

---

## Try It Yourself

```bash
# Install the client agent
pipx install claude-teammate-client

# Join an incident
eval "$(teammate war join INC-1234)"

# Now every Claude Code action mirrors to the war-room.
# Other participants see your Bash/Edit/Read calls live.
# Destructive actions need lead approval.

# When done
teammate war leave
```

War-room API source: https://github.com/JIUNG9/teammate/tree/main/src/teammate/war

Client agent source: https://github.com/JIUNG9/teammate/tree/main/src/teammate/client_agent

---

*Part 6 of "Building teammate." [← Part 5: MTTD similarity search](./05-mttd-similarity-search.md) · [Next: Lessons from shipping →](./07-lessons-retrospective.md)*
