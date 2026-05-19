"""`vigil memory-export` — departing-engineer handover artifact.

When an engineer leaves, their personal ``~/.claude/`` memory carries
real team value: who owns what, why we picked X over Y, the on-call
quirks nobody wrote down. This command pulls that out of their personal
memory file and dumps it as a single markdown handover, ready to leave
behind for the successor.

Differences from :mod:`vigil.memory_import`:

  * Default is to *include* TEAM_RULE / TEAM_FACT / REFERENCE entries —
    this is a leaving artifact, the user explicitly asked for the dump.
    PERSONAL entries are still SKIP by default.
  * Output is one self-contained markdown file the user gives to
    whoever inherits the role.
  * A "things you should know about how I worked" section is left
    blank for the user to fill in. We don't try to harvest it from
    memory — that's narrative, not data.

The classifier is the same one :mod:`vigil.memory_import` uses; we
re-use the heuristic and reverse only the *default action*, not the
*classification*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

from vigil.memory_import import (
    PERSONAL,
    REFERENCE,
    TEAM_FACT,
    TEAM_RULE,
    MemoryEntry,
    discover_memory_files,
    parse_memory_file,
)

# ---------- dataclasses ----------


@dataclass(frozen=True)
class ExportPlan:
    """The handover artifact the departing engineer leaves behind."""

    user: str
    today: _date
    memory_root: Path
    entries: list[MemoryEntry] = field(default_factory=list)
    redact: bool = False
    free_form_notes: str = ""

    def to_markdown(self) -> str:
        return _render_handover(self)


# ---------- redaction (this module — opt-in only) ----------


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_INTERNAL_HOSTNAME_RE = re.compile(
    r"\b[a-z0-9.-]+\.(?:internal|corp|local|cluster\.[a-z]+)\b",
    re.IGNORECASE,
)


def _apply_redactions(text: str) -> str:
    """Replace email and internal-hostname matches with generic placeholders.

    Used only when ``--no-redact`` is *not* passed. Heuristic by design —
    the leaving artifact is a starting point the successor will edit.
    """
    text = _EMAIL_RE.sub("alice.dev@acme-corp.com", text)
    text = _INTERNAL_HOSTNAME_RE.sub("db01.prod.internal", text)
    return text


# ---------- since-filter ----------


_SINCE_LINE_RE = re.compile(r"\bsince (\d{4})\b", re.IGNORECASE)
_AS_OF_LINE_RE = re.compile(r"\bas of (\d{4})\b", re.IGNORECASE)


def _entry_since_year(entry: MemoryEntry) -> int | None:
    """Best-effort: pull a year out of an entry, used for ``--since`` filtering.

    We don't try to be clever — the heuristic looks for "since YYYY" or
    "as of YYYY" tokens. If none are found, the entry is *kept* (we'd
    rather over-include than over-prune in a leaving artifact).
    """
    for re_ in (_SINCE_LINE_RE, _AS_OF_LINE_RE):
        m = re_.search(entry.text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


# ---------- planner ----------


def export_for_handover(
    memory_root: Path,
    *,
    user: str = "user",
    today: _date | None = None,
    since: str | None = None,
    redact: bool = True,
    free_form_notes: str = "",
) -> ExportPlan:
    """Build a handover plan from the user's memory.

    By default, includes TEAM_RULE / TEAM_FACT / REFERENCE entries.
    PERSONAL entries are still excluded — this is a *team* handover, not
    a journal.

    ``since="2024-01-01"`` keeps entries with a year stamp >= that year,
    plus all unstamped entries. ``redact=False`` skips the redaction
    pass entirely (the user accepts that internal hostnames / emails
    stay verbatim).
    """
    today = today or _date.today()
    files = discover_memory_files(memory_root)
    selected: list[MemoryEntry] = []
    since_year: int | None = None
    if since:
        m = re.match(r"^(\d{4})", since)
        if m:
            try:
                since_year = int(m.group(1))
            except ValueError:
                since_year = None

    for f in files:
        for entry in parse_memory_file(f):
            if entry.classification == PERSONAL:
                continue
            if since_year is not None:
                year = _entry_since_year(entry)
                if year is not None and year < since_year:
                    continue
            text = entry.text if not redact else _apply_redactions(entry.text)
            selected.append(
                MemoryEntry(
                    source=entry.source,
                    line=entry.line,
                    text=text,
                    classification=entry.classification,
                    redaction_flags=entry.redaction_flags,
                )
            )
    return ExportPlan(
        user=user,
        today=today,
        memory_root=memory_root,
        entries=selected,
        redact=redact,
        free_form_notes=free_form_notes,
    )


# ---------- renderer ----------


_SECTION_ORDER: tuple[str, ...] = (TEAM_RULE, TEAM_FACT, REFERENCE)
_SECTION_TITLES = {
    TEAM_RULE: "Team rules — conventions every engineer should follow",
    TEAM_FACT: "Team facts — services, owners, stack",
    REFERENCE: "References — pointers to external resources",
}


def _render_handover(plan: ExportPlan) -> str:
    lines: list[str] = []
    date_str = plan.today.isoformat()
    lines.append(f"# Handover — {plan.user} — {date_str}")
    lines.append("")
    lines.append(
        "This file is a leaving artifact. It captures the team-relevant "
        "facts that lived in the departing engineer's personal "
        "`~/.claude/` memory. Hand it to your successor or fold the "
        "useful entries into the team brain (`vigil memory-import` "
        "on the receiver's machine)."
    )
    lines.append("")
    redact_note = "redacted" if plan.redact else "verbatim (no redaction pass)"
    lines.append(f"- Memory root: `{plan.memory_root}`")
    lines.append(f"- Redaction: {redact_note}")
    lines.append(f"- Entries: {len(plan.entries)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    by_kind: dict[str, list[MemoryEntry]] = {k: [] for k in _SECTION_ORDER}
    for entry in plan.entries:
        if entry.classification in by_kind:
            by_kind[entry.classification].append(entry)

    for kind in _SECTION_ORDER:
        bucket = by_kind[kind]
        lines.append(f"## {_SECTION_TITLES[kind]} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("_None._")
            lines.append("")
            continue
        for entry in bucket:
            lines.append(f"- {entry.text}")
            lines.append(f"  _source: `{entry.source}` line {entry.line}_")
        lines.append("")

    lines.append("## Things you should know about how I worked")
    lines.append("")
    if plan.free_form_notes.strip():
        lines.append(plan.free_form_notes.strip())
        lines.append("")
    else:
        lines.append("<!-- The author left this section blank. Fill in:")
        lines.append("- on-call quirks the runbook doesn't mention")
        lines.append("- people you should talk to in your first month")
        lines.append("- the one tool/script that always saved you time")
        lines.append("- the one thing the team is wrong about that you didn't")
        lines.append("  push hard enough on")
        lines.append("-->")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `vigil memory-export`. If you spot something "
        "that shouldn't have left the building, redact it before you "
        "share the file. The exporter is read-only on `~/.claude/` — "
        "this artifact is the only thing it produces._"
    )
    return "\n".join(lines).rstrip() + "\n"


def write_handover(plan: ExportPlan, out_dir: Path) -> Path:
    """Write the handover to ``out_dir/HANDOVER-<user>-<date>.md``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = re.sub(r"[^A-Za-z0-9._-]+", "-", plan.user) or "user"
    name = f"HANDOVER-{safe_user}-{plan.today.isoformat()}.md"
    out_path = out_dir / name
    out_path.write_text(plan.to_markdown(), encoding="utf-8")
    return out_path


__all__ = [
    "ExportPlan",
    "export_for_handover",
    "write_handover",
]
