"""`teammate init` and `teammate scaffold` — set up the team brain on this machine.

Two distinct flows:

  - `teammate scaffold <dir>` — a TEAM LEAD creates a new team-brain repo
    from the bundled template. One-time, per organization. Outputs a
    fresh repo skeleton ready to commit + push to a private git remote.

  - `teammate init` — an INDIVIDUAL ENGINEER sets up teammate in an
    already-cloned team-brain repo. One-time per laptop. Detects Ollama,
    indexes the markdown, optionally registers gbrain.

This module ships the orchestrators for both. The CLI in `cli.py` exposes
them as `teammate scaffold` and `teammate init`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from teammate.brain import Brain
from teammate.rag import gbrain
from teammate.rag.index import discover_indexable_files, index_paths
from teammate.rag.ollama import OllamaClient


def _ok(msg: str) -> dict[str, str]:
    return {"status": "ok", "detail": msg}


def _skip(msg: str) -> dict[str, str]:
    return {"status": "skipped", "detail": msg}


def _fail(msg: str) -> dict[str, str]:
    return {"status": "failed", "detail": msg}


# ---------- scaffold (team lead, one-time per org) ----------


def _bundled_template_dir() -> Path:
    """Locate the team-brain skeleton bundled with the package."""
    pkg_root = Path(__file__).resolve().parent.parent.parent
    candidate = pkg_root / "templates" / "team-brain-skeleton"
    if candidate.is_dir():
        return candidate
    # Fall back to package-local copy (for installed wheels)
    return Path(__file__).resolve().parent / "templates" / "team-brain-skeleton"


def scaffold(target_dir: Path, team_name: str = "TEAM-NAME") -> dict[str, Any]:
    """Copy the bundled team-brain template into ``target_dir``.

    Replaces the literal placeholder ``TEAM-NAME`` in the seed files with
    the user's team name. Caller is responsible for `git init`-ing the
    output and pushing to a private remote.
    """
    target_dir = target_dir.resolve()
    if target_dir.exists() and any(target_dir.iterdir()):
        return {
            "status": "failed",
            "detail": (
                f"Target {target_dir} is not empty. "
                f"Pick an empty directory or `rm -rf` first."
            ),
        }

    src = _bundled_template_dir()
    if not src.is_dir():
        return _fail(f"Bundled template not found at {src}")

    target_dir.mkdir(parents=True, exist_ok=True)
    # shutil.copytree won't merge into an existing dir; we copy manually.
    for entry in src.rglob("*"):
        rel = entry.relative_to(src)
        dst = target_dir / rel
        if entry.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            text = entry.read_text(encoding="utf-8")
            text = text.replace("TEAM-NAME", team_name)
            dst.write_text(text, encoding="utf-8")

    return {
        "status": "ok",
        "detail": (
            f"Scaffolded team-brain at {target_dir}.\n"
            f"  Next steps:\n"
            f"    cd {target_dir}\n"
            f"    git init -b main && git add -A\n"
            f"    git commit -m 'init: team-brain'\n"
            f"    git remote add origin git@github.com:<your-org>/team-brain.git\n"
            f"    git push -u origin main"
        ),
    }


# ---------- init (individual engineer, per-laptop) ----------


def step_brain(brain_root: Path) -> dict[str, str]:
    brain = Brain(brain_root)
    if not brain.exists():
        return _fail(
            f"No CLAUDE.md found at {brain_root}. Are you in a team-brain repo? "
            f"If you're the team lead setting this up, run `teammate scaffold <dir>` first."
        )
    stats = brain.stats()
    return _ok(
        f"Brain detected at {brain_root}: "
        f"{stats['total']} markdown files "
        f"({stats['claude']} CLAUDE.md, {stats['skills']} skills, "
        f"{stats['rules']} rules, {stats['docs']} docs, {stats['knowledge']} knowledge)"
    )


def step_ollama(*, host: str | None = None) -> dict[str, str]:
    client = OllamaClient(host=host)
    if not client.is_up():
        return _skip(
            "Ollama not detected on localhost:11434. Install: "
            "https://ollama.com/download (open-source, runs locally). "
            f"Then: `ollama serve &` + `ollama pull {client.llm_model}` "
            f"+ `ollama pull {client.embedding_model}`."
        )
    try:
        models = client.list_models()
    except Exception as exc:
        return _fail(f"Ollama responded but list-models failed: {exc}")
    needed = {client.llm_model, client.embedding_model}
    missing = [m for m in needed if not any(m == name or name.startswith(f"{m}:") for name in models)]
    if missing:
        cmds = " && ".join(f"ollama pull {m}" for m in missing)
        return _ok(
            f"Ollama up. Missing models: {', '.join(missing)}. Pull with: {cmds}"
        )
    return _ok(f"Ollama up. Required models present: {', '.join(sorted(needed))}.")


def step_gbrain(brain_root: Path, *, register: bool = False) -> dict[str, str]:
    status = gbrain.detect()
    if not status.available:
        return _skip(status.notes)
    if not register:
        return _ok(
            f"{status.notes} Re-run `teammate init --register-gbrain` "
            f"to register the team-brain as a gbrain source."
        )
    ok, msg = gbrain.register_vault(brain_root)
    return _ok(msg) if ok else _fail(msg)


def step_index(brain_root: Path, *, ollama: OllamaClient | None = None) -> dict[str, str]:
    cache_dir = brain_root / ".teammate-cache"
    paths = discover_indexable_files([brain_root])
    if not paths:
        return _skip("No markdown found in the brain yet.")
    indexed, skipped = index_paths(paths, cache_dir, ollama=ollama)
    embed_status = (
        "with embeddings"
        if (ollama and ollama.is_up())
        else "keyword-only (Ollama down)"
    )
    return _ok(
        f"Indexed {indexed} files {embed_status} ({skipped} unchanged). "
        f"Cache: .teammate-cache/vault.sqlite"
    )


def run(brain_root: Path, *, register_gbrain: bool = False) -> dict[str, dict[str, str]]:
    """Run the full per-laptop init flow inside an already-cloned team-brain."""
    brain_root = brain_root.resolve()
    results: dict[str, dict[str, str]] = {}

    results["brain"] = step_brain(brain_root)
    if results["brain"]["status"] == "failed":
        # No point continuing without a brain.
        return results

    results["ollama"] = step_ollama()
    results["gbrain"] = step_gbrain(brain_root, register=register_gbrain)
    ollama = OllamaClient()
    results["index"] = step_index(brain_root, ollama=ollama if ollama.is_up() else None)
    return results


def render_summary(results: dict[str, dict[str, str]]) -> str:
    lines = ["teammate init —"]
    for step, result in results.items():
        status = result["status"]
        symbol = {"ok": "✓", "skipped": "·", "failed": "✗"}.get(status, "?")
        lines.append(f"  {symbol} {step}: {result['detail']}")
    return "\n".join(lines)


__all__ = [
    "render_summary",
    "run",
    "scaffold",
    "step_brain",
    "step_gbrain",
    "step_index",
    "step_ollama",
]
