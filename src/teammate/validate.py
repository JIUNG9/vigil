"""`teammate validate` — read-only structural shape checks for a team brain.

Validation is what CI runs on every push. It must:

  1. Be read-only. No mutations, no side effects, no network.
  2. Be cheap. Walk the markdown, run regexes, parse YAML — that's it.
  3. Have stable exit codes for CI:

        0 = all PASS
        1 = at least one FAIL  (broken brain — block the merge)
        2 = at least one WARN  (cosmetic — surface but don't block)

The checks are intentionally narrow. We're not enforcing prose quality. We're
catching the structural mistakes that turn a brain into a junk drawer:

  * CLAUDE.md missing or bloated
  * dangling internal links
  * orphan markdown nobody references
  * non-canonical paths (`wiki/`, `notes/`) that should be moved
  * binary blobs sneaking into `docs/` / `knowledge/`
  * unparseable YAML frontmatter
  * (opt-in) directory names that violate the team's naming convention

Each check returns a record with a stable `name` so CI tooling can grep on it.
"""

from __future__ import annotations

import json as _json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from teammate.naming import (
    Verdict,
    find_naming_config,
    load_naming_convention,
    validate_name,
)

# Canonical sections — paths under brain root that the brain "knows about".
# Anything outside this list under the root is potentially orphan/non-canonical.
_CANONICAL_PREFIXES: tuple[str, ...] = (
    "CLAUDE.md",
    ".claude/skills/",
    ".claude/rules/",
    ".claude/commands/",
    "docs/",
    "knowledge/",
    "runbooks/",
)

# Paths we never descend into.
_SKIP_DIR_PARTS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".teammate-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
        "build",
    }
)

# Non-canonical markdown homes — present, but the docs-team should migrate.
_NON_CANONICAL_PREFIXES: tuple[str, ...] = ("wiki/", "notes/", "wiki-archive/")

# Markdown link regex — captures inline `[text](target)` only.
# Skips images (`![alt](src)`) by requiring the leading char NOT be `!`.
_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")

# Code-fence regex — multi-line ```code``` blocks.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Inline code regex — `code`. We strip these before scanning for links so
# the linter doesn't false-positive on documented examples like `[label](target)`.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    """One named check's outcome."""

    name: str
    status: str  # PASS / WARN / FAIL
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationReport:
    """The full report — every check, plus the worst-status aggregate."""

    brain_root: Path
    checks: list[CheckResult]
    max_claude_md_kb: int

    @property
    def overall(self) -> str:
        statuses = {c.status for c in self.checks}
        if FAIL in statuses:
            return FAIL
        if WARN in statuses:
            return WARN
        return PASS

    @property
    def exit_code(self) -> int:
        return {PASS: 0, FAIL: 1, WARN: 2}[self.overall]

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "brain_root": str(self.brain_root),
            "max_claude_md_kb": self.max_claude_md_kb,
            "overall": self.overall,
            "exit_code": self.exit_code,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "summary": c.summary,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }
        return _json.dumps(payload, indent=2, sort_keys=True, default=str)


# ---------- helpers ----------


def _iter_markdown(brain_root: Path) -> list[Path]:
    """List every .md file under brain_root, skipping vendor/cache dirs."""
    out: list[Path] = []
    try:
        candidates = list(brain_root.rglob("*.md"))
    except OSError:
        return []
    for p in candidates:
        try:
            rel_parts = p.relative_to(brain_root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIR_PARTS for part in rel_parts):
            continue
        if not p.is_file():
            continue
        out.append(p)
    return out


def _iter_all_files(brain_root: Path) -> list[Path]:
    """Every file under brain_root, skipping vendor/cache dirs."""
    out: list[Path] = []
    try:
        candidates = list(brain_root.rglob("*"))
    except OSError:
        return []
    for p in candidates:
        try:
            rel_parts = p.relative_to(brain_root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIR_PARTS for part in rel_parts):
            continue
        if not p.is_file():
            continue
        out.append(p)
    return out


def _is_external_link(target: str) -> bool:
    """True if target is HTTP/mailto/anchor-only — not a brain-relative ref."""
    t = target.strip()
    if not t:
        return True
    if t.startswith(("http://", "https://", "mailto:", "tel:", "ftp://")):
        return True
    if t.startswith("#"):
        return True
    # Absolute filesystem paths aren't brain-relative; skip rather than FAIL.
    return t.startswith("/")


def _strip_anchor(target: str) -> str:
    """Drop `#anchor` and any `?query` from a link target."""
    t = target.split("#", 1)[0]
    t = t.split("?", 1)[0]
    return t


def _resolve_link(source: Path, brain_root: Path, target: str) -> Path | None:
    """Resolve a brain-relative link target against the source file's directory.

    Returns the resolved Path that the link points at, OR None if the link
    is external / unresolvable. The caller decides whether the resolved path
    actually exists.
    """
    if _is_external_link(target):
        return None
    cleaned = _strip_anchor(target).strip()
    if not cleaned:
        return None
    base = source.parent
    candidate = (base / cleaned).resolve()
    # Constrain to inside brain_root — links that escape upward are FAILs in
    # spirit, but we tolerate them as "external" rather than crash.
    try:
        candidate.relative_to(brain_root.resolve())
    except ValueError:
        return None
    return candidate


# ---------- individual checks ----------


def _check_claude_md_present(brain_root: Path) -> CheckResult:
    path = brain_root / "CLAUDE.md"
    if path.is_file():
        return CheckResult(
            name="claude_md_present",
            status=PASS,
            summary=f"CLAUDE.md found at {path}",
            details={"path": str(path)},
        )
    return CheckResult(
        name="claude_md_present",
        status=FAIL,
        summary=f"CLAUDE.md missing at {brain_root}",
        details={"path": str(path)},
    )


def _check_claude_md_size(brain_root: Path, max_kb: int) -> CheckResult:
    path = brain_root / "CLAUDE.md"
    if not path.is_file():
        return CheckResult(
            name="claude_md_size",
            status=PASS,
            summary="skipped — no CLAUDE.md to size",
            details={},
        )
    size_bytes = path.stat().st_size
    size_kb = size_bytes / 1024.0
    if size_bytes <= max_kb * 1024:
        return CheckResult(
            name="claude_md_size",
            status=PASS,
            summary=f"{size_kb:.1f} KB (limit {max_kb} KB)",
            details={"size_bytes": size_bytes, "limit_kb": max_kb},
        )
    return CheckResult(
        name="claude_md_size",
        status=WARN,
        summary=(
            f"{size_kb:.1f} KB exceeds {max_kb} KB — consider splitting into "
            f".claude/rules/<topic>.md"
        ),
        details={"size_bytes": size_bytes, "limit_kb": max_kb},
    )


def _strip_code(text: str) -> str:
    """Remove fenced and inline code blocks so we don't false-positive on
    documented link examples inside backticks."""
    text = _FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def _check_link_resolution(brain_root: Path) -> CheckResult:
    files = _iter_markdown(brain_root)
    unresolved: list[dict[str, str]] = []
    for source in files:
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        text = _strip_code(text)
        for match in _LINK_RE.finditer(text):
            label, target = match.group(1), match.group(2)
            if _is_external_link(target):
                continue
            resolved = _resolve_link(source, brain_root, target)
            if resolved is None:
                continue
            # Allow either file (.md or otherwise) or directory.
            if resolved.exists():
                continue
            # `[text](some-dir/)` — also accept if dir exists.
            if target.endswith("/") and resolved.is_dir():
                continue
            unresolved.append(
                {
                    "source": str(source.relative_to(brain_root)),
                    "label": label,
                    "target": target,
                }
            )
    if not unresolved:
        return CheckResult(
            name="markdown_link_resolution",
            status=PASS,
            summary=f"{len(files)} markdown files — every internal link resolves",
            details={"files_scanned": len(files)},
        )
    first = unresolved[0]
    return CheckResult(
        name="markdown_link_resolution",
        status=FAIL,
        summary=(
            f"{len(unresolved)} unresolved internal link(s); first: "
            f"{first['source']} -> {first['target']!r}"
        ),
        details={"unresolved": unresolved[:50], "total": len(unresolved)},
    )


def _is_canonical(relpath: str) -> bool:
    return any(relpath == p or relpath.startswith(p) for p in _CANONICAL_PREFIXES)


def _check_orphan_files(brain_root: Path) -> CheckResult:
    """Markdown files that aren't referenced from CLAUDE.md and aren't in a
    canonical section.

    A file is "reachable" if:
      - it's in a canonical section (.claude/skills, docs/, knowledge/, etc.), OR
      - CLAUDE.md links to it (transitively, one hop is enough — orphan
        analysis isn't a perfect graph reachability check).
    """
    files = _iter_markdown(brain_root)
    if not files:
        return CheckResult(
            name="orphan_files",
            status=PASS,
            summary="no markdown to analyze",
            details={"orphans": []},
        )

    referenced: set[str] = set()
    claude = brain_root / "CLAUDE.md"
    if claude.is_file():
        try:
            text = claude.read_text(encoding="utf-8")
        except OSError:
            text = ""
        text = _strip_code(text)
        for match in _LINK_RE.finditer(text):
            target = match.group(2)
            if _is_external_link(target):
                continue
            resolved = _resolve_link(claude, brain_root, target)
            if resolved is None:
                continue
            try:
                referenced.add(str(resolved.relative_to(brain_root.resolve())))
            except ValueError:
                continue

    orphans: list[str] = []
    for f in files:
        rel = str(f.relative_to(brain_root))
        if _is_canonical(rel):
            continue
        if rel in referenced:
            continue
        if rel == "CLAUDE.md":
            continue
        orphans.append(rel)
    if not orphans:
        return CheckResult(
            name="orphan_files",
            status=PASS,
            summary=f"{len(files)} markdown files, no orphans",
            details={"orphans": []},
        )
    return CheckResult(
        name="orphan_files",
        status=WARN,
        summary=f"{len(orphans)} orphan file(s) — link from CLAUDE.md or move into a section",
        details={"orphans": orphans[:50], "total": len(orphans)},
    )


def _check_non_canonical_paths(brain_root: Path) -> CheckResult:
    files = _iter_markdown(brain_root)
    suspects: list[str] = []
    for f in files:
        rel = str(f.relative_to(brain_root))
        if any(rel.startswith(p) for p in _NON_CANONICAL_PREFIXES):
            suspects.append(rel)
    if not suspects:
        return CheckResult(
            name="non_canonical_paths",
            status=PASS,
            summary="no markdown in wiki/ or notes/",
            details={},
        )
    return CheckResult(
        name="non_canonical_paths",
        status=WARN,
        summary=(
            f"{len(suspects)} markdown file(s) live outside canonical sections — "
            f"consider moving to docs/ or knowledge/"
        ),
        details={"paths": suspects[:50], "total": len(suspects)},
    )


def _check_binary_files_in_brain(brain_root: Path) -> CheckResult:
    """Binary blobs under docs/ or knowledge/ — probably accidental commits."""
    allowed_suffixes = {".md", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp"}
    suspects: list[str] = []
    for section in ("docs", "knowledge"):
        section_dir = brain_root / section
        if not section_dir.is_dir():
            continue
        try:
            for p in section_dir.rglob("*"):
                if not p.is_file():
                    continue
                if any(part in _SKIP_DIR_PARTS for part in p.parts):
                    continue
                if p.suffix.lower() in allowed_suffixes:
                    continue
                # No suffix or unknown suffix — flag.
                suspects.append(str(p.relative_to(brain_root)))
        except OSError:
            continue
    if not suspects:
        return CheckResult(
            name="binary_files_in_brain",
            status=PASS,
            summary="docs/ and knowledge/ contain only markdown + images",
            details={},
        )
    return CheckResult(
        name="binary_files_in_brain",
        status=WARN,
        summary=(
            f"{len(suspects)} non-markdown / non-image file(s) under docs/ or knowledge/"
        ),
        details={"paths": suspects[:50], "total": len(suspects)},
    )


def _check_frontmatter_parses(brain_root: Path) -> CheckResult:
    files = _iter_markdown(brain_root)
    for source in files:
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError:
            continue
        if not raw.startswith("---"):
            continue
        closing = raw.find("\n---", 3)
        if closing == -1:
            return CheckResult(
                name="frontmatter_parses",
                status=FAIL,
                summary=(
                    f"{source.relative_to(brain_root)}: opens with `---` but never closes"
                ),
                details={"source": str(source.relative_to(brain_root))},
            )
        fm_text = raw[3:closing].strip()
        try:
            yaml.safe_load(fm_text)
        except yaml.YAMLError as exc:
            return CheckResult(
                name="frontmatter_parses",
                status=FAIL,
                summary=(
                    f"{source.relative_to(brain_root)}: YAML frontmatter parse error: {exc}"
                ),
                details={
                    "source": str(source.relative_to(brain_root)),
                    "error": str(exc),
                },
            )
    return CheckResult(
        name="frontmatter_parses",
        status=PASS,
        summary=f"{len(files)} markdown files — all frontmatter parses",
        details={"files_scanned": len(files)},
    )


# ---------- naming-convention check (opt-in) ----------

# Directories under these prefixes are validated against the naming
# convention when the check is enabled. We deliberately do NOT walk all
# of brain_root — naming applies to logical sections, not bookkeeping
# directories like `.git/` or `.teammate-cache/`.
_NAMING_SCAN_PREFIXES: tuple[str, ...] = (
    "docs",
    "knowledge",
    ".claude/skills",
)


def _read_validate_section(brain_root: Path) -> dict[str, Any]:
    """Read ``[validate]`` from ``.teammate/config.toml``. Returns ``{}`` on miss."""
    cfg_path = brain_root / ".teammate" / "config.toml"
    if not cfg_path.is_file():
        return {}
    try:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("validate") or {}
    return section if isinstance(section, dict) else {}


def _iter_naming_targets(brain_root: Path) -> list[Path]:
    """Top-level directories under each naming-scan prefix."""
    out: list[Path] = []
    for prefix in _NAMING_SCAN_PREFIXES:
        section_dir = brain_root / prefix
        if not section_dir.is_dir():
            continue
        try:
            for child in section_dir.iterdir():
                if child.is_dir() and child.name not in _SKIP_DIR_PARTS:
                    out.append(child)
        except OSError:
            continue
    return out


def _check_naming_convention(brain_root: Path) -> CheckResult:
    """Validate directory names under canonical sections against the convention.

    Skipped silently when ``.teammate-naming.toml`` is absent — naming is
    opt-in. WARN on length / submodule warnings; FAIL on any hard rule
    violation. The first FAIL drives the overall status; we still
    enumerate up to 50 entries in ``details`` for human triage.
    """
    cfg_path = find_naming_config(brain_root)
    if cfg_path is None:
        return CheckResult(
            name="naming_convention",
            status=PASS,
            summary="skipped — no .teammate-naming.toml",
            details={"enabled": True, "config": None},
        )
    convention = load_naming_convention(cfg_path)
    if convention is None:
        return CheckResult(
            name="naming_convention",
            status=FAIL,
            summary=f"could not parse {cfg_path.name}",
            details={"config": str(cfg_path)},
        )

    targets = _iter_naming_targets(brain_root)
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    ok_count = 0
    for target in targets:
        result = validate_name(target.name, convention)
        rel = str(target.relative_to(brain_root))
        record = {
            "path": rel,
            "name": target.name,
            "verdict": result.verdict.value,
            "reason": result.reason,
        }
        if result.verdict is Verdict.FAIL:
            failures.append(record)
        elif result.verdict is Verdict.WARN:
            warnings.append(record)
        else:
            ok_count += 1

    if not targets:
        return CheckResult(
            name="naming_convention",
            status=PASS,
            summary="no candidate directories — nothing to check",
            details={"config": str(cfg_path), "scanned": 0},
        )

    if failures:
        first = failures[0]
        return CheckResult(
            name="naming_convention",
            status=FAIL,
            summary=(
                f"{len(failures)} naming violation(s); first: "
                f"{first['path']} — {first['reason']}"
            ),
            details={
                "config": str(cfg_path),
                "scanned": len(targets),
                "ok": ok_count,
                "failures": failures[:50],
                "warnings": warnings[:50],
            },
        )
    if warnings:
        first = warnings[0]
        return CheckResult(
            name="naming_convention",
            status=WARN,
            summary=(
                f"{len(warnings)} naming warning(s); first: "
                f"{first['path']} — {first['reason']}"
            ),
            details={
                "config": str(cfg_path),
                "scanned": len(targets),
                "ok": ok_count,
                "warnings": warnings[:50],
            },
        )
    return CheckResult(
        name="naming_convention",
        status=PASS,
        summary=f"{ok_count} director(y|ies) match the convention",
        details={
            "config": str(cfg_path),
            "scanned": len(targets),
            "ok": ok_count,
        },
    )


# ---------- public API ----------


def validate(
    brain_root: Path,
    *,
    max_claude_md_kb: int = 4,
    include_naming: bool | None = None,
) -> ValidationReport:
    """Run every check; return a ValidationReport with the worst status.

    The naming-convention check is OFF by default. Enable per-call via
    ``include_naming=True``, or per-repo via ``[validate] include_naming
    = true`` in ``.teammate/config.toml``. The CLI flag wins over the TOML.
    """
    brain_root = Path(brain_root).resolve()
    if include_naming is None:
        include_naming = bool(_read_validate_section(brain_root).get("include_naming", False))
    checks = [
        _check_claude_md_present(brain_root),
        _check_claude_md_size(brain_root, max_claude_md_kb),
        _check_link_resolution(brain_root),
        _check_orphan_files(brain_root),
        _check_non_canonical_paths(brain_root),
        _check_binary_files_in_brain(brain_root),
        _check_frontmatter_parses(brain_root),
    ]
    if include_naming:
        checks.append(_check_naming_convention(brain_root))
    return ValidationReport(
        brain_root=brain_root,
        checks=checks,
        max_claude_md_kb=max_claude_md_kb,
    )


__all__ = [
    "FAIL",
    "PASS",
    "WARN",
    "CheckResult",
    "ValidationReport",
    "validate",
]
