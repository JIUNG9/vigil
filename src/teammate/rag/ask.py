"""Retrieval + LLM orchestration: `teammate ask "<query>"`.

Flow::

    query
      ├── embed query (Ollama)              ──► retrieve top-k chunks via cosine
      │   if Ollama down                    ──► retrieve top-k via keyword score
      │
      └── build context block (top-k chunk texts + paths)
              │
              ▼
         Ollama LLM call(system = SYSTEM_PROMPT, prompt = context + query)
              │
              ▼
         streamed answer to stdout

When Ollama isn't running, we still return useful output: the matching
file paths + a short keyword-only snippet. Better than failing hard.
"""

from __future__ import annotations

import math
import pickle
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from teammate.rag.ollama import OllamaClient, OllamaError, OllamaUnavailable

SYSTEM_PROMPT = """\
You are teammate, a battle buddy for SREs joining regulated teams. You are
running locally on the user's laptop. You answer questions about the team's
compliance posture, recent advisory diffs, and the team's own CLAUDE.md
context — strictly from the chunks you are given. You do NOT make up control
IDs, framework names, or evidence.

Rules:

  1. If the answer is in the chunks, give it directly. Cite the file path
     for each fact in [brackets].
  2. If the chunks don't contain the answer, say so plainly. Do NOT speculate.
  3. Prefer concrete probe results, control IDs, and timestamps over
     paraphrase. Engineers reading this output will act on the specifics.
  4. Korean compliance terms (K-ISMS-P, KISA, 개인정보) are first-class —
     don't apologize for using them.
  5. Be terse. Engineers don't need preamble.
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
        # Reward exact-token matches more than substring hits.
        score += 2.0 * len(re.findall(rf"\b{re.escape(term)}\b", text_low))
        score += 0.5 * text_low.count(term)
    return score / max(1, len(text))


def _tokenize_query(q: str) -> list[str]:
    return list(re.findall(r"[A-Za-z0-9가-힣\-_.]{2,}", q.lower()))


def retrieve(
    db_path: Path,
    query: str,
    k: int = 6,
    ollama: OllamaClient | None = None,
) -> list[Hit]:
    """Retrieve top-k vault chunks relevant to ``query``."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))

    rows = conn.execute(
        "SELECT path, chunk_idx, text, embedding, framework, control, kind FROM chunks"
    ).fetchall()
    conn.close()
    if not rows:
        return []

    # Try embedding-based retrieval if Ollama is available + chunks have embeddings.
    use_embeddings = False
    qvec: list[float] | None = None
    if ollama and ollama.is_up() and any(r[3] is not None for r in rows):
        try:
            vecs = ollama.embed([query])
            if vecs:
                qvec = vecs[0]
                use_embeddings = True
        except (OllamaUnavailable, OllamaError):
            use_embeddings = False

    hits: list[Hit] = []
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
    return hits[:k]


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


def answer(
    query: str,
    db_path: Path,
    repo_root: Path,
    ollama: OllamaClient | None = None,
    k: int = 6,
) -> Iterator[str]:
    """Yield answer chunks. Either streamed LLM tokens or fallback text."""
    hits = retrieve(db_path, query, k=k, ollama=ollama)

    if not hits:
        yield (
            "No vault content matched. Run `teammate score` to populate the vault, "
            "or `teammate init` to set it up.\n"
        )
        return

    if not ollama or not ollama.is_up():
        # Fallback: list the top hits, no LLM synthesis.
        yield "Local LLM (Ollama) not running — returning matching files instead of a synthesized answer.\n\n"
        for h in hits:
            yield f"- {h.path}#chunk{h.chunk_idx} (score={h.score:.3f})\n"
        yield "\nStart Ollama (`ollama serve`) and re-run for a synthesized answer.\n"
        return

    context = _format_context(hits, repo_root)
    prompt = (
        f"## Context (top {len(hits)} chunks from the vault)\n\n"
        f"{context}\n\n"
        f"## Query\n\n{query}\n\n"
        f"## Answer\n"
    )
    try:
        yield from ollama.generate(prompt, system=SYSTEM_PROMPT, stream=True)
    except OllamaUnavailable:
        yield "\n\n(Ollama disconnected mid-stream. Try again.)\n"
    except OllamaError as exc:
        yield f"\n\n(Ollama errored: {exc})\n"


__all__ = ["Hit", "answer", "retrieve", "SYSTEM_PROMPT"]
