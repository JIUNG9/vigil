"""Tests for the v0.10 ``pr_review_assist`` routine."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from vigil.agent import RoutineConfig
from vigil.agent.base import OK, WARN
from vigil.agent.pr_review_assist import run as assist_run
from vigil.agent.runner import run_routine


def _seed_brain(root: Path) -> None:
    (root / "docs" / "runbooks").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "runbooks" / "auth-deploy.md").write_text(
        "# Auth deploy\n\nUses aws_vpc.shared (vpc-abc12345) for VPC.\n",
        encoding="utf-8",
    )
    (root / "docs" / "runbooks" / "billing.md").write_text(
        "# Billing\n\nNo infra references here.\n",
        encoding="utf-8",
    )


def _write_pr_file(root: Path, relpath: str, content: str) -> None:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


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
            "action": "modify",
            "severity": severity,
            "actor": "test",
            "metadata": {},
        }),
        encoding="utf-8",
    )


def test_pr_review_assist_warn_no_files(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"pr_number": 1, "pr_files": [],
               "invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = assist_run(cfg, today=date(2026, 5, 9))
    assert result.status == WARN
    assert "no PR files" in result.summary


def test_pr_review_assist_extracts_resources_and_finds_pages(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    _write_pr_file(
        brain, "infra/vpc.tf",
        'resource "aws_vpc" "shared" {\n  cidr_block = "10.0.0.0/16"\n}\n'
        '# id: vpc-abc12345\n',
    )
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"pr_number": 42, "pr_files": ["infra/vpc.tf"],
               "invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = assist_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    out_path = out / "pr-comments" / "pr-42.md"
    body = out_path.read_text(encoding="utf-8")
    assert "vpc-abc12345" in body
    assert "auth-deploy.md" in body


def test_pr_review_assist_no_resources_in_diff(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    _write_pr_file(brain, "README.md", "# Just docs.\n")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"pr_number": 7, "pr_files": ["README.md"],
               "invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = assist_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    body = (out / "pr-comments" / "pr-7.md").read_text(encoding="utf-8")
    assert "No infra resource" in body or "Nothing to cross-reference" in body


def test_pr_review_assist_resources_via_pr_diff(tmp_path: Path):
    """Resource extraction from pr_diff patches."""
    brain = tmp_path / "brain"
    _seed_brain(brain)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={
            "pr_number": 99,
            "pr_files": [],
            "pr_diff": [
                {"path": "infra/vpc.tf",
                 "patch": "+ # references vpc-abc12345 for the shared vpc\n"},
            ],
            "invalidations_root": str(tmp_path / "brain-invalidations"),
        },
    )
    result = assist_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    body = (out / "pr-comments" / "pr-99.md").read_text(encoding="utf-8")
    assert "vpc-abc12345" in body


def test_pr_review_assist_lists_recent_invalidations(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    _write_pr_file(brain, "infra/vpc.tf", "# vpc-abc12345\n")
    inv_root = tmp_path / "brain-invalidations"
    _write_event(inv_root, resource_type="aws_vpc",
                 resource_id="vpc-abc12345", severity="high")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"pr_number": 11, "pr_files": ["infra/vpc.tf"],
               "invalidations_root": str(inv_root)},
    )
    result = assist_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    body = (out / "pr-comments" / "pr-11.md").read_text(encoding="utf-8")
    assert "Recent invalidations" in body
    assert "HIGH" in body


def test_pr_review_assist_respects_pr_number_in_filename(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    _write_pr_file(brain, "infra/vpc.tf", "# vpc-abc12345\n")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"pr_number": 1234, "pr_files": ["infra/vpc.tf"],
               "invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = assist_run(cfg, today=date(2026, 5, 9))
    assert any("pr-1234.md" in str(p) for p in result.artifacts)


def test_pr_review_assist_summary_counts(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    _write_pr_file(brain, "infra/vpc.tf", "# vpc-abc12345\n")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"pr_number": 5, "pr_files": ["infra/vpc.tf"],
               "invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = assist_run(cfg, today=date(2026, 5, 9))
    assert result.status == OK
    assert "1 resource" in result.summary
    assert "1 affected" in result.summary or "page(s)" in result.summary


def test_pr_review_assist_runs_via_runner(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    _write_pr_file(brain, "infra/vpc.tf", "# vpc-abc12345\n")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=brain, out_dir=out,
        extra={"pr_number": 1, "pr_files": ["infra/vpc.tf"],
               "invalidations_root": str(tmp_path / "brain-invalidations")},
    )
    result = run_routine("pr_review_assist", cfg)
    assert result.name == "pr_review_assist"
    assert result.status == OK
