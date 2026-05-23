"""Tests for the provider abstraction (v0.3)."""

from __future__ import annotations

import warnings


def test_provider_abcs_are_importable():
    from vigil.providers import EmbeddingProvider, LLMProvider

    assert LLMProvider is not None
    assert EmbeddingProvider is not None


def test_ollama_provider_implements_both_abcs():
    from vigil.providers import EmbeddingProvider, LLMProvider, OllamaProvider

    assert issubclass(OllamaProvider, LLMProvider)
    assert issubclass(OllamaProvider, EmbeddingProvider)


def test_ollama_provider_is_up_returns_false_on_bogus_host(monkeypatch):
    """No real Ollama running on a bogus port — must return False, never raise."""
    from vigil.providers import OllamaProvider

    p = OllamaProvider(host="http://127.0.0.1:1")  # nothing listens on port 1
    assert p.is_up() is False


def test_ollama_provider_is_up_returns_false_on_connect_error(monkeypatch):
    """Mock httpx.get to raise ConnectError; is_up must swallow it."""
    import httpx

    from vigil.providers import OllamaProvider

    def _raise(*_a, **_kw):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "get", _raise)
    p = OllamaProvider(host="http://localhost:11434")
    assert p.is_up() is False


def test_ollama_provider_dim_default_is_768():
    from vigil.providers import OllamaProvider

    p = OllamaProvider()
    assert p.dim == 768


def test_ollama_provider_dim_override():
    from vigil.providers import OllamaProvider

    p = OllamaProvider(dim=1536)
    assert p.dim == 1536


def test_ollama_provider_model_id_returns_llm_model():
    from vigil.providers import OllamaProvider

    p = OllamaProvider(llm_model="llama3.2:3b")
    assert p.model_id == "llama3.2:3b"


def test_load_llm_provider_returns_none_for_unknown():
    from vigil.config import ProviderConfig
    from vigil.providers import load_llm_provider

    cfg = ProviderConfig(provider="some-future-thing", model="x")
    assert load_llm_provider(cfg) is None


def test_load_llm_provider_returns_none_for_explicit_none():
    from vigil.config import ProviderConfig
    from vigil.providers import load_llm_provider

    cfg = ProviderConfig(provider="none", model="")
    assert load_llm_provider(cfg) is None


def test_load_embedding_provider_returns_none_for_unknown():
    from vigil.config import ProviderConfig
    from vigil.providers import load_embedding_provider

    cfg = ProviderConfig(provider="some-future-thing", model="x")
    assert load_embedding_provider(cfg) is None


def test_load_llm_provider_builds_ollama():
    from vigil.config import ProviderConfig
    from vigil.providers import OllamaProvider, load_llm_provider

    cfg = ProviderConfig(
        provider="ollama",
        model="llama3.2:3b",
        options={"host": "http://localhost:11434"},
    )
    p = load_llm_provider(cfg)
    assert isinstance(p, OllamaProvider)
    assert p.model_id == "llama3.2:3b"


def test_back_compat_shim_still_exposes_ollama_client():
    """Existing v0.2 importers must keep working (with a DeprecationWarning)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Force a fresh import so the warning fires.
        import importlib
        import sys

        sys.modules.pop("teammate.rag.ollama", None)
        mod = importlib.import_module("teammate.rag.ollama")
        assert hasattr(mod, "OllamaClient")
        assert hasattr(mod, "OllamaUnavailable")
        assert hasattr(mod, "OllamaError")
        assert any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ), "deprecation warning should have fired"


def test_back_compat_exceptions_are_aliases():
    """OllamaUnavailable/OllamaError must alias the new ProviderUnavailable/Error."""
    from vigil.providers import (
        OllamaError,
        OllamaUnavailable,
        ProviderError,
        ProviderUnavailable,
    )

    assert OllamaUnavailable is ProviderUnavailable
    assert OllamaError is ProviderError
