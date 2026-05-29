"""Tests for `teammate doctor` — diagnostic CLI for corporate adopters."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from click.testing import CliRunner

from vigil.providers.base import EmbeddingProvider, LLMProvider

# ---------- test doubles ----------


class _UpEmbedder(EmbeddingProvider):
    """Always-up fake embedder. Reports a stable model_id and dim."""

    def __init__(self, model: str = "fake-embed", dim: int = 4):
        self._model = model
        self._dim = dim
        self.host = "http://fake-embed.local"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def is_up(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]


class _UpLLM(LLMProvider):
    def __init__(self, model: str = "fake-llm"):
        self._model = model
        self.host = "http://fake-llm.local"

    @property
    def model_id(self) -> str:
        return self._model

    def is_up(self) -> bool:
        return True

    def generate(self, prompt, system=None, *, stream=True) -> Iterator[str]:
        if False:
            yield ""
        return


class _DownLLM(_UpLLM):
    def is_up(self) -> bool:
        return False


# ---------- helpers ----------


def _seed_brain(root: Path) -> None:
    (root / "CLAUDE.md").write_text("# brain\n\nhello world.\n", encoding="utf-8")


def _stamp_index(brain_root: Path, model: str = "nomic-embed-text",
                 dim: int = 768, provider: str = "ollama",
                 chunks: int = 5) -> Path:
    cache = brain_root / ".vigil-cache"
    cache.mkdir(parents=True, exist_ok=True)
    db = cache / "vault.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
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
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    for k, v in {
        "provider": provider,
        "embedding_model": model,
        "embedding_dim": str(dim),
        "created_at": "2026-05-04T00:00:00+00:00",
        "teammate_version": "0.3.1",
    }.items():
        conn.execute(
            "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)", (k, v)
        )
    for i in range(chunks):
        conn.execute(
            "INSERT INTO chunks (path, chunk_idx, text, mtime) VALUES (?, ?, ?, ?)",
            (str(brain_root / "CLAUDE.md"), i, f"chunk {i}", 0.0),
        )
    conn.commit()
    conn.close()
    return db


def _invoke_doctor(brain_root: Path, monkeypatch, *args: str) -> object:
    from vigil.cli import doctor

    monkeypatch.chdir(brain_root)
    runner = CliRunner()
    return runner.invoke(doctor, list(args), catch_exceptions=False)


# ---------- tests ----------


def test_doctor_loads_config_and_reports_source(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    cfg_dir = tmp_path / ".vigil"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        '[llm]\nprovider = "ollama"\nmodel = "qwen2:7b"\n'
        'host = "http://internal.your-team.local:11434"\n'
        '[embedding]\nprovider = "ollama"\nmodel = "nomic-embed-text"\n'
        'host = "http://internal.your-team.local:11434"\n',
        encoding="utf-8",
    )
    result = _invoke_doctor(tmp_path, monkeypatch, "--json")
    payload = json.loads(result.output)
    cfg = next(c for c in payload["checks"] if c["name"] == "config")
    assert cfg["status"] == "PASS"
    assert cfg["details"]["config_source"] in {"repo", "merged"}
    assert cfg["details"]["llm_model"] == "qwen2:7b"


def test_doctor_marks_unreachable_llm_as_fail(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    # Use a known-dead host so OllamaProvider.is_up() returns False fast.
    cfg_dir = tmp_path / ".vigil"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        '[llm]\nprovider = "ollama"\nmodel = "llama3.2:3b"\n'
        'host = "http://127.0.0.1:1"\n'
        '[embedding]\nprovider = "ollama"\nmodel = "nomic-embed-text"\n'
        'host = "http://127.0.0.1:1"\n',
        encoding="utf-8",
    )
    result = _invoke_doctor(tmp_path, monkeypatch, "--json")
    payload = json.loads(result.output)
    llm_check = next(c for c in payload["checks"] if c["name"] == "llm.reachable")
    assert llm_check["status"] == "FAIL"
    assert payload["exit_code"] == 1
    assert result.exit_code == 1


def test_doctor_json_output_is_valid_json(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    result = _invoke_doctor(tmp_path, monkeypatch, "--json")
    payload = json.loads(result.output)
    assert "version" in payload
    assert "exit_code" in payload
    assert "checks" in payload
    assert isinstance(payload["checks"], list)
    # Every entry has the expected shape.
    for c in payload["checks"]:
        assert {"name", "status", "summary", "details"} <= set(c.keys())
        assert c["status"] in {"PASS", "WARN", "FAIL"}


def test_doctor_redacts_proxy_credentials(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    monkeypatch.setenv(
        "HTTPS_PROXY", "http://alice:secretvalue@proxy.your-team.local:3128"
    )
    monkeypatch.setenv("HTTP_PROXY", "http://alice:secretvalue@proxy.your-team.local:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,*.your-team.local")
    result = _invoke_doctor(tmp_path, monkeypatch, "--json")
    payload = json.loads(result.output)
    proxy = next(c for c in payload["checks"] if c["name"] == "proxy")
    rendered = proxy["summary"] + json.dumps(proxy["details"])
    assert "secretvalue" not in rendered
    assert "alice" not in rendered
    assert "***" in rendered


def test_doctor_index_status_when_missing(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    result = _invoke_doctor(tmp_path, monkeypatch, "--json")
    payload = json.loads(result.output)
    idx = next(c for c in payload["checks"] if c["name"] == "index")
    assert idx["status"] == "WARN"
    assert "no index" in idx["summary"].lower()


def test_doctor_index_status_when_stamped_matches(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    _stamp_index(tmp_path, model="nomic-embed-text", dim=768, chunks=7)
    result = _invoke_doctor(tmp_path, monkeypatch, "--json")
    payload = json.loads(result.output)
    idx = next(c for c in payload["checks"] if c["name"] == "index")
    # Default config uses nomic-embed-text @ 768d, so stamp matches.
    assert idx["status"] == "PASS"
    assert idx["details"]["chunks"] == 7
    assert idx["details"]["model"] == "nomic-embed-text"


def test_doctor_brain_warn_outside_repo(tmp_path: Path, monkeypatch):
    # No CLAUDE.md — brain check should WARN.
    result = _invoke_doctor(tmp_path, monkeypatch, "--json")
    payload = json.loads(result.output)
    brain = next(c for c in payload["checks"] if c["name"] == "brain")
    assert brain["status"] == "WARN"


def test_doctor_pretty_output_contains_all_check_names(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    result = _invoke_doctor(tmp_path, monkeypatch)
    out = result.output
    for name in (
        "config", "brain", "llm.reachable", "embedding.reachable",
        "models", "index", "proxy", "runtime",
    ):
        assert name in out, f"missing {name} in pretty output"
