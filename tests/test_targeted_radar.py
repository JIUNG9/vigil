"""Tests for the v0.10 ``targeted_radar`` routine."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date
from pathlib import Path

from vigil.agent import RoutineConfig
from vigil.agent.base import OK, WARN
from vigil.agent.runner import run_routine
from vigil.agent.targeted_radar import run as radar_run


def _seed(root: Path, *, with_services: bool = True) -> None:
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "people.md").write_text(
        "- alice <alice@team> — Auth Service owner\n"
        "- bob <bob@team> — Platform team\n"
        "- carol <carol@team> — Network engineer\n",
        encoding="utf-8",
    )
    if with_services:
        (root / "knowledge" / "services.md").write_text(
            "- auth-service: alice — owns aws_iam_role.deploy-bot, vpc-abc12345\n"
            "- billing: bob — owns aws_db_instance.billing-primary\n",
            encoding="utf-8",
        )
    (root / "docs" / "runbooks").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "runbooks" / "auth-deploy.md").write_text(
        "# Auth deploy\n\nUses aws_vpc.shared (vpc-abc12345) and "
        "aws_iam_role.deploy-bot.\n",
        encoding="utf-8",
    )


def _git_init_commit(root: Path, email: str, name: str) -> None:
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


def test_targeted_radar_warn_no_invalidation(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed(brain)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(brain_root=brain, out_dir=out, extra={})
    result = radar_run(cfg)
    assert result.status == WARN
    assert "no invalidation" in result.summary.lower()


def test_targeted_radar_owner_score(tmp_path: Path):
    """An owner declared in services.md gets +50 even with no git history."""
    brain = tmp_path / "brain"
    _seed(brain)
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-x",
        "resource_type": "aws_iam_role",
        "resource_id": "deploy-bot",
        "severity": "high",
        "action": "modify",
    }
    cfg = RoutineConfig(brain_root=brain, out_dir=out,
                        extra={"invalidation": inv})
    result = radar_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    out_path = out / "radar" / "inv-x.json"
    assert out_path.is_file()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    ids = [t["engineer_id"] for t in data["top"]]
    assert "alice" in ids


def test_targeted_radar_git_history_score(tmp_path: Path):
    """An engineer who edited a referencing page scores via git."""
    brain = tmp_path / "brain"
    _seed(brain, with_services=False)
    _git_init_commit(brain, "alice@team", "alice")
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-y",
        "resource_type": "aws_vpc",
        "resource_id": "vpc-abc12345",
        "severity": "high",
        "action": "detach",
    }
    cfg = RoutineConfig(brain_root=brain, out_dir=out,
                        extra={"invalidation": inv, "activity_days": 30})
    result = radar_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    data = json.loads((out / "radar" / "inv-y.json").read_text(encoding="utf-8"))
    alice_row = next((t for t in data["top"] if t["engineer_id"] == "alice"), None)
    assert alice_row is not None
    assert alice_row["score"] >= 30
    assert any("auth-deploy.md" in r for r in alice_row["reasons"])


def test_targeted_radar_open_pr_signal(tmp_path: Path):
    """Open-PR author gets +25 when their PR touches the resource."""
    brain = tmp_path / "brain"
    _seed(brain, with_services=False)
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-z",
        "resource_type": "aws_iam_role",
        "resource_id": "deploy-bot",
        "severity": "high",
        "action": "modify",
    }
    open_prs = [
        {"author": "bob@team", "number": 42,
         "files": ["docs/runbooks/auth-deploy.md"],
         "resources": ["aws_iam_role.deploy-bot"]},
    ]
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"invalidation": inv, "open_prs": open_prs},
    )
    result = radar_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    data = json.loads((out / "radar" / "inv-z.json").read_text(encoding="utf-8"))
    bob_row = next((t for t in data["top"] if t["engineer_id"] == "bob"), None)
    assert bob_row is not None
    assert any("PR #42" in r for r in bob_row["reasons"])


def test_targeted_radar_top_n_limit(tmp_path: Path):
    """`top_n` should clip the result list."""
    brain = tmp_path / "brain"
    _seed(brain)
    _git_init_commit(brain, "alice@team", "alice")
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-tn",
        "resource_type": "aws_iam_role",
        "resource_id": "deploy-bot",
        "severity": "high",
        "action": "modify",
    }
    cfg = RoutineConfig(brain_root=brain, out_dir=out,
                        extra={"invalidation": inv, "top_n": 1})
    result = radar_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    data = json.loads((out / "radar" / "inv-tn.json").read_text(encoding="utf-8"))
    assert len(data["top"]) <= 1


def test_targeted_radar_runs_via_runner(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed(brain)
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-r",
        "resource_type": "aws_iam_role",
        "resource_id": "deploy-bot",
        "severity": "high",
        "action": "modify",
    }
    cfg = RoutineConfig(brain_root=brain, out_dir=out, extra={"invalidation": inv})
    result = run_routine("targeted_radar", cfg)
    assert result.name == "targeted_radar"
    assert result.status == OK
