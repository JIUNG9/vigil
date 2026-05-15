"""Single-writer indexer for the brain.

Reads markdown from <brain_root>/archive/ and other curated dirs, embeds each
chunk via Ollama, upserts to Qdrant. Per-doc SHA tracking ensures re-runs
only re-embed changed content.

Run as a long-lived Deployment (replicas=1, strategy=Recreate) inside k8s,
or as a one-shot `teammate index --rebuild` from the CLI.
"""

from teammate.indexer.main import Indexer, IndexResult

__all__ = ["Indexer", "IndexResult"]
