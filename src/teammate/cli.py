"""`teammate` CLI — entry point for the team-brain workflow.

Subcommands:

  teammate scaffold <dir>     — TEAM LEAD: create a new team-brain repo
                                 from the bundled template. One-time per org.
  teammate init               — ENGINEER: set up teammate inside an
                                 already-cloned team-brain repo.
  teammate adopt              — mid-project file migration. Walk an existing
                                 project, classify markdown, fill template
                                 gaps. ``--dry-run`` default; ``--apply`` opt-in.
  teammate validate           — read-only structural check: CLAUDE.md presence
                                 + size, link resolution, orphan files,
                                 non-canonical paths, frontmatter.
  teammate ask "<query>"      — query the brain locally (provider + RAG).
  teammate index [--rebuild]  — rebuild / refresh the local sqlite-vec index.
  teammate stats              — show what's in the brain (file counts by section).
  teammate config show        — print the effective provider config.
  teammate config init        — write a starter `.teammate/config.toml`.
  teammate doctor [--json]    — diagnostic: config source, reachability,
                                 model availability, index, proxy/CA env.
  teammate agent run <name>   — run a colleague-agent routine locally
                                 (mainly invoked by `/schedule` runners).
  teammate agent listen       — open a Slack Socket Mode WebSocket and trigger
                                 K8s Jobs for matching messages in real time.
                                 Requires `pip install claude-teammate[listen]`.
  teammate memory-import      — harvest team-relevant facts from
                                 ``~/.claude/`` memory into a review draft.
                                 Default for every entry is SKIP — opt in
                                 to import. Read-only on ``~/.claude/``.
  teammate memory-export      — departing-engineer flow; dump team-relevant
                                 memory as a handover artifact.
"""

from __future__ import annotations

import json as _json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import click

from teammate import __version__
from teammate.adopt import adopt as run_adopt
from teammate.brain import Brain
from teammate.config import (
    ProviderConfig,
    TeammateConfig,
    load_config,
    write_starter_config,
)
from teammate.init import render_summary
from teammate.init import run as run_init
from teammate.init import scaffold as run_scaffold
from teammate.providers import (
    load_embedding_provider,
    load_llm_provider,
)
from teammate.rag.ask import answer
from teammate.rag.index import (
    IndexVersionMismatch,
    discover_indexable_files,
    index_paths,
)
from teammate.validate import validate as run_validate


@click.group()
@click.version_option(version=__version__, prog_name="teammate")
def main() -> None:
    """teammate — your team's brain in your team's git repo."""


# ---------- scaffold (team lead) ----------


@main.command()
@click.argument("target_dir", type=click.Path(path_type=Path))
@click.option("--team-name", default="TEAM-NAME", show_default=True,
              help="Team name to substitute into the bundled template.")
def scaffold(target_dir: Path, team_name: str) -> None:
    """Create a fresh team-brain repo in TARGET_DIR (must be empty)."""
    result = run_scaffold(target_dir, team_name=team_name)
    click.echo(result["detail"])
    if result["status"] == "failed":
        sys.exit(1)


# ---------- init (engineer) ----------


@main.command()
@click.option("--register-gbrain", is_flag=True,
              help="If gbrain is installed, register this brain as a source.")
@click.option("--install-pre-push", "install_pre_push", is_flag=True,
              help="Copy the bundled v0.9 pre-push hook to .git/hooks/pre-push.")
def init(register_gbrain: bool, install_pre_push: bool) -> None:
    """Set up teammate in this already-cloned team-brain repo."""
    brain_root = Path.cwd()
    results = run_init(
        brain_root,
        register_gbrain=register_gbrain,
        install_pre_push=install_pre_push,
    )
    click.echo(render_summary(results))
    if any(r["status"] == "failed" for r in results.values()):
        sys.exit(1)


# ---------- ask ----------


@main.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--rebuild", is_flag=True, help="Force a full re-index before answering.")
@click.option("--top-k", "top_k", type=int, default=6, show_default=True)
def ask(query: tuple[str, ...], rebuild: bool, top_k: int) -> None:
    """Ask a question about the brain. Streams a local-LLM answer."""
    brain_root = Path.cwd()
    cache_dir = brain_root / ".teammate-cache"
    cfg = load_config(brain_root)
    embedder = load_embedding_provider(cfg.embedding)
    llm = load_llm_provider(cfg.llm)
    paths = discover_indexable_files([brain_root])
    if paths:
        try:
            index_paths(paths, cache_dir, embedder=embedder, rebuild=rebuild)
        except IndexVersionMismatch as exc:
            click.echo(f"Index version mismatch: {exc}", err=True)
            click.echo("Hint: run `teammate index --rebuild`.", err=True)
            sys.exit(2)
    full_query = " ".join(query).strip()
    db_path = cache_dir / "vault.sqlite"
    for chunk in answer(
        full_query,
        db_path,
        brain_root,
        embedder=embedder,
        llm=llm,
        k=top_k,
        cache_dir=cache_dir,
        confidence=cfg.confidence,
        contradiction_cfg=cfg.contradiction,
        invalidations_cfg=cfg.invalidations,
        action="ask",
    ):
        click.echo(chunk, nl=False)
    click.echo("")


# ---------- index ----------


@main.command()
@click.option("--rebuild", is_flag=True, help="Drop the existing index and rebuild from scratch.")
@click.option("--output", "output_path", type=click.Path(path_type=Path),
              help="Write the index file to a custom path (default: .teammate-cache/vault.sqlite).")
def index(rebuild: bool, output_path: Path | None) -> None:
    """Build / refresh the local sqlite-vec index of the brain."""
    brain_root = Path.cwd()
    cache_dir = brain_root / ".teammate-cache"
    if output_path:
        cache_dir = output_path.parent if output_path.suffix else output_path
        cache_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config(brain_root)
    embedder = load_embedding_provider(cfg.embedding)
    paths = discover_indexable_files([brain_root])
    if not paths:
        click.echo("No markdown found in this directory. Are you in a team-brain repo?", err=True)
        sys.exit(1)
    try:
        indexed, skipped = index_paths(
            paths, cache_dir, embedder=embedder, rebuild=rebuild
        )
    except IndexVersionMismatch as exc:
        click.echo(f"Index version mismatch: {exc}", err=True)
        click.echo("Hint: run `teammate index --rebuild`.", err=True)
        sys.exit(2)
    click.echo(f"Indexed {indexed} files ({skipped} unchanged). Cache: {cache_dir}/vault.sqlite")


# ---------- stats ----------


@main.command()
def stats() -> None:
    """Show what's in the brain (file counts by section)."""
    brain = Brain(Path.cwd())
    if not brain.exists():
        click.echo("No CLAUDE.md found here. Are you in a team-brain repo?", err=True)
        sys.exit(1)
    s = brain.stats()
    click.echo(f"Brain at {brain.root}")
    click.echo(f"  Total markdown files: {s['total']}")
    click.echo(f"    CLAUDE.md          {s['claude']}")
    click.echo(f"    skills/            {s['skills']}")
    click.echo(f"    rules/             {s['rules']}")
    click.echo(f"    docs/              {s['docs']}")
    click.echo(f"    knowledge/         {s['knowledge']}")
    click.echo(f"    other              {s['other']}")


# ---------- config ----------


def _redact(options: dict) -> dict:
    """Redact api_key-ish values for safe display."""
    out = {}
    for k, v in options.items():
        if "api_key" in k.lower() and not k.lower().endswith("_env"):
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out


def _render_provider_section(name: str, cfg: ProviderConfig) -> str:
    lines = [f"[{name}]", f'  provider = "{cfg.provider}"',
             f'  model    = "{cfg.model}"']
    for k, v in _redact(cfg.options).items():
        lines.append(f"  {k} = {v!r}")
    return "\n".join(lines)


@main.group()
def config() -> None:
    """Inspect and manage provider configuration."""


@config.command("show")
def config_show() -> None:
    """Print the effective provider config (env > repo > user > defaults)."""
    brain_root = Path.cwd()
    cfg: TeammateConfig = load_config(brain_root)
    click.echo(f"# config_source: {cfg.config_source}")
    click.echo(_render_provider_section("llm", cfg.llm))
    click.echo("")
    click.echo(_render_provider_section("embedding", cfg.embedding))


@config.command("init")
@click.option(
    "--provider",
    type=click.Choice(["ollama", "anthropic", "openai", "http", "none"]),
    default="ollama",
    show_default=True,
    help="Which provider to scaffold the starter config for.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing config.toml.")
def config_init(provider: str, force: bool) -> None:
    """Write a starter ``.teammate/config.toml`` for the given provider."""
    brain_root = Path.cwd()
    target = brain_root / ".teammate" / "config.toml"
    if target.exists() and not force:
        click.echo(f"Config already exists at {target}. Use --force to overwrite.", err=True)
        sys.exit(1)

    if provider == "ollama":
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
    elif provider == "none":
        llm = ProviderConfig(provider="none", model="", options={})
        embedding = ProviderConfig(provider="none", model="", options={})
    else:
        # Placeholder for v0.4 providers — write a stub so users can fill it in.
        # The provider registry will return None for these in v0.3 (keyword-only).
        llm = ProviderConfig(
            provider=provider,
            model="<set-me>",
            options={"api_key_env": f"{provider.upper()}_API_KEY"},
        )
        embedding = ProviderConfig(
            provider=provider,
            model="<set-me>",
            options={"api_key_env": f"{provider.upper()}_API_KEY"},
        )

    path = write_starter_config(brain_root, llm, embedding)
    click.echo(f"Wrote starter config to {path}")
    if provider not in {"ollama", "none"}:
        click.echo(
            f"Note: the `{provider}` provider is not yet shipped in v0.3 — "
            f"teammate will fall back to keyword-only retrieval until v0.4.",
            err=True,
        )


# ---------- adopt ----------


@main.command()
@click.option("--apply", "do_apply", is_flag=True,
              help="Actually copy template gap files. Without it, runs as a dry-run.")
@click.option("--dry-run", "force_dry_run", is_flag=True,
              help="Force dry-run mode (default). Cannot be combined with --apply.")
@click.option("--include", "includes", multiple=True,
              help="Extra paths to include (repeat for multiple). Extends defaults.")
@click.option("--exclude", "excludes", multiple=True,
              help="Extra paths to exclude (repeat for multiple). Extends defaults.")
@click.option("--max-claude-md-kb", type=int, default=4, show_default=True,
              help="CLAUDE.md size budget. Larger files trigger a split suggestion.")
@click.option("--output", "output_path", type=click.Path(path_type=Path),
              default=Path("MIGRATION-PLAN.md"), show_default=True,
              help="Where to write the human-readable plan.")
def adopt(
    do_apply: bool,
    force_dry_run: bool,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
    max_claude_md_kb: int,
    output_path: Path,
) -> None:
    """Walk this project and classify markdown into a team-brain layout.

    Default is dry-run: no files are touched. Pass ``--apply`` to copy
    template gap files into place. Existing content is never moved or
    merged automatically — move suggestions are surfaced for human action.
    """
    if do_apply and force_dry_run:
        click.echo("Cannot combine --apply with --dry-run.", err=True)
        sys.exit(1)
    brain_root = Path.cwd()
    try:
        plan = run_adopt(
            brain_root,
            dry_run=not do_apply,
            apply=do_apply,
            include=list(includes),
            exclude=list(excludes),
            max_claude_md_kb=max_claude_md_kb,
        )
    except RuntimeError as exc:
        click.echo(f"adopt: {exc}", err=True)
        sys.exit(1)
    md = plan.to_markdown()
    try:
        output_path.write_text(md, encoding="utf-8")
    except OSError as exc:
        click.echo(f"failed to write plan: {exc}", err=True)
        sys.exit(1)
    mode = "APPLY" if do_apply else "DRY-RUN"
    click.echo(f"teammate adopt — {mode} — wrote plan to {output_path}")
    click.echo(
        f"  KEEP={len(plan.by_action('KEEP'))}  ADD={len(plan.by_action('ADD'))}  "
        f"MOVE_SUGGESTED={len(plan.by_action('MOVE_SUGGESTED'))}  "
        f"REVIEW={len(plan.by_action('REVIEW'))}  "
        f"SKIP_PER_ENGINEER={len(plan.by_action('SKIP_PER_ENGINEER'))}"
    )
    if do_apply:
        click.echo(f"  MIGRATION.md written at {brain_root / 'MIGRATION.md'}")


# ---------- validate ----------


@main.command()
@click.option("--json", "as_json", is_flag=True,
              help="Emit a machine-readable JSON report (no ANSI).")
@click.option("--max-claude-md-kb", type=int, default=4, show_default=True,
              help="Soft size budget for CLAUDE.md (WARN if exceeded).")
@click.option("--include-naming", is_flag=True, default=False,
              help="Run the naming-convention check against directory names "
                   "under docs/, knowledge/, and .claude/skills/. Reads "
                   ".teammate-naming.toml from the brain root.")
def validate(as_json: bool, max_claude_md_kb: int, include_naming: bool) -> None:
    """Read-only structural check of the brain.

    Exit codes: 0 on all-PASS, 1 on any FAIL, 2 on only-WARN.
    """
    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    # The flag opts in. If absent, validate() falls back to the
    # `[validate] include_naming` setting in .teammate/config.toml.
    include_flag: bool | None = True if include_naming else None
    report = run_validate(
        brain_root,
        max_claude_md_kb=max_claude_md_kb,
        include_naming=include_flag,
    )
    if as_json:
        click.echo(report.to_json())
    else:
        from rich.console import Console
        from rich.text import Text

        console = Console()
        console.print(f"[bold]teammate validate v{__version__}[/bold]\n")
        style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
        for c in report.checks:
            tag = Text(f"[{c.status}]", style=style.get(c.status, "white"))
            line = Text.assemble(
                tag, " ", Text(f"{c.name:<30}", style="bold"), Text(c.summary)
            )
            console.print(line)
        if report.overall == "PASS":
            console.print("\n[bold green]OK[/bold green]")
        elif report.overall == "WARN":
            console.print(
                "\n[bold yellow]WARN[/bold yellow] — verify these are intentional."
            )
        else:
            console.print(
                "\n[bold red]FAIL[/bold red] — at least one critical check failed."
            )
    sys.exit(report.exit_code)


# ---------- doctor ----------


# Statuses, ordered by severity. The aggregate exit code is driven by the
# worst status seen across all checks.
_PASS = "PASS"
_WARN = "WARN"
_FAIL = "FAIL"

# user:pass@host shape inside an http(s) URL. Captures the scheme so we can
# preserve it; everything between scheme and `@` is the credential pair.
_PROXY_CREDS_RE = re.compile(r"(https?://)[^:/@]+:[^@]+@")


def _redact_proxy_url(value: str) -> str:
    """Strip ``user:pass`` from any http(s) URL embedded in ``value``.

    Applied to ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``NO_PROXY``. ``NO_PROXY``
    won't carry creds in practice, but uniform handling is cheaper than
    asymmetry — and the regex is a no-op on credential-free strings.
    """
    if not value:
        return value
    return _PROXY_CREDS_RE.sub(r"\1***:***@", value)


def _check_result(
    name: str, status: str, summary: str, **details: Any
) -> dict[str, Any]:
    return {"name": name, "status": status, "summary": summary, "details": details}


def _safe_check(name: str, fn) -> dict[str, Any]:
    """Run a check function, converting any uncaught exception into a FAIL.

    Each check is responsible for returning a dict with ``status`` /
    ``summary`` / ``details``. If it raises, we still produce a structured
    record so JSON output stays well-formed.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — diagnostic surface, never raise
        return _check_result(name, _FAIL, f"check raised: {exc.__class__.__name__}: {exc}")


def _check_config(brain_root: Path) -> dict[str, Any]:
    cfg = load_config(brain_root)
    return _check_result(
        "config",
        _PASS,
        f"source={cfg.config_source}  llm={cfg.llm.provider}:{cfg.llm.model}  "
        f"embedding={cfg.embedding.provider}:{cfg.embedding.model}",
        config_source=cfg.config_source,
        llm_provider=cfg.llm.provider,
        llm_model=cfg.llm.model,
        embedding_provider=cfg.embedding.provider,
        embedding_model=cfg.embedding.model,
    )


def _check_brain(brain_root: Path) -> dict[str, Any]:
    brain = Brain(brain_root)
    if brain.exists():
        return _check_result(
            "brain", _PASS, f"CLAUDE.md present at {brain_root}",
            brain_root=str(brain_root),
        )
    return _check_result(
        "brain",
        _WARN,
        f"no CLAUDE.md at {brain_root} (running outside a brain repo?)",
        brain_root=str(brain_root),
    )


def _measure_reachability(provider) -> tuple[bool, float | None, str]:
    """Run ``is_up()`` with a wall-clock timer. Returns (up, latency_ms, host)."""
    host = getattr(provider, "host", "") or ""
    start = time.perf_counter()
    up = provider.is_up()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return bool(up), elapsed_ms, host


def _check_provider_reachable(label: str, provider) -> dict[str, Any]:
    if provider is None:
        return _check_result(
            label, _WARN, "provider disabled (none) — fallback to keyword search",
            host=None, latency_ms=None,
        )
    up, latency_ms, host = _measure_reachability(provider)
    status = _PASS if up else _FAIL
    summary = (
        f"{host}  {latency_ms:.0f} ms" if up else f"{host}  unreachable ({latency_ms:.0f} ms)"
    )
    return _check_result(
        label, status, summary,
        host=host, latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
        up=up,
    )


def _check_models(cfg: TeammateConfig, llm, embedder) -> dict[str, Any]:
    """Only meaningful for Ollama (the one provider with `list_models`).

    The ABCs don't define `list_models` — we duck-check it. For non-Ollama
    providers (none, or future v0.4 backends) we return WARN with a note.
    """
    candidates = [p for p in (llm, embedder) if p is not None]
    ollama_like = [p for p in candidates if hasattr(p, "list_models")]
    if not ollama_like:
        return _check_result(
            "models", _WARN,
            "skipped — neither provider exposes list_models()",
            available=None, missing=None,
        )
    # All Ollama-like providers in v0.3 share a host; query whichever we have.
    probe = ollama_like[0]
    try:
        available = probe.list_models()
    except Exception as exc:  # noqa: BLE001
        return _check_result(
            "models", _WARN,
            f"could not list models from {getattr(probe, 'host', '?')}: {exc}",
            available=None, missing=None,
        )
    wanted = {cfg.llm.model, cfg.embedding.model} - {""}
    missing = sorted(w for w in wanted if w and w not in available)
    if not missing:
        return _check_result(
            "models", _PASS,
            f"{', '.join(sorted(wanted))} all pulled",
            available=available, missing=[],
        )
    return _check_result(
        "models", _WARN,
        f"missing on the mirror: {', '.join(missing)} — pull them on the host",
        available=available, missing=missing,
    )


def _check_index(brain_root: Path, cfg: TeammateConfig, embedder) -> dict[str, Any]:
    """Read ``index_meta`` directly. Don't use ``open_index(embedder=...)`` —
    that would *raise* ``IndexVersionMismatch`` and abort the report. We
    want the mismatch as a soft WARN here, not a fatal exception.
    """
    db_path = brain_root / ".teammate-cache" / "vault.sqlite"
    if not db_path.exists():
        return _check_result(
            "index", _WARN,
            "no index yet — run `teammate index` to build it",
            db_path=str(db_path), exists=False,
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            meta = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
            try:
                chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            except sqlite3.OperationalError:
                chunk_count = 0
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return _check_result(
            "index", _FAIL, f"corrupt sqlite at {db_path}: {exc}",
            db_path=str(db_path),
        )

    stored_provider = meta.get("provider", "")
    stored_model = meta.get("embedding_model", "")
    stored_dim = meta.get("embedding_dim", "")
    stored_version = meta.get("teammate_version", "")
    stored_created = meta.get("created_at", "")

    # Compare against the configured embedder if present.
    if embedder is not None:
        cfg_model = embedder.model_id
        cfg_dim = str(embedder.dim)
        if (stored_model, stored_dim) != (cfg_model, cfg_dim):
            return _check_result(
                "index", _WARN,
                f"stamp mismatch: stored=({stored_model}, {stored_dim}d) "
                f"current=({cfg_model}, {cfg_dim}d) — run `teammate index --rebuild`",
                provider=stored_provider, model=stored_model, dim=stored_dim,
                chunks=chunk_count, teammate_version=stored_version,
                created_at=stored_created,
            )

    return _check_result(
        "index", _PASS,
        f"provider={stored_provider} model={stored_model} dim={stored_dim} "
        f"chunks={chunk_count}",
        provider=stored_provider, model=stored_model, dim=stored_dim,
        chunks=chunk_count, teammate_version=stored_version,
        created_at=stored_created,
    )


_PROXY_ENV_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTPX_VERIFY",
)


def _check_proxy_env() -> dict[str, Any]:
    """Print effective proxy/CA env. Redact creds. Always PASS — informational.

    Reads both upper and lower-case forms (httpx accepts either); the
    upper-case wins by convention.
    """
    seen: dict[str, str] = {}
    for var in _PROXY_ENV_VARS:
        raw = os.environ.get(var) or os.environ.get(var.lower())
        if raw:
            seen[var] = _redact_proxy_url(raw) if "PROXY" in var else raw
    if not seen:
        return _check_result(
            "proxy", _PASS, "no proxy / CA env detected",
            env={},
        )
    pieces = [f"{k}={v}" for k, v in seen.items()]
    return _check_result(
        "proxy", _PASS, "  ".join(pieces),
        env=seen,
    )


def _check_runtime() -> dict[str, Any]:
    py = ".".join(str(x) for x in sys.version_info[:3])
    return _check_result(
        "runtime", _PASS,
        f"python={py}  teammate={__version__}",
        python=py, teammate=__version__,
    )


def _aggregate_exit_code(checks: list[dict[str, Any]]) -> int:
    statuses = {c["status"] for c in checks}
    if _FAIL in statuses:
        return 1
    if _WARN in statuses:
        return 2
    return 0


def _render_report(checks: list[dict[str, Any]]) -> None:
    """Pretty-print to stdout via rich, one row per check. No JSON here."""
    from rich.console import Console
    from rich.text import Text

    console = Console()
    console.print(f"[bold]teammate doctor v{__version__}[/bold]\n")
    style = {_PASS: "green", _WARN: "yellow", _FAIL: "red"}
    for c in checks:
        tag = Text(f"[{c['status']}]", style=style.get(c["status"], "white"))
        line = Text.assemble(tag, " ", Text(f"{c['name']:<22}", style="bold"),
                             Text(c["summary"]))
        console.print(line)
    overall = _aggregate_exit_code(checks)
    if overall == 0:
        console.print("\n[bold green]OK[/bold green]")
    elif overall == 2:
        console.print("\n[bold yellow]WARN[/bold yellow] — verify these are intentional.")
    else:
        console.print("\n[bold red]FAIL[/bold red] — at least one critical check failed.")


def _build_report(brain_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run all checks, return (ordered_results, aggregate_dict)."""
    checks: list[dict[str, Any]] = []
    # Load config once — every other check depends on it.
    cfg_check = _safe_check("config", lambda: _check_config(brain_root))
    checks.append(cfg_check)
    try:
        cfg = load_config(brain_root)
    except Exception:  # noqa: BLE001
        cfg = None  # type: ignore[assignment]

    checks.append(_safe_check("brain", lambda: _check_brain(brain_root)))

    llm = embedder = None
    if cfg is not None:
        try:
            llm = load_llm_provider(cfg.llm)
        except Exception:  # noqa: BLE001
            llm = None
        try:
            embedder = load_embedding_provider(cfg.embedding)
        except Exception:  # noqa: BLE001
            embedder = None

    checks.append(_safe_check(
        "llm.reachable", lambda: _check_provider_reachable("llm.reachable", llm),
    ))
    checks.append(_safe_check(
        "embedding.reachable",
        lambda: _check_provider_reachable("embedding.reachable", embedder),
    ))
    if cfg is not None:
        checks.append(_safe_check(
            "models", lambda: _check_models(cfg, llm, embedder),
        ))
        checks.append(_safe_check(
            "index", lambda: _check_index(brain_root, cfg, embedder),
        ))
    checks.append(_safe_check("proxy", _check_proxy_env))
    checks.append(_safe_check("runtime", _check_runtime))

    aggregate = {
        "version": __version__,
        "brain_root": str(brain_root),
        "exit_code": _aggregate_exit_code(checks),
        "checks": checks,
    }
    return checks, aggregate


@main.command()
@click.option("--json", "as_json", is_flag=True,
              help="Emit a machine-readable JSON report (no ANSI).")
def doctor(as_json: bool) -> None:
    """Diagnostic — config, reachability, models, index, proxy/CA env.

    Returns exit 0 (all PASS), 1 (any FAIL), or 2 (only WARNs).
    """
    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    checks, aggregate = _build_report(brain_root)
    if as_json:
        # Pure JSON — no rich, no ANSI. The smoke test pipes us into
        # `python -m json.tool`, which fails on stray escape sequences.
        click.echo(_json.dumps(aggregate, indent=2, sort_keys=True, default=str))
    else:
        _render_report(checks)
    sys.exit(_aggregate_exit_code(checks))


# ---------- naming (v0.7) ----------


_NAMING_FILENAME = ".teammate-naming.toml"


@main.group()
def naming() -> None:
    """Validate repo / service names against the team's convention."""


def _resolve_naming_config(brain_root: Path) -> Path | None:
    """Locate ``.teammate-naming.toml`` for the active brain root."""
    from teammate.naming import find_naming_config

    return find_naming_config(brain_root)


@naming.command("check")
@click.argument("name", type=str)
def naming_check(name: str) -> None:
    """Validate a single repo name. Pass ``-`` to read names from stdin.

    Exit codes match the reference shell validator:
      0 — every input passed (or was an exempted exception)
      1 — at least one input failed validation
      2 — usage error
    """
    from teammate.naming import (
        Verdict,
        load_naming_convention,
        validate_name,
    )

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    cfg_path = _resolve_naming_config(brain_root)
    if cfg_path is None:
        click.echo(
            f"naming: no {_NAMING_FILENAME} found in {brain_root}. "
            f"Run `teammate naming init` to write a starter.",
            err=True,
        )
        sys.exit(2)
    convention = load_naming_convention(cfg_path)
    if convention is None:
        click.echo(f"naming: could not parse {cfg_path}", err=True)
        sys.exit(2)

    def _emit(result) -> int:
        verdict = result.verdict
        if verdict is Verdict.OK:
            click.echo(f"OK   {result.name}")
            return 0
        if verdict is Verdict.WARN:
            click.echo(f"WARN {result.name}  — {result.reason}", err=True)
            return 0
        click.echo(f"FAIL {result.name}  — {result.reason}", err=True)
        return 1

    rc = 0
    if name == "-":
        for line in sys.stdin:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            res = validate_name(stripped, convention)
            rc |= _emit(res)
        sys.exit(rc)
    res = validate_name(name, convention)
    sys.exit(_emit(res))


@naming.command("list")
def naming_list() -> None:
    """Print the effective naming convention (config source, vocabularies)."""
    from rich.console import Console
    from rich.table import Table

    from teammate.naming import load_naming_convention

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    cfg_path = _resolve_naming_config(brain_root)
    if cfg_path is None:
        click.echo(
            f"naming: no {_NAMING_FILENAME} found in {brain_root}.",
            err=True,
        )
        sys.exit(1)
    convention = load_naming_convention(cfg_path)
    if convention is None:
        click.echo(f"naming: could not parse {cfg_path}", err=True)
        sys.exit(1)

    console = Console()
    console.print(f"[bold]naming convention[/bold]  source: {convention.source}")
    console.print(f"  locale: {convention.language}")
    console.print(
        f"  tokens: {convention.min_tokens}..{convention.max_tokens}  "
        f"max_length: {convention.max_length}"
    )
    table = Table(title=None, show_header=True, header_style="bold")
    table.add_column("slot", style="cyan", no_wrap=True)
    table.add_column("strict")
    table.add_column("values")
    for slot, spec in (
        ("prefix", convention.prefix),
        ("category", convention.category),
        ("domain", convention.domain),
        ("service", convention.service),
        ("type", convention.type),
    ):
        values = ", ".join(spec.values) if spec.values else "(none)"
        table.add_row(slot, str(spec.strict).lower(), values)
    console.print(table)
    if convention.submodule_recommend:
        console.print(
            "  submodule.recommend: "
            f"{', '.join(convention.submodule_recommend)}"
        )
    if convention.submodule_forbid_duplicating:
        console.print(
            "  submodule.forbid_duplicating: "
            f"{', '.join(convention.submodule_forbid_duplicating)}"
        )
    if convention.exceptions:
        console.print(
            f"  exceptions: {', '.join(convention.exceptions)}"
        )
    else:
        console.print("  exceptions: (none)")


@naming.command("init")
@click.option(
    "--template",
    type=str,
    default="nexus-style",
    show_default=True,
    help="Starter template. One of: nexus-style, small-team, monorepo-only, strict-iac.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
def naming_init(template: str, force: bool) -> None:
    """Write a starter ``.teammate-naming.toml`` to the current directory."""
    from teammate.naming import list_templates, write_starter

    if template not in list_templates():
        click.echo(
            f"unknown template: {template!r}. "
            f"Known: {', '.join(list_templates())}",
            err=True,
        )
        sys.exit(2)
    target = Path.cwd() / _NAMING_FILENAME
    if target.exists() and not force:
        click.echo(
            f"{target} already exists. Use --force to overwrite.",
            err=True,
        )
        sys.exit(1)
    try:
        written = write_starter(target, template, force=force)
    except OSError as exc:
        click.echo(f"failed to write {target}: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Wrote starter naming config to {written}")
    click.echo(
        f"  template: {template}.  Edit the [token.*] sections, then "
        f"run `teammate naming check <name>` to test."
    )


# ---------- agent ----------


@main.group()
def agent() -> None:
    """Colleague-agent routines (judgment work, not CI shape checks)."""


@agent.command("run")
@click.argument("name", type=str)
@click.option("--out-dir", "out_dir", type=click.Path(path_type=Path),
              default=None,
              help="Where the routine drops its report. Default: <brain>/.teammate-agent/.")
@click.option("--dry-run/--no-dry-run", default=True, show_default=True,
              help="Routine still writes its file; --no-dry-run lets the runner "
                   "take side effects on top.")
@click.option("--pr-number", type=int, default=0,
              help="For pr_migration_plan — PR number to label the output.")
@click.option("--pr-files", "pr_files", multiple=True,
              help="For pr_migration_plan — repeat for each path in the PR diff.")
def agent_run(
    name: str,
    out_dir: Path | None,
    dry_run: bool,
    pr_number: int,
    pr_files: tuple[str, ...],
) -> None:
    """Run colleague-agent routine NAME (e.g. weekly_digest)."""
    from teammate.agent.base import RoutineConfig
    from teammate.agent.runner import list_routines, run_routine

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    if name not in list_routines():
        click.echo(
            f"unknown routine: {name!r}. Known: {', '.join(list_routines())}",
            err=True,
        )
        sys.exit(2)
    target_dir = out_dir if out_dir is not None else brain_root / ".teammate-agent"
    extra: dict[str, Any] = {}
    if name == "pr_migration_plan":
        extra = {"pr_number": pr_number, "pr_files": list(pr_files)}
    cfg = RoutineConfig(
        brain_root=brain_root,
        out_dir=Path(target_dir),
        dry_run=dry_run,
        extra=extra,
    )
    try:
        result = run_routine(name, cfg)
    except KeyError as exc:
        click.echo(f"agent: {exc}", err=True)
        sys.exit(2)
    click.echo(f"[{result.status}] {result.name} — {result.summary}")
    for art in result.artifacts:
        click.echo(f"  artifact: {art}")
    if result.status == "fail":
        sys.exit(1)


@agent.command("listen")
@click.option("--poll-interval", "poll_interval", type=int, default=60, show_default=True,
              help="Jira/Confluence polling interval in seconds. "
                   "Slack events arrive in real time via WebSocket.")
@click.option("--fail-on-disconnect/--no-fail-on-disconnect", default=True, show_default=True,
              help="Exit non-zero when the socket cannot reconnect "
                   "(pod restarts, which reconnects). Disable for local testing.")
def agent_listen(poll_interval: int, fail_on_disconnect: bool) -> None:
    """Open a Slack Socket Mode WebSocket and listen for real-time events.

    Triggers K8s Jobs for matching teammate routines on Slack message, Jira
    issue, or Confluence page changes. Designed to run as a single-replica
    Kubernetes Deployment with a liveness probe on /tmp/teammate-heartbeat.

    \b
    Required env:
      SLACK_APP_TOKEN   xapp-... (Socket Mode > App-Level Token, connections:write)
      SLACK_BOT_TOKEN   xoxb-... (Bot Token)

    \b
    Optional env:
      TEAMMATE_SLACK_CHANNELS   comma-separated channel names (default: all)
      ATLASSIAN_API_TOKEN       enables Jira/Confluence polling
      JIRA_BASE_URL             https://your-org.atlassian.net
      CONFLUENCE_BASE_URL       https://your-org.atlassian.net/wiki
      JIRA_WATCHER_JQL          JQL filter for jira_sync triggers
      CONFLUENCE_WATCHER_SPACES comma-separated Confluence space keys

    See docs/SOCKET-MODE.md for full setup.
    """
    from teammate.socket_listener import run as socket_run
    exit_code = socket_run(poll_interval=poll_interval, fail_on_disconnect=fail_on_disconnect)
    sys.exit(exit_code)


# ---------- memory-import ----------


@main.command("memory-import")
@click.option("--memory-root", "memory_root", type=click.Path(path_type=Path),
              default=None,
              help="Path to the user's `~/.claude/` memory dir. Default: discover via env.")
@click.option("--user", "user_name", default=None,
              help="Label for the draft filename. Default: $USER.")
@click.option("--interactive/--non-interactive", default=False, show_default=True,
              help="Reserved — v0.5 always writes a non-interactive draft you "
                   "edit by hand. Both modes write the same draft.")
def memory_import(
    memory_root: Path | None,
    user_name: str | None,
    interactive: bool,
) -> None:
    """Stage a memory-import draft from ``~/.claude/`` for human review.

    Reversed safety bias: every entry defaults to SKIP. The CLI never
    auto-imports — the user opts in per entry by checking the box on the
    generated draft file. ``~/.claude/`` is read-only.
    """
    from teammate.memory_import import harvest_user_memory, write_plan

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    if memory_root is None:
        # Default: assume the user has ~/.claude/. We don't try to walk
        # the project-id subdirs in v0.5 — the user passes the right
        # --memory-root if they have multiple.
        memory_root = Path.home() / ".claude"
    if not memory_root.exists():
        click.echo(
            f"memory-import: memory root not found at {memory_root}. "
            f"Pass --memory-root to point at your `~/.claude/` directory.",
            err=True,
        )
        sys.exit(1)
    user_label = user_name or os.environ.get("USER") or "user"

    plan = harvest_user_memory(
        memory_root=memory_root,
        brain_root=brain_root,
        user=user_label,
    )
    out_path = write_plan(plan)
    click.echo(f"memory-import: wrote draft to {out_path}")
    click.echo(
        f"  entries surfaced: {len(plan.entries)}  "
        f"(every box is unchecked — opt in per entry to import)"
    )


# ---------- memory-export ----------


@main.command("memory-export")
@click.option("--memory-root", "memory_root", type=click.Path(path_type=Path),
              default=None,
              help="Path to the user's `~/.claude/` memory dir. Default: ~/.claude.")
@click.option("--out-dir", "out_dir", type=click.Path(path_type=Path),
              default=None,
              help="Where to drop the handover. Default: cwd.")
@click.option("--user", "user_name", default=None,
              help="Label for the handover filename. Default: $USER.")
@click.option("--since", "since", default=None,
              help="Filter: keep entries with year stamp >= YYYY (and all unstamped).")
@click.option("--no-redact", is_flag=True,
              help="Skip the redaction pass. Internal hostnames + emails stay verbatim.")
def memory_export(
    memory_root: Path | None,
    out_dir: Path | None,
    user_name: str | None,
    since: str | None,
    no_redact: bool,
) -> None:
    """Produce a departing-engineer handover from ``~/.claude/`` memory.

    Includes TEAM_RULE / TEAM_FACT / REFERENCE entries by default;
    PERSONAL entries are excluded. ``--no-redact`` keeps the original
    text; the default pass replaces internal-hostname / email matches
    with generic placeholders.
    """
    from teammate.memory_export import export_for_handover, write_handover

    if memory_root is None:
        memory_root = Path.home() / ".claude"
    if not memory_root.exists():
        click.echo(
            f"memory-export: memory root not found at {memory_root}. "
            f"Pass --memory-root to point at your `~/.claude/` directory.",
            err=True,
        )
        sys.exit(1)
    user_label = user_name or os.environ.get("USER") or "user"
    target_dir = out_dir if out_dir is not None else Path.cwd()

    plan = export_for_handover(
        memory_root=memory_root,
        user=user_label,
        since=since,
        redact=not no_redact,
    )
    out_path = write_handover(plan, target_dir)
    click.echo(f"memory-export: wrote handover to {out_path}")
    click.echo(f"  entries: {len(plan.entries)}  redacted: {plan.redact}")


# ---------- adapter (v0.6) ----------


@main.group()
def adapter() -> None:
    """Per-engineer translation between personal and team-brain layouts."""


@adapter.command("show")
def adapter_show() -> None:
    """Print the effective adapter config. ``no adapter configured`` if absent."""
    from teammate.adapter import ADAPTER_FILENAME, load_adapter

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    adapter_obj = load_adapter(brain_root)
    if adapter_obj is None:
        home_path = Path.home() / ADAPTER_FILENAME
        brain_path = brain_root / ADAPTER_FILENAME
        click.echo("no adapter configured")
        click.echo(f"  searched: {home_path}")
        click.echo(f"  searched: {brain_path}")
        click.echo("Run `teammate adapter init` to write a starter file.")
        return
    click.echo(f"# adapter source: {adapter_obj.source}")
    click.echo("[paths]")
    if not adapter_obj.paths:
        click.echo("# (no path rules)")
    else:
        for personal, canonical in adapter_obj.paths.items():
            click.echo(f'"{personal}" = "{canonical}"')
    click.echo("")
    click.echo("[claude_md]")
    overrides = adapter_obj.personal_override_sections
    if not overrides:
        click.echo("personal_overrides_team = []")
    else:
        rendered = ", ".join(f'"{s}"' for s in overrides)
        click.echo(f"personal_overrides_team = [{rendered}]")


@adapter.command("init")
@click.option("--scope", type=click.Choice(["home", "brain"]), default="home",
              show_default=True,
              help="Where to write the file. ``home`` = ~/.teammate-adapter.toml "
                   "(per-engineer). ``brain`` = <brain-root>/.teammate-adapter.toml "
                   "(team-shipped fallback).")
@click.option("--force", is_flag=True, help="Overwrite if a file already exists.")
def adapter_init(scope: str, force: bool) -> None:
    """Write a starter ``.teammate-adapter.toml`` based on detected layouts."""
    from teammate.adapter import ADAPTER_FILENAME, write_starter_adapter

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    target = (
        Path.home() / ADAPTER_FILENAME
        if scope == "home"
        else brain_root / ADAPTER_FILENAME
    )
    if target.exists() and not force:
        click.echo(f"adapter file already exists at {target}. Use --force to overwrite.",
                   err=True)
        sys.exit(1)
    written = write_starter_adapter(target)
    click.echo(f"wrote starter adapter to {written}")
    click.echo("Edit it to add your personal-to-canonical path rules. "
               "See docs/ADAPTER.md.")


@adapter.command("validate")
def adapter_validate() -> None:
    """Check that every ``[paths]`` rule still matches real files."""
    from teammate.adapter import load_adapter, validate_adapter

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    adapter_obj = load_adapter(brain_root)
    if adapter_obj is None:
        click.echo("no adapter configured — nothing to validate")
        return
    warnings = validate_adapter(adapter_obj)
    if not warnings:
        click.echo(f"adapter OK ({len(adapter_obj.paths)} path rule(s))")
        return
    click.echo(f"adapter has {len(warnings)} warning(s):", err=True)
    for w in warnings:
        click.echo(f"  - {w}", err=True)
    sys.exit(2)


# ---------- audit (v0.6) ----------


@main.command()
@click.option("--since", "since", default=None,
              help="ISO date or datetime. Filter records with ts >= this.")
@click.option("--query-grep", "query_grep", default=None,
              help="Regex applied to the ``query`` field; only matching records are shown.")
@click.option("--limit", type=int, default=20, show_default=True,
              help="Maximum records to print. Most-recent last.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit raw JSONL on stdout instead of the human view.")
def audit(since: str | None, query_grep: str | None, limit: int, as_json: bool) -> None:
    """Read recent retrieval audit records.

    Audit lives at ``.teammate-cache/audit.jsonl`` and rotates weekly to
    ``audit-YYYY-WW.jsonl``. Both files are read by default.
    """
    import datetime as _dt

    from teammate.confidence import read_audit

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    cache_dir = brain_root / ".teammate-cache"
    if not cache_dir.exists():
        click.echo("no audit log yet — run `teammate ask` first", err=True)
        sys.exit(0)
    since_dt: _dt.datetime | None = None
    if since:
        try:
            since_dt = _dt.datetime.fromisoformat(since)
        except ValueError:
            click.echo(f"could not parse --since {since!r} as ISO date/datetime", err=True)
            sys.exit(2)
    records = read_audit(cache_dir, since=since_dt, query_grep=query_grep)
    records = records[-limit:] if limit > 0 else records
    if as_json:
        for rec in records:
            click.echo(_json.dumps(rec, sort_keys=True))
        return
    if not records:
        click.echo("no records matched")
        return
    for rec in records:
        flag = " *BELOW*" if rec.get("below_threshold") else ""
        click.echo(
            f"{rec.get('ts', '')}  {rec.get('action', 'ask'):<24}  "
            f"max={rec.get('max_score', 0):.2f}  k={rec.get('k', 0)}  "
            f"mode={rec.get('retrieval_mode', '?')}  "
            f"contradictions={rec.get('contradictions', 0)}{flag}  "
            f"{rec.get('query', '')[:80]}"
        )


# ---------- sync (v0.8) ----------


_SYNC_ROUTINES = ("confluence", "jira", "slack", "web")
_SYNC_ROUTINE_TO_NAME = {
    "confluence": "confluence_sync",
    "jira": "jira_sync",
    "slack": "slack_sync",
    "web": "web_pull",
}


def _read_sync_section(brain_root: Path, name: str) -> dict[str, Any]:
    """Read ``[sync.<name>]`` from the repo's ``.teammate/config.toml``.

    We don't go through ``TeammateConfig`` here — sync configs are
    free-form (lists of dicts, allowlist arrays) and do not belong on
    the typed dataclass. Returning a plain dict keeps the change
    boundary inside cli.py.
    """
    import tomllib

    cfg_path = brain_root / ".teammate" / "config.toml"
    if not cfg_path.is_file():
        return {}
    try:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    sync_section = data.get("sync") or {}
    if not isinstance(sync_section, dict):
        return {}
    section = sync_section.get(name) or {}
    return section if isinstance(section, dict) else {}


def _default_sync_out_dir(brain_root: Path, routine: str) -> Path:
    import datetime as _dt

    today = _dt.date.today().isoformat()
    return brain_root / "pending-imports" / f"{routine}-{today}"


@main.group()
def sync() -> None:
    """MCP-source sync routines — Confluence, Jira, Slack, Web (v0.8).

    Each subcommand stages PR-ready markdown drafts under
    ``pending-imports/<routine>-<date>/`` (or ``--out-dir``). The agent
    never auto-merges; humans review the draft files.
    """


def _run_sync(routine: str, out_dir: Path | None, dry_run: bool) -> None:
    from teammate.agent.base import RoutineConfig
    from teammate.agent.runner import run_routine

    if routine not in _SYNC_ROUTINES:
        click.echo(f"unknown sync routine: {routine!r}", err=True)
        sys.exit(2)
    name = _SYNC_ROUTINE_TO_NAME[routine]
    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    extra = _read_sync_section(brain_root, routine)
    target_dir = out_dir if out_dir is not None else _default_sync_out_dir(brain_root, routine)

    if dry_run:
        click.echo(
            f"[dry-run] would run {name} with config from "
            f"`.teammate/config.toml` [sync.{routine}] "
            f"and write to {target_dir}"
        )
        # Still surface the config so the user sees what would have run.
        click.echo(f"[dry-run] keys present in config: {sorted(extra.keys())}")
        return

    cfg = RoutineConfig(
        brain_root=brain_root,
        out_dir=Path(target_dir),
        dry_run=dry_run,
        extra=extra,
    )
    try:
        result = run_routine(name, cfg)
    except KeyError as exc:
        click.echo(f"sync: {exc}", err=True)
        sys.exit(2)
    click.echo(f"[{result.status}] {result.name} — {result.summary}")
    for art in result.artifacts:
        click.echo(f"  artifact: {art}")
    if result.status == "fail":
        sys.exit(1)


def _sync_subcommand(routine: str):
    """Build a click subcommand for one sync routine."""

    @sync.command(routine, help=f"Pull from {routine} sources and stage PR drafts.")
    @click.option(
        "--out-dir", "out_dir", type=click.Path(path_type=Path), default=None,
        help="Where to drop the staged drafts. "
             "Default: pending-imports/<routine>-<YYYY-MM-DD>/.",
    )
    @click.option(
        "--dry-run/--no-dry-run", default=False, show_default=True,
        help="Show what would happen without writing anything.",
    )
    def _cmd(out_dir: Path | None, dry_run: bool) -> None:
        _run_sync(routine, out_dir, dry_run)

    _cmd.__name__ = f"sync_{routine}"
    return _cmd


for _r in _SYNC_ROUTINES:
    _sync_subcommand(_r)


# ---------- impact (v0.9) ----------


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def _parse_duration(value: str):
    """Parse ``"1h"`` / ``"30m"`` / ``"7d"`` / ``"45s"`` into a ``timedelta``.

    Used by ``teammate impact list --since``. We accept only one unit per
    call; mixing (``"1h30m"``) is out of scope.
    """
    import datetime as _dt

    m = _DURATION_RE.match(value.strip())
    if not m:
        raise click.BadParameter(
            f"could not parse duration {value!r}. Use 30s / 5m / 2h / 7d.",
            param_hint="--since",
        )
    n = int(m.group(1))
    unit = m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return _dt.timedelta(seconds=n * seconds)


@main.group("impact")
def impact_group() -> None:
    """Pre / post terraform hooks — event-driven invalidation (v0.9).

    Use ``preview`` before ``terraform apply`` to find brain pages that
    reference the resources you're about to change. Use ``emit`` after
    apply to write a structured invalidation event the rest of the team
    will see.
    """


@impact_group.command("preview")
@click.option("--resource", "resources", multiple=True, required=True,
              help="Resource id (e.g. aws_vpc.shared, vpc-abc123). Repeat for multiple.")
@click.option("--state-path", "state_path", type=click.Path(path_type=Path),
              default=None,
              help="Optional path to terraform.tfstate — recorded as event metadata.")
@click.option("--invalidations-root", "invalidations_root",
              type=click.Path(path_type=Path), default=None,
              help="Override the on-disk path to the brain-invalidations repo.")
@click.option("--severity", "severity",
              type=click.Choice(["low", "medium", "high", "critical"]),
              default="high", show_default=True,
              help="Block threshold. HIGH or CRITICAL events block the apply.")
@click.option("--recency", "recency_hours", type=int, default=24, show_default=True,
              help="Look-back window in hours.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit a machine-readable JSON report.")
def impact_preview(
    resources: tuple[str, ...],
    state_path: Path | None,
    invalidations_root: Path | None,
    severity: str,
    recency_hours: int,
    as_json: bool,
) -> None:
    """Preview the brain impact of touching these resources.

    Exit codes:
      0 — no recent invalidations at or above ``--severity`` for the
          touched resources. Safe to apply.
      2 — at least one recent invalidation matches. Block.
    """
    import datetime as _dt

    from teammate.impact import preview as run_preview

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    report = run_preview(
        brain_root,
        list(resources),
        invalidations_root=invalidations_root,
        recency=_dt.timedelta(hours=recency_hours),
        severity_floor=severity,
    )

    if as_json:
        click.echo(_json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        sys.exit(2 if report.block else 0)

    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(
        f"[bold]teammate impact preview[/bold]  resources={len(resources)}  "
        f"window={recency_hours}h  block_at={severity.upper()}"
    )
    if state_path is not None:
        console.print(f"  state-path: {state_path}")

    if not report.pages:
        console.print("[dim]no brain pages reference the touched resources.[/dim]")
    else:
        page_table = Table(title="affected brain pages", show_lines=False)
        page_table.add_column("path", style="cyan")
        page_table.add_column("resource")
        page_table.add_column("matches", justify="right")
        for p in report.pages:
            page_table.add_row(p["path"], p["resource"], str(p["matches"]))
        console.print(page_table)

    if not report.recent_invalidations:
        console.print("[dim]no recent invalidations within the window.[/dim]")
    else:
        ev_table = Table(title="recent invalidations", show_lines=False)
        ev_table.add_column("timestamp", style="dim")
        ev_table.add_column("severity")
        ev_table.add_column("resource")
        ev_table.add_column("action")
        ev_table.add_column("source")
        for ev in report.recent_invalidations:
            ev_table.add_row(
                ev.get("timestamp", ""),
                ev.get("severity", "").upper(),
                f"{ev.get('resource_type','')}.{ev.get('resource_id','')}".strip("."),
                ev.get("action", ""),
                ev.get("source", ""),
            )
        console.print(ev_table)

    if report.block:
        console.print(
            f"\n[bold red]BLOCK[/bold red] — {len(report.recent_invalidations)} "
            f"invalidation(s) at or above {severity.upper()} touch your resources."
        )
        sys.exit(2)
    console.print("\n[bold green]OK[/bold green] — proceed with apply.")


@impact_group.command("emit")
@click.option("--resource", "resource", required=True,
              help="Resource id (e.g. aws_vpc.shared, vpc-abc123).")
@click.option("--action", "action", required=True,
              help="What changed: detach / modify / delete / create / ...")
@click.option("--severity", "severity",
              type=click.Choice(["low", "medium", "high", "critical"]),
              required=True)
@click.option("--source", "source", default="manual", show_default=True)
@click.option("--actor", "actor", default="",
              help="Who emitted the event (typically $USER or a CI bot id).")
@click.option("--state-path", "state_path", type=click.Path(path_type=Path),
              default=None,
              help="Optional path to terraform.tfstate — recorded as event metadata.")
@click.option("--invalidations-root", "invalidations_root",
              type=click.Path(path_type=Path), default=None)
def impact_emit(
    resource: str,
    action: str,
    severity: str,
    source: str,
    actor: str,
    state_path: Path | None,
    invalidations_root: Path | None,
) -> None:
    """Write an invalidation event to the brain-invalidations repo."""
    from teammate.impact import emit as run_emit

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    try:
        path = run_emit(
            brain_root,
            resource,
            action,
            severity,
            source=source,
            terraform_state_path=state_path,
            invalidations_root=invalidations_root,
            actor=actor or os.environ.get("USER", ""),
        )
    except ValueError as exc:
        click.echo(f"impact emit: {exc}", err=True)
        sys.exit(2)
    click.echo(f"wrote {path}")


@impact_group.command("list")
@click.option("--since", "since_str", default="24h", show_default=True,
              help="Look-back window. Examples: 30s / 5m / 2h / 7d.")
@click.option("--severity", "severity",
              type=click.Choice(["low", "medium", "high", "critical"]),
              default=None,
              help="Minimum severity to display. Default: all.")
@click.option("--invalidations-root", "invalidations_root",
              type=click.Path(path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True)
def impact_list(
    since_str: str,
    severity: str | None,
    invalidations_root: Path | None,
    as_json: bool,
) -> None:
    """Print recent invalidations as a table."""
    from teammate.impact import (
        _resolve_invalidations_root,
        read_recent_invalidations,
    )

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    root = _resolve_invalidations_root(brain_root, invalidations_root)
    if not root.exists():
        click.echo(
            f"impact list: no invalidations repo at {root} — nothing to read.",
            err=True,
        )
        sys.exit(0)
    since = _parse_duration(since_str)
    events = read_recent_invalidations(root, since=since, severity=severity)

    if as_json:
        click.echo(_json.dumps([ev.to_dict() for ev in events], indent=2, sort_keys=True))
        return

    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(
        f"[bold]teammate impact list[/bold]  window={since_str}  "
        f"severity>= {severity or 'any'}  root={root}"
    )
    if not events:
        console.print("[dim]no events in window.[/dim]")
        return
    table = Table(show_lines=False)
    table.add_column("timestamp", style="dim")
    table.add_column("severity")
    table.add_column("resource")
    table.add_column("action")
    table.add_column("source")
    table.add_column("actor", style="dim")
    for ev in events:
        full = f"{ev.resource_type}.{ev.resource_id}".strip(".")
        table.add_row(
            ev.timestamp,
            ev.severity.upper(),
            full,
            ev.action,
            ev.source,
            ev.actor or "-",
        )
    console.print(table)


# ---------- brain-pulse (v0.10) ----------


@main.command("brain-pulse")
@click.option("--since", "since_str", default="24h", show_default=True,
              help="Look-back window. Examples: 30s / 5m / 24h / 7d.")
@click.option("--user", "user_email", default=None,
              help="Override the engineer email. Default: git config user.email.")
@click.option("--invalidations-root", "invalidations_root",
              type=click.Path(path_type=Path), default=None,
              help="Override the on-disk path to the brain-invalidations repo.")
@click.option("--staging-dir", "staging_dir",
              type=click.Path(path_type=Path), default=None,
              help="Where the agent staged draft PRs. "
                   "Default: <brain>/.teammate-agent/draft-prs/.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit machine-readable JSON for scripting.")
def brain_pulse(
    since_str: str,
    user_email: str | None,
    invalidations_root: Path | None,
    staging_dir: Path | None,
    as_json: bool,
) -> None:
    """The engineer's morning ritual — targeted invalidations, brain changes, drafts.

    Aggregates the three signals an SRE wants at the top of the day:

      1. Resources YOU worked on with recent invalidations.
      2. Brain page changes the team made (last 24h by default).
      3. Pending PR-staged drafts the agent has produced.

    Read-only. Safe to run with no brain / no invalidations / no
    drafts present — emits an empty report and exits 0.
    """
    from teammate.brain_pulse import collect, parse_duration

    brain_root = Path(os.environ.get("TEAMMATE_BRAIN_ROOT") or Path.cwd())
    try:
        since = parse_duration(since_str)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--since") from exc

    pulse = collect(
        brain_root,
        user_email=user_email,
        since=since,
        since_label=since_str,
        invalidations_root=invalidations_root,
        staging_dir=staging_dir,
    )

    if as_json:
        click.echo(_json.dumps(pulse.to_dict(), indent=2, sort_keys=True))
        return

    _render_brain_pulse(pulse)


def _render_brain_pulse(pulse) -> None:  # type: ignore[no-untyped-def]
    """Rich-rendered dashboard for ``teammate brain-pulse``."""
    from rich.console import Console

    console = Console()
    rule = "─" * 45
    console.print()
    console.print(f"[bold]Brain Pulse — last {pulse.since}[/bold]")
    console.print(rule)
    console.print(
        f"  user: [cyan]{pulse.user_email or '(no git user.email — use --user)'}[/cyan]"
    )
    console.print()

    # 1) Targeted invalidations
    n_t = len(pulse.targeted)
    label_color = "yellow" if n_t else "dim"
    console.print(
        f"  [{label_color}]Resources YOU worked on with recent invalidations:"
        f"      [{n_t}][/{label_color}]"
    )
    if not pulse.targeted:
        console.print("     [dim](none)[/dim]")
    else:
        for t in pulse.targeted:
            sev = t.severity.upper()
            sev_style = {
                "CRITICAL": "[bold red]",
                "HIGH": "[red]",
                "MEDIUM": "[yellow]",
                "LOW": "[dim]",
            }.get(sev, "")
            close = "[/]" if sev_style else ""
            console.print(
                f"     - {t.resource} — {t.age_human}  "
                f"{sev_style}severity: {sev}{close}"
            )
            console.print(f"       affecting: {t.page}")
            if t.pr_hint:
                console.print(f"       hint: {t.pr_hint}")
    console.print()

    # 2) Brain changes
    n_c = len(pulse.brain_changes)
    console.print(f"  Brain page changes (last {pulse.since}):                   [{n_c}]")
    if not pulse.brain_changes:
        console.print("     [dim](none)[/dim]")
    else:
        # Show first 5 entries — full list with --since 7d
        for c in pulse.brain_changes[:5]:
            console.print(
                f"     - {c.author}: {c.path} ({c.kind})"
            )
        if n_c > 5:
            console.print(
                f"     ... ({n_c - 5} more — use --since 7d for full week)"
            )
    console.print()

    # 3) Pending drafts
    n_d = len(pulse.pending_drafts)
    console.print(f"  Pending PR-staged drafts (auto_pr_drafter):                [{n_d}]")
    if not pulse.pending_drafts:
        console.print("     [dim](none)[/dim]")
    else:
        for d in pulse.pending_drafts:
            console.print(
                f"     - {d.original_path} "
                f"(invalidation {d.invalidation_id}, severity {d.severity})"
            )
        console.print(
            "     run `gh pr review --request <id>` to triage."
        )
    console.print()

    if pulse.filtered_count:
        console.print(
            f"  [dim]Filtered as not-relevant-to-you: "
            f"                 [{pulse.filtered_count}][/dim]"
        )
        console.print()

    if pulse.recommended_actions:
        console.print("[bold]Today's recommended actions:[/bold]")
        for i, action in enumerate(pulse.recommended_actions, start=1):
            console.print(f"  {i}. {action}")
        console.print()

    console.print("Run `teammate ask \"...\"` to dig deeper.")
    console.print(rule)


if __name__ == "__main__":
    main()
