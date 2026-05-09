"""Integration test — ``rag.ask.answer()`` surfaces the invalidation banner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from teammate.config import (
    ConfidenceConfig,
    InvalidationsConfig,
)
from teammate.impact import _clear_cache, emit
from teammate.providers.base import LLMProvider
from teammate.rag.ask import answer
from teammate.rag.index import _SCHEMA  # noqa: WPS437

# ---------- helpers ----------


class _DownLLM(LLMProvider):
    """An LLM that always pretends to be down so ask() falls into the
    fallback path. The banner attaches to that path identically — easier
    to assert against without mocking streamed output.
    """

    @property
    def model_id(self) -> str:
        return "down:1b"

    def is_up(self) -> bool:
        return False

    def generate(self, *_args, **_kwargs):
        yield ""


def _seed_index(db: Path, body: str, path: Path) -> None:
    """Seed one keyword-matching chunk pointing at ``path`` with ``body``."""
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO chunks (path, chunk_idx, text, embedding, token_count, mtime, "
        "framework, control, kind) VALUES (?, ?, ?, NULL, 100, 1.0, '', '', 'doc')",
        (str(path), 0, body),
    )
    conn.commit()
    conn.close()


# ---------- tests ----------


def test_answer_emits_banner_when_chunk_matches_high_event(tmp_path):
    brain = tmp_path / "brain"
    brain.mkdir()
    chunk_file = brain / "docs" / "runbooks" / "auth-deploy.md"
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_text(
        "uses aws_vpc.shared (vpc-abc12345) for ingress\n",
        encoding="utf-8",
    )

    db = tmp_path / "vault.sqlite"
    _seed_index(
        db,
        body="auth deploy uses vpc-abc12345 for ingress",
        path=chunk_file,
    )

    inv_root = tmp_path / "brain-invalidations"
    emit(brain, "vpc-abc12345", "detach", "high",
         invalidations_root=inv_root)
    _clear_cache()

    cfg = InvalidationsConfig(enabled=True, repo_path=inv_root,
                              show_severity="high",
                              recency_window_hours=168)
    out = "".join(answer(
        "auth deploy",
        db,
        brain,
        embedder=None,
        llm=_DownLLM(),
        cache_dir=tmp_path / ".cache",
        confidence=ConfidenceConfig(),
        invalidations_cfg=cfg,
    ))
    assert "This answer references resources with recent infra changes" in out
    assert "vpc-abc12345" in out
    assert "HIGH" in out
    _clear_cache()


def test_answer_no_banner_when_no_invalidations(tmp_path):
    brain = tmp_path / "brain"
    brain.mkdir()
    chunk_file = brain / "docs" / "x.md"
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_text("vpc-99999999 reference\n", encoding="utf-8")

    db = tmp_path / "vault.sqlite"
    _seed_index(db, body="vpc-99999999 reference", path=chunk_file)

    inv_root = tmp_path / "brain-invalidations"
    inv_root.mkdir()
    _clear_cache()

    cfg = InvalidationsConfig(enabled=True, repo_path=inv_root)
    out = "".join(answer(
        "vpc reference",
        db,
        brain,
        embedder=None,
        llm=_DownLLM(),
        cache_dir=tmp_path / ".cache",
        invalidations_cfg=cfg,
    ))
    assert "This answer references resources" not in out
    _clear_cache()


def test_answer_respects_show_severity_high_for_low_event(tmp_path):
    """LOW events must NOT surface as a banner when show_severity=high."""
    brain = tmp_path / "brain"
    brain.mkdir()
    chunk_file = brain / "docs" / "y.md"
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_text("vpc-12345678 reference\n", encoding="utf-8")

    db = tmp_path / "vault.sqlite"
    _seed_index(db, body="vpc-12345678 reference", path=chunk_file)

    inv_root = tmp_path / "brain-invalidations"
    emit(brain, "vpc-12345678", "modify", "low",
         invalidations_root=inv_root)
    _clear_cache()

    cfg = InvalidationsConfig(enabled=True, repo_path=inv_root,
                              show_severity="high")
    out = "".join(answer(
        "vpc reference",
        db,
        brain,
        embedder=None,
        llm=_DownLLM(),
        cache_dir=tmp_path / ".cache",
        invalidations_cfg=cfg,
    ))
    assert "This answer references resources" not in out
    _clear_cache()


def test_answer_disabled_invalidations_skips_lookup(tmp_path):
    """When the feature is disabled in config, no banner regardless of events."""
    brain = tmp_path / "brain"
    brain.mkdir()
    chunk_file = brain / "docs" / "z.md"
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_text("aws_vpc.shared (vpc-abc12345)\n", encoding="utf-8")
    db = tmp_path / "vault.sqlite"
    _seed_index(db, body="aws_vpc.shared vpc-abc12345", path=chunk_file)

    inv_root = tmp_path / "brain-invalidations"
    emit(brain, "vpc-abc12345", "detach", "critical",
         invalidations_root=inv_root)
    _clear_cache()

    cfg = InvalidationsConfig(enabled=False, repo_path=inv_root)
    out = "".join(answer(
        "aws_vpc shared",
        db,
        brain,
        embedder=None,
        llm=_DownLLM(),
        cache_dir=tmp_path / ".cache",
        invalidations_cfg=cfg,
    ))
    assert "This answer references resources" not in out
    _clear_cache()


def test_answer_low_severity_recorded_in_audit_only(tmp_path):
    """Low-severity events suppress the banner but still bump the audit counter."""
    import json

    brain = tmp_path / "brain"
    brain.mkdir()
    chunk_file = brain / "docs" / "audited.md"
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_text("vpc-7777aaaa reference\n", encoding="utf-8")
    db = tmp_path / "vault.sqlite"
    _seed_index(db, body="vpc-7777aaaa reference", path=chunk_file)

    inv_root = tmp_path / "brain-invalidations"
    emit(brain, "vpc-7777aaaa", "modify", "low",
         invalidations_root=inv_root)
    _clear_cache()

    cache = tmp_path / ".cache"
    cfg = InvalidationsConfig(enabled=True, repo_path=inv_root,
                              show_severity="high")
    "".join(answer(
        "vpc-7777aaaa",
        db,
        brain,
        embedder=None,
        llm=_DownLLM(),
        cache_dir=cache,
        invalidations_cfg=cfg,
    ))
    audit_path = cache / "audit.jsonl"
    assert audit_path.exists()
    last = json.loads(audit_path.read_text().strip().splitlines()[-1])
    # The match counted; the banner did not surface.
    assert last.get("invalidations_matched", 0) >= 1
    _clear_cache()


def test_answer_default_invalidations_root_via_sibling(tmp_path):
    """When config has no repo_path, ask() probes ``<brain>/../brain-invalidations``."""
    parent = tmp_path
    brain = parent / "brain"
    brain.mkdir()
    chunk_file = brain / "docs" / "deploy.md"
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_text("vpc-deadbeef ref\n", encoding="utf-8")
    db = tmp_path / "vault.sqlite"
    _seed_index(db, body="vpc-deadbeef ref", path=chunk_file)

    sibling = parent / "brain-invalidations"
    emit(brain, "vpc-deadbeef", "detach", "high",
         invalidations_root=sibling)
    _clear_cache()

    # Pass repo_path=None so the auto-detect path runs.
    cfg = InvalidationsConfig(enabled=True, repo_path=None,
                              show_severity="high")
    out = "".join(answer(
        "vpc deadbeef",
        db,
        brain,
        embedder=None,
        llm=_DownLLM(),
        cache_dir=tmp_path / ".cache",
        invalidations_cfg=cfg,
    ))
    assert "vpc-deadbeef" in out
    assert "HIGH" in out
    _clear_cache()
