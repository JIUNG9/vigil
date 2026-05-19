"""Routine 9 — git-history-based notification routing.

Triggered per-event (not per-day): when a HIGH-severity invalidation is
added to the invalidations repo, this routine identifies the 3-5
engineers most likely to need it.

Scoring (signals are additive, the runner picks the top-N):

  +50    declared owner of the resource per ``knowledge/services.md``
         (or, if none, the engineer in ``knowledge/people.md`` whose
         role text mentions the resource type)
  +30    each brain page the engineer has authored or edited within
         the last 90 days that references the resource
  +25    open-PR author touching the resource. The agent has no
         tokens — the runner injects ``open_prs`` via
         ``RoutineConfig.extra``. When absent, this signal is skipped
         and the score still works on the other two.

Inputs (``RoutineConfig.extra``):

  invalidation        dict           — required. Shape::

                                       {"id": str,
                                        "resource_type": str,
                                        "resource_id": str,
                                        "severity": str,
                                        "action": str,
                                        "timestamp": str}

  open_prs            list[dict]     — optional. Each entry::

                                       {"author": email or id,
                                        "files": [str, ...],
                                        "number": int (optional)}

  activity_days       int            — git-log window (default 90)
  top_n               int            — how many engineers to surface (default 5)

Output: ``out_dir/radar/<invalidation-id>.json`` — a list of records::

    [
      {"engineer_id": "alice", "score": 105,
       "reasons": ["owner of auth-service (+50)",
                   "edited docs/runbooks/auth-deploy.md within 90d (+30)",
                   "open PR #42 touches the resource (+25)"]},
      ...
    ]

Sorted descending by score. Ties broken by engineer id (stable).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import date as _date
from pathlib import Path
from typing import Any

from vigil.agent._team_meta import Engineer, Service, load_team_meta
from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult
from vigil.invalidations import extract_resource_ids

# ---------- helpers ----------


def _git_authored_files(
    brain_root: Path, author_email: str, since_days: int
) -> set[str]:
    """Return the set of files an author authored/edited in the window."""
    if not author_email:
        return set()
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
        return set()
    if result.returncode != 0:
        return set()
    return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}


def _resource_match_keys(invalidation: dict[str, Any]) -> set[str]:
    """All strings that count as "this event's resource" for matching."""
    rid = str(invalidation.get("resource_id") or "").strip()
    rtype = str(invalidation.get("resource_type") or "").strip()
    out: set[str] = set()
    if rid:
        out.add(rid)
    if rtype and rid:
        out.add(f"{rtype}.{rid}")
    if rtype:
        out.add(rtype)
    return out


def _page_references_resource(
    brain_root: Path, relpath: str, keys: set[str]
) -> bool:
    """Cheap substring check — does this page mention any match key?"""
    p = brain_root / relpath
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    ids = extract_resource_ids(text)
    return any(k in ids or k in text for k in keys)


# ---------- scoring ----------


def _score_owner(
    engineer: Engineer, services: list[Service], invalidation: dict[str, Any]
) -> tuple[int, list[str]]:
    """+50 per declared ownership match."""
    rid = str(invalidation.get("resource_id") or "")
    rtype = str(invalidation.get("resource_type") or "")
    full = f"{rtype}.{rid}" if rtype else rid
    score = 0
    reasons: list[str] = []
    for svc in services:
        if svc.owner != engineer.id:
            continue
        if svc.matches_resource(rid) or svc.matches_resource(full):
            score += 50
            reasons.append(f"owner of {svc.name} (+50)")
    return score, reasons


def _score_git_history(
    brain_root: Path,
    engineer: Engineer,
    invalidation: dict[str, Any],
    *,
    activity_days: int,
) -> tuple[int, list[str]]:
    """+30 per brain page the engineer touched that references the resource."""
    keys = _resource_match_keys(invalidation)
    if not keys:
        return 0, []
    files = _git_authored_files(brain_root, engineer.email, activity_days)
    score = 0
    reasons: list[str] = []
    for relpath in sorted(files):
        if not relpath.endswith((".md", ".markdown")):
            continue
        if _page_references_resource(brain_root, relpath, keys):
            score += 30
            reasons.append(
                f"edited `{relpath}` within {activity_days}d (+30)"
            )
    return score, reasons


def _score_open_prs(
    engineer: Engineer,
    open_prs: list[dict[str, Any]],
    invalidation: dict[str, Any],
) -> tuple[int, list[str]]:
    """+25 per open PR by the engineer that touches the resource."""
    if not open_prs:
        return 0, []
    keys = _resource_match_keys(invalidation)
    score = 0
    reasons: list[str] = []
    for pr in open_prs:
        author = str(pr.get("author") or "").lower()
        if author not in {engineer.email, engineer.id}:
            continue
        files = list(pr.get("files") or [])
        # Crude — we don't have the full file content here, the runner
        # is expected to pre-filter to PRs whose diff likely matches.
        # We accept the PR as a match if any file path or any explicit
        # ``resources`` list intersects the invalidation keys.
        pr_resources = list(pr.get("resources") or [])
        hit = (
            any(any(k in f for k in keys) for f in files)
            or any(any(k in r for k in keys) for r in pr_resources)
        )
        if hit:
            score += 25
            number = pr.get("number")
            tag = f"#{number}" if number else "(open)"
            reasons.append(f"open PR {tag} touches the resource (+25)")
    return score, reasons


# ---------- routine ----------


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
) -> RoutineResult:
    """Identify the top-N engineers for one HIGH-severity event."""
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)
    radar_dir = config.out_dir / "radar"
    radar_dir.mkdir(parents=True, exist_ok=True)

    invalidation = dict(config.extra.get("invalidation") or {})
    if not invalidation.get("resource_id"):
        return RoutineResult(
            name="targeted_radar",
            status=WARN,
            summary="no invalidation supplied (extra.invalidation missing)",
            artifacts=[],
            runtime_seconds=time.perf_counter() - started,
        )

    open_prs = list(config.extra.get("open_prs") or [])
    activity_days = int(config.extra.get("activity_days", 90))
    top_n = int(config.extra.get("top_n", 5))

    meta = load_team_meta(config.brain_root)
    if not meta.engineers:
        return RoutineResult(
            name="targeted_radar",
            status=WARN,
            summary="no engineers declared in knowledge/people.md",
            artifacts=[],
            runtime_seconds=time.perf_counter() - started,
        )

    scored: list[dict[str, Any]] = []
    for engineer in meta.engineers:
        owner_score, owner_reasons = _score_owner(
            engineer, meta.services, invalidation
        )
        git_score, git_reasons = _score_git_history(
            config.brain_root, engineer, invalidation,
            activity_days=activity_days,
        )
        pr_score, pr_reasons = _score_open_prs(
            engineer, open_prs, invalidation
        )
        total = owner_score + git_score + pr_score
        if total <= 0:
            continue
        scored.append({
            "engineer_id": engineer.id,
            "email": engineer.email,
            "score": total,
            "reasons": owner_reasons + git_reasons + pr_reasons,
        })

    scored.sort(key=lambda r: (-int(r["score"]), str(r["engineer_id"])))
    top = scored[:top_n] if top_n > 0 else scored

    inv_id = str(invalidation.get("id") or "no-id")
    out_path = radar_dir / f"{inv_id}.json"
    out_path.write_text(
        json.dumps(
            {
                "invalidation_id": inv_id,
                "resource_type": invalidation.get("resource_type", ""),
                "resource_id": invalidation.get("resource_id", ""),
                "severity": invalidation.get("severity", ""),
                "action": invalidation.get("action", ""),
                "timestamp": invalidation.get("timestamp", ""),
                "generated_at": today.isoformat(),
                "top": top,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    summary = (
        f"{len(top)} engineer(s) selected from {len(meta.engineers)} candidate(s) "
        f"for {invalidation.get('resource_id','?')}"
    )
    return RoutineResult(
        name="targeted_radar",
        status=OK,
        summary=summary,
        artifacts=[out_path],
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
