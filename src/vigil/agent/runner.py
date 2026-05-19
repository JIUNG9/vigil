"""Stub dispatcher for colleague-agent routines.

Real-world deployment: the Anthropic-cloud ``/schedule`` runner (or a
self-hosted GitHub Actions cron) reads a routines config like
``examples/agent-routines.json``, picks a routine name, and calls
:func:`run_routine`. It then takes the resulting artifact files and
distributes them — Slack messages, GitHub issues, PR comments —
using whatever scoped tokens the runner holds.

The agent itself does *none* of the distribution. That's why this
module is intentionally tiny: dispatch + return.

Registry (v0.10): 11 routines.

  v0.5:    weekly_digest, orphan_triage, pr_migration_plan
  v0.8:    confluence_sync, jira_sync, slack_sync, web_pull
  v0.10:   invalidation_digest, targeted_radar,
           pr_review_assist, auto_pr_drafter
"""

from __future__ import annotations

from collections.abc import Callable

from vigil.agent.auto_pr_drafter import run as _auto_pr_drafter_run
from vigil.agent.base import RoutineConfig, RoutineResult
from vigil.agent.confluence_sync import run as _confluence_sync_run
from vigil.agent.daily_digest import run as _daily_digest_run
from vigil.agent.invalidation_digest import run as _invalidation_digest_run
from vigil.agent.jira_sync import run as _jira_sync_run
from vigil.agent.orphan_triage import run as _orphan_triage_run
from vigil.agent.pr_migration_plan import run as _pr_migration_plan_run
from vigil.agent.pr_review_assist import run as _pr_review_assist_run
from vigil.agent.slack_sync import run as _slack_sync_run
from vigil.agent.targeted_radar import run as _targeted_radar_run
from vigil.agent.web_pull import run as _web_pull_run
from vigil.agent.weekly_digest import run as _weekly_digest_run

_REGISTRY: dict[str, Callable[[RoutineConfig], RoutineResult]] = {
    "weekly_digest": _weekly_digest_run,
    "orphan_triage": _orphan_triage_run,
    "pr_migration_plan": _pr_migration_plan_run,
    "confluence_sync": _confluence_sync_run,
    "jira_sync": _jira_sync_run,
    "slack_sync": _slack_sync_run,
    "web_pull": _web_pull_run,
    "invalidation_digest": _invalidation_digest_run,
    "targeted_radar": _targeted_radar_run,
    "pr_review_assist": _pr_review_assist_run,
    "auto_pr_drafter": _auto_pr_drafter_run,
    "daily_digest": _daily_digest_run,
}


def list_routines() -> list[str]:
    """Stable order — what `agent run` shows in --help."""
    return sorted(_REGISTRY.keys())


def run_routine(name: str, config: RoutineConfig) -> RoutineResult:
    """Dispatch to the named routine. Raises ``KeyError`` on unknown names."""
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown routine: {name!r}. Known: {', '.join(list_routines())}"
        )
    return _REGISTRY[name](config)


__all__ = ["list_routines", "run_routine"]
