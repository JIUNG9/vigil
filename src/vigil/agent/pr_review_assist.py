"""Routine 10 — auto-comment on PRs that touch infra resources.

Triggered on a ``pull_request`` event (opened / synchronize). Runs
against the PR diff. The agent:

  1. Pulls the resource ids out of every changed file in the PR.
  2. Walks the brain looking for pages that reference any of those ids.
  3. Stages a markdown PR comment listing the affected pages, their last-
     update date, and any recent invalidations.

The runner posts the comment via ``gh pr comment`` using its own token.
The agent never posts.

Inputs (``RoutineConfig.extra``):

  pr_number          int           — PR id; used in the artifact name.
  pr_files           list[str]     — relative paths to changed files. The
                                     runner constructs this from the
                                     PR diff.
  pr_diff            list[dict]    — optional, richer shape
                                     ``[{"path": str, "patch": str}]``.
                                     When supplied, resource extraction
                                     also reads added lines.
  invalidations_root Path | str    — override invalidations repo path.
  recency_hours      int           — invalidation window (default 168 / 7d)

Output: ``out_dir/pr-comments/pr-<number>.md``.

Hard rule: the routine never posts comments. It writes one markdown file.
"""

from __future__ import annotations

import time
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Any

from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult
from vigil.impact import (
    InvalidationEvent,
    _resolve_invalidations_root,
    read_recent_invalidations,
)
from vigil.invalidations import extract_resource_ids

# ---------- helpers ----------


def _read_pr_file_text(brain_root: Path, relpath: str) -> str:
    """Read a PR-changed file from disk if present (post-checkout state)."""
    p = brain_root / relpath
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _resources_in_pr(
    brain_root: Path,
    pr_files: list[str],
    pr_diff: list[dict[str, Any]],
) -> set[str]:
    """Union of resource ids across all PR-changed files / diff hunks."""
    resources: set[str] = set()
    for relpath in pr_files:
        text = _read_pr_file_text(brain_root, relpath)
        resources |= extract_resource_ids(text)
    for entry in pr_diff:
        patch = str(entry.get("patch") or "")
        # Only added/context lines tend to introduce new resource ids;
        # we don't bother to filter prefixes — false positives just mean
        # the routine surfaces a page that's still relevant.
        resources |= extract_resource_ids(patch)
    return resources


def _walk_brain_md(brain_root: Path) -> list[Path]:
    out: list[Path] = []
    for sub in ("docs", "knowledge", ".claude/skills"):
        base = brain_root / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".md", ".markdown"}:
                out.append(path)
    return out


def _last_modified(path: Path) -> str:
    """ISO date for the file's mtime — robust to OSError."""
    import datetime as _dt
    try:
        ts = path.stat().st_mtime
        return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).date().isoformat()
    except OSError:
        return "unknown"


def _find_affected_pages(
    brain_root: Path, resources: set[str]
) -> list[dict[str, Any]]:
    """Return ``[{"path", "last_modified", "matched_resources"}]``.

    A page is "affected" when at least one resource id from the PR also
    appears in the page text.
    """
    if not resources:
        return []
    out: list[dict[str, Any]] = []
    for path in _walk_brain_md(brain_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        page_ids = extract_resource_ids(text)
        if not page_ids:
            continue
        matched = sorted(resources & page_ids)
        if not matched:
            continue
        try:
            relpath = str(path.relative_to(brain_root))
        except ValueError:
            relpath = str(path)
        out.append({
            "path": relpath,
            "last_modified": _last_modified(path),
            "matched_resources": matched,
        })
    out.sort(key=lambda r: r["path"])
    return out


def _events_for_resources(
    invalidations_root: Path,
    resources: set[str],
    recency_hours: int,
) -> list[InvalidationEvent]:
    if not resources or not invalidations_root.exists():
        return []
    events = read_recent_invalidations(
        invalidations_root,
        since=timedelta(hours=recency_hours),
        resource_filter=sorted(resources),
    )
    return events


# ---------- renderer ----------


def _render_comment(
    *,
    pr_number: int,
    pr_files: list[str],
    resources: set[str],
    affected: list[dict[str, Any]],
    events: list[InvalidationEvent],
    today: _date,
) -> str:
    lines: list[str] = []
    lines.append(f"## vigil — PR #{pr_number} brain impact")
    lines.append("")
    lines.append(
        f"_Generated {today.isoformat()} by `pr_review_assist`. "
        "The agent has read-only access to the brain; this comment is "
        "advisory._"
    )
    lines.append("")

    if not resources:
        lines.append(
            "No infra resource ids extracted from the changed files. "
            "Nothing to cross-reference."
        )
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    lines.append(f"**Resources detected in this PR ({len(resources)}):**")
    lines.append("")
    for r in sorted(resources):
        lines.append(f"- `{r}`")
    lines.append("")

    if not affected:
        lines.append(
            "**No brain pages reference these resources.** Either the "
            "resources are new, or no runbook / ADR mentions them yet — "
            "consider adding documentation."
        )
        lines.append("")
    else:
        lines.append(f"**Brain pages that may need review ({len(affected)}):**")
        lines.append("")
        for entry in affected:
            res_str = ", ".join(f"`{r}`" for r in entry["matched_resources"])
            lines.append(
                f"- `{entry['path']}` "
                f"(last updated {entry['last_modified']}) — references {res_str}"
            )
        lines.append("")

    if events:
        lines.append(f"**Recent invalidations on these resources ({len(events)}):**")
        lines.append("")
        for ev in events:
            full = (
                f"{ev.resource_type}.{ev.resource_id}"
                if ev.resource_type
                else ev.resource_id
            )
            lines.append(
                f"- `{full}` — {ev.action} "
                f"({ev.severity.upper()}) at {ev.timestamp} (source: {ev.source})"
            )
        lines.append("")
        lines.append(
            "Verify the affected runbooks reflect the post-event state "
            "before this PR is reviewed."
        )
        lines.append("")
    else:
        lines.append("_No recent invalidation events on these resources._")
        lines.append("")

    if pr_files:
        lines.append(f"<details>\n<summary>Changed files ({len(pr_files)})</summary>")
        lines.append("")
        for f in pr_files:
            lines.append(f"- `{f}`")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------- routine ----------


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
) -> RoutineResult:
    """Stage a PR comment that lists affected brain pages + recent events."""
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)
    comments_dir = config.out_dir / "pr-comments"
    comments_dir.mkdir(parents=True, exist_ok=True)

    pr_number_raw = config.extra.get("pr_number", 0)
    try:
        pr_number = int(pr_number_raw)
    except (TypeError, ValueError):
        pr_number = 0
    pr_files: list[str] = list(config.extra.get("pr_files") or [])
    pr_diff: list[dict[str, Any]] = list(config.extra.get("pr_diff") or [])
    recency_hours = int(config.extra.get("recency_hours", 168))
    invalidations_root_raw = config.extra.get("invalidations_root")
    invalidations_root = (
        Path(invalidations_root_raw) if invalidations_root_raw else None
    )

    if not pr_files and not pr_diff:
        out_path = comments_dir / f"pr-{pr_number}.md"
        out_path.write_text(
            _render_comment(
                pr_number=pr_number,
                pr_files=[],
                resources=set(),
                affected=[],
                events=[],
                today=today,
            ),
            encoding="utf-8",
        )
        return RoutineResult(
            name="pr_review_assist",
            status=WARN,
            summary=f"PR #{pr_number}: no PR files supplied",
            artifacts=[out_path],
            runtime_seconds=time.perf_counter() - started,
        )

    resources = _resources_in_pr(config.brain_root, pr_files, pr_diff)
    affected = _find_affected_pages(config.brain_root, resources)
    inv_root = _resolve_invalidations_root(config.brain_root, invalidations_root)
    events = _events_for_resources(inv_root, resources, recency_hours)

    out_path = comments_dir / f"pr-{pr_number}.md"
    out_path.write_text(
        _render_comment(
            pr_number=pr_number,
            pr_files=pr_files,
            resources=resources,
            affected=affected,
            events=events,
            today=today,
        ),
        encoding="utf-8",
    )

    summary = (
        f"PR #{pr_number}: {len(resources)} resource(s), "
        f"{len(affected)} affected page(s), {len(events)} event(s)"
    )
    return RoutineResult(
        name="pr_review_assist",
        status=OK,
        summary=summary,
        artifacts=[out_path],
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
