# Series 7 — Building Vigil (formerly *teammate*)

> **2026-05-19 update**: the project formerly called *teammate* was renamed to **Vigil** at v5.0.0. The original 7-article series in this directory is preserved as a historical artifact of the v0.10 → v4.0.0 design. The current shipping content is the **consolidated 3-article series** under `nexus/medium-final/`.

## v5 (current — 3 consolidated articles)

| Part | Title | Approx words |
|---|---|---|
| 1 | **Building Vigil: the DevSecOps Command Center I built instead of buying PagerDuty + FireHydrant + Blameless** | ~2,000 |
| 2 | **Vigil MTTD: adaptive SigNoz watchlists, P0–P3 auto-classification, bidirectional Slack sync** | ~1,600 |
| 3 | **Vigil MTTR: the analysis workbench (no more war-room chat) — and a real Postgres-lock case study** | ~2,500 |

Plus a companion CS-fundamentals piece: **kube-proxy vs Istio — a CS-fundamentals comparison** (~1,700 words).

The consolidated articles are the live versions on Medium and the source of truth. The original 7-part series below documents the design history but the chat-centered framing of parts 1, 4, and 6 was explicitly cut in v5.

## v4 history (preserved — original 7-part series under *teammate*)

| Part | Title | Words |
|---|---|---|
| 1 | [Why I built a local-first brain instead of Glean / Notion AI](01-local-first-brain.md) | ~1,900 |
| 2 | [Real-time Claude triggers via Slack Socket Mode](02-slack-socket-mode.md) | ~2,200 |
| 3 | [Importing 25,000 documents from 4 sources, idempotently](03-importers-25k-docs.md) | ~2,900 |
| 4 | [From per-pod SQLite to k8s-native Qdrant](04-qdrant-and-chat-ui.md) | ~1,900 |
| 5 | [MTTD before MTTR: similarity search over your incident corpus](05-mttd-similarity-search.md) | ~1,300 |
| 6 | [War-rooms that aren't blank: auto-pre-loaded incident response](06-war-rooms-auto-preloaded.md) | ~1,700 |
| 7 | [Lessons from shipping an SRE assistant](07-lessons-retrospective.md) | ~1,700 |

## v5 reflection (post-cutover)

| Part | Title | Words |
|---|---|---|
| 8 | [One tool, two names: shipping an SRE assistant that is also open source](08-rename-and-cutover.md) | ~2,200 |

Part 8 is written from the v5 vantage point. It covers the dual-name decision (Vigil for OSS, paro for internal), the four phases of the cutover, what broke, and what it actually costs to run vs idle.

For the full architecture overview: [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md).
