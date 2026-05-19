"""`vigil memory-import` — stage team-relevant facts from a personal
``~/.claude/`` memory file into a review draft on the team brain.

This is the load-bearing safety module of v0.5. The default for every
candidate entry is **SKIP**. Importing happens only when the user
explicitly checks an opt-in box on the generated draft. Redaction is a
confirmation step — never a bypass.

Hard rules:

    1. ``~/.claude/`` is read-only. No writes, ever.
    2. No entry is auto-imported. The draft has unchecked checkboxes.
       Even when the heuristic flags an entry as "obviously team", the
       box stays unchecked.
    3. Redaction flags (email patterns, internal hostnames, employer
       names) are surfaced *into* the draft, not used to silently rewrite
       the entry. The user confirms per entry.
    4. Output goes to ``<brain_root>/pending-imports/MEMORY-IMPORT-<user>-<date>.md``.
       The brain template's ``.gitignore`` excludes that directory by
       default; teams who want to commit drafts (e.g. for PR review) can
       remove the entry locally.

Classification heuristics (no LLM call by default):

    PERSONAL    — first-person preference / opinion ("I prefer", "my role").
                  Default action: SKIP. Almost never team-shareable.
    TEAM_RULE   — third-person convention ("we deploy", "team uses").
                  Default action: still SKIP — but flagged as a candidate.
    TEAM_FACT   — concrete claim about a service, owner, stack, date.
                  Default action: still SKIP — flagged as candidate.
    REFERENCE   — pointer to an external resource ("see Linear project X").
                  Default action: still SKIP — flagged as candidate.

The "default action: still SKIP" pattern is intentional. The whole
point of the reversed safety bias is that classification proposes; the
human disposes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

# ---------- classification ----------

PERSONAL = "PERSONAL"
TEAM_RULE = "TEAM_RULE"
TEAM_FACT = "TEAM_FACT"
REFERENCE = "REFERENCE"

# Order matters — we report in this order in the draft.
_CLASSIFICATIONS: tuple[str, ...] = (TEAM_RULE, TEAM_FACT, REFERENCE, PERSONAL)

# ---------- redaction patterns ----------

# Email addresses — we don't try to be RFC-correct, just catch the obvious.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Internal-looking hostnames: anything ending in `.internal`, `.corp`,
# `.local`, or `.cluster.*`. Tuned to surface, not to be exhaustive.
_INTERNAL_HOSTNAME_RE = re.compile(
    r"\b[a-z0-9.-]+\.(?:internal|corp|local|cluster\.[a-z]+)\b",
    re.IGNORECASE,
)

# Employer-name patterns — placeholders the user customizes via
# `force_skip` / per-team config. The OSS default ships generic shapes
# we'd never put real employers into. The CI hygiene job catches the
# real names elsewhere.
_DEFAULT_EMPLOYER_PATTERNS: tuple[str, ...] = (
    r"\bacme-corp\b",
    r"\bcustomer-xyz\b",
    r"\byour-org\b",
)


# ---------- dataclasses ----------


@dataclass(frozen=True)
class MemoryEntry:
    """One harvested entry from the user's ``~/.claude/`` memory."""

    source: str  # source file relpath (relative to memory_root)
    line: int  # 1-indexed line number where the entry started
    text: str  # the original entry text — preserved verbatim
    classification: str  # PERSONAL / TEAM_RULE / TEAM_FACT / REFERENCE
    redaction_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportPlan:
    """The draft an engineer reviews before any import lands.

    Every entry defaults to NOT imported. The draft is markdown a human
    edits to opt into specific entries. ``apply_plan`` is intentionally
    *not* part of v0.5 — the apply step lives in a future version once
    we have an answer to "what does importing actually mean for a brain
    that already has these conventions in CLAUDE.md?"
    """

    user: str
    today: _date
    memory_root: Path
    brain_root: Path
    entries: list[MemoryEntry] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        return _render_draft(self)

    def by_classification(self, kind: str) -> list[MemoryEntry]:
        return [e for e in self.entries if e.classification == kind]


# ---------- pure functions ----------


def classify(text: str) -> str:
    """Classify a single entry by heuristic. Pure; no I/O.

    Order matters: PERSONAL wins over TEAM_RULE because a sentence like
    "I prefer that we deploy via X" is fundamentally a personal preference
    even though it contains "we". The whole point of the reversed bias
    is to err on the side of "personal", since SKIP is the default
    anyway.
    """
    t = text.strip().lower()
    if not t:
        return PERSONAL

    # Strip leading list markers and headings so they don't blunt the heuristics.
    t = re.sub(r"^[-*+#>]+\s*", "", t)
    t = re.sub(r"^\d+[.)]\s*", "", t)

    # PERSONAL — strong personal markers anywhere.
    personal_markers = (
        r"\bi prefer\b",
        r"\bi want\b",
        r"\bi like\b",
        r"\bi don'?t\b",
        r"\bmy role\b",
        r"\bmy preference\b",
        r"\bmy opinion\b",
        r"\bfor me\b",
    )
    if any(re.search(p, t) for p in personal_markers):
        return PERSONAL

    # REFERENCE — pointer to an external resource.
    ref_markers = (
        r"\bsee linear\b",
        r"\bsee jira\b",
        r"\bsee confluence\b",
        r"\bconfluence\b.*\bpage\b",
        r"\b(?:slack|notion|github|gitlab|linear|jira)\s*[:#-]",
        r"\bproject\b\s+[a-z0-9-]+",  # "project foo" — heuristic enough
    )
    if any(re.search(p, t) for p in ref_markers):
        return REFERENCE

    # TEAM_RULE — third-person convention.
    rule_markers = (
        r"\bwe (deploy|use|run|require|prefer|ship|track|test|review|never|always)\b",
        r"\bteam (uses|requires|prefers|deploys|owns|tracks|reviews)\b",
        r"\bconvention is\b",
        r"\bevery (engineer|service|repo|pr) (must|should)\b",
        r"\bmust (use|run|deploy|ship|review|pin|commit)\b",
    )
    if any(re.search(p, t) for p in rule_markers):
        return TEAM_RULE

    # TEAM_FACT — concrete claim about ownership, stack, dates.
    fact_markers = (
        r"\bowner\b",
        r"\bowns\b",
        r"\bservice\b",
        r"\bstack\b",
        r"\b(production|prod|staging|dev) cluster\b",
        r"\b(aws|gcp|azure|kubernetes|terraform|argocd|github actions)\b",
        r"\bsince \d{4}\b",
        r"\bas of \d{4}\b",
    )
    if any(re.search(p, t) for p in fact_markers):
        return TEAM_FACT

    # No strong signal — default PERSONAL so the user has to opt in.
    return PERSONAL


def find_redaction_flags(
    text: str, *, employer_patterns: tuple[str, ...] | None = None
) -> list[str]:
    """Return human-readable flags for content the user should review.

    Flags are *not* redactions. We don't rewrite the entry. We surface
    that the entry contains an email / hostname / employer-name match,
    and let the user redact in the draft if they want.
    """
    flags: list[str] = []
    if _EMAIL_RE.search(text):
        flags.append("contains email address")
    if _INTERNAL_HOSTNAME_RE.search(text):
        flags.append("contains internal-looking hostname")
    patterns = employer_patterns if employer_patterns is not None else _DEFAULT_EMPLOYER_PATTERNS
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            flags.append(f"matches employer pattern: {p}")
            break
    return flags


def parse_memory_file(
    path: Path, *, employer_patterns: tuple[str, ...] | None = None
) -> list[MemoryEntry]:
    """Parse a single ``MEMORY.md``-shaped file into entries.

    Treats every non-blank, non-heading line as a candidate entry.
    Bullet lists and free-form lines both work. Headings (``# foo``) are
    skipped as structure, not content.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[MemoryEntry] = []
    relpath = path.name
    for idx, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        # Skip pure horizontal rules / fenced code delimiters.
        if stripped in {"---", "```"} or stripped.startswith("```"):
            continue
        kind = classify(stripped)
        flags = find_redaction_flags(stripped, employer_patterns=employer_patterns)
        entries.append(
            MemoryEntry(
                source=relpath,
                line=idx,
                text=stripped,
                classification=kind,
                redaction_flags=flags,
            )
        )
    return entries


def discover_memory_files(memory_root: Path) -> list[Path]:
    """Find ``MEMORY.md`` and the topic files it links to.

    Two layouts are supported:

      1. ``memory_root/MEMORY.md`` directly. The simplest case — the
         user passed a precise path.
      2. Claude Code's default layout:
         ``~/.claude/projects/<project-id>/memory/MEMORY.md``. If we
         don't find ``MEMORY.md`` directly under ``memory_root``, we
         look one level down (``memory_root/projects/<id>/memory/``)
         and re-anchor to the first project-memory dir we find. We
         accept this is a heuristic — multi-project users should pass
         ``--memory-root`` explicitly to pick the right one.

    Sibling ``feedback_*.md`` / ``project_*.md`` / ``reference_*.md``
    files at the same level as the discovered ``MEMORY.md`` are also
    surfaced.
    """
    out: list[Path] = []
    primary = memory_root / "MEMORY.md"
    anchor = memory_root
    if not primary.is_file():
        # Fall back to Claude Code's nested layout. We don't try to
        # de-dupe across projects; the first hit wins, and the user
        # passes ``--memory-root`` to be explicit if they have many.
        projects_dir = memory_root / "projects"
        if projects_dir.is_dir():
            for proj in sorted(projects_dir.iterdir()):
                if not proj.is_dir():
                    continue
                candidate = proj / "memory" / "MEMORY.md"
                if candidate.is_file():
                    primary = candidate
                    anchor = candidate.parent
                    break
    if primary.is_file():
        out.append(primary)
    if anchor.is_dir():
        for p in sorted(anchor.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() != ".md":
                continue
            if p == primary:
                continue
            name = p.name.lower()
            if name.startswith(("feedback_", "project_", "reference_")):
                out.append(p)
    return out


# ---------- planner ----------


def harvest_user_memory(
    memory_root: Path,
    brain_root: Path,
    *,
    user: str = "user",
    today: _date | None = None,
    employer_patterns: tuple[str, ...] | None = None,
    force_skip: list[str] | None = None,
) -> ImportPlan:
    """Walk the user's memory files, classify every entry, return a plan.

    No I/O is performed against the brain. The plan is a data structure
    the CLI later renders into a draft markdown file the user reviews.

    ``force_skip`` is a list of substrings; any entry whose text matches
    one is dropped from the plan entirely (not just "skipped" — never
    surfaced). Intended for known-personal phrases the user doesn't
    even want to see in the draft.
    """
    today = today or _date.today()
    force_skip_norm = [s.lower() for s in (force_skip or []) if s]
    plan_entries: list[MemoryEntry] = []
    skipped_files: list[str] = []
    files = discover_memory_files(memory_root)
    for f in files:
        try:
            entries = parse_memory_file(f, employer_patterns=employer_patterns)
        except OSError:
            skipped_files.append(str(f))
            continue
        for e in entries:
            if any(s in e.text.lower() for s in force_skip_norm):
                continue
            plan_entries.append(e)
    return ImportPlan(
        user=user,
        today=today,
        memory_root=memory_root,
        brain_root=brain_root,
        entries=plan_entries,
        skipped_files=skipped_files,
    )


# ---------- draft renderer ----------


def _render_draft(plan: ImportPlan) -> str:
    lines: list[str] = []
    date_str = plan.today.isoformat()
    lines.append(f"# Memory import draft — {plan.user} — {date_str}")
    lines.append("")
    lines.append(
        "Default for every entry below is **SKIP**. To import an entry, "
        "edit this file and check its `[ ] IMPORT THIS` box. Save your "
        "preferred redactions inline. The CLI never auto-imports — your "
        "checkmark is the only thing that lands the entry."
    )
    lines.append("")
    lines.append(f"- Source memory root: `{plan.memory_root}`")
    lines.append(f"- Target brain root: `{plan.brain_root}`")
    total = len(plan.entries)
    flagged = sum(1 for e in plan.entries if e.redaction_flags)
    lines.append(f"- Entries surfaced: {total}  (flagged for redaction: {flagged})")
    lines.append("")
    lines.append("---")
    lines.append("")

    section_titles = {
        TEAM_RULE: "Team rules — third-person conventions",
        TEAM_FACT: "Team facts — concrete claims",
        REFERENCE: "References — pointers to external resources",
        PERSONAL: "Personal — surfaced for completeness; almost never imports",
    }

    counter = 0
    for kind in _CLASSIFICATIONS:
        bucket = plan.by_classification(kind)
        lines.append(f"## {section_titles[kind]} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("_None._")
            lines.append("")
            continue
        for entry in bucket:
            counter += 1
            lines.append(f"### Entry {counter} — {entry.classification}")
            lines.append("")
            lines.append("- [ ] IMPORT THIS")
            lines.append(f"- Source: `{entry.source}` line {entry.line}")
            lines.append(f"- Original: {entry.text}")
            if entry.redaction_flags:
                joined = "; ".join(entry.redaction_flags)
                lines.append(f"- Redaction flags: {joined}")
                lines.append(
                    "- Suggested redactions: replace flagged values with "
                    "generic placeholders before checking the box."
                )
            else:
                lines.append("- Redaction flags: none")
            lines.append("")

    if plan.skipped_files:
        lines.append("## Files we couldn't read")
        lines.append("")
        for p in plan.skipped_files:
            lines.append(f"- `{p}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_When you're done, commit only this file. The brain's CI will "
        "diff it against future commits to show what landed and what "
        "didn't. Drafts live under `pending-imports/` and are gitignored "
        "by default — remove the entry from `.gitignore` if your team "
        "wants the draft itself in PR review._"
    )
    return "\n".join(lines).rstrip() + "\n"


def write_plan(plan: ImportPlan) -> Path:
    """Write the draft to ``<brain_root>/pending-imports/MEMORY-IMPORT-<user>-<date>.md``."""
    out_dir = plan.brain_root / "pending-imports"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = re.sub(r"[^A-Za-z0-9._-]+", "-", plan.user) or "user"
    name = f"MEMORY-IMPORT-{safe_user}-{plan.today.isoformat()}.md"
    out_path = out_dir / name
    out_path.write_text(plan.to_markdown(), encoding="utf-8")
    return out_path


__all__ = [
    "PERSONAL",
    "REFERENCE",
    "TEAM_FACT",
    "TEAM_RULE",
    "ImportPlan",
    "MemoryEntry",
    "classify",
    "discover_memory_files",
    "find_redaction_flags",
    "harvest_user_memory",
    "parse_memory_file",
    "write_plan",
]
