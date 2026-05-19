"""Provider registry + factories.

v0.3 ships a single concrete provider — Ollama. The abstraction floor exists
so v0.4 can add Anthropic, OpenAI, and an HTTP-generic shim without rewriting
the call sites in ``rag/ask.py``, ``rag/index.py``, or ``mcp_server.py``.

Factories return ``None`` when the provider name is ``"none"`` or unknown.
Callers fall back to keyword-only retrieval in that case — the index still
works, the answers just come from BM25 without LLM synthesis.
"""

from __future__ import annotations

from vigil.providers.base import (
    EmbeddingProvider,
    LLMProvider,
    ProviderError,
    ProviderUnavailable,
)
from vigil.providers.ollama import (
    DEFAULT_EMBEDDING_DIM,
    OllamaError,
    OllamaProvider,
    OllamaUnavailable,
)

# Registry keys = provider names that may appear in config. Values are
# constructor functions ``(ProviderConfig) -> Provider``.
#
# v0.3 only ships Ollama. The abstraction is real but the registry is empty
# beyond it. v0.4 will add anthropic / openai / http-generic, gated on the
# workplace deployment requirements.

_LLM_REGISTRY: dict[str, str] = {
    "ollama": "ollama",
}

_EMBEDDING_REGISTRY: dict[str, str] = {
    "ollama": "ollama",
}


def _build_ollama(config) -> OllamaProvider:  # type: ignore[no-untyped-def]
    opts = config.options or {}
    host = opts.get("host") or opts.get("base_url")
    timeout = float(opts.get("timeout_s", 30.0))
    dim = opts.get("dim")
    return OllamaProvider(
        host=host,
        llm_model=config.model,
        embedding_model=config.model,
        timeout_s=timeout,
        dim=int(dim) if dim is not None else None,
    )


def load_llm_provider(config) -> LLMProvider | None:  # type: ignore[no-untyped-def]
    """Build an ``LLMProvider`` from config, or ``None`` for fallback.

    Returns ``None`` when the provider name is ``"none"`` or unrecognized;
    callers degrade to keyword-only retrieval in that case.
    """
    name = (config.provider or "").lower()
    if name in {"", "none"}:
        return None
    if name not in _LLM_REGISTRY:
        return None
    if name == "ollama":
        return _build_ollama(config)
    return None


def load_embedding_provider(config) -> EmbeddingProvider | None:  # type: ignore[no-untyped-def]
    """Build an ``EmbeddingProvider`` from config, or ``None`` for fallback.

    Returns ``None`` when the provider name is ``"none"`` or unrecognized;
    indexing still proceeds without embeddings (keyword search retrieval).
    """
    name = (config.provider or "").lower()
    if name in {"", "none"}:
        return None
    if name not in _EMBEDDING_REGISTRY:
        return None
    if name == "ollama":
        return _build_ollama(config)
    return None


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "EmbeddingProvider",
    "LLMProvider",
    "OllamaError",
    "OllamaProvider",
    "OllamaUnavailable",
    "ProviderError",
    "ProviderUnavailable",
    "load_embedding_provider",
    "load_llm_provider",
]
