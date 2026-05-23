"""Tests for the v0.10 ``invalidation_digest`` routine."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date
from pathlib import Path

from vigil.agent import RoutineConfig
from vigil.agent.base import OK, WARN
from vigil.agent.invalidation_digest import run as digest_run
from vigil.agent.runner import run_routine


def _seed_brain_with_people(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "people.md").write_text(
        "# People\n\n"
        "- alice <alice@team> — Auth Service owner\n"
        "- bob <bob@team> — Platform team\n",
        encoding="utf-8",
    )
    (root / "docs" / "runbooks").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "runbooks" / "auth-deploy.md").write_text(
        "# Auth deploy runbook\n\nUses aws_vpc.shared (vpc-abc12345) "
        "and aws_iam_role.deploy-bot for the deploy.\n",
        encoding="utf-8",
    )


def _git_init_with_commit(root: Path, email: str = "alice@team", name: str = "alice") -> None:
    """Run a tiny git init + commit so `git log --author` returns the file."""
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", email], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", name], cwd=str(root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=str(root),
        check=True,
        env={**os.environ, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email},
    )


def _write_event(invalidations_root: Path, *, resource_type: str, resource_id: str,
                 severity: str = "high", action: str = "detach") -> Path:
    import datetime as _dt
    ts = _dt.datetime.now(_dt.UTC)
    target_dir = (
        invalidations_root / "invalidations"
        / f"{ts.year:04d}" / f"{ts.month:02d}" / f"{ts.day:02d}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{resource_id}-{action}.json"
    target.write_text(
        json.dumps({
            "id": f"id-{resource_id}",
            "timestamp": ts.isoformat(timespec="seconds"),
            "source": "test",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "action": action,
            "severity": severity,
            "actor": "test",
            "metadata": {},
        }),
        encoding="utf-8",
    )
    return target


def test_invalidation_digest_runs_via_runner(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain_with_people(brain)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = run_routine("invalidation_digest", cfg)
    assert result.name == "invalidation_digest"
    assert result.status in {OK, WARN}


def test_invalidation_digest_warn_when_no_people(tmp_path: Path):
    brain = tmp_path / "brain"
    (brain / "docs").mkdir(parents=True)
    (brain / "knowledge").mkdir(parents=True)
    cfg = RoutineConfig(
        brain_root=brain, out_dir=tmp_path / "out",
        extra={"invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = digest_run(cfg, today=date(2026, 5, 9))
    assert result.status == WARN
    assert "no engineers" in result.summary.lower()
    assert any("no-engineers" in str(p) for p in result.artifacts)


def test_invalidation_digest_writes_personal_digest(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain_with_people(brain)
    _git_init_with_commit(brain)
    inv_root = tmp_path / "brain-invalidations"
    _write_event(inv_root, resource_type="aws_vpc", resource_id="vpc-abc12345",
                 severity="high", action="detach")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"invalidations_root": str(inv_root)},
    )
    result = digest_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    digest_files = list((out / "digests").glob("alice-*.md"))
    assert digest_files, "alice should get a personal digest"
    body = digest_files[0].read_text(encoding="utf-8")
    assert "vpc-abc12345" in body or "aws_vpc.vpc-abc12345" in body
    assert "auth-deploy.md" in body


def test_invalidation_digest_skips_engineer_with_no_matches(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain_with_people(brain)
    _git_init_with_commit(brain, email="alice@team")
    inv_root = tmp_path / "brain-invalidations"
    # Event for a resource the runbook doesn't reference.
    _write_event(inv_root, resource_type="aws_s3_bucket",
                 resource_id="some-other-bucket-id", severity="high")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"invalidations_root": str(inv_root)},
    )
    result = digest_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    # No engineer-keyed digest, but a breadcrumb empty file is left.
    assert not list((out / "digests").glob("alice-*.md"))
    assert list((out / "digests").glob("_empty-*.md"))


def test_invalidation_digest_no_invalidations_repo(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain_with_people(brain)
    _git_init_with_commit(brain)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"invalidations_root": str(tmp_path / "no-such-dir")},
    )
    result = digest_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    # No events → no engineer-specific digest, breadcrumb only.
    assert list((out / "digests").glob("_empty-*.md"))


def test_invalidation_digest_severity_in_summary(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain_with_people(brain)
    _git_init_with_commit(brain)
    inv_root = tmp_path / "brain-invalidations"
    _write_event(inv_root, resource_type="aws_iam_role",
                 resource_id="deploy-bot", severity="medium", action="modify")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"invalidations_root": str(inv_root)},
    )
    result = digest_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    digest_files = list((out / "digests").glob("alice-*.md"))
    if digest_files:
        body = digest_files[0].read_text(encoding="utf-8")
        # Suggested action for MEDIUM is the "skim" line.
        assert "MEDIUM" in body
        assert "Skim" in body or "stale" in body.lower()


def test_invalidation_digest_extra_recency_window(tmp_path: Path):
    """Tight window must drop older events."""
    brain = tmp_path / "brain"
    _seed_brain_with_people(brain)
    _git_init_with_commit(brain)
    inv_root = tmp_path / "brain-invalidations"
    # Event from "now" — but the routine is asked for the last 0h window.
    _write_event(inv_root, resource_type="aws_vpc", resource_id="vpc-abc12345",
                 severity="high")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"recency_hours": 0, "invalidations_root": str(inv_root)},
    )
    result = digest_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    # 0h window: no events match → no engineer file.
    assert not list((out / "digests").glob("alice-*.md"))


def test_invalidation_digest_summary_mentions_event_count(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain_with_people(brain)
    _git_init_with_commit(brain)
    inv_root = tmp_path / "brain-invalidations"
    _write_event(inv_root, resource_type="aws_vpc", resource_id="vpc-abc12345")
    _write_event(inv_root, resource_type="aws_iam_role", resource_id="deploy-bot")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"invalidations_root": str(inv_root)},
    )
    result = digest_run(cfg, today=date(2026, 5, 9))
    # 2 events should be reflected in summary.
    assert "2 event" in result.summary
