"""Retrieval + LLM orchestration: `teammate ask "<query>"`.

Flow::

    query
      ├── embed query (EmbeddingProvider)   ──► retrieve top-k chunks via cosine
      │   if provider down                  ──► retrieve top-k via keyword score
      │
      ├── Guard 1: score threshold          ──► refuse below floor (embedding mode only)
      ├── Guard 3: append audit JSONL       ──► one line per retrieval
      ├── contradiction detector            ──► surface "two sources disagree" prefix
      │
      └── build context block (top-k chunk texts + paths)
              │
              ▼
         LLMProvider.generate(system=SYSTEM_PROMPT + citation rule, prompt=context + query)
              │
              ▼
         streamed answer wrapped by Guard 2 (citation filter)

When neither provider is reachable, we still return useful output: the
matching file paths + a short keyword-only snippet. Better than failing hard.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import math
import pickle
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from teammate.confidence import (
    CITATION_INSTRUCTION,
    AuditRecord,
    append_audit,
    citation_guard,
    render_below_threshold_message,
    resolve_action_floor,
)
from teammate.config import (
    ConfidenceConfig,
    ContradictionConfig,
    InvalidationsConfig,
)
from teammate.contradiction import (
    detect_contradictions,
    render_contradiction_prefix,
)
from teammate.providers.base import (
    EmbeddingProvider,
    LLMProvider,
    ProviderError,
    ProviderUnavailable,
)

SYSTEM_PROMPT = f"""\
You are teammate, a battle buddy for SREs joining regulated teams. You are
running locally on the user's laptop. You answer questions about the team's
compliance posture, recent advisory diffs, and the team's own CLAUDE.md
context — strictly from the chunks you are given. You do NOT make up control
IDs, framework names, or evidence.

Rules:

  1. If the answer is in the chunks, give it directly.
  2. If the chunks don't contain the answer, say so plainly. Do NOT speculate.
  3. Prefer concrete probe results, control IDs, and timestamps over
     paraphrase. Engineers reading this output will act on the specifics.
  4. Korean compliance terms (K-ISMS-P, KISA, 개인정보) are first-class —
     don't apologize for using them.
  5. Be terse. Engineers don't need preamble.

Citation rule (enforced by post-processing):

  {CITATION_INSTRUCTION}
"""


@dataclass(frozen=True, slots=True)
class Hit:
    path: str
    chunk_idx: int
    text: str
    score: float
    framework: str
    control: str
    kind: str


# ---------- retrieval ----------


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _keyword_score(text: str, terms: list[str]) -> float:
    """Cheap BM25-ish scoring fallback when no embeddings exist."""
    if not terms:
        return 0.0
    text_low = text.lower()
    score = 0.0
    for term in terms:
        score += 2.0 * len(re.findall(rf"\b{re.escape(term)}\b", text_low))
        score += 0.5 * text_low.count(term)
    return score / max(1, len(text))


def _tokenize_query(q: str) -> list[str]:
    return list(re.findall(r"[A-Za-z0-9가-힣\-_.]{2,}", q.lower()))


def retrieve(
    db_path: Path,
    query: str,
    k: int = 6,
    embedder: EmbeddingProvider | None = None,
) -> tuple[list[Hit], str]:
    """Retrieve top-k vault chunks relevant to ``query``.

    Returns ``(hits, mode)`` where ``mode`` is one of:

      - ``"embedding"`` — cosine similarity over real embeddings.
      - ``"keyword"``   — BM25-ish fallback.
      - ``"none"``      — no chunks at all (empty index).

    The mode matters for the score-threshold guard: keyword scores are
    unbounded and density-normalised; the 0.5 floor is meaningful only
    in embedding mode.
    """
    if not db_path.exists():
        return [], "none"
    conn = sqlite3.connect(str(db_path))

    rows = conn.execute(
        "SELECT path, chunk_idx, text, embedding, framework, control, kind FROM chunks"
    ).fetchall()
    conn.close()
    if not rows:
        return [], "none"

    use_embeddings = False
    qvec: list[float] | None = None
    if embedder and embedder.is_up() and any(r[3] is not None for r in rows):
        try:
            vecs = embedder.embed([query])
            if vecs:
                qvec = vecs[0]
                use_embeddings = True
        except (ProviderUnavailable, ProviderError):
            use_embeddings = False

    hits: list[Hit] = []
    mode = "embedding" if use_embeddings else "keyword"
    if use_embeddings and qvec is not None:
        for path, idx, text, blob, framework, control, kind in rows:
            if blob is None:
                continue
            vec = pickle.loads(blob)
            score = _cosine(qvec, vec)
            hits.append(
                Hit(
                    path=path,
                    chunk_idx=idx,
                    text=text,
                    score=score,
                    framework=framework or "",
                    control=control or "",
                    kind=kind or "",
                )
            )
    else:
        terms = _tokenize_query(query)
        for path, idx, text, _blob, framework, control, kind in rows:
            score = _keyword_score(text, terms)
            if score > 0:
                hits.append(
                    Hit(
                        path=path,
                        chunk_idx=idx,
                        text=text,
                        score=score,
                        framework=framework or "",
                        control=control or "",
                        kind=kind or "",
                    )
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k], mode


# ---------- orchestration ----------


def _format_context(hits: list[Hit], repo_root: Path) -> str:
    """Build the context block for the LLM prompt."""
    blocks: list[str] = []
    for hit in hits:
        try:
            rel = Path(hit.path).resolve().relative_to(repo_root.resolve())
            label = str(rel)
        except ValueError:
            label = hit.path
        blocks.append(f"--- [{label}] ---\n{hit.text.strip()}")
    return "\n\n".join(blocks)


def _hit_label(hit: Hit, repo_root: Path) -> str:
    try:
        return str(Path(hit.path).resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return hit.path


def answer(
    query: str,
    db_path: Path,
    repo_root: Path,
    embedder: EmbeddingProvider | None = None,
    llm: LLMProvider | None = None,
    k: int = 6,
    *,
    cache_dir: Path | None = None,
    confidence: ConfidenceConfig | None = None,
    contradiction_cfg: ContradictionConfig | None = None,
    invalidations_cfg: InvalidationsConfig | None = None,
    action: str = "ask",
    floor: float | None = None,
    audit: bool = True,
) -> Iterator[str]:
    """Yield answer chunks. Either streamed LLM tokens or fallback text.

    The four confidence guards are applied:

      1. Score threshold  — if mode is ``"embedding"`` and max score is
         below ``floor``, emit the "I don't know" message and stop.
      2. Citation guard   — wraps the LLM stream so paragraphs without a
         bracketed citation are replaced with ``(uncited claim removed)``.
      3. Audit log        — one JSONL line appended per retrieval, including
         the below-threshold case.
      4. Per-action floor — ``floor`` overrides ``confidence.score_threshold``;
         callers (agent routines) pass their per-action floor here.
    """
    confidence = confidence or ConfidenceConfig()
    contradiction_cfg = contradiction_cfg or ContradictionConfig()
    invalidations_cfg = invalidations_cfg or InvalidationsConfig()
    if floor is None:
        # Per-action floor (Guard 4): pulls from
        # ``[confidence.action_floors]`` first, falling back to
        # ``DEFAULT_ACTION_FLOORS``, then to ``score_threshold``.
        floor = resolve_action_floor(
            action,
            overrides=confidence.action_floors,
            default=confidence.score_threshold,
        )
    if cache_dir is None:
        cache_dir = repo_root / ".teammate-cache"

    hits, mode = retrieve(db_path, query, k=k, embedder=embedder)
    max_score = max((h.score for h in hits), default=0.0)
    min_score = min((h.score for h in hits), default=0.0)
    chunk_paths = [_hit_label(h, repo_root) for h in hits]
    llm_provider_name = type(llm).__name__ if llm is not None else ""
    llm_model = getattr(llm, "model_id", "") if llm is not None else ""

    # No hits at all — short-circuit, but still audit.
    if not hits:
        if audit:
            with contextlib.suppress(OSError):
                append_audit(
                    cache_dir,
                    AuditRecord(
                        ts=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                        action=action,
                        query=query,
                        k=k,
                        max_score=0.0,
                        min_score=0.0,
                        chunks_used=[],
                        llm_provider=llm_provider_name,
                        llm_model=llm_model,
                        answer_length_chars=0,
                        below_threshold=False,
                        retrieval_mode=mode,
                        contradictions=0,
                    ),
                )
        yield (
            "No vault content matched. Run `teammate index` to populate the index, "
            "or `teammate init` to set it up.\n"
        )
        return

    # Guard 1 — score threshold. Only meaningful in embedding mode.
    below = mode == "embedding" and max_score < floor
    if below:
        msg = render_below_threshold_message(
            query,
            closest_path=chunk_paths[0] if chunk_paths else None,
            closest_score=max_score,
            floor=floor,
        )
        if audit:
            with contextlib.suppress(OSError):
                append_audit(
                    cache_dir,
                    AuditRecord(
                        ts=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                        action=action,
                        query=query,
                        k=k,
                        max_score=max_score,
                        min_score=min_score,
                        chunks_used=chunk_paths,
                        llm_provider=llm_provider_name,
                        llm_model=llm_model,
                        answer_length_chars=len(msg),
                        below_threshold=True,
                        retrieval_mode=mode,
                        contradictions=0,
                    ),
                )
        yield msg
        return

    # Contradiction detector — Phase 1 always; Phase 2 only when configured.
    contradictions = detect_contradictions(
        hits,
        llm=llm if contradiction_cfg.use_llm_judge else None,
        score_floor=contradiction_cfg.score_floor,
        use_llm_judge=contradiction_cfg.use_llm_judge,
        max_llm_calls=contradiction_cfg.max_llm_calls,
    )
    prefix = render_contradiction_prefix(contradictions)

    # Invalidation banner (v0.9). Look up brain-invalidations events that
    # reference resources mentioned in the retrieved chunks. Only events
    # at or above ``show_severity`` surface as a user-visible banner;
    # everything else is logged to audit JSONL only — keeping the noise
    # floor where the team set it.
    invalidation_banner = ""
    matched_invalidations = 0
    if invalidations_cfg.enabled:
        from datetime import timedelta as _td

        from teammate.invalidations import (
            find_invalidations_for_chunks,
            render_banner,
        )

        inv_root = invalidations_cfg.repo_path
        if inv_root is None:
            sibling = repo_root.parent / "brain-invalidations"
            inv_root = sibling if sibling.is_dir() else (
                Path.home() / ".teammate" / "brain-invalidations"
            )
        if inv_root.exists():
            window = _td(hours=invalidations_cfg.recency_window_hours)
            matches = find_invalidations_for_chunks(hits, inv_root, since=window)
            matched_invalidations = sum(len(v) for v in matches.values())
            invalidation_banner = render_banner(
                matches, show_severity=invalidations_cfg.show_severity
            )

    # No LLM — fall back to the matched-files listing.
    if llm is None or not llm.is_up():
        body = (
            "Local LLM not running — returning matching files instead of a "
            "synthesized answer.\n\n"
        )
        for h in hits:
            body += f"- {_hit_label(h, repo_root)}#chunk{h.chunk_idx} (score={h.score:.3f})\n"
        body += "\nStart your LLM provider and re-run for a synthesized answer.\n"
        if audit:
            with contextlib.suppress(OSError):
                append_audit(
                    cache_dir,
                    AuditRecord(
                        ts=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                        action=action,
                        query=query,
                        k=k,
                        max_score=max_score,
                        min_score=min_score,
                        chunks_used=chunk_paths,
                        llm_provider=llm_provider_name,
                        llm_model=llm_model,
                        answer_length_chars=len(body) + len(prefix),
                        below_threshold=False,
                        retrieval_mode=mode,
                        contradictions=len(contradictions),
                        invalidations_matched=matched_invalidations,
                    ),
                )
        if invalidation_banner:
            yield invalidation_banner
        if prefix:
            yield prefix
        yield body
        return

    if invalidation_banner:
        yield invalidation_banner
    if prefix:
        yield prefix

    context = _format_context(hits, repo_root)
    prompt = (
        f"## Context (top {len(hits)} chunks from the vault)\n\n"
        f"{context}\n\n"
        f"## Query\n\n{query}\n\n"
        f"## Answer\n"
    )
    answer_chars = 0
    try:
        # Wrap the LLM stream with the citation guard.
        for piece in citation_guard(
            llm.generate(prompt, system=SYSTEM_PROMPT, stream=True)
        ):
            answer_chars += len(piece)
            yield piece
    except ProviderUnavailable:
        yield "\n\n(LLM provider disconnected mid-stream. Try again.)\n"
    except ProviderError as exc:
        yield f"\n\n(LLM provider errored: {exc})\n"
    finally:
        if audit:
            with contextlib.suppress(OSError):
                append_audit(
                    cache_dir,
                    AuditRecord(
                        ts=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                        action=action,
                        query=query,
                        k=k,
                        max_score=max_score,
                        min_score=min_score,
                        chunks_used=chunk_paths,
                        llm_provider=llm_provider_name,
                        llm_model=llm_model,
                        answer_length_chars=answer_chars + len(prefix),
                        below_threshold=False,
                        retrieval_mode=mode,
                        contradictions=len(contradictions),
                        invalidations_matched=matched_invalidations,
                    ),
                )


__all__ = ["Hit", "answer", "retrieve", "SYSTEM_PROMPT"]
