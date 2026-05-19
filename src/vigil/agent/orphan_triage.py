"""Routine 2 — nightly orphan-file classification.

`vigil validate` produces an "orphan_files" warning that lists
markdown files nobody references. The CI is right to surface them, but
the actual decision — *keep, move, archive?* — is judgment work. This
routine takes that list, applies heuristics, and stages a draft of one
GitHub issue per orphan with the proposed action.

Heuristics only. No LLM call. The runner can later upgrade to an
LLM-classified pass; this baseline keeps the routine deterministic and
debuggable.

Hard rules:

  * The brain is never edited. The routine writes one markdown file
    under ``out_dir``; the runner is what opens issues.
  * The proposed actions are ``keep``, ``move``, ``archive``. They are
    proposals, not executions.
"""

from __future__ import annotations

import time
from datetime import date as _date
from pathlib import Path

from vigil.agent.base import OK, RoutineConfig, RoutineResult
from vigil.validate import _check_orphan_files, _iter_markdown

# ---------- heuristics ----------

# Filenames that lean "draft / scratch" — propose archive.
_DRAFT_NAMES: tuple[str, ...] = (
    "draft",
    "scratch",
    "tmp",
    "temp",
    "wip",
    "old",
    "deprecated",
)

# Filenames that lean "real content but mis-located" — propose move.
_RUNBOOK_KEYWORDS: tuple[str, ...] = (
    "runbook",
    "playbook",
    "incident",
    "oncall",
    "on-call",
    "postmortem",
    "post-mortem",
)
_DECISION_KEYWORDS: tuple[str, ...] = (
    "adr",
    "decision",
    "rfc",
)


def _classify_orphan(brain_root: Path, relpath: str) -> tuple[str, str]:
    """Return ``(action, reason)`` for one orphan path."""
    p = brain_root / relpath
    stem = Path(relpath).stem.lower()

    # If the file is suspiciously old, archive it.
    try:
        mtime = p.stat().st_mtime
        age_days = (time.time() - mtime) / 86400.0
    except OSError:
        age_days = 0.0
    if age_days > 365:
        return ("archive", f"untouched for {int(age_days)} days; archive into docs/archive/")

    # Filename suggests draft / scratch — archive.
    if any(d in stem for d in _DRAFT_NAMES):
        return ("archive", f"filename suggests draft / scratch ({stem})")

    # Looks like a runbook → move into docs/runbooks/.
    if any(k in stem for k in _RUNBOOK_KEYWORDS):
        return ("move", "looks like a runbook; move into docs/runbooks/")

    # Looks like an ADR → move into knowledge/decisions/.
    if any(k in stem for k in _DECISION_KEYWORDS):
        return ("move", "looks like an ADR / decision record; move into knowledge/decisions/")

    # Tiny stub (< 200 bytes) — archive.
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    if size > 0 and size < 200:
        return ("archive", f"tiny ({size} bytes); likely a stub")

    # Has internal links → keep, but link from CLAUDE.md.
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        text = ""
    link_count = text.count("](")
    if link_count >= 3:
        return ("keep", f"has {link_count} link(s); link this file from CLAUDE.md so it stops being orphan")

    # Default: keep, ask owner to decide.
    return ("keep", "no strong signal — owner should decide if this belongs")


# ---------- proposed-issue body ----------


def _proposed_issue_body(
    brain_root: Path, relpath: str, action: str, reason: str
) -> str:
    title_action = action.upper()
    return (
        f"### Proposed: {title_action} `{relpath}`\n\n"
        f"**Reason:** {reason}\n\n"
        f"**Suggested next step:**\n\n"
        + _suggested_next_step(action, relpath)
        + "\n\n"
        + (
            "_This issue body was drafted by the `orphan_triage` colleague "
            "agent routine. The action is a proposal — close this issue if "
            "you disagree._\n"
        )
    )


def _suggested_next_step(action: str, relpath: str) -> str:
    if action == "archive":
        return (
            f"- `git mv {relpath} docs/archive/{Path(relpath).name}`\n"
            "- Open a PR. Reviewers confirm the file is no longer load-bearing."
        )
    if action == "move":
        target_hint = "docs/runbooks/" if "runbook" in relpath.lower() else "knowledge/decisions/"
        return (
            f"- `git mv {relpath} {target_hint}{Path(relpath).name}`\n"
            "- Open a PR. Update any references to the old path in the same commit."
        )
    return (
        f"- Add a link from `CLAUDE.md` (or any canonical page) pointing to "
        f"`{relpath}` so it's reachable — orphans live in the gap between "
        "'we keep this' and 'we tell anyone about it'.\n"
        "- Or, if you've decided it's stale, open a follow-up to archive it."
    )


# ---------- routine ----------


def run(config: RoutineConfig, *, today: _date | None = None) -> RoutineResult:
    """Generate ``orphan-triage-YYYY-MM-DD.md`` for the runner to consume."""
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)

    # Re-use validate's orphan finder rather than recomputing — keeps
    # the two systems in agreement on what "orphan" means.
    check = _check_orphan_files(config.brain_root)
    orphans: list[str] = list(check.details.get("orphans", []) or [])

    # validate caps at 50 — we want every orphan in the digest.
    if check.details.get("total", len(orphans)) > len(orphans):
        orphans = sorted({str(p.relative_to(config.brain_root))
                          for p in _iter_markdown(config.brain_root)
                          if not _is_canonical_or_referenced(config.brain_root, p)})

    classifications: list[tuple[str, str, str]] = []
    counts = {"keep": 0, "move": 0, "archive": 0}
    for rel in orphans:
        action, reason = _classify_orphan(config.brain_root, rel)
        classifications.append((rel, action, reason))
        counts[action] = counts.get(action, 0) + 1

    out_name = f"orphan-triage-{today.isoformat()}.md"
    out_path = config.out_dir / out_name
    out_path.write_text(
        _render_triage(config.brain_root, today, classifications, counts),
        encoding="utf-8",
    )

    summary = (
        f"{len(orphans)} orphan(s): "
        f"keep={counts['keep']}  move={counts['move']}  archive={counts['archive']}"
    )
    if not orphans:
        summary = "no orphans to triage"
    return RoutineResult(
        name="orphan_triage",
        status=OK,
        summary=summary,
        artifacts=[out_path],
        runtime_seconds=time.perf_counter() - started,
    )


def _is_canonical_or_referenced(brain_root: Path, path: Path) -> bool:
    """Quick fallback when validate truncates the orphan list.

    Mirrors :func:`validate._check_orphan_files`'s definition. Cheaper
    to recompute than to chase the truncation knob.
    """
    from vigil.validate import _is_canonical
    rel = str(path.relative_to(brain_root))
    if _is_canonical(rel):
        return True
    return rel == "CLAUDE.md"


# ---------- renderer ----------


def _render_triage(
    brain_root: Path,
    today: _date,
    classifications: list[tuple[str, str, str]],
    counts: dict[str, int],
) -> str:
    lines: list[str] = []
    lines.append(f"# Orphan triage — {today.isoformat()}")
    lines.append("")
    lines.append(f"- Brain root: `{brain_root}`")
    lines.append(f"- Orphans found: {len(classifications)}")
    lines.append(
        f"- Proposed actions: keep={counts['keep']}  move={counts['move']}  "
        f"archive={counts['archive']}"
    )
    lines.append("")
    lines.append(
        "_Proposals are heuristic. The runner stages one GitHub issue "
        "per row; close issues you disagree with rather than acting on "
        "them. The agent never edits the brain — it only stages drafts._"
    )
    lines.append("")

    if not classifications:
        lines.append("No orphans to triage.")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for action in ("archive", "move", "keep"):
        bucket = [c for c in classifications if c[1] == action]
        lines.append(f"## Proposed: {action} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("_None._")
            lines.append("")
            continue
        for relpath, _act, reason in bucket:
            lines.append(f"### `{relpath}`")
            lines.append("")
            lines.append(f"- Action: **{action}**")
            lines.append(f"- Reason: {reason}")
            lines.append("")
            lines.append("```markdown")
            lines.append(_proposed_issue_body(brain_root, relpath, action, reason).rstrip())
            lines.append("```")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["run"]
