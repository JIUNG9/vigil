"""Provider abstraction — the seam between vigil and any LLM/embedding backend.

Why this exists: v0.2 hard-coded Ollama. That works on a laptop. It does not
work behind a corporate proxy where the only path to a model is an internal
HTTP gateway, nor in a team that already has Anthropic / OpenAI keys and an
SSO-bound API budget. Providers let the same vigil binary swap backends
via config without touching code.

Two ABCs: ``LLMProvider`` for completion, ``EmbeddingProvider`` for vectors.
A single concrete class can implement both (Ollama does). Two exceptions —
``ProviderUnavailable`` (transport down, retry) and ``ProviderError``
(provider responded with an error, don't retry blindly). Callers degrade
to keyword search on ``ProviderUnavailable``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class ProviderUnavailable(RuntimeError):
    """Provider transport is down (network error, host not reachable, missing dep).

    Caller should fall back gracefully — keyword retrieval, cached response, etc.
    """


class ProviderError(RuntimeError):
    """Provider responded but errored (missing model, bad auth, rate limit).

    Caller should surface the message; retrying without a fix usually won't help.
    """


class LLMProvider(ABC):
    """Streaming text completion.

    Concrete providers implement ``generate``, ``is_up``, and ``model_id``.
    """

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system: str | None = None,
        *,
        stream: bool = True,
    ) -> Iterator[str]:
        """Yield text deltas as ``str``.

        Streaming-semantics contract — providers MUST:

          - Yield only ``str`` chunks. Each chunk is a piece of the model's
            text output. Non-text events (tool-call deltas, stop reasons,
            usage stats) are silently dropped at this layer; if a downstream
            caller needs them, that's a v0.4+ concern and goes through a
            different method, not this one.
          - When ``stream=True``: yield each delta as it arrives. The full
            answer is the concatenation of all yielded chunks.
          - When ``stream=False``: yield exactly one chunk containing the
            full response, then return.
          - Raise ``ProviderUnavailable`` if the transport drops mid-stream.
            Raise ``ProviderError`` for protocol-level errors (bad model
            name, auth failure, rate limit).
        """

    @abstractmethod
    def is_up(self) -> bool:
        """Cheap health check. Returns False on any error. Must not raise."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """The configured model identifier. Used for diagnostics and logs."""


class EmbeddingProvider(ABC):
    """Batch embedding to fixed-dimension float vectors.

    The ``dim`` property is load-bearing: the index stamps it at build
    time and refuses to query if the configured provider's dim differs.
    Mismatched dimensions silently corrupt similarity scores.
    """

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. One vector per input, all of length ``dim``."""

    @abstractmethod
    def is_up(self) -> bool:
        """Cheap health check. Returns False on any error. Must not raise."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """The configured embedding model identifier."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Vector dimension. Used to stamp the index at build time."""


__all__ = [
    "EmbeddingProvider",
    "LLMProvider",
    "ProviderError",
    "ProviderUnavailable",
]
