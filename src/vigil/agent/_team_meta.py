"""Shared parser for ``knowledge/people.md`` and ``knowledge/services.md``.

Three v0.10 routines (``invalidation_digest``, ``targeted_radar``,
``auto_pr_drafter``) need the same lookup: who owns this resource? who
edits this part of the tree? Centralising the parser here keeps the
schema honest and prevents three slightly-different regex copies from
drifting.

Schema (tolerant — accept either form):

  ``knowledge/people.md`` — one engineer per bullet line. Two shapes:

      - alice <alice@team>           — Auth Service owner, on-call rotation A
      - bob  <bob@example.com>       — Platform team

  …or a markdown table::

      | id    | email             | role                |
      |-------|-------------------|---------------------|
      | alice | alice@team        | Auth Service owner  |
      | bob   | bob@example.com   | Platform team       |

  ``knowledge/services.md`` — one service per bullet line. Two shapes:

      - auth-service: alice — owns aws_iam_role.deploy-bot, vpc-abc123
      - billing: bob — owns aws_db_instance.billing-primary

  …or a markdown table::

      | service       | owner | resources                              |
      |---------------|-------|----------------------------------------|
      | auth-service  | alice | aws_iam_role.deploy-bot, vpc-abc123   |
      | billing       | bob   | aws_db_instance.billing-primary       |

The parsers are intentionally permissive — empty files, missing files,
or malformed lines all yield empty results, never raise.

Hard rule: every match key (engineer id, service name) is lower-cased
on read so downstream comparisons don't need to repeat the dance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Engineer:
    """One row from ``knowledge/people.md``."""

    id: str               # short handle, lower-cased; e.g. "alice"
    email: str            # lower-cased; e.g. "alice@team"
    role: str = ""        # free-form description after the em-dash


@dataclass(frozen=True)
class Service:
    """One row from ``knowledge/services.md``."""

    name: str                              # short handle, lower-cased
    owner: str                             # engineer id (lower-cased)
    resources: tuple[str, ...] = ()        # e.g. ("aws_iam_role.deploy-bot",)
    description: str = ""

    def matches_resource(self, resource: str) -> bool:
        """``True`` if ``resource`` is in this service's owned-resources list.

        We accept exact match against any listed entry. The substring
        match handles the case where the page lists ``aws_vpc.shared``
        but an event names ``vpc-abc123`` — the routine's caller passes
        both forms.
        """
        if not resource:
            return False
        rl = resource.lower()
        return any(rl == r.lower() or rl in r.lower() or r.lower() in rl for r in self.resources)


# ---------- people.md ----------

# ``- alice <alice@team> — role`` — bullet-and-angle-bracket form.
_PEOPLE_BULLET_RE = re.compile(
    r"^\s*[-*]\s+([A-Za-z0-9._-]+)\s*<([^>]+)>\s*(?:[—-]+\s*(.+?))?\s*$"
)

# Table row form. We require ``id``, ``email``; role is optional.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


def _parse_people_table(lines: list[str]) -> list[Engineer]:
    """Walk a markdown table block; return engineer rows.

    The header is required to have ``id`` and ``email`` columns. Rows
    that don't have both are skipped.
    """
    if not lines:
        return []
    # Find header row + index of id / email / role columns
    header_idx = None
    for i, ln in enumerate(lines):
        if "id" in ln.lower() and "email" in ln.lower() and ln.lstrip().startswith("|"):
            header_idx = i
            break
    if header_idx is None:
        return []
    header_cells = [c.strip().lower() for c in lines[header_idx].strip().strip("|").split("|")]
    try:
        id_col = header_cells.index("id")
        email_col = header_cells.index("email")
    except ValueError:
        return []
    role_col = header_cells.index("role") if "role" in header_cells else -1

    out: list[Engineer] = []
    # Skip the separator line right after the header
    for ln in lines[header_idx + 2 :]:
        m = _TABLE_ROW_RE.match(ln)
        if not m:
            break
        cells = [c.strip() for c in m.group(1).split("|")]
        if max(id_col, email_col) >= len(cells):
            continue
        eng_id = cells[id_col].lower()
        email = cells[email_col].lower()
        role = cells[role_col] if 0 <= role_col < len(cells) else ""
        if not eng_id or not email:
            continue
        out.append(Engineer(id=eng_id, email=email, role=role))
    return out


def parse_people(people_md: Path) -> list[Engineer]:
    """Return the list of engineers declared in ``knowledge/people.md``.

    Empty / missing file → empty list. Order matches source-file order.
    Duplicate ids are kept (caller decides how to dedupe).
    """
    if not people_md.is_file():
        return []
    try:
        text = people_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Engineer] = []
    seen: set[str] = set()
    lines = text.splitlines()
    # First pass — bullet form (most common).
    for ln in lines:
        m = _PEOPLE_BULLET_RE.match(ln)
        if not m:
            continue
        eng_id = m.group(1).strip().lower()
        email = m.group(2).strip().lower()
        role = (m.group(3) or "").strip()
        key = f"{eng_id}|{email}"
        if key in seen:
            continue
        seen.add(key)
        out.append(Engineer(id=eng_id, email=email, role=role))
    # Second pass — table form. Augment, don't override.
    for eng in _parse_people_table(lines):
        key = f"{eng.id}|{eng.email}"
        if key in seen:
            continue
        seen.add(key)
        out.append(eng)
    return out


# ---------- services.md ----------

# ``- auth-service: alice — owns aws_iam_role.deploy-bot, vpc-abc123``
_SERVICES_BULLET_RE = re.compile(
    r"^\s*[-*]\s+([A-Za-z0-9._-]+)\s*[:|-]\s*([A-Za-z0-9._-]+)\s*(?:[—-]+\s*(.+?))?\s*$"
)

_OWNS_RE = re.compile(r"\bowns?\s+(.+)", re.IGNORECASE)


def _split_resources(text: str) -> tuple[str, ...]:
    """Split a "owns X, Y, Z" tail into a clean tuple."""
    if not text:
        return ()
    # Strip a leading "owns " keyword if present.
    m = _OWNS_RE.search(text)
    body = m.group(1) if m else text
    parts = [p.strip().strip(".,;") for p in re.split(r"[,;]", body)]
    return tuple(p for p in parts if p)


def _parse_services_table(lines: list[str]) -> list[Service]:
    if not lines:
        return []
    header_idx = None
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "service" in low and "owner" in low and ln.lstrip().startswith("|"):
            header_idx = i
            break
    if header_idx is None:
        return []
    header_cells = [c.strip().lower() for c in lines[header_idx].strip().strip("|").split("|")]
    try:
        name_col = header_cells.index("service")
        owner_col = header_cells.index("owner")
    except ValueError:
        return []
    res_col = header_cells.index("resources") if "resources" in header_cells else -1

    out: list[Service] = []
    for ln in lines[header_idx + 2 :]:
        m = _TABLE_ROW_RE.match(ln)
        if not m:
            break
        cells = [c.strip() for c in m.group(1).split("|")]
        if max(name_col, owner_col) >= len(cells):
            continue
        name = cells[name_col].lower()
        owner = cells[owner_col].lower()
        if not name or not owner:
            continue
        resources: tuple[str, ...] = ()
        if 0 <= res_col < len(cells):
            resources = _split_resources(cells[res_col])
        out.append(Service(name=name, owner=owner, resources=resources))
    return out


def parse_services(services_md: Path) -> list[Service]:
    """Return the list of services declared in ``knowledge/services.md``.

    Empty / missing file → empty list.
    """
    if not services_md.is_file():
        return []
    try:
        text = services_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Service] = []
    seen: set[str] = set()
    lines = text.splitlines()
    for ln in lines:
        m = _SERVICES_BULLET_RE.match(ln)
        if not m:
            continue
        name = m.group(1).strip().lower()
        owner = m.group(2).strip().lower()
        tail = (m.group(3) or "").strip()
        # Skip lines that look like people entries (bullet form has angle
        # brackets, services don't).
        if "<" in ln or "@" in m.group(2):
            continue
        resources = _split_resources(tail) if tail else ()
        if name in seen:
            continue
        seen.add(name)
        out.append(Service(
            name=name,
            owner=owner,
            resources=resources,
            description=tail if not _OWNS_RE.search(tail) else "",
        ))
    for svc in _parse_services_table(lines):
        if svc.name in seen:
            continue
        seen.add(svc.name)
        out.append(svc)
    return out


# ---------- helpers ----------


@dataclass(frozen=True)
class TeamMeta:
    """Convenience bundle of parsed people + services for one brain root."""

    engineers: list[Engineer] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)

    def engineer_by_email(self, email: str) -> Engineer | None:
        if not email:
            return None
        e = email.lower()
        for eng in self.engineers:
            if eng.email == e:
                return eng
        return None

    def engineer_by_id(self, eng_id: str) -> Engineer | None:
        if not eng_id:
            return None
        i = eng_id.lower()
        for eng in self.engineers:
            if eng.id == i:
                return eng
        return None

    def services_owning_resource(self, resource: str) -> list[Service]:
        """Return every service whose ``resources`` list contains ``resource``."""
        return [s for s in self.services if s.matches_resource(resource)]


def load_team_meta(brain_root: Path) -> TeamMeta:
    """Convenience loader — read both files at once.

    Looks for ``knowledge/people.md`` and ``knowledge/services.md``
    relative to ``brain_root``. Either may be missing.
    """
    return TeamMeta(
        engineers=parse_people(brain_root / "knowledge" / "people.md"),
        services=parse_services(brain_root / "knowledge" / "services.md"),
    )


__all__ = [
    "Engineer",
    "Service",
    "TeamMeta",
    "load_team_meta",
    "parse_people",
    "parse_services",
]
