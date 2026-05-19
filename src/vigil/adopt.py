"""`vigil adopt` — mid-project file migration into a team-brain layout.

Most teams don't start with vigil. They have years of markdown scattered
across a docs site, a `runbooks/` folder, a `wiki/` someone abandoned, plus
the inevitable root-level `NOTES.md`. `adopt` walks the existing project,
classifies what's there, fills gaps from the bundled template, and surfaces
a plan the team can read on a PR.

Hard rules:

  1. ``--dry-run`` is the default. ``--apply`` must be passed explicitly.
  2. Even with ``--apply``, we never move existing files. Move *suggestions*
     are surfaced in the plan; humans execute them in their own commit.
  3. Refuse to run on a brain with uncommitted git changes — the brain's
     git history is the audit trail and CI must never auto-mutate it.
     If there is no ``.git`` directory, we proceed: nothing to preserve.
  4. Per-engineer files (``.claude/settings*.json``) are never touched.
  5. If the same path exists in both template and the project, keep the
     project's version. Never auto-merge content.

Classification:

  KEEP                  — at template path; nothing to do.
  MOVE_SUGGESTED        — looks like a runbook/doc but lives at a
                          non-canonical path (e.g. ``wiki/foo.md``).
  REVIEW                — heterogeneous root-level markdown the tool can't
                          confidently classify.
  ADD                   — template path is empty; gap to fill.
  SKIP_PER_ENGINEER     — never team-shareable (settings*.json).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ---------- defaults ----------

# Paths that the brain template defines. Files at these paths/under these
# prefixes are KEEPers; if absent and present in the bundled template, ADD.
_CANONICAL_FILES: tuple[str, ...] = (
    "CLAUDE.md",
)
_CANONICAL_PREFIXES: tuple[str, ...] = (
    ".claude/skills/",
    ".claude/rules/",
    ".claude/commands/",
    "docs/",
    "knowledge/",
    "runbooks/",
)

# Default include patterns — these are the project areas adopt scans for
# adoption candidates. Patterns are simple substring matches against the
# relpath; we don't pull in fnmatch / glob complexity here.
_DEFAULT_INCLUDE: tuple[str, ...] = (
    "CLAUDE.md",
    "docs/",
    "knowledge/",
    "runbooks/",
    "wiki/",
    "notes/",
    ".claude/skills/",
    ".claude/rules/",
    ".claude/commands/",
)

# Default exclude — directory parts (top-level or any depth) that we never
# descend into.
_DEFAULT_EXCLUDE_DIR_PARTS: tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".vigil-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    # Author/personal output dirs — not team-brain content. Hard-coded to
    # protect against picking up the assistant's working directory if a user
    # runs adopt at $HOME by accident.
    "articles",
    "oss",
    "applications",
    "portfolio",
    "interview-prep",
    "invest",
    "resume",
    "safe-poc",
)

# Hidden-directory pattern (matches `.foo` at the start of a relative part).
_HIDDEN_DIR_RE = re.compile(r"^\.[a-zA-Z][a-zA-Z0-9_-]*$")

# Per-engineer files we explicitly skip — never team-shareable.
_PER_ENGINEER_PATTERNS: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
)


KEEP = "KEEP"
MOVE_SUGGESTED = "MOVE_SUGGESTED"
REVIEW = "REVIEW"
ADD = "ADD"
SKIP_PER_ENGINEER = "SKIP_PER_ENGINEER"


# ---------- dataclasses ----------


@dataclass(frozen=True)
class AdoptEntry:
    """One file's classification."""

    path: str  # relpath from brain_root
    action: str  # KEEP / MOVE_SUGGESTED / REVIEW / ADD / SKIP_PER_ENGINEER
    reason: str
    suggested_target: str | None = None  # for MOVE_SUGGESTED + ADD


@dataclass
class AdoptPlan:
    """The full migration plan — read-only summary of what `adopt` would do."""

    brain_root: Path
    entries: list[AdoptEntry] = field(default_factory=list)
    claude_md_split_suggestion: list[str] | None = None
    git_clean: bool = True
    git_present: bool = True

    def by_action(self, action: str) -> list[AdoptEntry]:
        return [e for e in self.entries if e.action == action]

    def to_markdown(self) -> str:
        """Render a human-readable migration plan suitable as a PR comment."""
        lines: list[str] = ["# vigil adopt — migration plan", ""]
        lines.append(f"**Brain root:** `{self.brain_root}`")
        if not self.git_present:
            lines.append(
                "**Git status:** no `.git` found — git audit trail check skipped."
            )
        elif not self.git_clean:
            lines.append(
                "**Git status:** uncommitted changes present — `--apply` will refuse."
            )
        else:
            lines.append("**Git status:** clean.")
        lines.append("")

        sections = [
            (KEEP, "Keep — already at template path"),
            (ADD, "Add — template gap to fill"),
            (MOVE_SUGGESTED, "Move suggested — non-canonical path"),
            (REVIEW, "Review — needs human classification"),
            (SKIP_PER_ENGINEER, "Skip — per-engineer, never team-shareable"),
        ]
        for action, header in sections:
            entries = self.by_action(action)
            lines.append(f"## {header} ({len(entries)})")
            lines.append("")
            if not entries:
                lines.append("_None._")
                lines.append("")
                continue
            for e in entries:
                target = (
                    f" → `{e.suggested_target}`" if e.suggested_target else ""
                )
                lines.append(f"- `{e.path}`{target} — {e.reason}")
            lines.append("")

        if self.claude_md_split_suggestion:
            lines.append("## CLAUDE.md split suggestion")
            lines.append("")
            lines.append(
                "CLAUDE.md exceeds the size budget. Suggested split (one chunk per "
                "`.claude/rules/<topic>.md`):"
            )
            lines.append("")
            for chunk in self.claude_md_split_suggestion:
                lines.append(f"- `.claude/rules/{chunk}`")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


# ---------- helpers ----------


def _git_clean(brain_root: Path) -> tuple[bool, bool]:
    """Return (git_present, clean).

    `clean` is True when there are no uncommitted changes. If git isn't
    installed or the dir isn't a repo, we report (False, True) — nothing
    to preserve, so the apply gate is open.
    """
    git_dir = brain_root / ".git"
    if not git_dir.exists():
        return False, True
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(brain_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # git not installed / hangs — be safe, treat as dirty.
        return True, False
    if result.returncode != 0:
        return True, False
    return True, result.stdout.strip() == ""


def _bundled_template_dir() -> Path:
    """Locate the team-brain skeleton bundled with the package.

    Resolution order:
      1. Source checkout (running from a repo clone) — `<repo>/templates/team-brain-skeleton`.
      2. Installed wheel (pip install) — `<sys.prefix>/share/vigil/team-brain-skeleton`
         per `pyproject.toml`'s `[tool.hatch.build.targets.wheel.shared-data]` entry.
      3. User-site install — `~/.local/share/vigil/team-brain-skeleton`.
      4. Final fallback — package-local `templates/` (dev convenience).
    """
    import site
    import sys

    pkg_root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        pkg_root / "templates" / "team-brain-skeleton",
        Path(sys.prefix) / "share" / "vigil" / "team-brain-skeleton",
        Path(site.getuserbase()) / "share" / "vigil" / "team-brain-skeleton",
        Path(__file__).resolve().parent / "templates" / "team-brain-skeleton",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    # Return the source-checkout path even if missing — caller surfaces a clear error.
    return candidates[0]


def _path_excluded(rel_parts: tuple[str, ...], extra_excludes: Iterable[str]) -> bool:
    """True if any part of the relative path matches an exclude rule."""
    for part in rel_parts:
        if part in _DEFAULT_EXCLUDE_DIR_PARTS:
            return True
        # Hidden directory — but allow `.claude/` and `.github/` since those
        # are first-class brain content.
        if _HIDDEN_DIR_RE.match(part) and part not in {".claude", ".github"}:
            return True
    relpath = "/".join(rel_parts)
    return any(pattern and pattern in relpath for pattern in extra_excludes)


def _path_included(relpath: str, includes: Iterable[str]) -> bool:
    for pattern in includes:
        if not pattern:
            continue
        if relpath == pattern:
            return True
        if pattern.endswith("/") and relpath.startswith(pattern):
            return True
        # Bare filename: exact match only.
        if not pattern.endswith("/") and relpath == pattern:
            return True
    return False


def _is_per_engineer(relpath: str) -> bool:
    return any(relpath == p or relpath.endswith("/" + p) for p in _PER_ENGINEER_PATTERNS)


def _is_canonical_path(relpath: str) -> bool:
    if relpath in _CANONICAL_FILES:
        return True
    return any(relpath.startswith(p) for p in _CANONICAL_PREFIXES)


def _looks_like_runbook(relpath: str) -> bool:
    """Heuristic: a runbook/doc that should move into a canonical section."""
    rel = relpath.lower()
    return (
        rel.startswith("wiki/")
        or rel.startswith("notes/")
        or rel.startswith("wiki-archive/")
    ) and rel.endswith(".md")


def _suggested_target_for(relpath: str) -> str | None:
    """Where would we suggest moving a non-canonical markdown file?"""
    if relpath.startswith("wiki/") and relpath.endswith(".md"):
        return "docs/" + relpath[len("wiki/") :]
    if relpath.startswith("notes/") and relpath.endswith(".md"):
        return "knowledge/" + relpath[len("notes/") :]
    if relpath.startswith("wiki-archive/") and relpath.endswith(".md"):
        return "docs/archive/" + relpath[len("wiki-archive/") :]
    return None


def _walk_existing(
    brain_root: Path, includes: Iterable[str], excludes: Iterable[str]
) -> list[tuple[str, Path]]:
    """Yield (relpath, abspath) for every file inside an included path."""
    results: list[tuple[str, Path]] = []
    try:
        candidates = list(brain_root.rglob("*"))
    except OSError:
        return []
    for p in candidates:
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(brain_root)
        except ValueError:
            continue
        rel_parts = rel.parts
        if _path_excluded(rel_parts, excludes):
            continue
        relpath = str(rel)
        if not _path_included(relpath, includes):
            continue
        results.append((relpath, p))
    return results


def _bundled_template_files() -> list[str]:
    """Relative paths of every file in the bundled team-brain template."""
    src = _bundled_template_dir()
    if not src.is_dir():
        return []
    out: list[str] = []
    for p in src.rglob("*"):
        if p.is_file():
            out.append(str(p.relative_to(src)))
    return sorted(out)


def _suggest_claude_md_split(claude_md_text: str, max_kb: int) -> list[str] | None:
    """Heuristic split: group H2s into <2KB chunks; return suggested filenames."""
    lines = claude_md_text.splitlines()
    chunks: list[str] = []
    current_h2 = "intro"
    current_size = 0
    chunk_idx = 1
    seen_any = False
    for line in lines:
        if line.startswith("## "):
            if seen_any and current_size > 0:
                slug = _slugify(current_h2) or f"chunk-{chunk_idx}"
                chunks.append(f"{slug}.md")
                chunk_idx += 1
                current_size = 0
            current_h2 = line[3:].strip()
            seen_any = True
        current_size += len(line) + 1
        if current_size >= 2048:
            slug = _slugify(current_h2) or f"chunk-{chunk_idx}"
            chunks.append(f"{slug}.md")
            chunk_idx += 1
            current_size = 0
    if not chunks:
        return None
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in chunks:
        if c in seen:
            c = f"{c[:-3]}-{len(deduped) + 1}.md"
        seen.add(c)
        deduped.append(c)
    return deduped


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return s.strip("-")


# ---------- frontmatter helpers (apply mode) ----------


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---"):
        return {}, raw
    closing = raw.find("\n---", 3)
    if closing == -1:
        return {}, raw
    fm_text = raw[3:closing].strip()
    body_start = raw.find("\n", closing + 4)
    body = raw[body_start + 1 :] if body_start != -1 else ""
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, body


def _serialize_with_frontmatter(fm: dict, body: str) -> str:
    if not fm:
        return body
    fm_text = yaml.safe_dump(fm, sort_keys=True).strip()
    return f"---\n{fm_text}\n---\n{body}"


def _stamp_template_marker(text: str) -> str:
    """Inject `vigil_template: true` into frontmatter, merging if present."""
    fm, body = _split_frontmatter(text)
    fm["vigil_template"] = True
    return _serialize_with_frontmatter(fm, body)


# ---------- public API ----------


def adopt(
    brain_root: Path,
    *,
    dry_run: bool = True,
    apply: bool = False,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_claude_md_kb: int = 4,
) -> AdoptPlan:
    """Walk the project, classify, optionally fill template gaps.

    Always returns a plan. With ``apply=True`` (and a clean git tree),
    template gap files are copied into place and a ``MIGRATION.md`` summary
    is written at the brain root.

    `include` and `exclude` *extend* the defaults (Click's ``multiple=True``
    semantics) rather than replacing them. To remove a default-included
    path, pass it via `--exclude`.
    """
    brain_root = Path(brain_root).resolve()
    includes = list(_DEFAULT_INCLUDE) + list(include or [])
    excludes = list(exclude or [])

    git_present, git_clean = _git_clean(brain_root)
    plan = AdoptPlan(brain_root=brain_root, git_clean=git_clean, git_present=git_present)

    # 1) Classify every file the user has under the include patterns.
    seen_relpaths: set[str] = set()
    for relpath, _abspath in _walk_existing(brain_root, includes, excludes):
        seen_relpaths.add(relpath)
        if _is_per_engineer(relpath):
            plan.entries.append(
                AdoptEntry(
                    path=relpath,
                    action=SKIP_PER_ENGINEER,
                    reason="per-engineer settings — never team-shareable",
                )
            )
            continue
        if _is_canonical_path(relpath):
            plan.entries.append(
                AdoptEntry(
                    path=relpath,
                    action=KEEP,
                    reason="already at canonical path",
                )
            )
            continue
        if _looks_like_runbook(relpath):
            target = _suggested_target_for(relpath)
            plan.entries.append(
                AdoptEntry(
                    path=relpath,
                    action=MOVE_SUGGESTED,
                    reason="non-canonical path; move into docs/ or knowledge/",
                    suggested_target=target,
                )
            )
            continue
        # Root-level markdown that we couldn't classify — surface for review.
        if "/" not in relpath and relpath.endswith(".md"):
            plan.entries.append(
                AdoptEntry(
                    path=relpath,
                    action=REVIEW,
                    reason="root-level markdown — needs human classification",
                )
            )
            continue
        # Anything else included but not classified is REVIEW, too.
        plan.entries.append(
            AdoptEntry(
                path=relpath,
                action=REVIEW,
                reason="included but not in a canonical section",
            )
        )

    # 2) Look for template gaps — paths the bundled template ships but the
    #    project doesn't have.
    src = _bundled_template_dir()
    template_files = _bundled_template_files()
    for tf in template_files:
        # Skip the GitHub workflows when the project already has a workflows
        # directory — we don't want to clobber team CI.
        candidate = brain_root / tf
        if candidate.exists():
            continue
        plan.entries.append(
            AdoptEntry(
                path=tf,
                action=ADD,
                reason="template gap — bundled file not present in project",
                suggested_target=tf,
            )
        )

    # 3) CLAUDE.md size split suggestion.
    claude = brain_root / "CLAUDE.md"
    if claude.is_file():
        size_bytes = claude.stat().st_size
        if size_bytes > max_claude_md_kb * 1024:
            try:
                text = claude.read_text(encoding="utf-8")
            except OSError:
                text = ""
            plan.claude_md_split_suggestion = _suggest_claude_md_split(
                text, max_claude_md_kb
            )

    # 4) Apply mode — copy ADD entries from the template, mark with frontmatter.
    if apply and not dry_run:
        if not git_clean:
            raise RuntimeError(
                "refusing to apply: brain has uncommitted changes — commit or stash first"
            )
        for entry in plan.by_action(ADD):
            src_path = src / entry.path
            if not src_path.is_file():
                continue
            dst_path = brain_root / entry.path
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dst_path.suffix.lower() == ".md":
                    text = src_path.read_text(encoding="utf-8")
                    text = _stamp_template_marker(text)
                    dst_path.write_text(text, encoding="utf-8")
                else:
                    shutil.copy2(src_path, dst_path)
            except OSError as exc:
                raise RuntimeError(
                    f"failed to copy template gap file {entry.path}: {exc}"
                ) from exc
        # Drop a MIGRATION.md summary so the team has a permanent record.
        (brain_root / "MIGRATION.md").write_text(
            plan.to_markdown(), encoding="utf-8"
        )

    return plan


__all__ = [
    "ADD",
    "KEEP",
    "MOVE_SUGGESTED",
    "REVIEW",
    "SKIP_PER_ENGINEER",
    "AdoptEntry",
    "AdoptPlan",
    "adopt",
]
