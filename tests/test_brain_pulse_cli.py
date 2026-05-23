"""Tests for ``teammate brain-pulse`` (v0.10)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from vigil.brain_pulse import collect, parse_duration
from vigil.cli import main as cli_main


def _seed(root: Path) -> None:
    (root / "docs" / "runbooks").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "runbooks" / "auth-deploy.md").write_text(
        "# Auth deploy\n\nUses vpc-abc12345 and aws_iam_role.deploy-bot.\n",
        encoding="utf-8",
    )


def _git_init(root: Path, email: str = "alice@team", name: str = "alice") -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", email], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", name], cwd=str(root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=str(root),
        check=True,
        env={**os.environ, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_NAME": name},
    )


def _write_event(invalidations_root: Path, *, resource_type: str,
                 resource_id: str, severity: str = "high") -> None:
    import datetime as _dt
    ts = _dt.datetime.now(_dt.UTC)
    target_dir = (
        invalidations_root / "invalidations"
        / f"{ts.year:04d}" / f"{ts.month:02d}" / f"{ts.day:02d}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{resource_id}.json"
    target.write_text(
        json.dumps({
            "id": f"id-{resource_id}",
            "timestamp": ts.isoformat(timespec="seconds"),
            "source": "test",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "action": "detach",
            "severity": severity,
            "actor": "test",
            "metadata": {},
        }),
        encoding="utf-8",
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_parse_duration_supports_units():
    assert parse_duration("30s").total_seconds() == 30
    assert parse_duration("5m").total_seconds() == 300
    assert parse_duration("24h").total_seconds() == 86400
    assert parse_duration("7d").total_seconds() == 7 * 86400


def test_parse_duration_rejects_invalid():
    with pytest.raises(ValueError):
        parse_duration("1week")


def test_brain_pulse_help_smoke(runner: CliRunner):
    result = runner.invoke(cli_main, ["brain-pulse", "--help"])
    assert result.exit_code == 0
    assert "brain-pulse" in result.output.lower() or "Engineer" in result.output


def test_brain_pulse_json_empty_brain(tmp_path: Path, runner: CliRunner,
                                       monkeypatch):
    """No brain / no invalidations / no drafts — must emit valid JSON."""
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    result = runner.invoke(
        cli_main,
        [
            "brain-pulse", "--user", "alice@team",
            "--json", "--since", "24h",
            "--invalidations-root", str(tmp_path / "no-invalidations"),
            "--staging-dir", str(tmp_path / "no-staging"),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["targeted"] == []
    assert data["brain_changes"] == []
    assert data["pending_drafts"] == []
    assert data["filtered_count"] == 0


def test_brain_pulse_user_flag_overrides_git(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    _seed(brain)
    _git_init(brain, "alice@team", "alice")
    inv_root = tmp_path / "brain-invalidations"
    _write_event(inv_root, resource_type="aws_vpc", resource_id="vpc-abc12345")

    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "brain-pulse", "--user", "alice@team", "--json", "--since", "24h",
            "--invalidations-root", str(inv_root),
            "--staging-dir", str(tmp_path / "no-staging"),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["user_email"] == "alice@team"


def test_brain_pulse_json_includes_targeted(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    _seed(brain)
    _git_init(brain, "alice@team", "alice")
    inv_root = tmp_path / "brain-invalidations"
    _write_event(inv_root, resource_type="aws_vpc",
                 resource_id="vpc-abc12345", severity="high")

    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "brain-pulse", "--user", "alice@team", "--json", "--since", "24h",
            "--invalidations-root", str(inv_root),
            "--staging-dir", str(tmp_path / "no-staging"),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["targeted"], "alice should see her edited file with the invalidation"
    first = data["targeted"][0]
    assert "vpc-abc12345" in first["resource"]
    assert first["page"].endswith("auth-deploy.md")


def test_brain_pulse_human_render_no_crash(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    _seed(brain)
    _git_init(brain, "alice@team", "alice")

    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "brain-pulse", "--user", "alice@team", "--since", "24h",
            "--invalidations-root", str(tmp_path / "brain-invalidations"),
            "--staging-dir", str(tmp_path / "no-staging"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Brain Pulse" in result.output


def test_brain_pulse_invalid_since(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["brain-pulse", "--user", "alice@team", "--since", "1week"],
    )
    assert result.exit_code != 0
    assert "could not parse" in result.output.lower() or "Invalid value" in result.output


def test_brain_pulse_includes_brain_changes(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    _seed(brain)
    _git_init(brain, "alice@team", "alice")
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "brain-pulse", "--user", "alice@team", "--json", "--since", "24h",
            "--invalidations-root", str(tmp_path / "brain-invalidations"),
            "--staging-dir", str(tmp_path / "no-staging"),
        ],
    )
    data = json.loads(result.output)
    paths = [c["path"] for c in data["brain_changes"]]
    assert any("auth-deploy.md" in p for p in paths)


def test_brain_pulse_collect_pending_drafts(tmp_path: Path):
    """`collect` reads draft-prs/ frontmatter into PendingDraft."""
    brain = tmp_path / "brain"
    brain.mkdir()
    drafts = tmp_path / "agent-out" / "draft-prs"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "auth-deploy-inv-1.md").write_text(
        '---\noriginal_path: "docs/runbooks/auth-deploy.md"\n'
        'invalidation_id: "inv-1"\nseverity: "high"\nrequires_review: true\n'
        '---\n\n# Rewritten\n',
        encoding="utf-8",
    )
    pulse = collect(
        brain, user_email="alice@team", staging_dir=drafts,
        invalidations_root=tmp_path / "no-inv",
    )
    assert pulse.pending_drafts
    assert pulse.pending_drafts[0].invalidation_id == "inv-1"
    assert pulse.pending_drafts[0].severity == "high"
