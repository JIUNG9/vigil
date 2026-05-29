"""Tests for the four v0.6 confidence guards."""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from vigil.confidence import (
    DEFAULT_ACTION_FLOORS,
    AuditRecord,
    append_audit,
    audit_log_path,
    citation_guard,
    filter_uncited_paragraphs,
    read_audit,
    render_below_threshold_message,
    resolve_action_floor,
)
from vigil.config import (
    ConfidenceConfig,
    load_config,
)
from vigil.providers.base import LLMProvider
from vigil.rag.ask import answer
from vigil.rag.index import _SCHEMA  # noqa: WPS437

# ---------- Guard 4: action floor table ----------


def test_default_action_floors_present():
    assert DEFAULT_ACTION_FLOORS["ask"] == 0.5
    assert DEFAULT_ACTION_FLOORS["agent.weekly_digest"] == 0.5
    assert DEFAULT_ACTION_FLOORS["agent.orphan_triage"] == 0.6
    assert DEFAULT_ACTION_FLOORS["agent.pr_migration_plan"] == 0.65
    assert DEFAULT_ACTION_FLOORS["execute"] == 0.85


def test_resolve_action_floor_uses_overrides():
    f = resolve_action_floor("ask", overrides={"ask": 0.7})
    assert f == 0.7


def test_resolve_action_floor_uses_default_table():
    f = resolve_action_floor("agent.orphan_triage")
    assert f == 0.6


def test_resolve_action_floor_unknown_action():
    f = resolve_action_floor("unknown_action_xyz")
    assert f == 0.5


def test_resolve_action_floor_invalid_override_falls_through():
    # Garbage override → silently fall through to the default table.
    f = resolve_action_floor("ask", overrides={"ask": "high"})
    assert f == 0.5


# ---------- Guard 1: below-threshold message ----------


def test_below_threshold_message_mentions_score_and_floor():
    msg = render_below_threshold_message(
        "deploy procedure",
        closest_path="docs/runbooks/deploy.md",
        closest_score=0.31,
        floor=0.5,
    )
    assert "0.31" in msg
    assert "0.50" in msg
    assert "docs/runbooks/deploy.md" in msg
    assert "I don't know" in msg


def test_below_threshold_message_omits_path_when_none():
    msg = render_below_threshold_message("x", closest_path=None, closest_score=0.0, floor=0.5)
    assert "Closest file" not in msg


# ---------- Guard 2: citation guard (eager) ----------


def test_filter_uncited_paragraphs_keeps_cited():
    text = (
        "We deploy via blue-green every Tuesday [docs/runbooks/deploy.md].\n\n"
        "Auth tokens rotate hourly [docs/auth.md]."
    )
    out = filter_uncited_paragraphs(text)
    assert "blue-green" in out
    assert "Auth tokens" in out


def test_filter_uncited_paragraphs_strips_uncited():
    text = (
        "We deploy via blue-green every Tuesday [docs/runbooks/deploy.md].\n\n"
        "Some uncited claim that should not survive."
    )
    out = filter_uncited_paragraphs(text)
    assert "blue-green" in out
    assert "Some uncited claim" not in out
    assert "(uncited claim removed)" in out


def test_filter_uncited_paragraphs_handles_paren_citations():
    text = "We deploy on Tuesday (docs/runbooks/deploy.md). Some other thing."
    # A single-paragraph answer is checked as one unit.
    out = filter_uncited_paragraphs(text)
    assert "deploy" in out


def test_filter_uncited_paragraphs_empty():
    assert filter_uncited_paragraphs("") == ""


# ---------- Guard 2: citation guard (streaming) ----------


def _stream(*chunks: str) -> Iterator[str]:
    yield from chunks


def test_citation_guard_passes_cited_paragraph():
    out = "".join(citation_guard(_stream(
        "We deploy via blue-green [docs/deploy.md] on Tuesdays.\n\nNext paragraph.",
    )))
    # First paragraph survives.
    assert "blue-green" in out


def test_citation_guard_strips_uncited_paragraph():
    out = "".join(citation_guard(_stream(
        "Uncited paragraph claiming things.\n\n",
        "Cited one [docs/x.md] over here.",
    )))
    assert "Uncited paragraph" not in out
    assert "(uncited claim removed)" in out
    assert "docs/x.md" in out


def test_citation_guard_flushes_residual_short_answer():
    """The advisor flagged this: short answers without ``\\n\\n`` must still
    be inspected. Otherwise a one-paragraph reply vanishes silently."""
    # No blank-line boundary at all.
    out = "".join(citation_guard(_stream(
        "This is a short ", "answer with ", "a citation [docs/x.md].",
    )))
    assert "[docs/x.md]" in out
    assert "(uncited claim removed)" not in out


def test_citation_guard_flushes_residual_uncited_short_answer():
    out = "".join(citation_guard(_stream(
        "This is a short answer with no citation at all.",
    )))
    assert "(uncited claim removed)" in out


def test_citation_guard_paragraph_level_not_sentence_level():
    """A single paragraph with one citation at the end satisfies the rule."""
    out = "".join(citation_guard(_stream(
        "Sentence one. Sentence two. Sentence three [docs/x.md].\n\n",
    )))
    assert "Sentence one" in out
    assert "Sentence three" in out


# ---------- Guard 3: audit log ----------


def _record(query: str, *, ts: str | None = None, max_score: float = 0.7,
            below: bool = False, mode: str = "embedding") -> AuditRecord:
    return AuditRecord(
        ts=ts or "2026-05-08T10:00:00+00:00",
        query=query,
        k=6,
        max_score=max_score,
        min_score=0.2,
        chunks_used=["docs/a.md"],
        llm_provider="OllamaLLMProvider",
        llm_model="llama3.2:3b",
        answer_length_chars=200,
        below_threshold=below,
        retrieval_mode=mode,
        contradictions=0,
        action="ask",
    )


def test_append_audit_creates_jsonl(tmp_path):
    cache = tmp_path / ".vigil-cache"
    out = append_audit(cache, _record("hello"))
    assert out == audit_log_path(cache)
    raw = out.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    rec = json.loads(raw.splitlines()[0])
    assert rec["query"] == "hello"
    assert rec["max_score"] == 0.7
    assert rec["below_threshold"] is False


def test_append_audit_appends_multiple_lines(tmp_path):
    cache = tmp_path / ".vigil-cache"
    append_audit(cache, _record("first"))
    append_audit(cache, _record("second"))
    lines = audit_log_path(cache).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["query"] == "first"
    assert json.loads(lines[1])["query"] == "second"


def test_audit_rotation_renames_when_iso_week_changes(tmp_path, monkeypatch):
    cache = tmp_path / ".vigil-cache"

    # Write a record stamped to a Tuesday in ISO week 19 (May 5, 2026).
    week_a = _dt.datetime(2026, 5, 5, 10, 0, 0, tzinfo=_dt.UTC)
    append_audit(cache, _record("first-week query"), now=week_a)
    active = audit_log_path(cache)
    assert active.exists()
    # Force the active log's mtime to look like it was written in week 19.
    import os
    ts = week_a.timestamp()
    os.utime(active, (ts, ts))

    # Now append in ISO week 20 (May 12, 2026).
    week_b = _dt.datetime(2026, 5, 12, 10, 0, 0, tzinfo=_dt.UTC)
    append_audit(cache, _record("second-week query"), now=week_b)

    archive = cache / "audit-2026-W19.jsonl"
    assert archive.exists()
    archive_contents = archive.read_text(encoding="utf-8")
    assert "first-week query" in archive_contents
    # New active log only has the second week's entry.
    new_active = audit_log_path(cache).read_text(encoding="utf-8")
    assert "second-week query" in new_active
    assert "first-week query" not in new_active


def test_audit_rotation_no_op_within_same_week(tmp_path):
    cache = tmp_path / ".vigil-cache"
    monday = _dt.datetime(2026, 5, 4, 10, 0, 0, tzinfo=_dt.UTC)
    friday = _dt.datetime(2026, 5, 8, 10, 0, 0, tzinfo=_dt.UTC)
    append_audit(cache, _record("a"), now=monday)
    import os
    ts = monday.timestamp()
    os.utime(audit_log_path(cache), (ts, ts))
    append_audit(cache, _record("b"), now=friday)
    archives = list(cache.glob("audit-*.jsonl"))
    assert archives == []


def test_audit_rotation_concats_when_archive_already_exists(tmp_path):
    """A 3-week-quiet brain that wakes up and finds an old archive name
    should append to it, not crash."""
    cache = tmp_path / ".vigil-cache"
    cache.mkdir(parents=True)
    archive = cache / "audit-2026-W19.jsonl"
    archive.write_text(
        json.dumps({"query": "old archived"}) + "\n", encoding="utf-8"
    )
    week_a = _dt.datetime(2026, 5, 5, 10, 0, 0, tzinfo=_dt.UTC)
    append_audit(cache, _record("active in week 19"), now=week_a)
    import os
    ts = week_a.timestamp()
    os.utime(audit_log_path(cache), (ts, ts))
    week_b = _dt.datetime(2026, 5, 12, 10, 0, 0, tzinfo=_dt.UTC)
    append_audit(cache, _record("week-20 query"), now=week_b)

    archive_lines = archive.read_text(encoding="utf-8").splitlines()
    # Old + active-from-week-19 both present.
    assert any("old archived" in line for line in archive_lines)
    assert any("active in week 19" in line for line in archive_lines)


def test_read_audit_returns_active_and_archived(tmp_path):
    cache = tmp_path / ".vigil-cache"
    cache.mkdir()
    (cache / "audit-2026-W18.jsonl").write_text(
        json.dumps({"ts": "2026-05-05T10:00:00+00:00", "query": "old", "max_score": 0.4}) + "\n",
        encoding="utf-8",
    )
    audit_log_path(cache).write_text(
        json.dumps({"ts": "2026-05-12T10:00:00+00:00", "query": "new", "max_score": 0.7}) + "\n",
        encoding="utf-8",
    )
    records = read_audit(cache)
    queries = [r["query"] for r in records]
    assert "old" in queries
    assert "new" in queries


def test_read_audit_filters_by_since(tmp_path):
    cache = tmp_path / ".vigil-cache"
    cache.mkdir()
    audit_log_path(cache).write_text(
        json.dumps({"ts": "2026-05-05T10:00:00+00:00", "query": "old"}) + "\n"
        + json.dumps({"ts": "2026-05-12T10:00:00+00:00", "query": "new"}) + "\n",
        encoding="utf-8",
    )
    records = read_audit(
        cache, since=_dt.datetime(2026, 5, 10, tzinfo=_dt.UTC)
    )
    queries = [r["query"] for r in records]
    assert queries == ["new"]


def test_read_audit_filters_by_query_grep(tmp_path):
    cache = tmp_path / ".vigil-cache"
    cache.mkdir()
    audit_log_path(cache).write_text(
        json.dumps({"ts": "2026-05-05T10:00:00+00:00", "query": "deploy procedure"}) + "\n"
        + json.dumps({"ts": "2026-05-06T10:00:00+00:00", "query": "auth tokens"}) + "\n",
        encoding="utf-8",
    )
    records = read_audit(cache, query_grep="deploy")
    assert len(records) == 1
    assert records[0]["query"] == "deploy procedure"


# ---------- integration: answer() emits below-threshold + audits ----------


class _DownLLM(LLMProvider):
    """An LLM that always pretends to be down."""

    @property
    def model_id(self) -> str:
        return "down:1b"

    def is_up(self) -> bool:
        return False

    def generate(self, *_args, **_kwargs):
        yield ""


def test_answer_audits_empty_index(tmp_path):
    db = tmp_path / "vault.sqlite"
    cache = tmp_path / ".cache"
    list(answer("nothing here", db, tmp_path, embedder=None, llm=None,
                cache_dir=cache))
    records = read_audit(cache)
    assert len(records) == 1
    assert records[0]["retrieval_mode"] == "none"


def test_answer_audits_keyword_mode_no_threshold_gate(tmp_path):
    db = tmp_path / "vault.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO chunks (path, chunk_idx, text, embedding, token_count, mtime, "
        "framework, control, kind) VALUES (?, ?, ?, NULL, 100, 1.0, '', '', 'doc')",
        (str(tmp_path / "x.md"), 0, "deploy procedure runs on Tuesdays."),
    )
    conn.commit()
    conn.close()

    cache = tmp_path / ".cache"
    out = "".join(answer(
        "deploy procedure",
        db,
        tmp_path,
        embedder=None,
        llm=_DownLLM(),
        cache_dir=cache,
    ))
    # Keyword-mode + LLM down → fallback file list, NOT the "I don't know"
    # message (the threshold doesn't apply in keyword mode).
    assert "I don't know" not in out
    records = read_audit(cache)
    assert records[-1]["retrieval_mode"] == "keyword"
    assert records[-1]["below_threshold"] is False


def test_answer_honours_action_floor_override(tmp_path):
    """Setting ``[confidence.action_floors] ask = 0.99`` must reject a
    0.7-cosine chunk. This was the silent-override gap the advisor flagged."""
    import pickle

    db = tmp_path / "vault.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO chunks (path, chunk_idx, text, embedding, token_count, mtime, "
        "framework, control, kind) VALUES (?, ?, ?, ?, 100, 1.0, '', '', 'doc')",
        (
            str(tmp_path / "okay.md"),
            0,
            "decent match",
            pickle.dumps([1.0, 0.0, 0.0, 0.0]),
        ),
    )
    conn.commit()
    conn.close()

    class _FakeEmbedder:
        @property
        def model_id(self) -> str:
            return "fake"

        @property
        def dim(self) -> int:
            return 4

        def is_up(self) -> bool:
            return True

        def embed(self, texts):
            # Cosine ≈ 0.71 → safe under default 0.5 floor, blocked by 0.99.
            return [[0.7, 0.7, 0.0, 0.0] for _ in texts]

    cache = tmp_path / ".cache"
    cfg = ConfidenceConfig(
        score_threshold=0.5,
        action_floors={"ask": 0.99},
    )
    out = "".join(answer(
        "anything",
        db,
        tmp_path,
        embedder=_FakeEmbedder(),
        llm=None,
        cache_dir=cache,
        confidence=cfg,
        action="ask",
    ))
    assert "I don't know" in out
    records = read_audit(cache)
    assert records[-1]["below_threshold"] is True


def test_answer_below_threshold_with_embedding_mode(tmp_path):
    """Inject a fake embedder so the retrieval path goes through embedding
    mode but the cosine score lands below 0.5."""
    import pickle

    db = tmp_path / "vault.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    # Stored embedding is orthogonal-ish to the query embedding so cosine
    # is low.
    conn.execute(
        "INSERT INTO chunks (path, chunk_idx, text, embedding, token_count, mtime, "
        "framework, control, kind) VALUES (?, ?, ?, ?, 100, 1.0, '', '', 'doc')",
        (
            str(tmp_path / "lonely.md"),
            0,
            "Some text",
            pickle.dumps([1.0, 0.0, 0.0, 0.0]),
        ),
    )
    conn.commit()
    conn.close()

    class _FakeEmbedder:
        @property
        def model_id(self) -> str:
            return "fake"

        @property
        def dim(self) -> int:
            return 4

        def is_up(self) -> bool:
            return True

        def embed(self, texts):
            # Query vector is orthogonal-ish.
            return [[0.1, 1.0, 0.0, 0.0] for _ in texts]

    cache = tmp_path / ".cache"
    out = "".join(answer(
        "an unrelated query",
        db,
        tmp_path,
        embedder=_FakeEmbedder(),
        llm=None,
        cache_dir=cache,
        confidence=ConfidenceConfig(score_threshold=0.5),
    ))
    assert "I don't know" in out
    records = read_audit(cache)
    assert records[-1]["below_threshold"] is True
    assert records[-1]["retrieval_mode"] == "embedding"


# ---------- config wiring ----------


def test_load_config_reads_confidence_section(tmp_path):
    (tmp_path / ".vigil").mkdir()
    (tmp_path / ".vigil" / "config.toml").write_text(
        "[llm]\nprovider = \"none\"\nmodel = \"\"\n"
        "[embedding]\nprovider = \"none\"\nmodel = \"\"\n"
        "[confidence]\nscore_threshold = 0.7\n"
        "[confidence.action_floors]\nask = 0.6\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.confidence.score_threshold == 0.7
    assert cfg.confidence.action_floors == {"ask": 0.6}


def test_load_config_reads_contradiction_section(tmp_path):
    (tmp_path / ".vigil").mkdir()
    (tmp_path / ".vigil" / "config.toml").write_text(
        "[llm]\nprovider = \"none\"\nmodel = \"\"\n"
        "[embedding]\nprovider = \"none\"\nmodel = \"\"\n"
        "[contradiction]\nuse_llm_judge = true\nmax_llm_calls = 5\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.contradiction.use_llm_judge is True
    assert cfg.contradiction.max_llm_calls == 5


def test_load_config_defaults_when_section_absent(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.confidence.score_threshold == 0.5
    assert cfg.contradiction.use_llm_judge is False


# ---------- CLI: audit subcommand ----------


def _run_cli(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "vigil.cli"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_audit_no_log(tmp_path):
    rc, out, _err = _run_cli(["audit"], cwd=tmp_path)
    # No cache dir → soft exit 0 with a message.
    assert rc == 0


def test_cli_audit_human_view(tmp_path):
    cache = tmp_path / ".vigil-cache"
    cache.mkdir()
    audit_log_path(cache).write_text(
        json.dumps({
            "ts": "2026-05-08T10:00:00+00:00",
            "action": "ask",
            "query": "deploy procedure",
            "k": 6,
            "max_score": 0.72,
            "retrieval_mode": "embedding",
            "contradictions": 0,
        }) + "\n",
        encoding="utf-8",
    )
    rc, out, _err = _run_cli(["audit"], cwd=tmp_path)
    assert rc == 0
    assert "deploy procedure" in out


def test_cli_audit_query_grep(tmp_path):
    cache = tmp_path / ".vigil-cache"
    cache.mkdir()
    audit_log_path(cache).write_text(
        json.dumps({"ts": "2026-05-08T10:00:00+00:00", "query": "deploy"}) + "\n"
        + json.dumps({"ts": "2026-05-08T11:00:00+00:00", "query": "auth"}) + "\n",
        encoding="utf-8",
    )
    rc, out, _err = _run_cli(["audit", "--query-grep", "deploy"], cwd=tmp_path)
    assert rc == 0
    assert "deploy" in out
    # The auth record was filtered out.
    assert "auth" not in out
