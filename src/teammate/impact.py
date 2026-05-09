"""Event-driven invalidation — pre / post terraform hooks (v0.9).

This module owns the "brain-vs-infra freshness" half of the team-brain
problem. The brain-vs-brain half (orphan files, dead links, stale
runbooks) lives in :mod:`teammate.validate`. They are different problems
with different solutions: validate runs a structural check; ``impact``
listens for cloud events.

Three commands wire onto this module:

  - ``teammate impact preview``  — pre-terraform hook. Greps the brain
    for pages that mention the resources you are about to change. If a
    HIGH-severity invalidation already exists for any of them within the
    recency window, exits 2 (block).
  - ``teammate impact emit``     — post-terraform hook. Writes a
    structured JSON event to the brain-invalidations repo on disk.
  - ``teammate impact list``     — read recent events as a table.

Event schema lives on :class:`InvalidationEvent`. Files land at::

    <invalidations_root>/invalidations/YYYY/MM/DD/<resource-slug>-<action>-<ts>.json

The folder layout is grep-friendly and gitable. No daemon, no SQLite,
no S3 — just JSON files in a git repo.

Resource discovery is grep-based: we walk ``docs/``, ``knowledge/``, and
``.claude/skills/`` and look for the resource id as a substring (and as
an HCL address ``aws_<type>.<name>``). Cheap and correct enough — the
team's brain is small and the worst-case is a false match (which surfaces
as "verify against current state", not as a missed warning).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

# ---------- severity ----------


SEVERITY_LEVELS = ("low", "medium", "high", "critical")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_LEVELS)}
# Backwards-compatible alias kept for internal consumers that imported the
# private name during early development. Public API is ``SEVERITY_RANK``.
_SEVERITY_RANK = SEVERITY_RANK


def severity_at_least(actual: str, threshold: str) -> bool:
    """Return True if ``actual`` is ``>=`` ``threshold`` on the severity scale.

    Unknown values rank below ``low`` so a malformed event never trips
    the gate.
    """
    a = SEVERITY_RANK.get(actual.lower(), -1)
    t = SEVERITY_RANK.get(threshold.lower(), 0)
    return a >= t


# ---------- dataclass ----------


@dataclass(frozen=True)
class InvalidationEvent:
    """One cloud event that invalidates part of the brain.

    The dataclass is the JSON contract on disk. Every field is a string
    or a plain dict so the file is portable across runtimes.
    """

    id: str
    timestamp: str  # ISO 8601 UTC, ``2026-05-09T14:00:00+00:00``
    source: str  # cloudtrail | terraform | manual | …
    resource_type: str  # e.g. ``aws_vpc``, ``aws_iam_role``
    resource_id: str  # e.g. ``vpc-abc123``, ``my-role``
    action: str  # detach | modify | delete | create | …
    severity: str  # low | medium | high | critical
    actor: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain ``dict`` (for JSON dump or rich tables)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvalidationEvent:
        """Inverse of :meth:`to_dict`. Tolerant — missing keys default."""
        return cls(
            id=str(data.get("id") or _new_id()),
            timestamp=str(data.get("timestamp") or _now_iso()),
            source=str(data.get("source") or "unknown"),
            resource_type=str(data.get("resource_type") or ""),
            resource_id=str(data.get("resource_id") or ""),
            action=str(data.get("action") or "modify"),
            severity=str(data.get("severity") or "medium").lower(),
            actor=str(data.get("actor") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


# ---------- ImpactReport ----------


@dataclass(frozen=True)
class ImpactReport:
    """Output of :func:`preview`. Drives the ``impact preview`` exit code."""

    pages: list[dict[str, Any]] = field(default_factory=list)
    recent_invalidations: list[dict[str, Any]] = field(default_factory=list)
    block: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages": list(self.pages),
            "recent_invalidations": list(self.recent_invalidations),
            "block": self.block,
        }


# ---------- helpers ----------


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with ``+00:00`` suffix, second precision."""
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Filesystem-safe slug. Lowercase, kebab-case, no leading/trailing ``-``."""
    out = _SLUG_RE.sub("-", value.lower()).strip("-")
    return out or "resource"


def _resolve_invalidations_root(brain_root: Path,
                                invalidations_root: Path | None) -> Path:
    """Pick the on-disk root for the invalidations repo.

    Order:
      1. explicit ``invalidations_root`` argument
      2. ``<brain_root>/../brain-invalidations`` if it exists
      3. ``~/.teammate/brain-invalidations`` (created on demand)
    """
    if invalidations_root is not None:
        return Path(invalidations_root)
    sibling = brain_root.parent / "brain-invalidations"
    if sibling.is_dir():
        return sibling
    return Path.home() / ".teammate" / "brain-invalidations"


# ---------- brain page discovery ----------


_BRAIN_SUBTREES = ("docs", "knowledge", ".claude/skills")
_BRAIN_EXTS = {".md", ".markdown"}


def _walk_brain_files(brain_root: Path) -> list[Path]:
    out: list[Path] = []
    for sub in _BRAIN_SUBTREES:
        base = brain_root / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in _BRAIN_EXTS:
                out.append(path)
    return out


def _resource_search_terms(resource: str) -> list[str]:
    """Build the set of substrings we accept as a "match" for ``resource``.

    We accept the full HCL address (``aws_vpc.shared``), the bare id
    (``vpc-abc123``), and the type prefix (``aws_vpc``) when the input is
    an HCL address.
    """
    terms = {resource}
    if "." in resource and resource.split(".", 1)[0].startswith("aws_"):
        rtype, _ = resource.split(".", 1)
        terms.add(rtype)
    return [t for t in terms if t]


def find_pages_for_resources(
    brain_root: Path, resources: list[str]
) -> list[dict[str, Any]]:
    """Walk the brain, return every page that mentions any of ``resources``.

    Result entries: ``{"path": str (relative to brain_root), "resource":
    str, "matches": int}``. One page may appear multiple times if it
    matches multiple resources.
    """
    files = _walk_brain_files(brain_root)
    out: list[dict[str, Any]] = []
    for resource in resources:
        terms = _resource_search_terms(resource)
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            matches = sum(text.count(t) for t in terms)
            if matches:
                try:
                    rel = str(path.relative_to(brain_root))
                except ValueError:
                    rel = str(path)
                out.append({"path": rel, "resource": resource, "matches": matches})
    return out


# ---------- invalidations file IO ----------


def _date_dir(invalidations_root: Path, ts: _dt.datetime) -> Path:
    return (
        invalidations_root
        / "invalidations"
        / f"{ts.year:04d}"
        / f"{ts.month:02d}"
        / f"{ts.day:02d}"
    )


def emit(
    brain_root: Path,
    resource: str,
    action: str,
    severity: str,
    source: str = "manual",
    terraform_state_path: Path | None = None,
    *,
    invalidations_root: Path | None = None,
    actor: str = "",
    metadata: dict[str, Any] | None = None,
    resource_type: str = "",
) -> Path:
    """Write a structured invalidation event. Returns the written path.

    The folder layout is ``<root>/invalidations/YYYY/MM/DD/`` so a year of
    events stays grep-able. The filename is stable enough to be diffed
    in a PR but unique enough that two engineers can race.

    ``terraform_state_path`` is recorded as metadata when present so the
    on-call engineer can replay ``terraform state show`` against the
    matching resource later. We don't read the file — that would couple
    the hook to the state backend.
    """
    severity_norm = severity.lower()
    if severity_norm not in SEVERITY_LEVELS:
        raise ValueError(
            f"unknown severity {severity!r}. Use one of: {', '.join(SEVERITY_LEVELS)}"
        )
    if not resource:
        raise ValueError("resource must be a non-empty string")

    # Resource type defaults to the type half of an HCL address, else
    # the resource itself stripped of trailing identifier characters.
    rtype = resource_type or (
        resource.split(".", 1)[0]
        if "." in resource and resource.split(".", 1)[0].startswith("aws_")
        else ""
    )
    rid = resource.split(".", 1)[1] if "." in resource else resource

    md = dict(metadata or {})
    if terraform_state_path is not None:
        md.setdefault("terraform_state_path", str(terraform_state_path))

    now = _dt.datetime.now(_dt.UTC)
    event = InvalidationEvent(
        id=_new_id(),
        timestamp=now.isoformat(timespec="seconds"),
        source=source or "manual",
        resource_type=rtype,
        resource_id=rid,
        action=action,
        severity=severity_norm,
        actor=actor,
        metadata=md,
    )

    root = _resolve_invalidations_root(brain_root, invalidations_root)
    target_dir = _date_dir(root, now)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Filename: <slug>-<action>-<unix-ts>.json. Unix ts breaks ties when
    # two engineers emit the same resource+action on the same day.
    slug = slugify(resource)
    fname = f"{slug}-{slugify(action)}-{int(now.timestamp())}.json"
    target = target_dir / fname

    # If a collision happens (same second, same slug), append the event id
    # — file uniqueness wins over filename brevity.
    if target.exists():
        target = target_dir / f"{slug}-{slugify(action)}-{int(now.timestamp())}-{event.id[:8]}.json"

    target.write_text(
        json.dumps(event.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _iter_event_files(invalidations_root: Path) -> list[Path]:
    base = invalidations_root / "invalidations"
    if not base.is_dir():
        return []
    return sorted(base.rglob("*.json"))


def _parse_iso(value: str) -> _dt.datetime | None:
    try:
        dt = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt


def read_recent_invalidations(
    invalidations_root: Path,
    since: timedelta = timedelta(hours=24),
    *,
    severity: str | None = None,
    resource_filter: list[str] | None = None,
) -> list[InvalidationEvent]:
    """Read every JSON event newer than ``since``.

    ``severity``         — minimum severity (e.g. ``"high"``); events
                           below the floor are dropped.
    ``resource_filter``  — when set, keep only events whose
                           ``resource_id`` or full HCL address (``type.id``)
                           matches one of the supplied strings (substring,
                           case-sensitive — resource ids are stable
                           identifiers).
    """
    cutoff = _dt.datetime.now(_dt.UTC) - since
    events: list[InvalidationEvent] = []
    for path in _iter_event_files(invalidations_root):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ev = InvalidationEvent.from_dict(data)
        ts = _parse_iso(ev.timestamp)
        if ts is None or ts < cutoff:
            continue
        if severity is not None and not severity_at_least(ev.severity, severity):
            continue
        if resource_filter is not None:
            full = f"{ev.resource_type}.{ev.resource_id}" if ev.resource_type else ev.resource_id
            if not any(
                term and (term in full or term in ev.resource_id) for term in resource_filter
            ):
                continue
        events.append(ev)
    # Newest-first is the dashboard order; the CLI table reads top-down.
    events.sort(key=lambda e: e.timestamp, reverse=True)
    return events


# ---------- preview ----------


def preview(
    brain_root: Path,
    resources: list[str],
    *,
    invalidations_root: Path | None = None,
    recency: timedelta = timedelta(hours=24),
    severity_floor: str = "high",
) -> ImpactReport:
    """Pre-terraform hook: find brain pages + recent events for ``resources``.

    Block semantics: ``block=True`` iff at least one event with severity
    ``>= severity_floor`` exists for any of the touched resources within
    the recency window. The CLI maps that to exit code 2.

    The report's ``recent_invalidations`` is filtered to the touched
    resources only — see advisor flag J. Otherwise the table fills up
    with noise from unrelated infra.
    """
    if not resources:
        return ImpactReport(pages=[], recent_invalidations=[], block=False)

    pages = find_pages_for_resources(brain_root, resources)
    root = _resolve_invalidations_root(brain_root, invalidations_root)
    events = read_recent_invalidations(
        root, since=recency, resource_filter=resources
    )

    block = any(severity_at_least(ev.severity, severity_floor) for ev in events)
    return ImpactReport(
        pages=pages,
        recent_invalidations=[ev.to_dict() for ev in events],
        block=block,
    )


# ---------- session cache (advisor flag B) ----------


# 60-second cache shared within a single CLI invocation. Module-level
# dict keyed on (root, since seconds, severity, resources tuple). We use
# ``time.monotonic`` so wall-clock skew never invalidates the cache
# spuriously.
_CACHE_TTL = 60.0
_event_cache: dict[tuple[Any, ...], tuple[float, list[InvalidationEvent]]] = {}


def read_recent_invalidations_cached(
    invalidations_root: Path,
    since: timedelta = timedelta(hours=24),
    *,
    severity: str | None = None,
    resource_filter: list[str] | None = None,
) -> list[InvalidationEvent]:
    """Cached variant. 60-second TTL; safe for runtime hot paths."""
    key = (
        str(invalidations_root),
        since.total_seconds(),
        severity or "",
        tuple(resource_filter or ()),
    )
    now = time.monotonic()
    cached = _event_cache.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return list(cached[1])
    events = read_recent_invalidations(
        invalidations_root, since=since,
        severity=severity, resource_filter=resource_filter,
    )
    _event_cache[key] = (now, events)
    return list(events)


def _clear_cache() -> None:
    """Test-only — drop the 60s session cache."""
    _event_cache.clear()


__all__ = [
    "ImpactReport",
    "InvalidationEvent",
    "SEVERITY_LEVELS",
    "emit",
    "find_pages_for_resources",
    "preview",
    "read_recent_invalidations",
    "read_recent_invalidations_cached",
    "severity_at_least",
    "slugify",
]
