"""Configuration loader for teammate.

Precedence (highest first):

  1. Environment variables (``TEAMMATE_LLM_*`` / ``TEAMMATE_EMBEDDING_*``)
  2. Per-repo config: ``<brain_root>/.teammate/config.toml``
  3. Per-user config: ``~/.teammate/config.toml``
  4. Built-in defaults (Ollama on localhost:11434)

TOML schema::

    [llm]
    provider = "ollama"
    model    = "llama3.2:3b"
    host     = "http://localhost:11434"

    [embedding]
    provider = "ollama"
    model    = "nomic-embed-text"
    host     = "http://localhost:11434"

We use the stdlib ``tomllib`` for parsing. We hand-roll a tiny serializer for
``write_starter_config`` rather than pulling ``tomli-w`` — four keys per
section, no nested tables, doesn't justify a dependency.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from teammate.rag import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_OLLAMA_HOST,
)

# ---------- dataclasses ----------


@dataclass(frozen=True)
class ProviderConfig:
    """A single provider's identity + transport options."""

    provider: str  # "ollama" | "anthropic" | "openai" | "http" | "none"
    model: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContradictionConfig:
    """Settings for the contradiction detector. See contradiction.py."""

    use_llm_judge: bool = False
    score_floor: float = 0.5
    max_llm_calls: int = 3


@dataclass(frozen=True)
class ConfidenceConfig:
    """Settings for the four confidence guards. See confidence.py."""

    score_threshold: float = 0.5
    action_floors: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class InvalidationsConfig:
    """Settings for the v0.9 event-driven invalidation layer.

    The defaults are deliberately conservative: enabled, but only HIGH
    and above severity surface as a banner. Anything below stays in the
    audit log so noise doesn't drown the actual signal.
    """

    enabled: bool = True
    repo_path: Path | None = None
    show_severity: str = "high"
    recency_window_hours: int = 168  # one week


@dataclass(frozen=True)
class TeammateConfig:
    """Effective config after precedence resolution."""

    llm: ProviderConfig
    embedding: ProviderConfig
    config_source: str  # "default" | "repo" | "user" | "env" | "merged"
    contradiction: ContradictionConfig = field(default_factory=ContradictionConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    invalidations: InvalidationsConfig = field(default_factory=InvalidationsConfig)


# ---------- defaults ----------


def _default_llm() -> ProviderConfig:
    return ProviderConfig(
        provider="ollama",
        model=DEFAULT_LLM_MODEL,
        options={"host": DEFAULT_OLLAMA_HOST},
    )


def _default_embedding() -> ProviderConfig:
    return ProviderConfig(
        provider="ollama",
        model=DEFAULT_EMBEDDING_MODEL,
        options={"host": DEFAULT_OLLAMA_HOST},
    )


# ---------- TOML reading ----------


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


_KNOWN_TRANSPORT_KEYS = {"host", "base_url", "api_key_env", "timeout_s", "dim"}


def _provider_from_section(
    section: dict[str, Any], default: ProviderConfig
) -> ProviderConfig:
    """Fold a TOML section onto a default ProviderConfig."""
    if not section:
        return default
    provider = str(section.get("provider", default.provider))
    model = str(section.get("model", default.model))
    options: dict[str, Any] = dict(default.options)
    for key, val in section.items():
        if key in {"provider", "model"}:
            continue
        # Stash everything else as an option (host, base_url, api_key_env, ...).
        options[key] = val
    return ProviderConfig(provider=provider, model=model, options=options)


def _merge_toml(
    data: dict[str, Any], llm: ProviderConfig, embedding: ProviderConfig
) -> tuple[ProviderConfig, ProviderConfig]:
    llm_section = data.get("llm") or {}
    emb_section = data.get("embedding") or {}
    return (
        _provider_from_section(llm_section, llm),
        _provider_from_section(emb_section, embedding),
    )


# ---------- env overrides ----------


def _apply_env_overrides(
    llm: ProviderConfig, embedding: ProviderConfig
) -> tuple[ProviderConfig, ProviderConfig, bool]:
    """Apply TEAMMATE_* env vars on top of (llm, embedding). Returns (llm, emb, changed)."""
    changed = False

    def _override(cfg: ProviderConfig, prefix: str) -> tuple[ProviderConfig, bool]:
        provider = os.environ.get(f"{prefix}_PROVIDER")
        model = os.environ.get(f"{prefix}_MODEL")
        host = os.environ.get(f"{prefix}_HOST") or os.environ.get(f"{prefix}_BASE_URL")
        api_key_env = os.environ.get(f"{prefix}_API_KEY_ENV")
        if not any((provider, model, host, api_key_env)):
            return cfg, False
        new_options = dict(cfg.options)
        if host:
            new_options["host"] = host
        if api_key_env:
            new_options["api_key_env"] = api_key_env
        return (
            ProviderConfig(
                provider=provider or cfg.provider,
                model=model or cfg.model,
                options=new_options,
            ),
            True,
        )

    new_llm, c1 = _override(llm, "TEAMMATE_LLM")
    new_emb, c2 = _override(embedding, "TEAMMATE_EMBEDDING")
    changed = c1 or c2
    return new_llm, new_emb, changed


# ---------- public API ----------


def _contradiction_from_data(data: dict[str, Any]) -> ContradictionConfig:
    section = data.get("contradiction") or {}
    if not isinstance(section, dict):
        return ContradictionConfig()
    try:
        score_floor = float(section.get("score_floor", 0.5))
    except (TypeError, ValueError):
        score_floor = 0.5
    try:
        max_llm_calls = int(section.get("max_llm_calls", 3))
    except (TypeError, ValueError):
        max_llm_calls = 3
    return ContradictionConfig(
        use_llm_judge=bool(section.get("use_llm_judge", False)),
        score_floor=score_floor,
        max_llm_calls=max_llm_calls,
    )


def _invalidations_from_data(data: dict[str, Any]) -> InvalidationsConfig:
    section = data.get("invalidations") or {}
    if not isinstance(section, dict):
        return InvalidationsConfig()
    enabled = bool(section.get("enabled", True))
    raw_repo = section.get("repo_path") or None
    repo_path: Path | None = Path(str(raw_repo)).expanduser() if raw_repo else None
    show_sev = str(section.get("show_severity", "high")).lower()
    if show_sev not in {"low", "medium", "high", "critical"}:
        show_sev = "high"
    try:
        window = int(section.get("recency_window_hours", 168))
    except (TypeError, ValueError):
        window = 168
    return InvalidationsConfig(
        enabled=enabled,
        repo_path=repo_path,
        show_severity=show_sev,
        recency_window_hours=window,
    )


def _confidence_from_data(data: dict[str, Any]) -> ConfidenceConfig:
    section = data.get("confidence") or {}
    if not isinstance(section, dict):
        return ConfidenceConfig()
    try:
        threshold = float(section.get("score_threshold", 0.5))
    except (TypeError, ValueError):
        threshold = 0.5
    floors_raw = section.get("action_floors") or {}
    floors: dict[str, float] = {}
    if isinstance(floors_raw, dict):
        for k, v in floors_raw.items():
            try:
                floors[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    return ConfidenceConfig(score_threshold=threshold, action_floors=floors)


def load_config(brain_root: Path) -> TeammateConfig:
    """Resolve effective config for the given brain root.

    Reads (in order, last wins):
      1. defaults
      2. ~/.teammate/config.toml
      3. <brain_root>/.teammate/config.toml
      4. env vars
    """
    llm = _default_llm()
    embedding = _default_embedding()
    source = "default"
    contradiction = ContradictionConfig()
    confidence = ConfidenceConfig()
    invalidations = InvalidationsConfig()

    user_path = Path.home() / ".teammate" / "config.toml"
    repo_path = brain_root / ".teammate" / "config.toml"

    user_data = _read_toml(user_path)
    if user_data is not None:
        llm, embedding = _merge_toml(user_data, llm, embedding)
        contradiction = _contradiction_from_data(user_data)
        confidence = _confidence_from_data(user_data)
        invalidations = _invalidations_from_data(user_data)
        source = "user"

    repo_data = _read_toml(repo_path)
    if repo_data is not None:
        llm, embedding = _merge_toml(repo_data, llm, embedding)
        # Repo overrides user for the v0.6 sections too.
        if "contradiction" in repo_data:
            contradiction = _contradiction_from_data(repo_data)
        if "confidence" in repo_data:
            confidence = _confidence_from_data(repo_data)
        if "invalidations" in repo_data:
            invalidations = _invalidations_from_data(repo_data)
        source = "repo" if source == "default" else "merged"

    llm, embedding, env_changed = _apply_env_overrides(llm, embedding)
    if env_changed:
        source = "env" if source == "default" else "merged"

    return TeammateConfig(
        llm=llm,
        embedding=embedding,
        config_source=source,
        contradiction=contradiction,
        confidence=confidence,
        invalidations=invalidations,
    )


# ---------- TOML writing (hand-rolled, four keys max per section) ----------


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return f'"{_toml_escape(str(value))}"'


def _render_section(name: str, cfg: ProviderConfig) -> str:
    lines = [f"[{name}]", f'provider = "{_toml_escape(cfg.provider)}"',
             f'model = "{_toml_escape(cfg.model)}"']
    for key in ("host", "base_url", "api_key_env", "timeout_s", "dim"):
        if key in cfg.options:
            lines.append(f"{key} = {_render_value(cfg.options[key])}")
    # Surface any other options we don't know about — keep config round-trippable.
    for key, val in cfg.options.items():
        if key in _KNOWN_TRANSPORT_KEYS:
            continue
        lines.append(f"{key} = {_render_value(val)}")
    return "\n".join(lines)


def write_starter_config(
    brain_root: Path, llm: ProviderConfig, embedding: ProviderConfig
) -> Path:
    """Write a starter ``.teammate/config.toml`` under ``brain_root``.

    Returns the absolute path written. Caller decides whether to overwrite.
    """
    cfg_dir = brain_root / ".teammate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.toml"
    body = (
        "# teammate config — see docs/PROVIDERS.md\n"
        "# Precedence: env vars > this file > ~/.teammate/config.toml > defaults\n"
        "\n"
        f"{_render_section('llm', llm)}\n"
        "\n"
        f"{_render_section('embedding', embedding)}\n"
        "\n"
        "# Real-time event listener (Slack Socket Mode)\n"
        "# Set SLACK_APP_TOKEN + SLACK_BOT_TOKEN env vars, then: teammate agent listen\n"
        "# See docs/SOCKET-MODE.md\n"
        "[listener]\n"
        "# channels to watch — empty string means all channels\n"
        'slack_channels = ""\n'
        "# Jira/Confluence polling interval in seconds (Slack is real-time via WebSocket)\n"
        "poll_interval = 60\n"
    )
    cfg_path.write_text(body, encoding="utf-8")
    return cfg_path


__all__ = [
    "ConfidenceConfig",
    "ContradictionConfig",
    "InvalidationsConfig",
    "ProviderConfig",
    "TeammateConfig",
    "load_config",
    "write_starter_config",
]
