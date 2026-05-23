"""Tests for the v0.6 contradiction detector."""

from __future__ import annotations

from collections.abc import Iterator

from vigil.contradiction import (
    KIND_PARAMETER,
    KIND_PROCEDURE,
    Contradiction,
    detect_contradictions,
    render_contradiction_prefix,
)
from vigil.rag.ask import Hit


def _hit(path: str, text: str, score: float = 0.7) -> Hit:
    return Hit(
        path=path,
        chunk_idx=0,
        text=text,
        score=score,
        framework="",
        control="",
        kind="doc",
    )


# ---------- Phase 1 heuristic ----------


def test_no_contradictions_when_chunks_agree():
    a = _hit("docs/runbooks/a.md", "We deploy via blue-green every Tuesday morning.")
    b = _hit("docs/runbooks/b.md", "We deploy via blue-green every Tuesday morning.")
    assert detect_contradictions([a, b]) == []


def test_no_contradictions_when_unrelated():
    a = _hit("docs/runbooks/a.md", "Auth service runs on port 8080.")
    b = _hit("docs/runbooks/b.md", "Billing reconciles at midnight UTC daily.")
    assert detect_contradictions([a, b]) == []


def test_parameter_drift_on_postgres_versions():
    a = _hit(
        "docs/runbooks/migrate.md",
        "We currently run RDS on PostgreSQL 13 and migrate to PG14 next quarter.",
    )
    b = _hit(
        "docs/runbooks/db-policy.md",
        "We currently run RDS on PostgreSQL 16 and migrate to PG17 next quarter.",
    )
    out = detect_contradictions([a, b])
    assert len(out) == 1
    assert out[0].kind == KIND_PARAMETER
    assert "13" in out[0].evidence_a or "14" in out[0].evidence_a


def test_negation_mismatch_is_procedure_conflict():
    a = _hit(
        "docs/runbooks/auth.md",
        "Always rotate the auth tokens before deploy because stale tokens cause errors.",
    )
    b = _hit(
        "docs/runbooks/auth-legacy.md",
        "Do not rotate the auth tokens before deploy because stale tokens cause errors.",
    )
    out = detect_contradictions([a, b])
    assert len(out) == 1
    assert out[0].kind == KIND_PROCEDURE
    assert "auth" in out[0].evidence_a.lower()


def test_low_score_chunks_are_skipped():
    a = _hit(
        "docs/runbooks/a.md",
        "We currently run RDS on PostgreSQL 13 and migrate to PG14 next quarter.",
        score=0.2,
    )
    b = _hit(
        "docs/runbooks/b.md",
        "We currently run RDS on PostgreSQL 16 and migrate to PG17 next quarter.",
        score=0.2,
    )
    # Both below default 0.5 floor → no candidate pair.
    assert detect_contradictions([a, b]) == []


def test_same_file_pairs_skipped():
    a = _hit("docs/runbooks/x.md", "Deploy on PostgreSQL 13 weekly without exception.")
    b = _hit("docs/runbooks/x.md", "Deploy on PostgreSQL 16 weekly without exception.")
    assert detect_contradictions([a, b]) == []


def test_dedup_returns_single_entry_per_pair():
    a = _hit(
        "docs/x.md",
        "Step one: rotate the keys before deploy on PostgreSQL 13. "
        "Step two: rotate the keys before deploy on PostgreSQL 14.",
    )
    b = _hit(
        "docs/y.md",
        "Step one: rotate the keys before deploy on PostgreSQL 16. "
        "Step two: rotate the keys before deploy on PostgreSQL 17.",
    )
    out = detect_contradictions([a, b])
    assert len(out) == 1


def test_three_chunks_pairwise():
    a = _hit("docs/a.md", "We currently run on PostgreSQL 13 in production.")
    b = _hit("docs/b.md", "We currently run on PostgreSQL 16 in production.")
    c = _hit("docs/c.md", "Frontend uses Vite and Tailwind for the UI.")
    out = detect_contradictions([a, b, c])
    # Only the a/b pair conflicts; c is unrelated.
    assert len(out) == 1
    pair = sorted([out[0].chunk_a, out[0].chunk_b])
    assert pair == ["docs/a.md", "docs/b.md"]


# ---------- LLM judge (Phase 2) ----------


class _FakeLLM:
    """Minimal LLM stub that returns scripted verdicts."""

    def __init__(self, verdicts: list[str], model_id: str = "fake:1b"):
        self._verdicts = list(verdicts)
        self._model_id = model_id
        self.calls: list[tuple[str, str]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    def is_up(self) -> bool:
        return True

    def generate(self, prompt: str, system: str | None = None, *, stream: bool = True) -> Iterator[str]:
        self.calls.append((system or "", prompt))
        verdict = self._verdicts.pop(0) if self._verdicts else "UNSURE: ran out of verdicts"
        yield verdict


def test_llm_judge_drops_false_positives():
    a = _hit("docs/a.md", "We currently run on PostgreSQL 13 in production.")
    b = _hit("docs/b.md", "We currently run on PostgreSQL 16 in production.")
    llm = _FakeLLM(["NO: those are different services, not a conflict"])
    out = detect_contradictions([a, b], llm=llm, use_llm_judge=True)
    assert out == []
    assert len(llm.calls) == 1


def test_llm_judge_confirms_real_conflict_and_uses_summary():
    a = _hit("docs/a.md", "We currently run on PostgreSQL 13 in production.")
    b = _hit("docs/b.md", "We currently run on PostgreSQL 16 in production.")
    llm = _FakeLLM(["YES: PG13 vs PG16 in production"])
    out = detect_contradictions([a, b], llm=llm, use_llm_judge=True)
    assert len(out) == 1
    assert out[0].summary == "PG13 vs PG16 in production"


def test_llm_judge_off_by_default():
    a = _hit("docs/a.md", "We currently run on PostgreSQL 13 in production.")
    b = _hit("docs/b.md", "We currently run on PostgreSQL 16 in production.")
    llm = _FakeLLM(["NO: not really"])
    # Default ``use_llm_judge=False`` — the LLM should not be called.
    out = detect_contradictions([a, b], llm=llm)
    assert len(out) == 1
    assert llm.calls == []


def test_llm_judge_capped_by_max_calls():
    # Two pairs that would otherwise be flagged; cap at 1.
    chunks = [
        _hit("docs/a.md", "We currently run on PostgreSQL 13 in production."),
        _hit("docs/b.md", "We currently run on PostgreSQL 16 in production."),
        _hit("docs/c.md", "Always rotate the auth tokens before deploy daily."),
        _hit("docs/d.md", "Do not rotate the auth tokens before deploy daily."),
    ]
    llm = _FakeLLM(["YES: pg conflict", "YES: token conflict"])
    out = detect_contradictions(chunks, llm=llm, use_llm_judge=True, max_llm_calls=1)
    # Only one pair confirmed by the LLM. The second pair is filtered without
    # an LLM call (because we ran out of calls).
    assert len(out) == 1
    assert len(llm.calls) == 1


def test_llm_judge_handles_provider_exception():
    a = _hit("docs/a.md", "We currently run on PostgreSQL 13 in production.")
    b = _hit("docs/b.md", "We currently run on PostgreSQL 16 in production.")

    class _ExplodingLLM:
        @property
        def model_id(self) -> str:
            return "boom:1b"

        def is_up(self) -> bool:
            return True

        def generate(self, *_args, **_kwargs):
            raise RuntimeError("transport down")

    out = detect_contradictions([a, b], llm=_ExplodingLLM(), use_llm_judge=True)
    # Provider exception → judge silently treats as not-a-conflict, drops the finding.
    assert out == []


# ---------- render prefix ----------


def test_render_prefix_empty_for_no_findings():
    assert render_contradiction_prefix([]) == ""


def test_render_prefix_includes_both_files_and_evidence():
    c = Contradiction(
        chunk_a="docs/a.md",
        chunk_b="docs/b.md",
        kind=KIND_PARAMETER,
        summary="PG13 vs PG16",
        evidence_a="We run PG13.",
        evidence_b="We run PG16.",
    )
    out = render_contradiction_prefix([c])
    assert "Two sources disagree" in out
    assert "docs/a.md" in out
    assert "docs/b.md" in out
    assert "PG13" in out
    assert "PG16" in out
    assert "Resolve manually" in out


# ---------- integration with answer() ----------


def test_answer_emits_contradiction_prefix(tmp_path, monkeypatch):
    """The ``answer()`` function emits the prefix when contradictions are found."""
    import sqlite3

    from vigil.providers.base import LLMProvider
    from vigil.rag.ask import answer
    from vigil.rag.index import _SCHEMA  # noqa: WPS437 — direct DDL is fine for tests

    db = tmp_path / "vault.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO chunks (path, chunk_idx, text, embedding, token_count, mtime, "
        "framework, control, kind) VALUES (?, ?, ?, NULL, 100, 1.0, '', '', 'doc')",
        (
            str(tmp_path / "docs" / "a.md"),
            0,
            "We currently run on PostgreSQL 13 in production for the auth service.",
        ),
    )
    conn.execute(
        "INSERT INTO chunks (path, chunk_idx, text, embedding, token_count, mtime, "
        "framework, control, kind) VALUES (?, ?, ?, NULL, 100, 1.0, '', '', 'doc')",
        (
            str(tmp_path / "docs" / "b.md"),
            0,
            "We currently run on PostgreSQL 16 in production for the auth service.",
        ),
    )
    conn.commit()
    conn.close()

    class _StubLLM(LLMProvider):
        @property
        def model_id(self) -> str:
            return "stub"

        def is_up(self) -> bool:
            return True

        def generate(self, prompt, system=None, *, stream=True):
            yield "Sounds good [docs/a.md]\n"

    from vigil.config import ContradictionConfig

    text = "".join(
        answer(
            "What postgres version do we run?",
            db,
            tmp_path,
            embedder=None,
            llm=_StubLLM(),
            k=6,
            cache_dir=tmp_path / ".cache",
            # Keyword fallback scores are tiny; lower the contradiction
            # floor so the integration check still exercises the pair.
            contradiction_cfg=ContradictionConfig(score_floor=0.0),
        )
    )
    assert "Two sources disagree" in text
