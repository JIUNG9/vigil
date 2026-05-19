"""Routine 8 — daily per-engineer invalidation digest.

For each engineer declared in ``knowledge/people.md``, look at their
``git log`` activity over the last 7 days from the brain repo and
cross-reference with recent invalidations (last 24h by default). When
an engineer's recent commits touched files that reference now-invalidated
resources, a personalized digest is staged at::

    out_dir/digests/<engineer-id>-YYYY-MM-DD.md

The runner picks those digests up and emails / Slacks them. The agent
holds no tokens — staging is the contract boundary.

Config (``RoutineConfig.extra``):

  invalidations_root  Path | str  — override the on-disk path. Default:
                                    sibling ``brain-invalidations`` dir
                                    discovered by :mod:`vigil.impact`.
  recency_hours       int         — invalidation window (default 24)
  activity_days       int         — git-log window per engineer (default 7)

The routine is read-only on the brain. It writes only into
``out_dir/digests/``.
"""

from __future__ import annotations

import subprocess
import time
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Any

from vigil.agent._team_meta import Engineer, load_team_meta
from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult
from vigil.impact import (
    InvalidationEvent,
    _resolve_invalidations_root,
    read_recent_invalidations,
)
from vigil.invalidations import extract_resource_ids

# ---------- git activity ----------


def _git_files_for_author(
    brain_root: Path,
    author_email: str,
    since_days: int,
) -> list[str]:
    """Return repo-relative paths an author touched in the last N days.

    Empty list when git is unavailable, the email matches no commits, or
    the working dir isn't a repo. Stable, sorted output.
    """
    if not author_email:
        return []
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={since_days} days ago",
                f"--author={author_email}",
                "--name-only",
                "--pretty=format:",
            ],
            cwd=str(brain_root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    files = {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}
    return sorted(files)


# ---------- match builder ----------


def _suggested_action(event: InvalidationEvent) -> str:
    """Per-event "what should the engineer do" line."""
    sev = event.severity.upper()
    action = event.action.lower()
    if sev in {"HIGH", "CRITICAL"}:
        return (
            f"Verify the page reflects post-{action} state. If a HIGH-severity "
            f"auto-PR draft already exists for this event, review that first."
        )
    if sev == "MEDIUM":
        return (
            f"Skim the page for stale references to this {action}. "
            "Open a PR if anything reads wrong."
        )
    return "Low-severity heads-up — no action required unless something looks off."


def _engineer_matches(
    *,
    engineer: Engineer,
    touched_files: list[str],
    events: list[InvalidationEvent],
    brain_root: Path,
) -> list[dict[str, Any]]:
    """Cross-reference an engineer's edits with recent invalidations.

    Returns a list of ``{event, page, age, action}`` dicts — one per
    (event, brain page) pair where the engineer recently edited the
    page AND the page references the event's resource.
    """
    if not touched_files or not events:
        return []
    matches: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for relpath in touched_files:
        page = brain_root / relpath
        if not page.is_file():
            continue
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ids = extract_resource_ids(text)
        if not ids:
            continue
        for ev in events:
            full = (
                f"{ev.resource_type}.{ev.resource_id}"
                if ev.resource_type
                else ev.resource_id
            )
            if ev.resource_id in ids or full in ids:
                key = (ev.id, relpath)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                matches.append({
                    "event": ev,
                    "page": relpath,
                    "engineer": engineer.id,
                })
    return matches


# ---------- renderer ----------


def _render_digest(
    *,
    engineer: Engineer,
    matches: list[dict[str, Any]],
    today: _date,
    activity_days: int,
    recency_hours: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# Invalidation digest for {engineer.id} — {today.isoformat()}")
    lines.append("")
    lines.append(
        f"_Window: invalidations in the last {recency_hours}h, your commits "
        f"in the last {activity_days}d._"
    )
    lines.append("")
    lines.append(f"Email: {engineer.email}")
    if engineer.role:
        lines.append(f"Role:  {engineer.role}")
    lines.append("")
    lines.append("## Resources you worked on that have recent invalidations")
    lines.append("")
    if not matches:
        lines.append("_None — nothing for you to review today._")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for m in matches:
        ev: InvalidationEvent = m["event"]
        page: str = m["page"]
        full = (
            f"{ev.resource_type}.{ev.resource_id}"
            if ev.resource_type
            else ev.resource_id
        )
        lines.append(
            f"- **{full}** — {ev.action} "
            f"({ev.severity.upper()}) at {ev.timestamp}"
        )
        lines.append(f"  - affecting: `{page}`")
        lines.append(f"  - suggested: {_suggested_action(ev)}")
    lines.append("")
    lines.append(
        "_This digest was drafted by the `invalidation_digest` colleague-agent "
        "routine. The agent never edits the brain; the runner is what delivers "
        "this to you._"
    )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------- routine ----------


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
) -> RoutineResult:
    """Stage one digest file per engineer with relevant invalidations."""
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)
    digest_dir = config.out_dir / "digests"
    digest_dir.mkdir(parents=True, exist_ok=True)

    recency_hours = int(config.extra.get("recency_hours", 24))
    activity_days = int(config.extra.get("activity_days", 7))
    invalidations_root_raw = config.extra.get("invalidations_root")
    invalidations_root = (
        Path(invalidations_root_raw) if invalidations_root_raw else None
    )

    meta = load_team_meta(config.brain_root)
    if not meta.engineers:
        out_path = digest_dir / f"_no-engineers-{today.isoformat()}.md"
        out_path.write_text(
            "# invalidation_digest — no engineers declared\n\n"
            "Add engineers to `knowledge/people.md` to enable per-engineer "
            "digests.\n",
            encoding="utf-8",
        )
        return RoutineResult(
            name="invalidation_digest",
            status=WARN,
            summary="no engineers declared in knowledge/people.md",
            artifacts=[out_path],
            runtime_seconds=time.perf_counter() - started,
        )

    inv_root = _resolve_invalidations_root(config.brain_root, invalidations_root)
    if not inv_root.exists():
        events: list[InvalidationEvent] = []
    else:
        events = read_recent_invalidations(
            inv_root, since=timedelta(hours=recency_hours)
        )

    written: list[Path] = []
    matched_engineers = 0
    for engineer in meta.engineers:
        touched = _git_files_for_author(
            config.brain_root, engineer.email, activity_days
        )
        matches = _engineer_matches(
            engineer=engineer,
            touched_files=touched,
            events=events,
            brain_root=config.brain_root,
        )
        if not matches:
            continue
        matched_engineers += 1
        out_path = digest_dir / f"{engineer.id}-{today.isoformat()}.md"
        out_path.write_text(
            _render_digest(
                engineer=engineer,
                matches=matches,
                today=today,
                activity_days=activity_days,
                recency_hours=recency_hours,
            ),
            encoding="utf-8",
        )
        written.append(out_path)

    if not written:
        # Always leave a breadcrumb so the runner knows the routine ran.
        breadcrumb = digest_dir / f"_empty-{today.isoformat()}.md"
        breadcrumb.write_text(
            f"# invalidation_digest — {today.isoformat()}\n\n"
            f"No engineer had matches for events in the last {recency_hours}h.\n",
            encoding="utf-8",
        )
        written = [breadcrumb]

    summary = (
        f"{matched_engineers} engineer(s) with matches, "
        f"{len(events)} event(s) in window, "
        f"{len(meta.engineers)} engineer(s) total"
    )
    return RoutineResult(
        name="invalidation_digest",
        status=OK,
        summary=summary,
        artifacts=written,
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
