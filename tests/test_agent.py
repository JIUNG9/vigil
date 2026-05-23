"""Tests for the colleague-agent routines.

Three routines, three sub-suites:

  * weekly_digest      — subprocess-driven; we patch the subprocess form
                         to return canned validate / doctor JSON.
  * orphan_triage      — pure imports; runs against a fixture brain.
  * pr_migration_plan  — runs adopt --dry-run, post-filters; verify
                         filtering and rendering.

Plus runner dispatch + CLI wiring.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from click.testing import CliRunner

from vigil.agent import RoutineConfig, RoutineResult
from vigil.agent.base import FAIL, OK, WARN
from vigil.agent.runner import list_routines, run_routine
from vigil.cli import main as cli_main


def _seed_brain(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text("# brain\n\nsee [docs](docs/README.md)\n", encoding="utf-8")
    (root / ".claude" / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "skills" / "demo" / "SKILL.md").write_text(
        "# Demo\n", encoding="utf-8"
    )
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "README.md").write_text("# Docs\n", encoding="utf-8")
    (root / "knowledge").mkdir(exist_ok=True)
    (root / "knowledge" / "people.md").write_text("# People\n", encoding="utf-8")


# ---------- runner ----------


def test_runner_lists_all_routines():
    routines = list_routines()
    # v0.5: weekly_digest, orphan_triage, pr_migration_plan
    # v0.8: + confluence_sync, jira_sync, slack_sync, web_pull
    # v0.10: + invalidation_digest, targeted_radar,
    #         pr_review_assist, auto_pr_drafter
    # v0.11.2: + daily_digest
    for expected in (
        "weekly_digest",
        "orphan_triage",
        "pr_migration_plan",
        "confluence_sync",
        "jira_sync",
        "slack_sync",
        "web_pull",
        "invalidation_digest",
        "targeted_radar",
        "pr_review_assist",
        "auto_pr_drafter",
        "daily_digest",
    ):
        assert expected in routines, f"missing routine: {expected}"
    assert len(routines) == 12


def test_runner_unknown_routine_raises_keyerror(tmp_path: Path):
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=tmp_path / "out")
    try:
        run_routine("does_not_exist", cfg)
    except KeyError as exc:
        assert "does_not_exist" in str(exc)
    else:
        raise AssertionError("expected KeyError for unknown routine")


# ---------- orphan_triage ----------


def test_orphan_triage_no_orphans_clean_brain(tmp_path: Path):
    _seed_brain(tmp_path)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=out)
    result = run_routine("orphan_triage", cfg)
    assert isinstance(result, RoutineResult)
    assert result.status == OK
    assert "no orphans" in result.summary.lower()
    assert len(result.artifacts) == 1
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "Orphan triage" in body


def test_orphan_triage_classifies_runbook_filename(tmp_path: Path):
    _seed_brain(tmp_path)
    (tmp_path / "stray-runbook.md").write_text("# rb\n", encoding="utf-8")
    out = tmp_path / "agent-out"
    from vigil.agent.orphan_triage import run as triage_run
    result = triage_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "stray-runbook.md" in body
    assert "move" in body.lower()
    assert "docs/runbooks/" in body


def test_orphan_triage_classifies_draft_filename(tmp_path: Path):
    _seed_brain(tmp_path)
    (tmp_path / "scratch-notes.md").write_text("# s\n", encoding="utf-8")
    out = tmp_path / "agent-out"
    from vigil.agent.orphan_triage import run as triage_run
    result = triage_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "scratch-notes.md" in body
    assert "archive" in body.lower()


def test_orphan_triage_classifies_decision_filename(tmp_path: Path):
    _seed_brain(tmp_path)
    (tmp_path / "ADR-007.md").write_text("# adr\n", encoding="utf-8")
    out = tmp_path / "agent-out"
    from vigil.agent.orphan_triage import run as triage_run
    result = triage_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "knowledge/decisions" in body


def test_orphan_triage_filename_includes_date(tmp_path: Path):
    _seed_brain(tmp_path)
    out = tmp_path / "agent-out"
    from vigil.agent.orphan_triage import run as triage_run
    result = triage_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    assert result.artifacts[0].name == "orphan-triage-2026-05-07.md"


def test_orphan_triage_proposes_issue_body(tmp_path: Path):
    _seed_brain(tmp_path)
    (tmp_path / "stray.md").write_text("# stray\n", encoding="utf-8")
    out = tmp_path / "agent-out"
    from vigil.agent.orphan_triage import run as triage_run
    result = triage_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    body = result.artifacts[0].read_text(encoding="utf-8")
    # The proposed issue body is rendered in a fenced markdown block.
    assert "Proposed:" in body


def test_orphan_triage_never_mutates_brain(tmp_path: Path):
    _seed_brain(tmp_path)
    (tmp_path / "stray.md").write_text("# stray\n", encoding="utf-8")
    out = tmp_path / "agent-out"
    before = sorted(p.name for p in tmp_path.iterdir())
    from vigil.agent.orphan_triage import run as triage_run
    triage_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    after = sorted(p.name for p in tmp_path.iterdir())
    # `agent-out` is the only addition.
    assert "agent-out" in after
    # Original files unchanged.
    for name in before:
        assert name in after
    assert (tmp_path / "stray.md").read_text(encoding="utf-8") == "# stray\n"


# ---------- weekly_digest ----------


def _patch_subprocess(monkeypatch, validate_payload: dict, doctor_payload: dict, *,
                      validate_rc: int = 0, doctor_rc: int = 0):
    """Replace ``_run_subcommand`` so tests don't shell out."""
    from vigil.agent import weekly_digest as wd

    def fake(args: list[str], cwd: Path, timeout: int = 60):
        if "validate" in args:
            return validate_rc, json.dumps(validate_payload), ""
        if "doctor" in args:
            return doctor_rc, json.dumps(doctor_payload), ""
        return 0, "", ""

    monkeypatch.setattr(wd, "_run_subcommand", fake)


def test_weekly_digest_writes_dated_file(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    _patch_subprocess(
        monkeypatch,
        validate_payload={"overall": "PASS", "exit_code": 0, "checks": []},
        doctor_payload={
            "checks": [
                {"name": "config", "status": "PASS", "summary": "ok"},
                {"name": "brain", "status": "PASS", "summary": "ok"},
            ],
            "exit_code": 0,
        },
    )
    out = tmp_path / "agent-out"
    from vigil.agent.weekly_digest import run as digest_run
    result = digest_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    assert result.status == OK
    assert result.artifacts[0].name == "weekly-digest-2026-05-07.md"


def test_weekly_digest_status_warn_on_validate_warn(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    _patch_subprocess(
        monkeypatch,
        validate_payload={"overall": "WARN", "exit_code": 2, "checks": []},
        doctor_payload={"checks": [], "exit_code": 0},
    )
    out = tmp_path / "agent-out"
    from vigil.agent.weekly_digest import run as digest_run
    result = digest_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    assert result.status == WARN


def test_weekly_digest_status_fail_on_validate_fail(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    _patch_subprocess(
        monkeypatch,
        validate_payload={"overall": "FAIL", "exit_code": 1, "checks": []},
        doctor_payload={"checks": [], "exit_code": 0},
    )
    out = tmp_path / "agent-out"
    from vigil.agent.weekly_digest import run as digest_run
    result = digest_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    assert result.status == FAIL


def test_weekly_digest_oversize_claude_md(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    big = "# brain\n" + ("x" * 6 * 1024) + "\n"
    (tmp_path / "CLAUDE.md").write_text(big, encoding="utf-8")
    _patch_subprocess(
        monkeypatch,
        validate_payload={"overall": "PASS", "exit_code": 0, "checks": []},
        doctor_payload={"checks": [], "exit_code": 0},
    )
    out = tmp_path / "agent-out"
    from vigil.agent.weekly_digest import run as digest_run
    result = digest_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
        max_claude_md_kb=4,
    )
    assert result.status == WARN
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "over budget" in body


def test_weekly_digest_slack_chunk_extractable(tmp_path: Path, monkeypatch):
    """The runner must be able to pull a chunk for Slack."""
    _seed_brain(tmp_path)
    _patch_subprocess(
        monkeypatch,
        validate_payload={"overall": "PASS", "exit_code": 0, "checks": []},
        doctor_payload={"checks": [], "exit_code": 0},
    )
    out = tmp_path / "agent-out"
    from vigil.agent.weekly_digest import extract_slack_chunk
    from vigil.agent.weekly_digest import run as digest_run
    result = digest_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    body = result.artifacts[0].read_text(encoding="utf-8")
    chunk = extract_slack_chunk(body)
    assert chunk is not None
    assert "weekly digest" in chunk.lower()


def test_weekly_digest_handles_no_git(tmp_path: Path, monkeypatch):
    """The digest doesn't crash when ``git log`` fails (no repo)."""
    _seed_brain(tmp_path)
    _patch_subprocess(
        monkeypatch,
        validate_payload={"overall": "PASS", "exit_code": 0, "checks": []},
        doctor_payload={"checks": [], "exit_code": 0},
    )
    out = tmp_path / "agent-out"
    from vigil.agent.weekly_digest import run as digest_run
    result = digest_run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "No git history" in body or "no git history" in body.lower()


def test_weekly_digest_handles_missing_subcommand_output(tmp_path: Path, monkeypatch):
    """Empty stdout from a subprocess shouldn't crash the routine."""
    from vigil.agent import weekly_digest as wd

    def fake(args: list[str], cwd: Path, timeout: int = 60):
        return 1, "", "boom"

    monkeypatch.setattr(wd, "_run_subcommand", fake)
    _seed_brain(tmp_path)
    out = tmp_path / "agent-out"
    result = wd.run(
        RoutineConfig(brain_root=tmp_path, out_dir=out),
        today=date(2026, 5, 7),
    )
    # Couldn't parse either output — status should be WARN (not OK / not FAIL).
    assert result.status == WARN


# ---------- pr_migration_plan ----------


def test_pr_migration_plan_filters_to_pr_files(tmp_path: Path):
    _seed_brain(tmp_path)
    # An orphan we'll add to the PR.
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "old.md").write_text("# old\n", encoding="utf-8")
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=out,
        extra={"pr_number": 42, "pr_files": ["wiki/old.md"]},
    )
    result = run_routine("pr_migration_plan", cfg)
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "PR #42" in body
    assert "wiki/old.md" in body


def test_pr_migration_plan_empty_when_no_overlap(tmp_path: Path):
    _seed_brain(tmp_path)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=out,
        extra={"pr_number": 99, "pr_files": ["src/main.py"]},
    )
    result = run_routine("pr_migration_plan", cfg)
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "No adopt-relevant changes" in body


def test_pr_migration_plan_filename_includes_pr_number(tmp_path: Path):
    _seed_brain(tmp_path)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=out,
        extra={"pr_number": 7, "pr_files": []},
    )
    result = run_routine("pr_migration_plan", cfg)
    assert result.artifacts[0].name == "pr-migration-plan-PR7.md"


def test_pr_migration_plan_handles_missing_pr_files(tmp_path: Path):
    _seed_brain(tmp_path)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=out,
        extra={"pr_number": 0, "pr_files": []},
    )
    result = run_routine("pr_migration_plan", cfg)
    assert result.status == OK


def test_pr_migration_plan_does_not_apply_adopt(tmp_path: Path):
    """Routine must not add template files — it's read-only on the brain."""
    _seed_brain(tmp_path)
    before = sorted(str(p.relative_to(tmp_path))
                    for p in tmp_path.rglob("*") if p.is_file())
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=out,
        extra={"pr_number": 1, "pr_files": ["CLAUDE.md"]},
    )
    run_routine("pr_migration_plan", cfg)
    after = [p for p in tmp_path.rglob("*")
             if p.is_file() and "agent-out" not in str(p.relative_to(tmp_path))]
    after_rel = sorted(str(p.relative_to(tmp_path)) for p in after)
    assert before == after_rel


# ---------- CLI ----------


def test_cli_agent_run_unknown_routine(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["agent", "run", "no_such_routine"])
    assert result.exit_code == 2
    assert "unknown routine" in result.output.lower()


def test_cli_agent_run_orphan_triage_emits_artifact(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    out = tmp_path / "agent-out"
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["agent", "run", "orphan_triage", "--out-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    files = list(out.glob("orphan-triage-*.md"))
    assert len(files) == 1


def test_cli_agent_run_pr_plan_with_pr_files(tmp_path: Path, monkeypatch):
    _seed_brain(tmp_path)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "old.md").write_text("# old\n", encoding="utf-8")
    out = tmp_path / "agent-out"
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "agent", "run", "pr_migration_plan",
            "--out-dir", str(out),
            "--pr-number", "42",
            "--pr-files", "wiki/old.md",
        ],
    )
    assert result.exit_code == 0, result.output
    files = list(out.glob("pr-migration-plan-PR42.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "wiki/old.md" in body
