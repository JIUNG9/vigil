"""Tests for the config loader and TOML round-tripping."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_load_config_returns_defaults_when_no_file(tmp_path: Path):
    from vigil.config import load_config

    cfg = load_config(tmp_path)
    assert cfg.config_source == "default"
    assert cfg.llm.provider == "ollama"
    assert cfg.llm.model == "llama3.2:3b"
    assert cfg.embedding.provider == "ollama"
    assert cfg.embedding.model == "nomic-embed-text"


def test_repo_config_overrides_defaults(tmp_path: Path):
    from vigil.config import load_config

    repo_cfg = tmp_path / ".vigil" / "config.toml"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text(
        '[llm]\n'
        'provider = "ollama"\n'
        'model = "qwen2:7b"\n'
        'host = "http://10.0.0.5:11434"\n'
        '\n'
        '[embedding]\n'
        'provider = "ollama"\n'
        'model = "mxbai-embed-large"\n'
        'host = "http://10.0.0.5:11434"\n',
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    assert cfg.llm.model == "qwen2:7b"
    assert cfg.llm.options.get("host") == "http://10.0.0.5:11434"
    assert cfg.embedding.model == "mxbai-embed-large"
    assert cfg.config_source in {"repo", "merged"}


def test_env_vars_override_file(tmp_path: Path, monkeypatch):
    from vigil.config import load_config

    repo_cfg = tmp_path / ".vigil" / "config.toml"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text(
        '[llm]\n'
        'provider = "ollama"\n'
        'model = "qwen2:7b"\n'
        '\n'
        '[embedding]\n'
        'provider = "ollama"\n'
        'model = "nomic-embed-text"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("TEAMMATE_LLM_MODEL", "llama3.2:1b")
    monkeypatch.setenv("TEAMMATE_LLM_HOST", "http://override:11434")

    cfg = load_config(tmp_path)
    assert cfg.llm.model == "llama3.2:1b"
    assert cfg.llm.options.get("host") == "http://override:11434"


def test_write_starter_config_round_trips(tmp_path: Path):
    from vigil.config import ProviderConfig, write_starter_config

    llm = ProviderConfig(
        provider="ollama",
        model="llama3.2:3b",
        options={"host": "http://localhost:11434"},
    )
    embedding = ProviderConfig(
        provider="ollama",
        model="nomic-embed-text",
        options={"host": "http://localhost:11434"},
    )
    path = write_starter_config(tmp_path, llm, embedding)
    assert path.exists()

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    assert data["llm"]["provider"] == "ollama"
    assert data["llm"]["model"] == "llama3.2:3b"
    assert data["llm"]["host"] == "http://localhost:11434"
    assert data["embedding"]["model"] == "nomic-embed-text"


def test_write_starter_config_respects_uncommon_options(tmp_path: Path):
    """Options outside the well-known set should still round-trip."""
    from vigil.config import ProviderConfig, write_starter_config

    llm = ProviderConfig(
        provider="anthropic",
        model="claude-haiku-4-5",
        options={"api_key_env": "ANTHROPIC_API_KEY", "timeout_s": 60},
    )
    embedding = ProviderConfig(
        provider="ollama",
        model="nomic-embed-text",
        options={"host": "http://localhost:11434"},
    )
    path = write_starter_config(tmp_path, llm, embedding)
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["llm"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert data["llm"]["timeout_s"] == 60


# ---------- v0.9 InvalidationsConfig ----------


def test_invalidations_defaults(tmp_path: Path):
    from vigil.config import load_config

    cfg = load_config(tmp_path)
    assert cfg.invalidations.enabled is True
    assert cfg.invalidations.show_severity == "high"
    assert cfg.invalidations.recency_window_hours == 168
    assert cfg.invalidations.repo_path is None


def test_invalidations_section_loaded_from_repo_config(tmp_path: Path):
    from vigil.config import load_config

    repo_cfg = tmp_path / ".vigil" / "config.toml"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text(
        '[llm]\nprovider = "ollama"\nmodel = "llama3.2:3b"\n'
        '[embedding]\nprovider = "ollama"\nmodel = "nomic-embed-text"\n'
        '[invalidations]\n'
        'enabled = false\n'
        'show_severity = "medium"\n'
        'recency_window_hours = 48\n'
        'repo_path = "/tmp/brain-invalidations"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.invalidations.enabled is False
    assert cfg.invalidations.show_severity == "medium"
    assert cfg.invalidations.recency_window_hours == 48
    assert cfg.invalidations.repo_path == Path("/tmp/brain-invalidations")


def test_invalidations_clamps_unknown_severity(tmp_path: Path):
    from vigil.config import load_config

    repo_cfg = tmp_path / ".vigil" / "config.toml"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text(
        '[llm]\nprovider = "ollama"\nmodel = "x"\n'
        '[embedding]\nprovider = "ollama"\nmodel = "y"\n'
        '[invalidations]\nshow_severity = "severe"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    # Unknown values must fall back to "high" — never silently raise.
    assert cfg.invalidations.show_severity == "high"


def test_user_config_loaded_when_no_repo_config(tmp_path: Path, monkeypatch):
    """A user-scoped config under HOME should override defaults."""
    from vigil.config import load_config

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Cover Path.home() on Windows-style env too just in case.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    user_cfg = fake_home / ".vigil" / "config.toml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(
        '[llm]\n'
        'provider = "ollama"\n'
        'model = "user-scoped-model"\n'
        '[embedding]\n'
        'provider = "ollama"\n'
        'model = "nomic-embed-text"\n',
        encoding="utf-8",
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    cfg = load_config(repo_root)
    assert cfg.llm.model == "user-scoped-model"
