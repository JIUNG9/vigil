"""``vigil brain-pulse`` — the engineer's morning ritual.

A single command that aggregates the three signals an SRE wants at the
top of the day:

  1. Resources YOU worked on with recent invalidations.
     (cross-reference of your git activity ↔ recent invalidation events)
  2. Brain page changes the team made (last 24h by default).
  3. Pending PR-staged drafts the agent has produced (from
     ``out_dir/draft-prs/`` — typically populated by the
     ``auto_pr_drafter`` routine).

Everything is read-only. ``brain-pulse`` does not call out to GitHub,
does not fetch from a daemon, and does not rely on any per-engineer
state file other than ``git config user.email`` and the brain repo
itself.

This module owns the aggregation; ``cli.py`` owns the rendering.
``--json`` mode goes directly off the dataclass for scripting.
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from vigil.agent._sync_common import parse_frontmatter
from vigil.impact import (
    InvalidationEvent,
    _resolve_invalidations_root,
    read_recent_invalidations,
)
from vigil.invalidations import extract_resource_ids

# ---------- duration parsing (CLI uses this too) ----------


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def parse_duration(value: str) -> timedelta:
    """Parse ``"30s"`` / ``"5m"`` / ``"24h"`` / ``"7d"``.

    Raises :class:`ValueError` on unrecognised input — the CLI catches it
    and surfaces a friendly ``BadParameter``.
    """
    m = _DURATION_RE.match((value or "").strip())
    if not m:
        raise ValueError(
            f"could not parse duration {value!r}. Use 30s / 5m / 2h / 7d."
        )
    n = int(m.group(1))
    unit = m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return timedelta(seconds=n * seconds)


# ---------- dataclasses ----------


@dataclass(frozen=True)
class TargetedInvalidation:
    """One invalidation event matched against the user's recent edits."""

    resource: str
    age_human: str
    severity: str
    page: str
    pr_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BrainChange:
    """One commit that touched a markdown file in the brain."""

    sha: str
    author: str
    timestamp: str
    path: str
    kind: str  # "modified" | "new" | "deleted"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PendingDraft:
    """One staged auto_pr_drafter draft."""

    path: str
    original_path: str
    invalidation_id: str
    severity: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BrainPulse:
    """Top-level aggregation. ``cli.py`` renders this; ``--json`` dumps it."""

    user_email: str
    since: str
    targeted: list[TargetedInvalidation] = field(default_factory=list)
    brain_changes: list[BrainChange] = field(default_factory=list)
    pending_drafts: list[PendingDraft] = field(default_factory=list)
    filtered_count: int = 0
    recommended_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_email": self.user_email,
            "since": self.since,
            "targeted": [t.to_dict() for t in self.targeted],
            "brain_changes": [c.to_dict() for c in self.brain_changes],
            "pending_drafts": [d.to_dict() for d in self.pending_drafts],
            "filtered_count": self.filtered_count,
            "recommended_actions": list(self.recommended_actions),
        }


# ---------- helpers ----------


def detect_user_email(brain_root: Path) -> str:
    """``git config user.email`` from the brain repo, or empty."""
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=str(brain_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip().lower()


def _git_user_files(
    brain_root: Path, email: str, since: timedelta
) -> list[str]:
    if not email:
        return []
    days = max(1, int(since.total_seconds() // 86400))
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={days} days ago",
                f"--author={email}",
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
    return sorted({ln.strip() for ln in result.stdout.splitlines() if ln.strip()})


def _humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h}h ago"
    d = seconds // 86400
    return f"{d}d ago"


def _git_changed_md(
    brain_root: Path, since: timedelta
) -> list[BrainChange]:
    """All markdown files touched in the brain repo since ``since``."""
    days = max(1, int(since.total_seconds() // 86400))
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={days} days ago",
                "--name-status",
                "--pretty=format:--%H|%ae|%aI",
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
    out: list[BrainChange] = []
    sha = ""
    author = ""
    ts = ""
    for raw in result.stdout.splitlines():
        if raw.startswith("--"):
            parts = raw[2:].split("|")
            if len(parts) >= 3:
                sha, author, ts = parts[0], parts[1].lower(), parts[2]
            continue
        if not raw.strip():
            continue
        # name-status line: "M\tdocs/x.md"
        bits = raw.split("\t")
        if len(bits) < 2:
            continue
        kind_letter = bits[0].strip().upper()[:1]
        path = bits[-1].strip()
        if not path.endswith((".md", ".markdown")):
            continue
        kind = {"M": "modified", "A": "new", "D": "deleted"}.get(
            kind_letter, "modified"
        )
        out.append(BrainChange(
            sha=sha[:8],
            author=author,
            timestamp=ts,
            path=path,
            kind=kind,
        ))
    return out


def _targeted_invalidations(
    brain_root: Path,
    user_email: str,
    user_files: list[str],
    events: list[InvalidationEvent],
    *,
    now: _dt.datetime | None = None,
) -> tuple[list[TargetedInvalidation], int]:
    """Cross-reference user edits ↔ recent events. Returns (matches, filtered).

    ``filtered`` is the number of events that did NOT match — useful for
    the "not relevant to you" line in the dashboard.
    """
    if not user_files or not events:
        return [], len(events)
    now = now or _dt.datetime.now(_dt.UTC)
    out: list[TargetedInvalidation] = []
    seen: set[tuple[str, str]] = set()
    matched_event_ids: set[str] = set()
    for relpath in user_files:
        if not relpath.endswith((".md", ".markdown")):
            continue
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
                if key in seen:
                    continue
                seen.add(key)
                matched_event_ids.add(ev.id)
                ts = _parse_iso(ev.timestamp)
                age = _humanize(now - ts) if ts else "recently"
                pr_hint = (
                    f"your PR touched {relpath}" if relpath in user_files else ""
                )
                out.append(TargetedInvalidation(
                    resource=full,
                    age_human=age,
                    severity=ev.severity.lower(),
                    page=relpath,
                    pr_hint=pr_hint,
                ))
    out.sort(key=lambda t: _severity_rank(t.severity), reverse=True)
    filtered = len(events) - len(matched_event_ids)
    return out, max(filtered, 0)


def _parse_iso(value: str) -> _dt.datetime | None:
    try:
        dt = _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt


_SEV_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _severity_rank(value: str) -> int:
    return _SEV_ORDER.get((value or "").lower(), -1)


def _read_pending_drafts(staging_dir: Path) -> list[PendingDraft]:
    """Read frontmatter from ``out_dir/draft-prs/*.md``."""
    if not staging_dir.is_dir():
        return []
    drafts: list[PendingDraft] = []
    for path in sorted(staging_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta = parse_frontmatter(text)
        if not meta:
            continue
        drafts.append(PendingDraft(
            path=str(path),
            original_path=str(meta.get("original_path", "")),
            invalidation_id=str(meta.get("invalidation_id", "")),
            severity=str(meta.get("severity", "")),
        ))
    return drafts


def _build_recommended_actions(
    targeted: list[TargetedInvalidation],
    drafts: list[PendingDraft],
) -> list[str]:
    """Compose the bottom-of-screen action list."""
    actions: list[str] = []
    # 1) For each high-severity targeted match, suggest verification.
    for t in targeted:
        if t.severity in {"high", "critical"}:
            actions.append(
                f"Verify `{t.page}` reflects new state of `{t.resource}`"
            )
            if len(actions) >= 3:
                break
    # 2) If matching drafts exist, mention them next.
    for t in targeted[:3]:
        for d in drafts:
            if d.original_path == t.page:
                hint = f"Review staged draft `{d.path}` for `{t.page}`"
                if hint not in actions:
                    actions.append(hint)
                break
    return actions


# ---------- main entry point ----------


def collect(
    brain_root: Path,
    *,
    user_email: str | None = None,
    since: timedelta = timedelta(hours=24),
    since_label: str | None = None,
    invalidations_root: Path | None = None,
    staging_dir: Path | None = None,
    now: _dt.datetime | None = None,
) -> BrainPulse:
    """Aggregate the three signals into a :class:`BrainPulse`.

    ``since_label`` — preserve the user-supplied ``--since`` string
    verbatim (e.g. ``"24h"`` instead of the ``"1d"`` you'd get from
    re-formatting the timedelta). When ``None`` we recompute.
    """
    email = (user_email or detect_user_email(brain_root)).lower()

    # Default: brain-staged drafts live in ``<brain>/.vigil-agent/draft-prs/``.
    if staging_dir is None:
        staging_dir = brain_root / ".vigil-agent" / "draft-prs"

    user_files = _git_user_files(brain_root, email, since) if email else []

    inv_root = _resolve_invalidations_root(brain_root, invalidations_root)
    events: list[InvalidationEvent] = []
    if inv_root.exists():
        events = read_recent_invalidations(inv_root, since=since)

    targeted, filtered_count = _targeted_invalidations(
        brain_root, email, user_files, events, now=now,
    )
    brain_changes = _git_changed_md(brain_root, since)
    drafts = _read_pending_drafts(staging_dir)
    actions = _build_recommended_actions(targeted, drafts)

    since_str = since_label or _format_since(since)
    return BrainPulse(
        user_email=email,
        since=since_str,
        targeted=targeted,
        brain_changes=brain_changes,
        pending_drafts=drafts,
        filtered_count=filtered_count,
        recommended_actions=actions,
    )


def _format_since(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


__all__ = [
    "BrainChange",
    "BrainPulse",
    "PendingDraft",
    "TargetedInvalidation",
    "collect",
    "detect_user_email",
    "parse_duration",
]
