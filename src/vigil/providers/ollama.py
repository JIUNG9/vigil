"""Ollama provider — implements both ``LLMProvider`` and ``EmbeddingProvider``.

Hits the raw Ollama HTTP API via ``httpx``. No official Ollama Python SDK —
keeps the dependency footprint at the floor and lets us catch network errors
at the transport layer without an extra wrapper.

Endpoints used:

  - ``GET  /api/tags``       — health check + model listing
  - ``POST /api/embed``      — newer batch embedding endpoint
  - ``POST /api/embeddings`` — older singular endpoint, used as a fallback
  - ``POST /api/generate``   — streaming completion

Embedding dimension defaults to 768 (``nomic-embed-text``). Override via the
``dim`` constructor argument when configuring a different embedding model.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from vigil.providers.base import (
    EmbeddingProvider,
    LLMProvider,
    ProviderError,
    ProviderUnavailable,
)
from vigil.rag import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_OLLAMA_HOST,
)

# Back-compat aliases. Existing code catches OllamaUnavailable / OllamaError;
# keep those names valid so the v0.2 → v0.3 swap is import-only.
OllamaUnavailable = ProviderUnavailable
OllamaError = ProviderError

# Default embedding dim for nomic-embed-text. Other models override via config.
DEFAULT_EMBEDDING_DIM = 768


class OllamaProvider(LLMProvider, EmbeddingProvider):
    """Ollama HTTP client with both LLM and embedding capabilities.

    ``timeout_s`` applies per request. Embeddings are sub-second on a small
    model. ``generate`` streams — the timeout governs time-to-first-token,
    not total response time.
    """

    def __init__(
        self,
        host: str | None = None,
        llm_model: str | None = None,
        embedding_model: str | None = None,
        timeout_s: float = 30.0,
        dim: int | None = None,
    ):
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
        self.llm_model = llm_model or DEFAULT_LLM_MODEL
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        self.timeout_s = timeout_s
        self._dim = dim if dim is not None else DEFAULT_EMBEDDING_DIM

    # ---- properties ----

    @property
    def model_id(self) -> str:
        return self.llm_model

    @property
    def dim(self) -> int:
        return self._dim

    # ---- health ----

    def is_up(self) -> bool:
        """Returns False on any transport error. Never raises."""
        try:
            import httpx
        except ImportError:
            return False
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=2.0)
            return r.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException, OSError):
            return False
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """Return names of locally-pulled Ollama models."""
        try:
            import httpx
        except ImportError as exc:
            raise ProviderUnavailable("httpx not installed") from exc
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=self.timeout_s)
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
            raise ProviderUnavailable(str(exc)) from exc
        r.raise_for_status()
        data = r.json()
        return [m["name"] for m in data.get("models", [])]

    # ---- embeddings ----

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per input.

        Tries the newer ``/api/embed`` endpoint first; falls back to
        ``/api/embeddings`` (singular) for older Ollama versions.
        """
        try:
            import httpx
        except ImportError as exc:
            raise ProviderUnavailable("httpx not installed") from exc

        m = self.embedding_model
        payload: dict[str, Any] = {"model": m, "input": texts}
        try:
            r = httpx.post(
                f"{self.host}/api/embed", json=payload, timeout=self.timeout_s
            )
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
            raise ProviderUnavailable(str(exc)) from exc

        if r.status_code == 200:
            return r.json()["embeddings"]
        if r.status_code == 404:
            out: list[list[float]] = []
            for t in texts:
                try:
                    rr = httpx.post(
                        f"{self.host}/api/embeddings",
                        json={"model": m, "prompt": t},
                        timeout=self.timeout_s,
                    )
                    rr.raise_for_status()
                    out.append(rr.json()["embedding"])
                except (httpx.HTTPError, OSError) as exc:
                    raise ProviderError(f"embedding failed: {exc}") from exc
            return out
        raise ProviderError(f"embed failed: HTTP {r.status_code}: {r.text[:200]}")

    # ---- generation ----

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        *,
        stream: bool = True,
    ) -> Iterator[str]:
        """Stream text deltas from Ollama. Yields ``str`` chunks only."""
        try:
            import httpx
        except ImportError as exc:
            raise ProviderUnavailable("httpx not installed") from exc

        m = self.llm_model
        payload: dict[str, Any] = {
            "model": m,
            "prompt": prompt,
            "stream": stream,
        }
        if system:
            payload["system"] = system

        try:
            with httpx.stream(
                "POST",
                f"{self.host}/api/generate",
                json=payload,
                timeout=self.timeout_s,
            ) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", errors="ignore")[:200]
                    raise ProviderError(
                        f"generate failed: HTTP {r.status_code}: {body}"
                    )
                if not stream:
                    body = r.read().decode("utf-8", errors="ignore")
                    try:
                        data = json.loads(body)
                        yield data.get("response", "")
                        return
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            f"non-streaming parse failed: {exc}"
                        ) from exc
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "response" in chunk:
                        yield chunk["response"]
                    if chunk.get("done"):
                        break
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
            raise ProviderUnavailable(str(exc)) from exc


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "OllamaError",
    "OllamaProvider",
    "OllamaUnavailable",
]
