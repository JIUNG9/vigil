"""Git-backed team vault federation.

The thesis: Claude Teamspace gives teams shared workspace state in
Anthropic's cloud. Regulated teams (Korean fintech, public sector,
defense, anyone subject to data-residency rules) cannot put their
compliance state in someone else's cloud. They CAN put it in a private
git repository they already own.

teammate sync turns ``compliance-vault/`` into a separate git checkout
pointing at a private team-vault repository. Each engineer's local
``teammate score`` writes to their local vault as usual; ``teammate sync
push`` commits and pushes their attestations to the shared team repo.
``teammate sync pull`` rebases other engineers' attestations into the
local vault. The result is a verifiable team audit trail — every
attestation is dual-signed (git commit by the engineer's identity, PDF
content by Fulcio/GitHub OIDC).

Why a separate git checkout, not a submodule?

Submodules are notoriously confusing — every contributor has to remember
``--recurse-submodules`` and the parent repo carries a pointer that drifts.
A separate checkout inside ``compliance-vault/`` is independent: the
team's main code repository doesn't need to know about the vault repo at
all. ``git status`` from the project root ignores ``compliance-vault/``
because it's a different git working tree.

ASCII flow::

    Engineer A                    Engineer B                    Engineer C
    ──────────                    ──────────                    ──────────
    teammate score                teammate score                teammate score
        │                             │                             │
        ▼                             ▼                             ▼
    local vault                   local vault                   local vault
        │                             │                             │
        │ teammate sync push          │ teammate sync push          │ teammate sync push
        │                             │                             │
        └────────────┐  ┌─────────────┘            ┌────────────────┘
                     ▼  ▼                          │
              ┌────────────────────┐               │
              │ private team-vault │◄──────────────┘
              │ git repo           │
              │ (org-owned, no     │
              │  Anthropic cloud)  │
              └────────────────────┘
                     │
                     │ teammate sync pull
                     ▼
              every engineer's local vault converges to the
              union of attestations, rebased onto a linear timeline
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_FILE = ".teammate-sync.json"


# ---------- types ----------


class SyncError(RuntimeError):
    """Sync operation failed. Caller should print the message and exit non-zero."""


@dataclass(frozen=True, slots=True)
class SyncStatus:
    initialized: bool
    remote: str
    branch: str
    ahead: int
    behind: int
    dirty: bool
    last_local_commit: str


# ---------- helpers ----------


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in ``cwd``. Raises SyncError on failure when check=True."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise SyncError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _config_path(vault_path: Path) -> Path:
    return vault_path / CONFIG_FILE


def _load_config(vault_path: Path) -> dict[str, Any]:
    p = _config_path(vault_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_config(vault_path: Path, cfg: dict[str, Any]) -> None:
    p = _config_path(vault_path)
    p.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")


def _is_initialized(vault_path: Path) -> bool:
    return (vault_path / ".git").exists()


# ---------- operations ----------


def init(vault_path: Path, remote_url: str, branch: str = "main") -> str:
    """Initialize the vault as a separate git checkout pointing at ``remote_url``.

    Removes the local-only ``.gitignore`` (the vault is now a tracked git
    repo), initializes a fresh git repo, sets the remote, attempts to
    pull existing content, and writes ``.teammate-sync.json``.

    Returns a human-readable status string.
    """
    if not vault_path.exists():
        raise SyncError(
            f"Vault not found at {vault_path}. Run `teammate init` first."
        )
    if _is_initialized(vault_path):
        raise SyncError(
            f"{vault_path} already has a .git directory. Use `teammate sync status` "
            f"or remove .git/ manually if you want to re-initialize."
        )

    # Drop the local-only .gitignore — the vault is now meant to be tracked.
    gi = vault_path / ".gitignore"
    if gi.exists():
        gi.unlink()

    _git(["init", "-b", branch], cwd=vault_path)
    _git(["remote", "add", "origin", remote_url], cwd=vault_path)

    # Try to pull existing content. Empty remote is fine (returns non-zero).
    fetch = _git(["fetch", "origin", branch], cwd=vault_path, check=False)
    if fetch.returncode == 0:
        # Remote has content — reset our (empty) tree to match origin.
        _git(["reset", "--hard", f"origin/{branch}"], cwd=vault_path)
        pulled = "pulled existing team vault from origin"
    else:
        pulled = "remote is empty (or unreachable); will populate on first push"

    cfg = {
        "remote": remote_url,
        "branch": branch,
        "initialized_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_config(vault_path, cfg)
    return f"Initialized vault sync against {remote_url} ({branch}). {pulled}."


def push(vault_path: Path, message: str | None = None) -> str:
    """Stage, commit, and push the local vault state.

    The commit message defaults to a timestamp + identity summary so each
    engineer's contribution is easy to find in `git log`. Caller can
    override with ``--message``.
    """
    if not _is_initialized(vault_path):
        raise SyncError(
            f"Vault at {vault_path} is not sync-initialized. "
            f"Run `teammate sync init <url>` first."
        )
    cfg = _load_config(vault_path)
    branch = cfg.get("branch", "main")

    _git(["add", "-A"], cwd=vault_path)

    # Check if there's anything to commit.
    status = _git(["status", "--porcelain"], cwd=vault_path, check=False)
    if not status.stdout.strip():
        # Nothing to commit, but maybe local has unpushed commits — try push.
        push_only = _git(["push", "origin", branch], cwd=vault_path, check=False)
        if push_only.returncode == 0:
            return "Nothing to commit. Pushed any pre-existing local commits."
        return "Nothing to commit; nothing to push."

    # Build a default message that surfaces who attested what.
    if not message:
        ident = _git(["config", "user.email"], cwd=vault_path, check=False)
        email = ident.stdout.strip() or "unknown"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = f"vault: attestation push from {email} @ {ts}"

    _git(["commit", "-m", message], cwd=vault_path)
    _git(["push", "origin", branch], cwd=vault_path)
    return f"Pushed: {message}"


def pull(vault_path: Path) -> str:
    """Pull other engineers' attestations into the local vault.

    Uses ``git pull --rebase`` so the team timeline stays linear. If a
    rebase conflict arises (rare — most attestations live in distinct
    timestamped files), the caller is responsible for resolving it.
    """
    if not _is_initialized(vault_path):
        raise SyncError(
            f"Vault at {vault_path} is not sync-initialized. "
            f"Run `teammate sync init <url>` first."
        )
    cfg = _load_config(vault_path)
    branch = cfg.get("branch", "main")
    result = _git(["pull", "--rebase", "origin", branch], cwd=vault_path)
    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "up to date"
    return f"Pulled from origin/{branch}. {summary}"


def status(vault_path: Path) -> SyncStatus:
    """Return rich status for `teammate sync status`."""
    if not _is_initialized(vault_path):
        return SyncStatus(
            initialized=False,
            remote="",
            branch="",
            ahead=0,
            behind=0,
            dirty=False,
            last_local_commit="",
        )

    cfg = _load_config(vault_path)
    remote = cfg.get("remote", "")
    branch = cfg.get("branch", "main")

    # ahead/behind
    rev_list = _git(
        ["rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"],
        cwd=vault_path,
        check=False,
    )
    if rev_list.returncode == 0 and rev_list.stdout.strip():
        try:
            behind_str, ahead_str = rev_list.stdout.strip().split()
            behind = int(behind_str)
            ahead = int(ahead_str)
        except ValueError:
            behind, ahead = 0, 0
    else:
        behind, ahead = 0, 0

    # dirty
    porcelain = _git(["status", "--porcelain"], cwd=vault_path, check=False)
    dirty = bool(porcelain.stdout.strip())

    # last commit
    last = _git(
        ["log", "-1", "--format=%h %s"], cwd=vault_path, check=False
    )
    last_local = last.stdout.strip() if last.returncode == 0 else ""

    return SyncStatus(
        initialized=True,
        remote=remote,
        branch=branch,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        last_local_commit=last_local,
    )


__all__ = [
    "SyncError",
    "SyncStatus",
    "init",
    "pull",
    "push",
    "status",
]
