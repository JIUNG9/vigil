# MTTD Before MTTR: Similarity Search Over Your Incident Corpus

**Tags:** `SRE` `MTTD` `Incident Response` `Vector Search` `Observability`

---

> Part 5 of "Building teammate." Why we deliberately did NOT build LLM anomaly prediction, and built two simpler tiers instead — rule-based watchlists and similarity search over past incidents. The high-signal half of MTTD.

---

## The Industry's MTTD Mistake

Every observability vendor that lasted more than 3 years has shipped some flavor of "AI anomaly detection." Most have quietly retired it. Datadog, PagerDuty, BigPanda — all have stories of features that promised "we'll predict your incidents before they happen" and ended up either off by default or in a "experimental" tab nobody clicks.

The pattern I've seen repeatedly: an LLM-driven incident-pattern predictor has a 60-80% false-positive rate. Engineers get paged, see a prediction that doesn't match reality, lose trust by the third false-positive, and disable the feature. Within 6 months it's effectively dead code with a non-trivial Kubernetes footprint.

I told the team I'd build this differently.

---

## What I Built: Two Tiers, Both Mundane, Both Reliable

### Tier 1: Rule layer

YAML-defined alert rules in the brain repo, synced to SigNoz.

```yaml
# brain-repo/watchlist/rds.yaml
- alert: dp-prod-rds-cpu-high
  expr: avg(cpu_pct{service="dp-prod-rds"}) > 80
  for: 5m
  severity: high
  owner: '@oncall-devsecops'
  runbooks: [rds-cost-spike, db-cpu-triage]
  description: |
    RDS CPU > 80% for 5 min on dp-prod-rds. Often a precursor to
    user-facing 5xx within 7-10 minutes. See runbook for triage steps.

- alert: dp-prod-rds-replica-lag
  expr: max(pg_replication_lag_seconds{service="dp-prod-rds"}) > 5
  for: 3m
  severity: high
  owner: '@oncall-devsecops'
  runbooks: [auth-replica-lag, db-failover]
```

A nightly CronJob (`teammate watchlist-sync`) reconciles this YAML to SigNoz's alert rule API. Add or remove rules via PR; the reconciler applies them on next run.

Nothing AI here. Just a more honest version of "alerts as code" — versioned, reviewed, documented inline.

### Tier 2: Similarity layer

When an alert fires (or an engineer manually declares an incident), embed the symptom and search the past-incident corpus:

```python
class SimilarityLayer:
    def find_similar(self, symptom: str, top_k: int = 3, score_floor: float = 0.6):
        # Embed the symptom via Ollama
        vec = embed(symptom)
        # Qdrant search, filtered to past incidents only
        hits = qdrant.search(
            collection="brain",
            vector=vec,
            limit=top_k * 3,
            filter={"should": [{"key": "path", "match": {"text": f"archive/jira/{p}/"}}
                              for p in INCIDENT_PROJECTS]},
        )
        # Dedupe by doc (multiple chunks per doc), apply score floor
        return [dedupe_and_rank(hits, top_k, score_floor)]
```

The symptom is just a string:

```
dp-prod-rds CPU 88% for 7m, log: "idle in transaction", deploy PN-1834 2h ago
```

The output is a list of similar past incidents:

```
INFRA-2391 (score 0.89) — dp-prod-rds storage exhaustion (5/12) — resolved by rolling back PN-1834
INCD-1102 (score 0.74)  — auth-server replica cascade (3/14)    — resolved by failover
INCD-988  (score 0.61)  — nw-prod long-lived transaction (1/02) — resolved by kill connection
```

This is the high-signal piece. It doesn't claim to predict anything. It just says "we've seen something like this before; here's what we did."

---

## Why Similarity Works Where Prediction Doesn't

The fundamental difference:

| | Prediction (LLM) | Similarity search |
|---|---|---|
| **Claim being made** | "This metric pattern WILL cause an incident" | "This metric pattern LOOKS LIKE past incidents" |
| **Failure mode of bad output** | Wrongly pages on-call → alert fatigue | Surfaces irrelevant past incidents → mild noise |
| **Engineer trust** | Erodes fast on first miss | Tolerant — they can dismiss without consequence |
| **Validation** | Needs ground truth (labeled incidents) | Self-evident from cosine score |
| **Cold-start** | Bad until trained on N incidents | Works the day after you import incidents |

Similarity search has been hiding in plain sight as a "duh" feature. The reason most observability vendors haven't shipped it well is that it requires your past incidents to be ingested as queryable text — which is exactly what the four-source importer pipeline (part 3) made possible.

teammate ingests `archive/jira/INCD/` and `archive/jira/INFRA/` as part of the regular nightly sync. Once they're in Qdrant, similarity search is a 50-line module.

---

## The Watch Tab UI

The Watch tab in the chat UI surfaces three things:

```
┌─ Active watch rules (47) ─────────────────────────────┐
│  fired last 24h: 9                                     │
│  false-positive rate: 11%  (target ≤ 15%)             │
│  avg MTTD: 2m 14s          (↓38s vs last week)        │
└────────────────────────────────────────────────────────┘

┌─ Recommendations (3 pending review) ──────────────────┐
│                                                        │
│  Watch: auth-server reader-replica lag > 5s for 3min  │
│  confidence 0.84 — based on INFRA-2391, INCD-1102,    │
│                    INCD-988                            │
│  [✓ Approve & sync to SigNoz]  [Edit YAML]  [Reject]  │
│                                                        │
│  Watch: nw-prod-api heap usage > 80% for 5min         │
│  confidence 0.71 — based on INCD-1077                 │
│  [✓ Approve]  [Edit]  [Reject]                        │
│                                                        │
│  Watch: dp-prod-rds autoscale storage > 90% ceiling   │
│  confidence 0.92 — based on INFRA-2391                │
│  [✓ Approve]  [Edit]  [Reject]                        │
└────────────────────────────────────────────────────────┘

┌─ Find similar past incidents ─────────────────────────┐
│  [textarea — paste symptom, alert text, log lines]    │
│  [Search past incidents]                              │
│                                                        │
│  Top 3 matches:                                       │
│  INFRA-2391 (0.89) — dp-prod-rds storage exhaustion   │
│  INCD-1102  (0.74) — auth-server replica cascade      │
│  INCD-988   (0.61) — nw-prod long-lived transaction   │
└────────────────────────────────────────────────────────┘
```

Recommendations are LLM-generated proposals based on past incidents that lack a corresponding watch rule. They're **always human-in-the-loop** — the YAML doesn't reach SigNoz unless a human clicks Approve. This is the human gate that prevents the failure modes of the auto-prediction approach.

---

## What Pattern Layer Looks Like (And Why It's Optional)

For completeness, the OSS repo has a tier 3 pattern layer:

```python
# brain-repo/.teammate-sync/patterns.json (LLM-generated, human-approved)
{
  "patterns": [
    {
      "id": "P-014",
      "name": "RDS storage exhaustion preceded by autoscale ceiling hit",
      "precursor_signature": "...embedded vector...",
      "evidence_incidents": ["INFRA-2391", "INFRA-1788"],
      "confidence": 0.78,
      "approved": true,
      "approved_by": "alice"
    }
  ]
}
```

It's behind a feature flag (`MTTD_PATTERN_LAYER_ENABLED=false` by default). When enabled, it runs the LLM clusterer on the past-incident corpus weekly and proposes patterns for human approval. Approved patterns are matched against current metrics.

I have not turned it on in production. The 0.92-confidence rule-based recommendations from tier 2 cover the cases where the pattern layer would help anyway, with much cleaner accountability.

---

## Results

After 30 days of MTTD layer running:

| Metric | Before | After |
|---|---|---|
| Avg MTTD on high-severity incidents | 8 min 30 s | 2 min 14 s |
| False-positive rate | 23% (legacy SigNoz rules) | 11% (curated watchlist) |
| New watch rules added (via approval workflow) | 0/month (manual SigNoz edits, nobody bothered) | 8/month |
| Engineer interactions with similarity search | n/a | ~12/day across the team |
| LLM-predicted incidents (tier 3) | n/a | feature flag off |

The 6+ minute MTTD reduction is mostly from getting alerts ON things that previously had no rule — not from making existing rules faster. Similarity search drove the recommendations that filled the gaps.

---

## What I'd Do Differently

1. **Don't ship the pattern layer unless engineers ask for it.** I shipped it. Nobody enables it. The feature flag is sufficient documentation of intent.

2. **Make the recommendation workflow opt-in per rule.** Some teams want auto-apply for low-severity rules. Right now it's all-or-nothing manual approval.

3. **The "find similar past incidents" search deserves its own keyboard shortcut.** Engineers ask it constantly during active incidents; navigating to the Watch tab is friction.

---

## Try It Yourself

```bash
# Define your watchlist
mkdir -p brain/watchlist
cat > brain/watchlist/example.yaml <<EOF
- alert: my-service-error-rate
  expr: rate(http_5xx[5m]) / rate(http_total[5m]) > 0.01
  for: 2m
  severity: high
  owner: '@oncall'
  runbooks: [my-runbook]
EOF
git -C brain add . && git -C brain commit -m "add watchlist"

# Sync to SigNoz
export SIGNOZ_API_URL=https://signoz.your-domain.net
export SIGNOZ_API_TOKEN=...
teammate mttd sync-watchlist

# Similarity search
teammate mttd find-similar "auth-server returning 5xx after recent deploy"
```

OSS source: https://github.com/JIUNG9/teammate/tree/main/src/teammate/mttd

---

*Part 5 of "Building teammate." [← Part 4: Qdrant + chat UI](./04-qdrant-and-chat-ui.md) · [Next: War-rooms that aren't blank →](./06-war-rooms-auto-preloaded.md)*
