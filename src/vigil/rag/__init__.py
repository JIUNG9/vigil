"""Local LLM + RAG over the compliance vault.

The headline pillar. Without this, ``vigil`` is just a compliance scanner.
With this, the new SRE on day 1 can ``vigil ask "what's our current K-ISMS-P
posture and which controls are failing?"`` and get a grounded answer from a
local LLM running on their laptop, no cloud round-trip.

Design choices:

- **Ollama as the LLM and embeddings host.** Ubiquitous, no API key, runs
  offline. Default LLM model: ``llama3.2:3b`` (small, fast on laptops).
  Default embedding model: ``nomic-embed-text`` (small, fast, English-good).
  Both are configurable via env vars or ``vigil.toml``.
- **Sqlite-backed vector store.** No external vector DB. Embeddings are
  stored as binary blobs in a single sqlite file under ``.vigil-cache/``.
  Cosine similarity is computed in pure Python at query time. Plenty fast
  for a single-team vault (hundreds of files, not millions).
- **Graceful fallback.** If Ollama isn't running, ``ask`` falls back to
  keyword search (returns relevant file paths without LLM synthesis).
  If the embedding model isn't available, indexer falls back to BM25.
- **gbrain compatibility.** If ``gbrain`` binary is on PATH, ``vigil
  init`` offers to register the vault as a gbrain source. The two systems
  coexist — gbrain handles cross-machine sync, vigil's mini-RAG handles
  the local query path.

ASCII flow::

    vigil ask "<query>"
            │
            ▼
       rag.ask.answer()
            │
       ┌────┴────┐
       ▼         ▼
    Ollama    Vault on disk
    (HTTP    (markdown files)
     local)
       │         │
       └────┬────┘
            ▼
    Top-k chunks (embedding similarity)
            │
            ▼
    Ollama LLM call ← system prompt + context + query
            │
            ▼
    Streamed answer to terminal
"""

DEFAULT_LLM_MODEL = "llama3.2:3b"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

__all__ = ["DEFAULT_LLM_MODEL", "DEFAULT_EMBEDDING_MODEL", "DEFAULT_OLLAMA_HOST"]
