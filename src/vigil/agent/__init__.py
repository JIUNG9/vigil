"""Colleague-agent routines — judgment work the CI shape-checker can't do.

CI runs `vigil validate` on every push: deterministic, read-only, and
it answers "is this brain structurally sound?". The colleague agent
answers fuzzier questions: "is this orphan worth keeping?", "what should
this PR's migration plan look like for a human reviewer?", "what does the
team need to know about brain health this week?"

The split is intentional:

  CI (validate)        — deterministic. Same input, same output. Block-the-merge.
  Agent (this package) — judgment. Reads, classifies, drafts. Never mutates.

Each routine takes a :class:`RoutineConfig` and returns a
:class:`RoutineResult`. The agent never edits the brain — it stages drafts
and leaves the actual issue / Slack / PR-comment side effects to the
runner (Anthropic-cloud `/schedule`, self-hosted GitHub Actions, or a
local cron). This keeps the trust surface narrow: the agent has read-only
filesystem access; the runner is the only thing with scoped tokens.
"""

from __future__ import annotations

from vigil.agent.base import RoutineConfig, RoutineResult

__all__ = ["RoutineConfig", "RoutineResult"]
