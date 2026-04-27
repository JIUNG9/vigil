"""`teammate` CLI — entry point for every subcommand.

Subcommands:

  teammate init             scaffold vault, install hooks, detect Ollama/gbrain, index
  teammate score [PATH]     run probes, write to vault, print ASCII table
                              --json, --quiet, --sign, --as-admin, --vault PATH
  teammate ask "<query>"    retrieve vault chunks, stream Ollama answer
                              --rebuild, --top-k INT, --vault PATH
  teammate watch            pull KISA RSS + NVD CVE diffs into vault
                              --source kisa|nvd|all, --vault PATH, --days INT
  teammate attest           render the latest score as a signed PDF
                              --sign, --vault PATH

The CLI uses ``click`` so subcommand discovery + ``--help`` work natively.
Output is intentionally terse. The Medium screencast wants ``teammate score``
to fit on one phone screen.
"""

from __future__ import annotations

import json as json_mod
import os
import sys
from pathlib import Path

import click

from teammate import __version__
from teammate.attest import SigstoreUnavailable, attest as build_attestation
from teammate.catalogs import load_default
from teammate.init import render_summary, run as run_init
from teammate.rag.ask import answer
from teammate.rag.index import discover_indexable_files, index_paths
from teammate.rag.ollama import OllamaClient
from teammate.score import run_all
from teammate import sync as sync_mod
from teammate.vault import Vault
from teammate.watch import run as run_watch

# ---------- shared helpers ----------


def _resolve_vault(repo_root: Path, override: Path | None) -> Path:
    if override:
        return override.resolve()
    return (repo_root / "compliance-vault").resolve()


def _emit_score_table(summary, outcomes, *, quiet: bool) -> None:
    if quiet:
        return
    pct = (
        f"{summary.overall_pct * 100:.1f}%"
        if summary.overall_pct is not None
        else "n/a"
    )
    click.echo(
        f"\nteammate score — overall: {pct}  "
        f"(pass={summary.counts.get('pass', 0)} "
        f"partial={summary.counts.get('partial', 0)} "
        f"fail={summary.counts.get('fail', 0)} "
        f"n/a={summary.counts.get('n_a', 0)} "
        f"indet={summary.counts.get('indeterminate', 0)})"
    )
    click.echo(f"target: {summary.target_path}")
    click.echo(f"commit: {summary.commit or '(no git)'}")
    click.echo("")
    headers = ("probe", "result", "framework:control", "severity")
    click.echo("  ".join(f"{h:<22}" for h in headers))
    click.echo("  ".join("-" * 22 for _ in headers))
    for o in outcomes:
        ref = (
            f"{o.framework}:{o.control_id}"
            if o.framework and o.control_id
            else "—"
        )
        click.echo(
            f"  ".join(
                [
                    f"{o.probe_id[:22]:<22}",
                    f"{o.result[:22]:<22}",
                    f"{ref[:22]:<22}",
                    f"{o.severity[:22]:<22}",
                ]
            )
        )


# ---------- click command tree ----------


@click.group()
@click.version_option(version=__version__, prog_name="teammate")
def main() -> None:
    """teammate — battle buddy for new SREs joining regulated teams."""


@main.command()
@click.option("--force", is_flag=True, envvar="TEAMMATE_FORCE_INIT",
              help="Overwrite an existing pre-push hook.")
@click.option("--register-gbrain", is_flag=True,
              help="If gbrain is installed, register the vault as a source.")
def init(force: bool, register_gbrain: bool) -> None:
    """Scaffold vault, install hooks, detect Ollama/gbrain, build index."""
    repo_root = Path.cwd()
    results = run_init(repo_root, force=force, register_gbrain=register_gbrain)
    click.echo(render_summary(results))
    if any(r["status"] == "failed" for r in results.values()):
        sys.exit(1)


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path),
                required=False)
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location (default: ./compliance-vault).")
@click.option("--json", "as_json", is_flag=True,
              help="Emit JSON to stdout instead of an ASCII table. Vault is still written.")
@click.option("--quiet", is_flag=True,
              help="Suppress the table (vault is still written).")
@click.option("--sign", is_flag=True,
              help="Generate a signed PDF attestation alongside the score.")
@click.option("--as-admin", is_flag=True, envvar="TEAMMATE_ADMIN_MODE",
              help="Set TEAMMATE_ADMIN_MODE=1 — promotes 'partial' to pass/fail "
                   "via gh api when GITHUB_TOKEN has admin:repo scope.")
def score(
    path: Path | None,
    vault_path: Path | None,
    as_json: bool,
    quiet: bool,
    sign: bool,
    as_admin: bool,
) -> None:
    """Run probes against PATH (default: cwd) and write to the vault."""
    repo_root = (path or Path.cwd()).resolve()
    if as_admin:
        os.environ["TEAMMATE_ADMIN_MODE"] = "1"
    catalogs = load_default()
    summary, outcomes = run_all(repo_root, catalogs)

    vault = Vault(_resolve_vault(repo_root, vault_path))
    vault.write_score_run(summary, outcomes)

    if sign:
        try:
            pdf, sig, crt = build_attestation(summary, outcomes, sign=True)
        except SigstoreUnavailable as exc:
            click.echo(f"sign skipped: {exc}", err=True)
            pdf, sig, crt = build_attestation(summary, outcomes, sign=False)
        vault.write_attestation(pdf, sig, crt, summary)
    else:
        # Always render at least an unsigned PDF preview alongside the run.
        pdf, sig, crt = build_attestation(summary, outcomes, sign=False)
        vault.write_attestation(pdf, None, None, summary)

    if as_json:
        click.echo(
            json_mod.dumps(
                {
                    "overall_pct": summary.overall_pct,
                    "counts": summary.counts,
                    "timestamp": summary.timestamp,
                    "commit": summary.commit,
                    "target_path": summary.target_path,
                    "outcomes": [
                        {
                            "probe_id": o.probe_id,
                            "result": o.result,
                            "framework": o.framework,
                            "control_id": o.control_id,
                            "severity": o.severity,
                            "detail": o.detail,
                        }
                        for o in outcomes
                    ],
                },
                indent=2,
            )
        )
    else:
        _emit_score_table(summary, outcomes, quiet=quiet)


@main.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location.")
@click.option("--rebuild", is_flag=True, help="Force a full re-index before answering.")
@click.option("--top-k", "top_k", type=int, default=6, show_default=True)
def ask(query: tuple[str, ...], vault_path: Path | None, rebuild: bool, top_k: int) -> None:
    """Ask a question about the vault. Streams a local-LLM answer."""
    repo_root = Path.cwd()
    cache_dir = repo_root / ".teammate-cache"
    ollama = OllamaClient()
    paths = discover_indexable_files([_resolve_vault(repo_root, vault_path), repo_root])
    if paths:
        index_paths(paths, cache_dir, ollama=ollama if ollama.is_up() else None,
                    rebuild=rebuild)
    full_query = " ".join(query).strip()
    db_path = cache_dir / "vault.sqlite"
    for chunk in answer(full_query, db_path, repo_root, ollama=ollama, k=top_k):
        click.echo(chunk, nl=False)
    click.echo("")


@main.command()
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location.")
@click.option("--source", "sources", multiple=True,
              type=click.Choice(["kisa", "nvd", "all"]), default=["all"],
              show_default=True)
@click.option("--days", "nvd_days", type=int, default=7, show_default=True,
              help="NVD CVE lookback window in days.")
def watch(vault_path: Path | None, sources: tuple[str, ...], nvd_days: int) -> None:
    """Pull advisory feeds and write diffs into the vault."""
    repo_root = Path.cwd()
    requested: list[str]
    if "all" in sources:
        requested = ["kisa", "nvd"]
    else:
        requested = list(sources)
    vault_root = _resolve_vault(repo_root, vault_path)
    summary = run_watch(vault_root, sources=requested, nvd_days=nvd_days)
    for source, info in summary.items():
        click.echo(
            f"{source}: fetched={info['fetched']} new={info['new']}"
            + (f" — first new: {info['first_new_title']}" if info["new"] else "")
        )


@main.command()
@click.option("--sign", is_flag=True, help="Sign the latest score via sigstore (interactive OIDC).")
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location.")
def attest(sign: bool, vault_path: Path | None) -> None:
    """Render the latest score as a (signed or preview) PDF and place it in the vault."""
    repo_root = Path.cwd()
    catalogs = load_default()
    summary, outcomes = run_all(repo_root, catalogs)
    vault = Vault(_resolve_vault(repo_root, vault_path))
    if sign:
        try:
            pdf, sig, crt = build_attestation(summary, outcomes, sign=True)
        except SigstoreUnavailable as exc:
            click.echo(f"sign skipped: {exc}", err=True)
            pdf, sig, crt = build_attestation(summary, outcomes, sign=False)
    else:
        pdf, sig, crt = build_attestation(summary, outcomes, sign=False)
    out_path = vault.write_attestation(pdf, sig, crt, summary)
    click.echo(f"wrote attestation: {out_path}")


@main.group()
def sync() -> None:
    """Git-backed team vault federation. Beats Teamspace on the data-residency axis."""


@sync.command("init")
@click.argument("git_url")
@click.option("--branch", default="main", show_default=True,
              help="Branch on the team-vault remote.")
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location.")
def sync_init(git_url: str, branch: str, vault_path: Path | None) -> None:
    """Initialize the vault as a separate git checkout against GIT_URL.

    GIT_URL should be a private git repository the team owns
    (e.g., git@github.com:org/team-vault.git). It can be empty — the
    first push will populate it.
    """
    target = _resolve_vault(Path.cwd(), vault_path)
    try:
        msg = sync_mod.init(target, git_url, branch=branch)
    except sync_mod.SyncError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(msg)


@sync.command("push")
@click.option("-m", "--message", default=None, help="Commit message override.")
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location.")
def sync_push(message: str | None, vault_path: Path | None) -> None:
    """Stage, commit, and push the local vault state to the team remote."""
    target = _resolve_vault(Path.cwd(), vault_path)
    try:
        msg = sync_mod.push(target, message=message)
    except sync_mod.SyncError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(msg)


@sync.command("pull")
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location.")
def sync_pull(vault_path: Path | None) -> None:
    """Rebase other engineers' attestations into the local vault."""
    target = _resolve_vault(Path.cwd(), vault_path)
    try:
        msg = sync_mod.pull(target)
    except sync_mod.SyncError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(msg)


@sync.command("status")
@click.option("--vault", "vault_path", type=click.Path(path_type=Path),
              help="Override vault location.")
def sync_status(vault_path: Path | None) -> None:
    """Show the team-vault sync state (initialized, remote, ahead/behind, dirty)."""
    target = _resolve_vault(Path.cwd(), vault_path)
    s = sync_mod.status(target)
    if not s.initialized:
        click.echo("Vault is local-only (not sync-initialized). Run `teammate sync init <git-url>` to federate.")
        return
    click.echo(f"remote:    {s.remote}")
    click.echo(f"branch:    {s.branch}")
    click.echo(f"ahead:     {s.ahead}")
    click.echo(f"behind:    {s.behind}")
    click.echo(f"dirty:     {'yes' if s.dirty else 'no'}")
    click.echo(f"last:      {s.last_local_commit}")


if __name__ == "__main__":
    main()
