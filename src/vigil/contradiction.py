"""Contradiction detector — flag when two sources disagree.

When the retriever pulls 6 chunks for a query, two of them might say opposite
things. The synthesis-by-LLM path will happily blend "use PG13" and "use PG16"
into "use PG14" — a half-truth that costs you at 3 AM. This module surfaces
the conflict instead of erasing it.

Two phases::

    Phase 1: heuristic    — n-gram overlap + numeric / boolean disagreement.
                            Free, runs by default. Best-effort, catches
                            the obvious cases.
    Phase 2: LLM judge    — sends candidate pairs to the LLM with a tight
                            yes/no prompt. Opt-in via config. Only runs on
                            pairs Phase 1 already flagged.

Cost ceiling: with k=6 chunks, max pairs = 15. Phase 1 prunes aggressively;
Phase 2 typically sees 0–3 pairs. Configurable via ``[contradiction]`` in
``.vigil/config.toml``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vigil.providers.base import LLMProvider
    from vigil.rag.ask import Hit


# ---------- public datatypes ----------


@dataclass(frozen=True, slots=True)
class Contradiction:
    """A detected disagreement between two retrieved chunks.

    Attributes:
        chunk_a:    first chunk's path label.
        chunk_b:    second chunk's path label.
        kind:       ``procedure_conflict`` | ``parameter_drift`` | ``scope_overlap``.
        summary:    one-line human description of the disagreement.
        evidence_a: short snippet from A used as the disagreement signal.
        evidence_b: short snippet from B used as the disagreement signal.
    """

    chunk_a: str
    chunk_b: str
    kind: str
    summary: str
    evidence_a: str = ""
    evidence_b: str = ""


# Known kinds. Keep tiny — downstream renderers switch on these.
KIND_PROCEDURE = "procedure_conflict"
KIND_PARAMETER = "parameter_drift"
KIND_SCOPE = "scope_overlap"


# ---------- Phase 1: heuristic ----------


# Numbers (incl. version-y forms like "PG13", "1.10.5") that frequently encode
# the load-bearing parameter in a sentence.
_NUMBER_RE = re.compile(r"\b[A-Za-z]{0,3}\d+(?:[.\-]\d+)*\b")
# A small vocabulary of strong negations / boolean-flip words. We only flag a
# pair when one chunk has them and the other does not, in a sentence the two
# share substantial overlap with.
_NEGATION_WORDS = {
    "not", "never", "no", "without", "disable", "disabled", "off", "false",
    "deprecated", "removed", "stop", "stopped", "rollback", "revert",
    "do not", "don't", "must not",
}


def _split_sentences(text: str) -> list[str]:
    """Crude sentence splitter. Markdown is messy; trailing periods aren't
    reliable. Good enough for "is there a sentence-level conflict here?"."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + n]) for i in range(0, len(tokens) - n + 1)}


def _normalise(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _shared_subject(sent_a: str, sent_b: str, *, min_overlap: int = 4) -> bool:
    """Treat sentences as "about the same thing" when they share an n-gram
    of at least ``min_overlap`` tokens. The threshold is intentionally
    generous — Phase 2 (LLM) is the actual quality bar.
    """
    ta = _normalise(sent_a)
    tb = _normalise(sent_b)
    if len(ta) < min_overlap or len(tb) < min_overlap:
        return False
    grams_a = _ngrams(ta, min_overlap)
    grams_b = _ngrams(tb, min_overlap)
    return bool(grams_a & grams_b)


def _has_negation(sent: str) -> bool:
    low = sent.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in _NEGATION_WORDS)


def _numbers_in(sent: str) -> set[str]:
    return {m.group(0).lower() for m in _NUMBER_RE.finditer(sent)}


def _heuristic_pair(
    text_a: str, text_b: str
) -> tuple[str, str, str, str] | None:
    """Pairwise heuristic. Returns ``(kind, summary, evidence_a, evidence_b)``
    when the two texts contain a sentence-level conflict, else ``None``.

    "Conflict" = one of:

      - *parameter_drift*: two sentences sharing a 4-gram subject but with
        non-overlapping numeric tokens.
      - *procedure_conflict*: two sentences sharing a 4-gram subject where
        exactly one carries a strong negation word.
    """
    sents_a = _split_sentences(text_a)
    sents_b = _split_sentences(text_b)
    for sa in sents_a:
        for sb in sents_b:
            if not _shared_subject(sa, sb):
                continue
            nums_a = _numbers_in(sa)
            nums_b = _numbers_in(sb)
            if nums_a and nums_b and not (nums_a & nums_b):
                return (
                    KIND_PARAMETER,
                    f"Numeric/version drift: {sorted(nums_a)} vs {sorted(nums_b)}",
                    sa,
                    sb,
                )
            neg_a = _has_negation(sa)
            neg_b = _has_negation(sb)
            if neg_a != neg_b:
                return (
                    KIND_PROCEDURE,
                    "Negation mismatch on the same subject",
                    sa,
                    sb,
                )
    return None


# ---------- Phase 2: LLM judge ----------


_JUDGE_SYSTEM = """\
You are a contradiction detector. Given two short text excerpts from a team's
runbooks, decide whether they disagree on any operational claim that an
on-call engineer would act on (a command, a parameter, a procedure, a yes/no
decision). Reply with exactly one line:

  YES: <one-sentence summary of the conflict>
  NO: <one-sentence reason they don't conflict>
  UNSURE: <one-sentence reason you can't tell>

Do NOT speculate. Do NOT pretend a difference in wording is a contradiction.
Two excerpts that simply discuss different aspects of the same system are NOT
a contradiction.
"""


def _judge_with_llm(
    llm: LLMProvider, text_a: str, text_b: str
) -> tuple[bool, str]:
    """Ask the LLM. Returns ``(is_contradiction, summary)``.

    Conservative: only ``YES:`` counts as a contradiction. Anything else
    (``NO``, ``UNSURE``, malformed) is treated as not-a-conflict.
    """
    prompt = (
        f"## Excerpt A\n{text_a.strip()[:1200]}\n\n"
        f"## Excerpt B\n{text_b.strip()[:1200]}\n\n"
        "Verdict:"
    )
    try:
        chunks = list(llm.generate(prompt, system=_JUDGE_SYSTEM, stream=False))
    except Exception:  # noqa: BLE001 — provider is best-effort here
        return False, ""
    raw = "".join(chunks).strip()
    first_line = raw.splitlines()[0].strip() if raw else ""
    upper = first_line.upper()
    if upper.startswith("YES"):
        # Strip the "YES:" prefix for the summary.
        _, _, tail = first_line.partition(":")
        return True, tail.strip() or "LLM judge flagged a conflict"
    return False, ""


# ---------- orchestration ----------


def _label(hit: Hit | object) -> str:
    """Path label suitable for showing the user. ``Hit.path`` is absolute;
    we keep the basename + parent to stay readable in markdown."""
    raw = getattr(hit, "path", "")
    if not raw:
        return "?"
    parts = str(raw).split("/")
    if len(parts) <= 2:
        return raw
    return "/".join(parts[-2:])


def detect_contradictions(
    chunks: Iterable[Hit],
    llm: LLMProvider | None = None,
    *,
    score_floor: float = 0.5,
    use_llm_judge: bool = False,
    max_llm_calls: int = 3,
) -> list[Contradiction]:
    """Find contradictions among ``chunks``.

    Pairs are formed only between chunks whose ``score`` is at least
    ``score_floor`` — low-confidence hits aren't worth checking. Phase 1
    (heuristic) runs unconditionally on the candidate pairs. Phase 2
    (LLM judge) only runs when ``use_llm_judge=True`` AND ``llm`` is not
    None, and never makes more than ``max_llm_calls`` calls per query.

    The output is deduplicated by ``(chunk_a, chunk_b)`` — the heuristic
    will only return the first sentence-level conflict per pair.
    """
    eligible = [c for c in chunks if getattr(c, "score", 0.0) >= score_floor]
    findings: list[Contradiction] = []
    seen_pairs: set[tuple[str, str]] = set()
    llm_calls = 0
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            a, b = eligible[i], eligible[j]
            label_a, label_b = _label(a), _label(b)
            key = tuple(sorted([label_a, label_b]))
            # Skip pairs from the same file — they're not "two sources".
            if label_a == label_b:
                continue
            if key in seen_pairs:
                continue
            heuristic = _heuristic_pair(a.text, b.text)
            if heuristic is None:
                continue
            kind, summary, ev_a, ev_b = heuristic
            seen_pairs.add(key)

            if use_llm_judge and llm is not None:
                if llm_calls >= max_llm_calls:
                    # Budget exhausted. With LLM-judge ON, every finding
                    # must pass the judge — drop unjudged candidates rather
                    # than emit them on heuristic alone.
                    continue
                llm_calls += 1
                is_conflict, llm_summary = _judge_with_llm(llm, ev_a, ev_b)
                if not is_conflict:
                    # LLM says it isn't a real conflict; trust the LLM and
                    # drop the heuristic finding.
                    continue
                summary = llm_summary or summary

            findings.append(
                Contradiction(
                    chunk_a=label_a,
                    chunk_b=label_b,
                    kind=kind,
                    summary=summary,
                    evidence_a=ev_a,
                    evidence_b=ev_b,
                )
            )
    return findings


def render_contradiction_prefix(contradictions: list[Contradiction]) -> str:
    """Format contradictions as a user-visible prefix block.

    Returns an empty string when the list is empty so callers can
    unconditionally concatenate it.
    """
    if not contradictions:
        return ""
    lines: list[str] = ["**Two sources disagree on this:**", ""]
    for c in contradictions:
        lines.append(f"- `[{c.chunk_a}]` says: \"{c.evidence_a.strip()}\"")
        lines.append(f"- `[{c.chunk_b}]` says: \"{c.evidence_b.strip()}\"")
        if c.summary:
            lines.append(f"  ({c.kind}: {c.summary})")
        lines.append("")
    lines.append("Resolve manually before acting. Continuing with synthesis below.")
    lines.append("")
    return "\n".join(lines) + "\n"


__all__ = [
    "Contradiction",
    "KIND_PARAMETER",
    "KIND_PROCEDURE",
    "KIND_SCOPE",
    "detect_contradictions",
    "render_contradiction_prefix",
]
