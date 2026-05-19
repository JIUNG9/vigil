"""Runtime integration — surface invalidation events at ``vigil ask`` time.

The companion to :mod:`vigil.impact`. ``impact`` writes events;
``invalidations`` reads them back at retrieval time and decides which
chunks deserve a "this might be stale" banner.

Flow at runtime::

  retrieved chunks  ──►  extract resource ids from chunk text (regex)
                          │
                          ▼
                  read recent events from invalidations repo (60s cache)
                          │
                          ▼
                  filter to events whose resource_id appears in any chunk
                          │
                          ▼
                  ask.py renders the banner if ≥ 1 event ≥ show_severity
                  audit log records EVERY matched event regardless of severity

Resource extraction is heuristic regex (no LLM calls). The patterns
cover the AWS resource types that move most often in incident response —
VPC, subnet, IAM, RDS, security groups, EC2 instances, ECS tasks. Custom
patterns (other clouds, in-house resource ids) are out of scope for v0.9
and will land in v0.10 as ``[invalidations.extra_patterns]``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from vigil.impact import (
    InvalidationEvent,
    read_recent_invalidations_cached,
)

# ---------- resource extraction ----------


# Anchored AWS resource regexes. Width bounds are taken from the AWS docs
# (``vpc-`` is 8 or 17 hex chars; modern accounts use 17). We also accept
# the bare HCL address form ``aws_<type>.<name>`` used inside terraform
# diffs and runbooks.
#
# Each pattern is anchored on a non-word char (or start of string) on
# the left so common-English false positives like "i-think" or
# "vpc-tutorial-1" don't trip the gate. The right side allows hex up to
# the AWS-spec maximum.
_RESOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w-])(vpc-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(subnet-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(sg-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(igw-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(nat-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(rtb-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(eni-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(eip-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(i-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(vol-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(ami-[0-9a-f]{8,17})(?![\w-])"),
    re.compile(r"(?<![\w-])(snap-[0-9a-f]{8,17})(?![\w-])"),
    # ARN — anything between ``arn:aws:`` and the next whitespace / quote.
    # Region + account fields are sometimes empty (e.g. ``arn:aws:s3:::``).
    re.compile(r"(arn:aws:[a-z0-9-]+:[a-z0-9-]*:[0-9]*:[\w/.:-]+)"),
    # Terraform addresses: aws_<type>.<name>. Require the dot, both halves
    # alphanumeric/underscore, to keep noise out.
    re.compile(r"\b(aws_[a-z][a-z0-9_]+\.[A-Za-z][A-Za-z0-9_-]+)\b"),
)


# Type names we care about — used for surfacing "the chunk mentions
# resource type ``aws_vpc`` even if it doesn't have a vpc-id". We only
# match these as a *type-only* signal when they appear in HCL-shaped
# context. The resource extraction proper requires an instance id.
_KNOWN_AWS_TYPES = frozenset({
    "aws_vpc", "aws_subnet", "aws_security_group", "aws_iam_role",
    "aws_iam_policy", "aws_db_instance", "aws_db_cluster",
    "aws_rds_cluster", "aws_lb", "aws_elb", "aws_route53_record",
    "aws_s3_bucket", "aws_kms_key", "aws_ecs_service",
    "aws_eks_cluster", "aws_lambda_function", "aws_cloudfront_distribution",
})


def extract_resource_ids(text: str) -> set[str]:
    """Return the set of resource ids referenced anywhere in ``text``.

    Cheap, regex-only. Output values are the matched substrings exactly
    as they appear in the source (no canonicalisation), so an event
    written with ``vpc-abc12345`` correlates with a chunk that has
    ``vpc-abc12345`` and not with one that has the longer
    ``vpc-0abc12345fffeeee0``.
    """
    if not text:
        return set()
    out: set[str] = set()
    for pattern in _RESOURCE_PATTERNS:
        for match in pattern.findall(text):
            out.add(match)
    return out


# ---------- chunk matching ----------


def _chunk_path(chunk: object) -> str:
    """Pull the ``path`` attribute from a Hit-like object.

    Accepts anything with a ``.path`` attribute (the ``Hit`` dataclass
    from :mod:`vigil.rag.ask`) or a plain dict with ``"path"``.
    """
    if hasattr(chunk, "path"):
        return str(chunk.path)
    if isinstance(chunk, dict):
        return str(chunk.get("path", ""))
    return ""


def _chunk_text(chunk: object) -> str:
    if hasattr(chunk, "text"):
        return str(chunk.text)
    if isinstance(chunk, dict):
        return str(chunk.get("text", ""))
    return ""


def find_invalidations_for_chunks(
    chunks: Iterable[object],
    invalidations_root: Path,
    since: timedelta = timedelta(days=14),
) -> dict[str, list[InvalidationEvent]]:
    """Map ``chunk_path → [InvalidationEvent, ...]`` for matching chunks.

    A chunk "matches" an event when at least one resource id extracted
    from the chunk text equals the event's ``resource_id`` (or the event's
    full HCL address ``resource_type.resource_id``).

    Empty input → empty dict. No invalidations repo on disk → empty dict.
    """
    chunk_list = list(chunks)
    if not chunk_list or not invalidations_root.exists():
        return {}

    # Read once. The 60s session cache absorbs duplicate calls inside the
    # same `vigil ask` run.
    events = read_recent_invalidations_cached(invalidations_root, since=since)
    if not events:
        return {}

    out: dict[str, list[InvalidationEvent]] = {}
    for chunk in chunk_list:
        path = _chunk_path(chunk)
        text = _chunk_text(chunk)
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
                out.setdefault(path, []).append(ev)
    return out


# ---------- banner rendering ----------


_BANNER_HEAD = (
    "⚠️  This answer references resources with recent infra changes:\n"
)
_BANNER_RULE = "─" * 45 + "\n"

# Cloud-side id prefixes worth showing in the banner parenthetical.
# `aws_iam_role.deploy-bot` has no separate cloud id; `aws_vpc.shared
# (vpc-abc123)` does. Only the latter benefits from the extra rendering.
_CLOUD_ID_PREFIXES = (
    "vpc-", "subnet-", "sg-", "igw-", "nat-", "rtb-", "eni-", "eip-",
    "i-", "vol-", "ami-", "snap-",
)


def _humanize_age(timestamp: str, *, now: object | None = None) -> str:
    """Render an event age as ``"2 hours ago"`` / ``"9 hours ago"`` / ``"3 days ago"``.

    Accepts a parseable ISO timestamp; returns ``"recently"`` if parsing
    fails. ``now`` is injectable for deterministic tests.
    """
    import datetime as _dt

    try:
        ts = _dt.datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return "recently"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.UTC)
    current = now if isinstance(now, _dt.datetime) else _dt.datetime.now(_dt.UTC)
    delta = current - ts
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def render_banner(
    matches: dict[str, list[InvalidationEvent]],
    *,
    show_severity: str = "high",
    now: object | None = None,
) -> str:
    """Build the human-readable banner string for the matched events.

    Returns the empty string when no event meets ``show_severity``. The
    caller (``ask.answer``) is expected to skip yielding when this is
    empty and instead log to the audit JSONL only.
    """
    from vigil.impact import severity_at_least

    if not matches:
        return ""

    # Flatten to one row per (event, page) pair so the same VPC detach
    # affecting two runbooks renders both lines.
    rows: list[tuple[InvalidationEvent, str]] = []
    for path, events in matches.items():
        for ev in events:
            if severity_at_least(ev.severity, show_severity):
                rows.append((ev, path))

    if not rows:
        return ""

    # Stable order: highest-severity first, then newest-first within a
    # severity bucket. Python sorts are stable, so two passes (oldest
    # first, then severity descending) gives the desired layering.
    from vigil.impact import SEVERITY_RANK as RANK

    rows.sort(key=lambda r: r[0].timestamp, reverse=True)
    rows.sort(key=lambda r: RANK.get(r[0].severity, -1), reverse=True)

    body = _BANNER_HEAD
    for ev, path in rows:
        full = (
            f"{ev.resource_type}.{ev.resource_id}"
            if ev.resource_type
            else ev.resource_id
        )
        # The "(<id>)" parenthetical only adds value when there's a
        # genuine cloud-side id distinct from the HCL address — e.g.
        # ``aws_vpc.shared (vpc-abc123)``. For IAM-style resources where
        # the HCL name *is* the cloud-side identifier we suppress it.
        bare = ev.resource_id
        is_cloud_id = bare.startswith(_CLOUD_ID_PREFIXES) or bare.startswith("arn:aws:")
        suffix = f" ({bare})" if ev.resource_type and is_cloud_id and bare != full else ""
        age = _humanize_age(ev.timestamp, now=now)
        body += (
            f"   • {full}{suffix} — {ev.action} {age}, "
            f"severity: {ev.severity.upper()}\n"
            f"     affecting {path}\n"
        )
    body += (
        "\n"
        "The retrieved runbooks may be stale. Verify against current infra state\n"
        "before acting. Source: brain-invalidations log.\n"
    )
    body += _BANNER_RULE
    return body


__all__ = [
    "extract_resource_ids",
    "find_invalidations_for_chunks",
    "render_banner",
]
