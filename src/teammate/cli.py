"""`teammate` CLI — entry point for the team-brain workflow.

Subcommands:

  teammate scaffold <dir>     — TEAM LEAD: create a new team-brain repo
                                 from the bundled template. One-time per org.
  teammate init               — ENGINEER: set up teammate inside an
                                 already-cloned team-brain repo.
  teammate ask "<query>"      — query the brain locally (Ollama + RAG).
  teammate index [--rebuild]  — rebuild / refresh the local sqlite-vec index.
  teammate stats              — show what's in the brain (file counts by section).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from teammate import __version__
from teammate.brain import Brain
from teammate.init import render_summary, run as run_init, scaffold as run_scaffold
from teammate.rag.ask import answer
from teammate.rag.index import discover_indexable_files, index_paths
from teammate.rag.ollama import OllamaClient


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
def init(register_gbrain: bool) -> None:
    """Set up teammate in this already-cloned team-brain repo."""
    brain_root = Path.cwd()
    results = run_init(brain_root, register_gbrain=register_gbrain)
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
    ollama = OllamaClient()
    paths = discover_indexable_files([brain_root])
    if paths:
        index_paths(paths, cache_dir, ollama=ollama if ollama.is_up() else None,
                    rebuild=rebuild)
    full_query = " ".join(query).strip()
    db_path = cache_dir / "vault.sqlite"
    for chunk in answer(full_query, db_path, brain_root, ollama=ollama, k=top_k):
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
    ollama = OllamaClient()
    paths = discover_indexable_files([brain_root])
    if not paths:
        click.echo("No markdown found in this directory. Are you in a team-brain repo?", err=True)
        sys.exit(1)
    indexed, skipped = index_paths(paths, cache_dir, ollama=ollama if ollama.is_up() else None,
                                   rebuild=rebuild)
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


if __name__ == "__main__":
    main()
