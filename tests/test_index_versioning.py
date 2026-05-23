"""Tests for index versioning — the (provider, model, dim) stamp."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from vigil.providers.base import EmbeddingProvider, LLMProvider


class FakeEmbedder(EmbeddingProvider):
    """Test double — deterministic, never makes a network call."""

    def __init__(self, model: str = "fake-embed", dim: int = 4):
        self._model = model
        self._dim = dim

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def is_up(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            seed = (sum(ord(c) for c in t) % 7) / 7.0
            out.append([seed + i * 0.01 for i in range(self._dim)])
        return out


class _UnusedLLM(LLMProvider):
    @property
    def model_id(self) -> str:
        return "unused"

    def is_up(self) -> bool:
        return False

    def generate(self, prompt, system=None, *, stream=True) -> Iterator[str]:
        if False:
            yield ""
        return


def _seed_brain(tmp_path: Path) -> Path:
    md = tmp_path / "CLAUDE.md"
    md.write_text(
        "# Team Brain\n\n"
        "Hello world. This is a test paragraph.\n\n"
        "Another paragraph for chunking.\n",
        encoding="utf-8",
    )
    return md


def test_first_index_stamps_meta(tmp_path: Path):
    import sqlite3

    from vigil.rag.index import index_paths

    md = _seed_brain(tmp_path)
    cache = tmp_path / ".teammate-cache"
    embedder = FakeEmbedder(model="fake-A", dim=4)
    indexed, _ = index_paths([md], cache, embedder=embedder)
    assert indexed == 1

    conn = sqlite3.connect(str(cache / "vault.sqlite"))
    rows = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
    conn.close()

    # _provider_name strips a "Provider" suffix; FakeEmbedder has none, so it
    # lowercases the class name verbatim. (For OllamaProvider it'd be "ollama".)
    assert rows["provider"] == "fakeembedder"
    assert rows["embedding_model"] == "fake-A"
    assert rows["embedding_dim"] == "4"
    assert "created_at" in rows
    assert "teammate_version" in rows


def test_second_index_with_same_provider_succeeds(tmp_path: Path):
    from vigil.rag.index import index_paths

    md = _seed_brain(tmp_path)
    cache = tmp_path / ".teammate-cache"
    embedder = FakeEmbedder(model="fake-A", dim=4)

    index_paths([md], cache, embedder=embedder)
    # No exception on the second call.
    indexed, skipped = index_paths([md], cache, embedder=embedder)
    assert indexed >= 0
    assert skipped >= 0


def test_second_index_with_different_provider_raises(tmp_path: Path):
    from vigil.rag.index import IndexVersionMismatch, index_paths

    md = _seed_brain(tmp_path)
    cache = tmp_path / ".teammate-cache"

    index_paths([md], cache, embedder=FakeEmbedder(model="fake-A", dim=4))

    with pytest.raises(IndexVersionMismatch) as exc_info:
        index_paths([md], cache, embedder=FakeEmbedder(model="fake-B", dim=4))

    msg = str(exc_info.value)
    assert "fake-A" in msg
    assert "fake-B" in msg
    assert "rebuild" in msg.lower()


def test_different_dim_raises(tmp_path: Path):
    from vigil.rag.index import IndexVersionMismatch, index_paths

    md = _seed_brain(tmp_path)
    cache = tmp_path / ".teammate-cache"

    index_paths([md], cache, embedder=FakeEmbedder(model="fake-A", dim=4))
    with pytest.raises(IndexVersionMismatch):
        index_paths([md], cache, embedder=FakeEmbedder(model="fake-A", dim=8))


def test_rebuild_wipes_and_restamps(tmp_path: Path):
    import sqlite3

    from vigil.rag.index import index_paths

    md = _seed_brain(tmp_path)
    cache = tmp_path / ".teammate-cache"

    index_paths([md], cache, embedder=FakeEmbedder(model="fake-A", dim=4))

    # Different embedder, but with rebuild=True it must succeed.
    index_paths(
        [md], cache, embedder=FakeEmbedder(model="fake-B", dim=8), rebuild=True
    )

    conn = sqlite3.connect(str(cache / "vault.sqlite"))
    rows = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
    conn.close()

    assert rows["embedding_model"] == "fake-B"
    assert rows["embedding_dim"] == "8"


def test_open_index_validates_stamp(tmp_path: Path):
    from vigil.rag.index import IndexVersionMismatch, index_paths, open_index

    md = _seed_brain(tmp_path)
    cache = tmp_path / ".teammate-cache"
    index_paths([md], cache, embedder=FakeEmbedder(model="fake-A", dim=4))

    # open_index without an embedder is fine.
    conn = open_index(cache)
    conn.close()

    # open_index with a matching embedder is fine.
    conn = open_index(cache, embedder=FakeEmbedder(model="fake-A", dim=4))
    conn.close()

    # open_index with a mismatched embedder raises.
    with pytest.raises(IndexVersionMismatch):
        open_index(cache, embedder=FakeEmbedder(model="fake-B", dim=4))


def test_no_embedder_skips_stamp_check(tmp_path: Path):
    """Indexing without an embedder doesn't stamp; subsequent indexes are unconstrained."""
    from vigil.rag.index import index_paths

    md = _seed_brain(tmp_path)
    cache = tmp_path / ".teammate-cache"

    indexed, _ = index_paths([md], cache, embedder=None)
    assert indexed == 1

    # Now index with a real embedder — should stamp now (no prior stamp).
    indexed2, _ = index_paths(
        [md], cache, embedder=FakeEmbedder(model="fake-A", dim=4), rebuild=True
    )
    assert indexed2 == 1
