"""Tests for ``teammate.impact`` and the ``teammate impact`` CLI surface (v0.9)."""

from __future__ import annotations

import datetime as _dt
import json
from datetime import timedelta
from pathlib import Path

from click.testing import CliRunner

from teammate.cli import main as cli_main
from teammate.impact import (
    SEVERITY_LEVELS,
    ImpactReport,
    InvalidationEvent,
    _clear_cache,
    emit,
    find_pages_for_resources,
    preview,
    read_recent_invalidations,
    read_recent_invalidations_cached,
    severity_at_least,
    slugify,
)

# ---------- helpers ----------


def _seed_brain(root: Path, *, runbook: str = "auth-deploy") -> Path:
    """Write a tiny brain that mentions ``aws_vpc.shared`` + ``vpc-abc123`` + ``aws_iam_role``."""
    (root / "docs" / "runbooks").mkdir(parents=True)
    (root / "docs" / "runbooks" / f"{runbook}.md").write_text(
        "# auth deploy\n\nUses aws_vpc.shared (vpc-abc12345) and aws_iam_role.deploy-bot.\n",
        encoding="utf-8",
    )
    (root / "docs" / "unrelated.md").write_text(
        "# unrelated\nThis page mentions nothing infra-y.\n",
        encoding="utf-8",
    )
    (root / "knowledge").mkdir()
    (root / "knowledge" / "people.md").write_text("# people\n", encoding="utf-8")
    return root


# ---------- severity helpers ----------


def test_severity_levels_ordered():
    assert SEVERITY_LEVELS == ("low", "medium", "high", "critical")


def test_severity_at_least_basic():
    assert severity_at_least("high", "high")
    assert severity_at_least("critical", "high")
    assert severity_at_least("medium", "low")


def test_severity_at_least_below_threshold():
    assert not severity_at_least("low", "high")
    assert not severity_at_least("medium", "high")


def test_severity_at_least_unknown_actual_returns_false():
    # An event with a malformed severity must never trip the gate.
    assert not severity_at_least("severe", "low")


def test_slugify_kebab_case():
    assert slugify("aws_vpc.shared") == "aws-vpc-shared"
    assert slugify("My Resource Id!") == "my-resource-id"
    assert slugify("") == "resource"


# ---------- InvalidationEvent dataclass ----------


def test_event_round_trip(tmp_path: Path):
    ev = InvalidationEvent(
        id="abc",
        timestamp="2026-05-09T14:00:00+00:00",
        source="terraform",
        resource_type="aws_vpc",
        resource_id="vpc-abc123",
        action="detach",
        severity="high",
        actor="alice",
        metadata={"plan": "destroy"},
    )
    serialised = ev.to_dict()
    reloaded = InvalidationEvent.from_dict(serialised)
    assert reloaded == ev


def test_event_from_dict_tolerates_missing_keys():
    ev = InvalidationEvent.from_dict({})
    assert ev.severity == "medium"
    assert ev.action == "modify"
    assert ev.source == "unknown"
    assert ev.id  # auto-filled


# ---------- emit ----------


def test_emit_writes_a_file(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "brain-invalidations"

    out = emit(
        brain,
        resource="aws_vpc.shared",
        action="detach",
        severity="high",
        source="manual",
        invalidations_root=inv_root,
        actor="alice",
        metadata={"plan_path": "/tmp/plan"},
    )
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["resource_type"] == "aws_vpc"
    assert data["resource_id"] == "shared"
    assert data["severity"] == "high"
    assert data["action"] == "detach"
    assert data["actor"] == "alice"
    assert data["metadata"]["plan_path"] == "/tmp/plan"


def test_emit_lays_files_under_yyyy_mm_dd(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    out = emit(brain, "vpc-abc12345", "modify", "low",
               invalidations_root=inv_root)
    parts = out.relative_to(inv_root).parts
    # invalidations / YYYY / MM / DD / file.json
    assert parts[0] == "invalidations"
    assert len(parts[1]) == 4  # year
    assert len(parts[2]) == 2  # month
    assert len(parts[3]) == 2  # day
    assert parts[-1].endswith(".json")


def test_emit_rejects_unknown_severity(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    try:
        emit(brain, "vpc-abc12345", "modify", "severe",
             invalidations_root=tmp_path / "inv")
    except ValueError as exc:
        assert "severity" in str(exc).lower()
    else:  # pragma: no cover — defensive
        raise AssertionError("expected ValueError")


def test_emit_records_terraform_state_path(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    state = tmp_path / "terraform.tfstate"
    state.write_text("{}", encoding="utf-8")
    out = emit(brain, "aws_iam_role.bot", "delete", "critical",
               terraform_state_path=state, invalidations_root=inv_root)
    data = json.loads(out.read_text())
    assert data["metadata"]["terraform_state_path"] == str(state)


def test_emit_unique_filenames_on_collision(tmp_path: Path):
    """Two events for the same resource+action+second produce distinct files."""
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    a = emit(brain, "aws_vpc.shared", "detach", "high",
             invalidations_root=inv_root)
    b = emit(brain, "aws_vpc.shared", "detach", "high",
             invalidations_root=inv_root)
    assert a != b
    assert a.exists() and b.exists()


# ---------- find_pages_for_resources ----------


def test_find_pages_matches_terraform_address(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    pages = find_pages_for_resources(brain, ["aws_vpc.shared"])
    assert any("auth-deploy.md" in p["path"] for p in pages)


def test_find_pages_matches_bare_id(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    pages = find_pages_for_resources(brain, ["vpc-abc12345"])
    assert pages and pages[0]["matches"] >= 1


def test_find_pages_empty_when_no_match(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    pages = find_pages_for_resources(brain, ["aws_lambda_function.unused"])
    assert pages == []


# ---------- read_recent_invalidations ----------


def test_read_recent_filters_by_age(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "aws_vpc.shared", "detach", "high",
         invalidations_root=inv_root)
    found = read_recent_invalidations(inv_root, since=timedelta(hours=1))
    assert len(found) == 1
    # And a zero-window slice excludes everything.
    assert read_recent_invalidations(inv_root, since=timedelta(seconds=-1)) == []


def test_read_recent_filters_by_severity(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-abc12345", "modify", "low", invalidations_root=inv_root)
    emit(brain, "aws_iam_role.bot", "delete", "critical",
         invalidations_root=inv_root)

    high_only = read_recent_invalidations(
        inv_root, since=timedelta(hours=1), severity="high"
    )
    assert len(high_only) == 1
    assert high_only[0].severity == "critical"


def test_read_recent_filters_by_resource(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-abc12345", "modify", "low", invalidations_root=inv_root)
    emit(brain, "aws_iam_role.bot", "delete", "critical",
         invalidations_root=inv_root)

    only_vpc = read_recent_invalidations(
        inv_root, since=timedelta(hours=1), resource_filter=["vpc-abc12345"]
    )
    assert len(only_vpc) == 1
    assert only_vpc[0].resource_id == "vpc-abc12345"


def test_read_recent_skips_corrupt_files(tmp_path: Path):
    inv_root = tmp_path / "inv"
    target_dir = (
        inv_root / "invalidations"
        / f"{_dt.datetime.now(_dt.UTC):%Y/%m/%d}"
    )
    target_dir.mkdir(parents=True)
    (target_dir / "garbage.json").write_text("this is not json", encoding="utf-8")
    found = read_recent_invalidations(inv_root, since=timedelta(hours=1))
    assert found == []


def test_read_recent_returns_newest_first(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    a = emit(brain, "vpc-aaaa1111", "modify", "low", invalidations_root=inv_root)
    b = emit(brain, "vpc-bbbb2222", "modify", "low", invalidations_root=inv_root)
    found = read_recent_invalidations(inv_root, since=timedelta(hours=1))
    # Both written; the second event has the larger timestamp string.
    assert len(found) == 2
    assert found[0].timestamp >= found[1].timestamp
    assert {a, b}  # used


# ---------- preview ----------


def test_preview_blocks_on_recent_high(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    inv_root = tmp_path / "inv"
    emit(brain, "aws_vpc.shared", "detach", "high",
         invalidations_root=inv_root)
    report = preview(brain, ["aws_vpc.shared"], invalidations_root=inv_root)
    assert isinstance(report, ImpactReport)
    assert report.block is True
    assert report.recent_invalidations
    assert report.pages


def test_preview_does_not_block_on_recent_low(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    inv_root = tmp_path / "inv"
    emit(brain, "aws_vpc.shared", "modify", "low",
         invalidations_root=inv_root)
    report = preview(brain, ["aws_vpc.shared"], invalidations_root=inv_root)
    assert report.block is False


def test_preview_filters_invalidations_to_touched_resources(tmp_path: Path):
    """Advisor flag J — recent_invalidations is filtered, not global noise."""
    brain = tmp_path / "brain"
    _seed_brain(brain)
    inv_root = tmp_path / "inv"
    emit(brain, "aws_iam_role.bot", "delete", "critical",
         invalidations_root=inv_root)
    emit(brain, "aws_vpc.shared", "detach", "low",
         invalidations_root=inv_root)
    report = preview(brain, ["aws_vpc.shared"], invalidations_root=inv_root)
    # Only the VPC event touches us, the IAM event is filtered out.
    assert all(
        ev["resource_id"] == "shared"
        for ev in report.recent_invalidations
    )
    assert report.block is False


def test_preview_empty_resources_returns_no_block(tmp_path: Path):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    report = preview(brain, [], invalidations_root=tmp_path / "inv")
    assert report.block is False
    assert report.pages == []


# ---------- session cache ----------


def test_read_recent_cached_returns_same_events(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-deadbeef", "modify", "low",
         invalidations_root=inv_root)
    _clear_cache()
    a = read_recent_invalidations_cached(inv_root, since=timedelta(hours=1))
    b = read_recent_invalidations_cached(inv_root, since=timedelta(hours=1))
    assert [ev.id for ev in a] == [ev.id for ev in b]
    _clear_cache()


def test_cache_returns_independent_list(tmp_path: Path):
    """Mutating the returned list must not corrupt the cache."""
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-deadbeef", "modify", "low", invalidations_root=inv_root)
    _clear_cache()
    a = read_recent_invalidations_cached(inv_root, since=timedelta(hours=1))
    a.clear()
    b = read_recent_invalidations_cached(inv_root, since=timedelta(hours=1))
    assert b, "cache returned a shared mutable list"
    _clear_cache()


# ---------- CLI: emit / list / preview ----------


def test_cli_emit_then_list(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    inv_root = tmp_path / "inv"

    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "emit",
        "--resource", "aws_vpc.shared",
        "--action", "detach",
        "--severity", "high",
        "--invalidations-root", str(inv_root),
    ])
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output

    result = runner.invoke(cli_main, [
        "impact", "list",
        "--since", "1h",
        "--invalidations-root", str(inv_root),
    ])
    assert result.exit_code == 0, result.output
    assert "aws_vpc" in result.output or "shared" in result.output


def test_cli_emit_unknown_severity_errors(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "emit",
        "--resource", "vpc-abc12345",
        "--action", "modify",
        "--severity", "severe",
        "--invalidations-root", str(tmp_path / "inv"),
    ])
    # Click's choice validation triggers exit 2.
    assert result.exit_code == 2


def test_cli_preview_blocks_with_exit_2(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    inv_root = tmp_path / "inv"
    emit(brain, "aws_vpc.shared", "detach", "critical",
         invalidations_root=inv_root)
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))

    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "preview",
        "--resource", "aws_vpc.shared",
        "--invalidations-root", str(inv_root),
    ])
    assert result.exit_code == 2, result.output
    assert "BLOCK" in result.output


def test_cli_preview_passes_with_exit_0(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    inv_root = tmp_path / "inv"
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))

    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "preview",
        "--resource", "aws_vpc.shared",
        "--invalidations-root", str(inv_root),
    ])
    # No invalidations exist yet → not blocked.
    assert result.exit_code == 0, result.output


def test_cli_preview_json_emits_valid_json(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    _seed_brain(brain)
    inv_root = tmp_path / "inv"
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "preview",
        "--resource", "aws_vpc.shared",
        "--invalidations-root", str(inv_root),
        "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "pages" in payload and "block" in payload


def test_cli_list_with_severity_filter(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-aaaaaaaa", "modify", "low",
         invalidations_root=inv_root)
    emit(brain, "aws_iam_role.bot", "delete", "critical",
         invalidations_root=inv_root)
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "list",
        "--since", "2h",
        "--severity", "high",
        "--invalidations-root", str(inv_root),
    ])
    assert result.exit_code == 0, result.output
    # Only the critical one shows; vpc-aaaa not shown.
    assert "CRITICAL" in result.output
    assert "vpc-aaaaaaaa" not in result.output


def test_cli_list_invalid_duration_errors(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    # Create the repo so the missing-repo early-out doesn't mask the
    # duration parse error.
    emit(brain, "vpc-deadbeef", "modify", "low", invalidations_root=inv_root)
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "list",
        "--since", "1week",
        "--invalidations-root", str(inv_root),
    ])
    assert result.exit_code != 0


def test_cli_list_no_repo_returns_zero(tmp_path: Path, monkeypatch):
    """Missing repo is normal on a fresh laptop — exit 0, no events."""
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "impact", "list",
        "--since", "1h",
        "--invalidations-root", str(tmp_path / "does-not-exist"),
    ])
    assert result.exit_code == 0
