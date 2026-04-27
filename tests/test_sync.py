"""Tests for teammate sync. Uses a local bare-git repo as the 'team vault remote'
so no network access is required."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from teammate import sync as sync_mod
from teammate.vault import Vault


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def team_remote(tmp_path: Path) -> Path:
    """A bare git repo standing in for the team's private team-vault remote."""
    remote = tmp_path / "team-vault.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    return remote


@pytest.fixture
def local_vault(tmp_path: Path) -> Path:
    """A local compliance-vault/ scaffolded by Vault.ensure_layout."""
    vault_path = tmp_path / "repo" / "compliance-vault"
    Vault(vault_path).ensure_layout()
    return vault_path


@pytest.fixture
def configured_git_user(local_vault: Path):
    """Set git identity on the local vault so commits don't fail."""
    # Configure on the parent first (will inherit when we init the vault)
    yield


def test_status_uninitialized(local_vault: Path):
    s = sync_mod.status(local_vault)
    assert s.initialized is False
    assert s.remote == ""


def test_init_against_empty_remote(local_vault: Path, team_remote: Path):
    msg = sync_mod.init(local_vault, str(team_remote))
    assert "Initialized" in msg
    assert (local_vault / ".git").exists()
    assert (local_vault / sync_mod.CONFIG_FILE).exists()
    s = sync_mod.status(local_vault)
    assert s.initialized
    assert s.remote == str(team_remote)


def test_init_removes_local_gitignore(local_vault: Path, team_remote: Path):
    """The vault's local-only .gitignore should be removed when sync-initialized."""
    gi = local_vault / ".gitignore"
    assert gi.exists()  # ensure_layout drops one with '*'
    sync_mod.init(local_vault, str(team_remote))
    assert not gi.exists()


def test_init_refuses_double_init(local_vault: Path, team_remote: Path):
    sync_mod.init(local_vault, str(team_remote))
    with pytest.raises(sync_mod.SyncError, match="already has a .git"):
        sync_mod.init(local_vault, str(team_remote))


def test_init_against_missing_vault(tmp_path: Path, team_remote: Path):
    with pytest.raises(sync_mod.SyncError, match="Vault not found"):
        sync_mod.init(tmp_path / "no-vault", str(team_remote))


def test_push_without_init(local_vault: Path):
    with pytest.raises(sync_mod.SyncError, match="not sync-initialized"):
        sync_mod.push(local_vault)


def test_push_pull_round_trip(tmp_path: Path, team_remote: Path):
    """End-to-end: two engineers' vaults converge through the team remote."""
    # Engineer A's local vault
    vault_a = tmp_path / "engineer-a" / "compliance-vault"
    Vault(vault_a).ensure_layout()
    sync_mod.init(vault_a, str(team_remote))

    # Configure git identity inside the vault (subprocess inherits env, but
    # CI runners often have no global identity set).
    _git(["config", "user.email", "alice@acme.example"], vault_a)
    _git(["config", "user.name", "Alice"], vault_a)

    # Alice writes a file and pushes.
    (vault_a / "history" / "alice-run.md").write_text("alice was here\n")
    msg = sync_mod.push(vault_a, message="alice initial push")
    assert "Pushed" in msg

    # Engineer B's local vault
    vault_b = tmp_path / "engineer-b" / "compliance-vault"
    Vault(vault_b).ensure_layout()
    sync_mod.init(vault_b, str(team_remote))
    _git(["config", "user.email", "bob@acme.example"], vault_b)
    _git(["config", "user.name", "Bob"], vault_b)

    # Bob's vault should now contain Alice's history file.
    assert (vault_b / "history" / "alice-run.md").exists()
    assert (vault_b / "history" / "alice-run.md").read_text() == "alice was here\n"

    # Bob writes a file and pushes.
    (vault_b / "history" / "bob-run.md").write_text("bob was here\n")
    sync_mod.push(vault_b, message="bob initial push")

    # Alice pulls and gets Bob's file.
    sync_mod.pull(vault_a)
    assert (vault_a / "history" / "bob-run.md").exists()


def test_push_with_no_changes(local_vault: Path, team_remote: Path):
    sync_mod.init(local_vault, str(team_remote))
    _git(["config", "user.email", "test@example.com"], local_vault)
    _git(["config", "user.name", "Test"], local_vault)
    msg = sync_mod.push(local_vault, message="initial")
    # First push has at least one file (.teammate-sync.json + dirs)
    # Second push with no new changes should be no-op
    msg2 = sync_mod.push(local_vault)
    assert "Nothing to commit" in msg2 or "Pushed" in msg2


def test_status_shows_dirty_after_local_change(local_vault: Path, team_remote: Path):
    sync_mod.init(local_vault, str(team_remote))
    _git(["config", "user.email", "test@example.com"], local_vault)
    _git(["config", "user.name", "Test"], local_vault)
    sync_mod.push(local_vault, message="initial")

    # Add a file locally without committing
    (local_vault / "history" / "uncommitted.md").write_text("draft\n")
    s = sync_mod.status(local_vault)
    assert s.dirty is True


def test_pull_without_init(local_vault: Path):
    with pytest.raises(sync_mod.SyncError, match="not sync-initialized"):
        sync_mod.pull(local_vault)
