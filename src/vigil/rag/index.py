"""Vault indexer — chunks markdown files, embeds via a provider, stores in sqlite.

Index lives at ``.vigil-cache/vault.sqlite`` (per repo). Schema::

    chunks (
        id           INTEGER PRIMARY KEY,
        path         TEXT NOT NULL,           -- absolute path to source file
        chunk_idx    INTEGER NOT NULL,        -- 0-based chunk number within file
        text         TEXT NOT NULL,           -- the chunk text
        embedding    BLOB,                    -- pickled list[float], or NULL if no model
        token_count  INTEGER,                 -- approx; for budget tracking
        mtime        REAL NOT NULL,           -- source file mtime at index time
        framework    TEXT,                    -- parsed from frontmatter, optional
        control      TEXT,                    -- parsed from frontmatter, optional
        kind         TEXT                     -- score | evidence | advisory | attestation | doc
    )

    UNIQUE (path, chunk_idx)

    index_meta (
        key          TEXT PRIMARY KEY,
        value        TEXT NOT NULL
    )

The ``index_meta`` table stamps the provider identity at index time —
``(provider, embedding_model, embedding_dim)``. Switching providers without
``--rebuild`` would silently corrupt similarity scores (vectors of different
dim, different geometry), so the indexer refuses to open an index whose
stamp doesn't match the configured provider.

Re-indexing is incremental: a file whose mtime hasn't changed since last
index is skipped. ``--rebuild`` flag in the CLI clears the table first.

Chunking strategy is intentionally simple: split on blank lines, then group
adjacent paragraphs into ~500-token windows. No semantic chunking, no
hierarchical embeddings. This is v0.1; smarter chunking is a v0.2 nice-to-have.
"""

from __future__ import annotations

import datetime as _dt
import pickle
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vigil import __version__ as _TEAMMATE_VERSION
from vigil.providers.base import (
    EmbeddingProvider,
    ProviderError,
    ProviderUnavailable,
)

# ---------- chunking ----------

# A wildly approximate "tokens" estimator: 4 chars per token. Good enough
# to keep windows under the embedding model's input limit without pulling
# in tiktoken.
_CHARS_PER_TOKEN = 4
_TARGET_TOKENS_PER_CHUNK = 500
_TARGET_CHARS_PER_CHUNK = _TARGET_TOKENS_PER_CHUNK * _CHARS_PER_TOKEN


class IndexVersionMismatch(RuntimeError):
    """Raised when the stored index stamp doesn't match the configured provider.

    The fields are surfaced on the exception so callers (CLI, MCP) can render
    a precise rebuild hint.
    """

    def __init__(
        self,
        *,
        stored_provider: str,
        stored_model: str,
        stored_dim: int,
        configured_provider: str,
        configured_model: str,
        configured_dim: int,
    ):
        self.stored_provider = stored_provider
        self.stored_model = stored_model
        self.stored_dim = stored_dim
        self.configured_provider = configured_provider
        self.configured_model = configured_model
        self.configured_dim = configured_dim
        super().__init__(
            f"Index was built by `{stored_model}` ({stored_dim}d, {stored_provider}) "
            f"but current config is `{configured_model}` ({configured_dim}d, "
            f"{configured_provider}). Run `vigil index --rebuild` to re-embed "
            f"under the new provider."
        )


@dataclass(frozen=True, slots=True)
class Chunk:
    path: Path
    chunk_idx: int
    text: str
    framework: str
    control: str
    kind: str
    mtime: float


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and return (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    closing = text.find("\n---", 3)
    if closing == -1:
        return {}, text
    raw = text[3:closing].strip()
    body_start = text.find("\n", closing + 4)
    body = text[body_start + 1 :] if body_start != -1 else ""
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        meta = {}
    return meta if isinstance(meta, dict) else {}, body


def chunk_markdown(path: Path) -> list[Chunk]:
    """Read a markdown file, parse frontmatter, return Chunk list."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    meta, body = _parse_frontmatter(raw)
    framework = str(meta.get("framework", "")) if meta else ""
    control = str(meta.get("control", "")) if meta else ""
    kind = str(meta.get("vigil_kind", "doc")) if meta else "doc"
    mtime = path.stat().st_mtime if path.exists() else 0.0

    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_chars = 0
    chunk_idx = 0
    for para in paragraphs:
        if buf and buf_chars + len(para) > _TARGET_CHARS_PER_CHUNK:
            chunks.append(
                Chunk(
                    path=path,
                    chunk_idx=chunk_idx,
                    text="\n\n".join(buf),
                    framework=framework,
                    control=control,
                    kind=kind,
                    mtime=mtime,
                )
            )
            chunk_idx += 1
            buf = []
            buf_chars = 0
        buf.append(para)
        buf_chars += len(para) + 2
    if buf:
        chunks.append(
            Chunk(
                path=path,
                chunk_idx=chunk_idx,
                text="\n\n".join(buf),
                framework=framework,
                control=control,
                kind=kind,
                mtime=mtime,
            )
        )
    return chunks


# ---------- index db ----------


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB,
    token_count INTEGER,
    mtime REAL NOT NULL,
    framework TEXT,
    control TEXT,
    kind TEXT,
    UNIQUE (path, chunk_idx)
);

CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);
CREATE INDEX IF NOT EXISTS chunks_kind_idx ON chunks(kind);
CREATE INDEX IF NOT EXISTS chunks_framework_idx ON chunks(framework);

CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    cur = conn.execute("SELECT key, value FROM index_meta")
    return dict(cur.fetchall())


def _write_meta(conn: sqlite3.Connection, meta: dict[str, str]) -> None:
    for key, value in meta.items():
        conn.execute(
            "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
            (key, value),
        )


def _provider_name(embedder: EmbeddingProvider) -> str:
    """Best-effort provider name (used to stamp the index)."""
    cls = type(embedder).__name__
    if cls.endswith("Provider"):
        return cls[: -len("Provider")].lower()
    return cls.lower()


def _stamp_index(conn: sqlite3.Connection, embedder: EmbeddingProvider) -> None:
    meta = {
        "provider": _provider_name(embedder),
        "embedding_model": embedder.model_id,
        "embedding_dim": str(embedder.dim),
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "vigil_version": _TEAMMATE_VERSION,
    }
    _write_meta(conn, meta)
    conn.commit()


def _check_stamp(
    conn: sqlite3.Connection, embedder: EmbeddingProvider | None
) -> None:
    """Raise ``IndexVersionMismatch`` if the stamp disagrees with the embedder."""
    if embedder is None:
        return
    meta = _read_meta(conn)
    if not meta:
        return
    stored_provider = meta.get("provider", "")
    stored_model = meta.get("embedding_model", "")
    try:
        stored_dim = int(meta.get("embedding_dim", "0"))
    except ValueError:
        stored_dim = 0
    cfg_provider = _provider_name(embedder)
    cfg_model = embedder.model_id
    cfg_dim = embedder.dim
    if (stored_provider, stored_model, stored_dim) != (
        cfg_provider,
        cfg_model,
        cfg_dim,
    ):
        raise IndexVersionMismatch(
            stored_provider=stored_provider,
            stored_model=stored_model,
            stored_dim=stored_dim,
            configured_provider=cfg_provider,
            configured_model=cfg_model,
            configured_dim=cfg_dim,
        )


def open_index(
    cache_dir: Path,
    embedder: EmbeddingProvider | None = None,
) -> sqlite3.Connection:
    """Open or create the vault index db. Returns a connection.

    If ``embedder`` is provided and the index has a stamp that disagrees
    with the embedder's ``(provider, model_id, dim)``, raises
    ``IndexVersionMismatch``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / "vault.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    _check_stamp(conn, embedder)
    return conn


def index_paths(
    paths: Iterable[Path],
    cache_dir: Path,
    embedder: EmbeddingProvider | None = None,
    rebuild: bool = False,
) -> tuple[int, int]:
    """Index every markdown file in ``paths``. Returns (indexed, skipped).

    If ``embedder`` is provided and reachable, embed chunks. Otherwise leave
    embedding NULL — fallback retrieval will use keyword search.

    Raises ``IndexVersionMismatch`` if the existing index stamp disagrees
    with the configured embedder. Pass ``rebuild=True`` to wipe and re-stamp.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / "vault.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)

    if rebuild:
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM index_meta")
        conn.commit()
    else:
        _check_stamp(conn, embedder)

    # Stamp the index now if it's empty and we have an embedder.
    if embedder is not None and not _read_meta(conn):
        _stamp_index(conn, embedder)

    indexed = 0
    skipped = 0

    new_chunks: list[Chunk] = []
    for path in paths:
        if not path.exists() or path.suffix != ".md":
            continue
        cur = conn.execute(
            "SELECT MAX(mtime) FROM chunks WHERE path = ?", (str(path),)
        )
        last_mtime = cur.fetchone()[0]
        if last_mtime is not None and last_mtime >= path.stat().st_mtime - 1e-6:
            skipped += 1
            continue
        conn.execute("DELETE FROM chunks WHERE path = ?", (str(path),))
        for chunk in chunk_markdown(path):
            new_chunks.append(chunk)
        indexed += 1

    if not new_chunks:
        conn.commit()
        conn.close()
        return indexed, skipped

    embeddings: list[list[float] | None] = [None] * len(new_chunks)
    if embedder and embedder.is_up():
        try:
            batch_size = 32
            for i in range(0, len(new_chunks), batch_size):
                batch = new_chunks[i : i + batch_size]
                vecs = embedder.embed([c.text for c in batch])
                for j, vec in enumerate(vecs):
                    embeddings[i + j] = vec
        except (ProviderUnavailable, ProviderError):
            # Leave embeddings as None; keyword search still works.
            pass

    for chunk, vec in zip(new_chunks, embeddings, strict=False):
        blob = pickle.dumps(vec) if vec is not None else None
        token_estimate = max(1, len(chunk.text) // _CHARS_PER_TOKEN)
        conn.execute(
            "INSERT INTO chunks (path, chunk_idx, text, embedding, token_count, "
            "mtime, framework, control, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(chunk.path),
                chunk.chunk_idx,
                chunk.text,
                blob,
                token_estimate,
                chunk.mtime,
                chunk.framework,
                chunk.control,
                chunk.kind,
            ),
        )
    conn.commit()
    conn.close()
    return indexed, skipped


def discover_indexable_files(roots: list[Path]) -> list[Path]:
    """Walk roots, return all .md files we should index.

    Currently indexes:
      - Everything under compliance-vault/ (the team's own state)
      - The root CLAUDE.md, if present (team's tribal knowledge)
      - docs/*.md (architecture/reference)
      - README.md (project context)
    """
    out: list[Path] = []
    for root in roots:
        root = root.resolve()
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".md":
            out.append(root)
            continue
        for relpath in (
            "compliance-vault",
            "docs",
        ):
            candidate = root / relpath
            if candidate.is_dir():
                out.extend(p for p in candidate.rglob("*.md"))
        for name in ("CLAUDE.md", "README.md"):
            p = root / name
            if p.exists():
                out.append(p)
    return sorted(set(out))


__all__ = [
    "Chunk",
    "IndexVersionMismatch",
    "chunk_markdown",
    "discover_indexable_files",
    "index_paths",
    "open_index",
]
