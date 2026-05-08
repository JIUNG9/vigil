"""Shared dataclasses for colleague-agent routines.

A routine takes a :class:`RoutineConfig`, does its read-only work against
the brain, and returns a :class:`RoutineResult` that the runner consumes.

Frozen dataclasses on purpose — routines are pure functions of their
inputs. The runner is what carries mutable state (open issues, posted
Slack messages, scoped tokens).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Status vocabulary. Kept tiny so a downstream runner can switch on it
# without parsing a free-form string.
OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class RoutineConfig:
    """Per-invocation configuration for a routine.

    Attributes:
        brain_root:   read-only filesystem root the routine scans.
        out_dir:      where the routine drops its report markdown. Created
                      if missing. The runner is responsible for any further
                      distribution (Slack, GitHub issues, PR comments).
        dry_run:      default ``True``. The routine still writes its report
                      file — that's how the runner picks it up — but takes
                      no other side effects.
        extra:        routine-specific arguments. Free-form so we don't
                      grow the dataclass every time we add a routine. Each
                      routine documents its own keys.
        action_floor: per-action confidence floor for retrieval calls made
                      by the routine. ``None`` means "use the default for
                      this routine's action name from
                      ``confidence.DEFAULT_ACTION_FLOORS``." Routines that
                      don't call retrieval ignore this field; v0.6 plumbs
                      it through for v0.7+ routines that will.
    """

    brain_root: Path
    out_dir: Path
    dry_run: bool = True
    extra: dict[str, Any] = field(default_factory=dict)
    action_floor: float | None = None


@dataclass(frozen=True)
class RoutineResult:
    """The outcome a routine reports back to the runner.

    Attributes:
        name:            routine name, matching the runner's dispatch key.
        status:          ``ok`` | ``warn`` | ``fail``. ``warn`` means the
                         routine ran but the brain has issues worth
                         reporting; ``fail`` means the routine itself
                         could not produce a useful report.
        summary:         single-line human summary, suitable for a Slack
                         post or GitHub Action job summary.
        artifacts:       absolute paths to files the routine wrote. The
                         runner reads them; we don't pipe content through
                         the result struct.
        runtime_seconds: wall-clock duration. Useful for the runner's own
                         dashboards.
    """

    name: str
    status: str
    summary: str
    artifacts: list[Path] = field(default_factory=list)
    runtime_seconds: float = 0.0


__all__ = ["FAIL", "OK", "WARN", "RoutineConfig", "RoutineResult"]
