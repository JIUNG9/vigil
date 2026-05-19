"""Routine 3 — PR-time migration plan.

Given the list of files a PR touches, run ``adopt --dry-run`` against
the brain and post-filter the resulting plan to entries whose path is
in the PR diff. Render the filtered plan as a markdown comment the
runner can drop straight into the PR.

Why post-filter rather than extending the adopt API: ``adopt`` is
already a pure function over the project tree and v0.4 has 124 tests
locking it down. Reaching into adopt for a path-scoped variant would
risk regressions in unrelated callers. Post-filtering on the entries
list is one line of glue.

The actual ``gh pr comment`` call lives in the runner. This routine
only stages the file.
"""

from __future__ import annotations

import time
from datetime import date as _date
from pathlib import Path

from vigil.adopt import (
    ADD,
    KEEP,
    MOVE_SUGGESTED,
    REVIEW,
    SKIP_PER_ENGINEER,
    AdoptEntry,
    AdoptPlan,
)
from vigil.adopt import (
    adopt as _adopt,  # noqa: PLC0414  — keep the symbol distinct from the module name
)
from vigil.agent.base import OK, RoutineConfig, RoutineResult


def _filter_to_pr_paths(plan: AdoptPlan, pr_files: list[str]) -> list[AdoptEntry]:
    """Keep only entries whose path is in the PR's changed-files list.

    We compare the entry's relpath against pr_files exactly. Callers
    that want directory-level matching can pass the canonicalized
    paths in ``pr_files`` (e.g. ``docs/runbooks/foo.md``).
    """
    pr_set = {p.strip() for p in pr_files if p.strip()}
    if not pr_set:
        return []
    return [e for e in plan.entries if e.path in pr_set]


def _render_pr_comment(
    pr_number: int,
    today: _date,
    brain_root: Path,
    filtered: list[AdoptEntry],
    full_plan: AdoptPlan,
) -> str:
    lines: list[str] = []
    lines.append(f"## vigil adopt — PR #{pr_number} migration plan")
    lines.append("")
    lines.append(f"_Generated {today.isoformat()} against `{brain_root}`._")
    lines.append("")
    lines.append(
        "Only entries that touch files changed in this PR are surfaced. "
        "The full adopt plan (template gaps elsewhere in the brain, "
        "non-canonical paths in unrelated dirs) is *not* shown — review "
        "those in their own PR."
    )
    lines.append("")
    if not filtered:
        lines.append(
            "No adopt-relevant changes in this PR's file set. The diff "
            "doesn't touch any path adopt cares about (`CLAUDE.md`, "
            "`docs/`, `knowledge/`, `wiki/`, `notes/`, `runbooks/`, "
            "`.claude/skills/`, `.claude/rules/`, `.claude/commands/`)."
        )
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    sections = [
        (KEEP, "Keep — already at canonical path"),
        (ADD, "Add — template gap to fill"),
        (MOVE_SUGGESTED, "Move suggested — non-canonical path"),
        (REVIEW, "Review — needs human classification"),
        (SKIP_PER_ENGINEER, "Skip — per-engineer, never team-shareable"),
    ]
    for action, header in sections:
        bucket = [e for e in filtered if e.action == action]
        lines.append(f"### {header} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("_None._")
            lines.append("")
            continue
        for e in bucket:
            target = f" → `{e.suggested_target}`" if e.suggested_target else ""
            lines.append(f"- `{e.path}`{target} — {e.reason}")
        lines.append("")

    if full_plan.claude_md_split_suggestion:
        lines.append("### CLAUDE.md split suggestion")
        lines.append("")
        lines.append(
            "CLAUDE.md exceeds the size budget. Suggested split (one "
            "chunk per `.claude/rules/<topic>.md`):"
        )
        lines.append("")
        for chunk in full_plan.claude_md_split_suggestion:
            lines.append(f"- `.claude/rules/{chunk}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------- routine ----------


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
    max_claude_md_kb: int = 4,
) -> RoutineResult:
    """Stage ``pr-migration-plan-PR<N>.md`` for the runner to post."""
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)

    pr_number_raw = config.extra.get("pr_number", 0)
    try:
        pr_number = int(pr_number_raw)
    except (TypeError, ValueError):
        pr_number = 0
    pr_files: list[str] = list(config.extra.get("pr_files", []) or [])

    # Run adopt --dry-run against the brain. We never apply.
    plan = _adopt(
        config.brain_root,
        dry_run=True,
        apply=False,
        max_claude_md_kb=max_claude_md_kb,
    )
    filtered = _filter_to_pr_paths(plan, pr_files)

    out_name = f"pr-migration-plan-PR{pr_number}.md"
    out_path = config.out_dir / out_name
    out_path.write_text(
        _render_pr_comment(
            pr_number=pr_number,
            today=today,
            brain_root=config.brain_root,
            filtered=filtered,
            full_plan=plan,
        ),
        encoding="utf-8",
    )

    summary = (
        f"PR #{pr_number}: {len(filtered)} adopt entr"
        f"{'y' if len(filtered) == 1 else 'ies'} from {len(pr_files)} changed file(s)"
    )
    return RoutineResult(
        name="pr_migration_plan",
        status=OK,
        summary=summary,
        artifacts=[out_path],
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
