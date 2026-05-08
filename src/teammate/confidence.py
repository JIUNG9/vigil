"""Four confidence guards for the retrieval path.

These realise the "I don't know" thesis from the launch article: a team-brain
tool that synthesises something whenever asked is worse than one that refuses
to bluff. The four guards together push the worst-case answer towards
honest abstention.

  Guard 1 — score threshold     refuse to synthesise when top-k max < floor.
  Guard 2 — citation guard      strip paragraphs the LLM emitted without a
                                ``[file]`` citation. Requires a system prompt
                                that demands citations.
  Guard 3 — audit JSONL         one line per retrieval, weekly rotation,
                                lazy (no daemon).
  Guard 4 — per-action floor    different floors for ask / weekly_digest /
                                orphan_triage / pr_migration_plan, set in
                                ``[confidence] action_floors.<name>``.

The score threshold is meaningful only when retrieval used embeddings —
the keyword-fallback score is unbounded and density-normalised. We log
this in the audit line and skip the gate when ``mode != "embedding"``.
That asymmetry is documented in ``docs/CONFIDENCE.md``.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# ---------- score threshold (Guard 1) ----------


# Default per-action floors. Lower for read-only Q&A, higher for any action
# that ends up surfacing as an issue / PR comment / mutation. The floors
# are conservative on purpose — better to refuse than to bluff.
DEFAULT_ACTION_FLOORS: dict[str, float] = {
    "ask": 0.5,
    "agent.weekly_digest": 0.5,
    "agent.orphan_triage": 0.6,
    "agent.pr_migration_plan": 0.65,
    # Reserved for future routines that take real side effects:
    "execute": 0.85,
}


def resolve_action_floor(
    action: str,
    overrides: dict[str, float] | None = None,
    default: float = 0.5,
) -> float:
    """Return the floor for ``action``. ``overrides`` wins over the default
    table, default table wins over ``default``. Unknown actions get
    ``default``.
    """
    if overrides and action in overrides:
        try:
            return float(overrides[action])
        except (TypeError, ValueError):
            pass
    if action in DEFAULT_ACTION_FLOORS:
        return DEFAULT_ACTION_FLOORS[action]
    return default


def render_below_threshold_message(
    query: str,
    *,
    closest_path: str | None,
    closest_score: float,
    floor: float,
) -> str:
    """The "I don't know" reply. One paragraph, terse, points at the
    closest match so the user can decide whether to widen the search.
    """
    head = (
        f"I don't know — the closest match scored {closest_score:.2f}, "
        f"below the floor of {floor:.2f}."
    )
    if closest_path:
        head += f" Closest file: `{closest_path}`."
    tail = (
        " Consider rewording the query, or run "
        "`teammate index --rebuild` if you expected this to be in the brain."
    )
    return head + tail + "\n"


# ---------- citation guard (Guard 2) ----------


# Paragraph = run of non-blank lines. We split on a blank-line boundary so
# bullet lists stay together. Match either ``[anything]`` or ``(anything)``
# pointing at a path-shaped string. Conservative — the system prompt asks for
# brackets, but some models reach for parentheses.
_CITATION_RE = re.compile(r"[\[\(]\s*[^\[\]\(\)\n]*?\.[A-Za-z0-9]+\s*[\]\)]")
_FALLBACK_CITATION_RE = re.compile(r"\[[^\[\]\n]+/[^\[\]\n]+\]")
_UNCITED_REPLACEMENT = "(uncited claim removed)\n\n"

CITATION_INSTRUCTION = (
    "Every paragraph in your answer MUST cite at least one source file in "
    "[brackets] using its path (e.g. `[docs/runbooks/auth-deploy.md]`). "
    "Paragraphs without a bracketed citation will be stripped before the "
    "user sees them. If the chunks don't contain enough to cite, say so."
)


def _has_citation(paragraph: str) -> bool:
    """True if ``paragraph`` contains at least one bracketed citation."""
    if _CITATION_RE.search(paragraph):
        return True
    return bool(_FALLBACK_CITATION_RE.search(paragraph))


def filter_uncited_paragraphs(text: str) -> str:
    """Eager (non-streaming) variant. Useful for tests and offline tools.

    Splits on blank-line boundaries, replaces uncited paragraphs with the
    sentinel string, joins back. A short answer with no blank lines is
    treated as a single paragraph.
    """
    if not text.strip():
        return text
    parts = re.split(r"\n\s*\n", text)
    out: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if _has_citation(part):
            out.append(part.rstrip())
        else:
            out.append("(uncited claim removed)")
    return "\n\n".join(out) + ("\n" if text.endswith("\n") else "")


def citation_guard(stream: Iterable[str]) -> Iterator[str]:
    """Streaming wrapper around an LLM token stream.

    Buffers tokens until a paragraph boundary (blank line). When the
    paragraph closes, emits it verbatim if it contains a citation, else
    emits ``(uncited claim removed)\\n\\n`` in its place.

    The residual buffer is flushed at end-of-stream with the same check —
    short answers without an explicit blank-line separator still get
    inspected. This is the spec ambiguity the advisor flagged: don't
    silently drop short answers.
    """
    buf = ""
    boundary = re.compile(r"\n\s*\n")
    for token in stream:
        buf += token
        # Drain every complete paragraph from the buffer.
        while True:
            m = boundary.search(buf)
            if m is None:
                break
            paragraph = buf[: m.start()]
            buf = buf[m.end() :]
            if not paragraph.strip():
                continue
            if _has_citation(paragraph):
                yield paragraph.rstrip() + "\n\n"
            else:
                yield _UNCITED_REPLACEMENT
    # End-of-stream flush.
    residual = buf.strip()
    if residual:
        if _has_citation(residual):
            yield residual + "\n"
        else:
            yield "(uncited claim removed)\n"


# ---------- audit log (Guard 3) ----------


@dataclass(frozen=True)
class AuditRecord:
    """One retrieval, one line. Schema is the on-disk JSONL contract."""

    ts: str
    query: str
    k: int
    max_score: float
    min_score: float
    chunks_used: list[str] = field(default_factory=list)
    llm_provider: str = ""
    llm_model: str = ""
    answer_length_chars: int = 0
    below_threshold: bool = False
    retrieval_mode: str = "embedding"  # "embedding" | "keyword" | "none"
    contradictions: int = 0
    action: str = "ask"

    def to_jsonl(self) -> str:
        """Serialise to a single JSON line (no trailing newline)."""
        return json.dumps(
            {
                "ts": self.ts,
                "action": self.action,
                "query": self.query,
                "k": self.k,
                "max_score": round(self.max_score, 4),
                "min_score": round(self.min_score, 4),
                "chunks_used": self.chunks_used,
                "llm_provider": self.llm_provider,
                "llm_model": self.llm_model,
                "answer_length_chars": self.answer_length_chars,
                "below_threshold": self.below_threshold,
                "retrieval_mode": self.retrieval_mode,
                "contradictions": self.contradictions,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def audit_log_path(cache_dir: Path) -> Path:
    """Canonical path to the active audit JSONL file."""
    return cache_dir / "audit.jsonl"


def _archive_path(cache_dir: Path, dt: _dt.datetime) -> Path:
    """Per-week archive name. ISO week is week-of-year stable."""
    iso_year, iso_week, _ = dt.isocalendar()
    return cache_dir / f"audit-{iso_year:04d}-W{iso_week:02d}.jsonl"


def _iso_week_key(dt: _dt.datetime) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _maybe_rotate(cache_dir: Path, now: _dt.datetime) -> None:
    """Lazy weekly rotation. Renames the active log to its archive name
    when the active log's mtime falls in a different ISO week than ``now``.

    Called inside ``append_audit`` — no daemon needed. Per the docs:
    a 3-week-quiet brain still gets exactly one rotation on the next write.
    """
    active = audit_log_path(cache_dir)
    if not active.exists():
        return
    try:
        last_mtime = _dt.datetime.fromtimestamp(active.stat().st_mtime, _dt.UTC)
    except OSError:
        return
    if _iso_week_key(last_mtime) == _iso_week_key(now):
        return
    archive = _archive_path(cache_dir, last_mtime)
    if archive.exists():
        # Already archived under this name; concat to keep both retrievals.
        try:
            with active.open("r", encoding="utf-8") as src, \
                    archive.open("a", encoding="utf-8") as dst:
                for line in src:
                    dst.write(line)
            active.unlink()
        except OSError:
            return
        return
    try:
        active.rename(archive)
    except OSError:
        return


def append_audit(cache_dir: Path, record: AuditRecord, *, now: _dt.datetime | None = None) -> Path:
    """Append a record to the active audit log.

    Creates the cache directory if missing. Performs lazy weekly rotation
    immediately before writing. Returns the path written to.
    """
    now = now or _dt.datetime.now(_dt.UTC)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _maybe_rotate(cache_dir, now)
    target = audit_log_path(cache_dir)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(record.to_jsonl() + "\n")
    return target


def read_audit(
    cache_dir: Path,
    *,
    since: _dt.datetime | None = None,
    query_grep: str | None = None,
    include_archived: bool = True,
) -> list[dict]:
    """Read recent audit records. ``since`` is inclusive (>=).

    ``include_archived`` controls whether archived weekly files are read
    in addition to the active one.
    """
    files: list[Path] = []
    if include_archived:
        files.extend(sorted(cache_dir.glob("audit-*.jsonl")))
    active = audit_log_path(cache_dir)
    if active.exists():
        files.append(active)
    grep_re = re.compile(query_grep) if query_grep else None
    out: list[dict] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since is not None:
                        ts = rec.get("ts", "")
                        try:
                            ts_dt = _dt.datetime.fromisoformat(ts)
                        except ValueError:
                            continue
                        if ts_dt < since:
                            continue
                    if grep_re is not None and not grep_re.search(rec.get("query", "")):
                        continue
                    out.append(rec)
        except OSError:
            continue
    return out


__all__ = [
    "AuditRecord",
    "CITATION_INSTRUCTION",
    "DEFAULT_ACTION_FLOORS",
    "append_audit",
    "audit_log_path",
    "citation_guard",
    "filter_uncited_paragraphs",
    "read_audit",
    "render_below_threshold_message",
    "resolve_action_floor",
]
