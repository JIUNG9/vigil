---
adr_id: 0001
title: Adopt teammate as the team's brain
status: accepted
date: YYYY-MM-DD
---

# ADR-0001: Adopt teammate as the team's brain

## Context

Tribal knowledge was scattered across Slack DMs, outdated Confluence pages, and
private notebooks. Onboarding new engineers took weeks of "ask whoever's around."
Cloud-hosted shared workspaces (e.g., Claude Teamspace) are not an option for us
because we need data residency / cost control / sovereignty / [pick your reason].

## Decision

We adopt **teammate** as the team brain:

- This repository is the canonical source of truth for team knowledge.
- Each engineer queries it locally via Claude Code + a local LLM (Ollama).
- The repository is private but team-wide; CI builds a sqlite-vec index that
  every engineer can pull as a release artifact to skip local embedding.

## Consequences

- **+** Single source of truth for "how the team works."
- **+** Local-LLM Q&A means engineers stop pinging Slack for answers that are
  in the docs.
- **+** Onboarding velocity goes up because the brain is queryable from day 1.
- **-** Someone has to maintain the brain. Stale entries are worse than none.
  We assign quarterly review of `knowledge/services.md` and `knowledge/people.md`.
- **-** If the team isn't disciplined about writing things down, the brain
  decays. Mitigation: every PR in service repos that introduces a non-obvious
  decision should also open a small PR here adding an ADR.
