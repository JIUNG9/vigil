"""Indexer — embeds brain markdown into Qdrant.

Idempotent: tracks per-doc SHA in Qdrant payload. A doc whose source SHA
matches the stored payload is skipped (no re-embed).

Single-writer by design. The K8s Deployment uses replicas=1 + Recreate to
guarantee at most one instance is upserting at any time. Qdrant handles
concurrent READS fine via MVCC — only writes are serialized.

Env vars:
  QDRANT_URL          default http://qdrant:6333
  QDRANT_COLLECTION   default brain
  OLLAMA_URL          default http://ollama:11434
  OLLAMA_EMBED_MODEL  default nomic-embed-text
  TEAMMATE_BRAIN_ROOT default cwd
  TEAMMATE_INDEXED_DIRS comma-separated subdirs to index (default: archive,docs,knowledge)
  TEAMMATE_CHUNK_SIZE default 800 chars
  TEAMMATE_CHUNK_OVERLAP default 100 chars
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class IndexResult:
    """Outcome of one indexer run."""
    docs_seen: int = 0
    docs_unchanged: int = 0
    docs_embedded: int = 0
    chunks_upserted: int = 0
    duration_sec: float = 0.0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"indexer: seen={self.docs_seen} unchanged={self.docs_unchanged} "
            f"embedded={self.docs_embedded} chunks={self.chunks_upserted} "
            f"errors={len(self.errors)} in {self.duration_sec:.1f}s"
        )


class Indexer:
    """Reads brain markdown, embeds, upserts to Qdrant."""

    def __init__(self, brain_root: Path | None = None):
        self.brain_root = Path(brain_root or os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
        self.qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
        self.collection = os.environ.get("QDRANT_COLLECTION", "brain")
        self.ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
        self.embed_model = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self.chunk_size = int(os.environ.get("TEAMMATE_CHUNK_SIZE", "800"))
        self.chunk_overlap = int(os.environ.get("TEAMMATE_CHUNK_OVERLAP", "100"))
        dirs_raw = os.environ.get("TEAMMATE_INDEXED_DIRS", "archive,docs,knowledge,decisions")
        self.indexed_dirs = [d.strip() for d in dirs_raw.split(",") if d.strip()]

    # ----- public API -----

    def ensure_collection(self) -> None:
        """Create the Qdrant collection if it doesn't exist. Idempotent."""
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx required: pip install 'claude-vigil[rag]'") from exc

        # nomic-embed-text emits 768-dim vectors
        spec = {"vectors": {"size": 768, "distance": "Cosine"}}
        with httpx.Client(timeout=30) as client:
            r = client.get(f"{self.qdrant_url}/collections/{self.collection}")
            if r.status_code == 200:
                return
            r = client.put(f"{self.qdrant_url}/collections/{self.collection}", json=spec)
            r.raise_for_status()
            log.info("created Qdrant collection: %s", self.collection)

    def run(self) -> IndexResult:
        """Walk brain, upsert changed docs, return outcome."""
        started = time.time()
        result = IndexResult()
        self.ensure_collection()

        for path in self._iter_markdown():
            result.docs_seen += 1
            try:
                source_sha = self._sha(path)
                if self._already_indexed(path, source_sha):
                    result.docs_unchanged += 1
                    continue
                chunks = list(self._chunk_file(path))
                vectors = self._embed_batch([c["text"] for c in chunks])
                self._upsert(path, source_sha, chunks, vectors)
                result.docs_embedded += 1
                result.chunks_upserted += len(chunks)
            except Exception as exc:
                msg = f"{path}: {exc}"
                log.warning("indexer error: %s", msg)
                result.errors.append(msg)

        result.duration_sec = time.time() - started
        log.info("%s", result)
        return result

    # ----- file walking & chunking -----

    def _iter_markdown(self) -> Iterator[Path]:
        for subdir in self.indexed_dirs:
            root = self.brain_root / subdir
            if not root.exists():
                continue
            for p in root.rglob("*.md"):
                if p.is_file():
                    yield p

    def _sha(self, path: Path) -> str:
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()[:16]

    def _chunk_file(self, path: Path) -> Iterator[dict]:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(self.brain_root).as_posix()
        # Strip YAML frontmatter so we don't embed the metadata
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end > 0:
                body = text[end + 5:]
        # Simple character-based chunker. Future: token-aware splitter.
        i = 0
        idx = 0
        while i < len(body):
            chunk = body[i:i + self.chunk_size]
            yield {
                "text": chunk.strip(),
                "path": rel,
                "chunk_idx": idx,
            }
            idx += 1
            i += self.chunk_size - self.chunk_overlap

    # ----- Qdrant helpers -----

    def _already_indexed(self, path: Path, source_sha: str) -> bool:
        """Skip if Qdrant already has chunks for this path with the same SHA."""
        try:
            import httpx
        except ImportError:
            return False
        rel = path.relative_to(self.brain_root).as_posix()
        body = {
            "limit": 1,
            "filter": {
                "must": [
                    {"key": "path", "match": {"value": rel}},
                    {"key": "source_sha", "match": {"value": source_sha}},
                ]
            },
            "with_payload": False,
        }
        with httpx.Client(timeout=15) as client:
            r = client.post(f"{self.qdrant_url}/collections/{self.collection}/points/scroll", json=body)
            if r.status_code != 200:
                return False
            return bool(r.json().get("result", {}).get("points"))

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via Ollama."""
        import httpx
        out = []
        with httpx.Client(timeout=60) as client:
            for t in texts:
                r = client.post(
                    f"{self.ollama_url}/api/embeddings",
                    json={"model": self.embed_model, "prompt": t},
                )
                r.raise_for_status()
                out.append(r.json()["embedding"])
        return out

    def _upsert(self, path: Path, source_sha: str, chunks: list[dict], vectors: list[list[float]]) -> None:
        """Upsert all chunks for a single doc, replacing prior versions."""
        import httpx

        rel = path.relative_to(self.brain_root).as_posix()
        # First, delete any prior chunks for this path (idempotent re-index).
        with httpx.Client(timeout=30) as client:
            client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/delete",
                json={"filter": {"must": [{"key": "path", "match": {"value": rel}}]}},
            )

            points = []
            for ch, vec in zip(chunks, vectors, strict=True):
                # Deterministic point id so re-runs are idempotent.
                pid = hashlib.md5(f"{rel}:{ch['chunk_idx']}".encode()).hexdigest()
                # Qdrant requires integer or UUID; we use the first 16 hex chars as int.
                point_id = int(pid[:15], 16)
                points.append({
                    "id": point_id,
                    "vector": vec,
                    "payload": {
                        "path": rel,
                        "source_sha": source_sha,
                        "chunk_idx": ch["chunk_idx"],
                        "text": ch["text"],
                    },
                })

            r = client.put(
                f"{self.qdrant_url}/collections/{self.collection}/points",
                json={"points": points},
                params={"wait": "true"},
            )
            r.raise_for_status()


def main() -> int:
    """CLI entry point: vigil index --rebuild calls this."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    indexer = Indexer()
    result = indexer.run()
    print(str(result))
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
