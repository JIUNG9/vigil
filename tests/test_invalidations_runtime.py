"""Tests for ``teammate.invalidations`` — runtime banner + chunk matching (v0.9)."""

from __future__ import annotations

import datetime as _dt
from datetime import timedelta
from pathlib import Path

from teammate.impact import (
    InvalidationEvent,
    _clear_cache,
    emit,
)
from teammate.invalidations import (
    extract_resource_ids,
    find_invalidations_for_chunks,
    render_banner,
)

# ---------- helpers ----------


class _FakeHit:
    """Mimic ``rag.ask.Hit`` — anything with ``.path`` + ``.text`` works."""

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


# ---------- extract_resource_ids ----------


def test_extract_vpc_id_match():
    out = extract_resource_ids("see vpc-abc12345 for details")
    assert "vpc-abc12345" in out


def test_extract_does_not_match_english_words():
    """Word-boundary anchoring keeps "i-think" / "vpc-tutorial" out."""
    out = extract_resource_ids("i-think we should use a vpc-tutorial-2 example")
    # i-think isn't 8+ hex chars; vpc-tutorial fails the hex requirement.
    assert "i-think" not in out
    assert "vpc-tutorial" not in out


def test_extract_terraform_address():
    out = extract_resource_ids(
        "module references aws_iam_role.deploy-bot for the policy"
    )
    assert "aws_iam_role.deploy-bot" in out


def test_extract_arn():
    text = "arn:aws:s3:::my-bucket plus arn:aws:iam::123456789012:role/foo"
    out = extract_resource_ids(text)
    assert any("arn:aws:s3" in m for m in out)
    assert any("arn:aws:iam" in m for m in out)


def test_extract_subnet_and_sg():
    out = extract_resource_ids("subnet-0123abcd attached to sg-deadbeef00")
    assert "subnet-0123abcd" in out
    assert "sg-deadbeef00" in out


def test_extract_empty_text_returns_empty_set():
    assert extract_resource_ids("") == set()
    assert extract_resource_ids("no infra here") == set()


def test_extract_multiple_distinct_ids():
    text = (
        "the runbook touches vpc-12345678 and i-87654321 plus aws_db_instance.primary"
    )
    out = extract_resource_ids(text)
    assert {"vpc-12345678", "i-87654321", "aws_db_instance.primary"} <= out


# ---------- find_invalidations_for_chunks ----------


def test_find_invalidations_matches_by_resource_id(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-abc12345", "modify", "high",
         invalidations_root=inv_root)
    chunks = [_FakeHit("docs/runbooks/auth.md",
                       "uses vpc-abc12345 to talk to RDS")]
    _clear_cache()
    out = find_invalidations_for_chunks(chunks, inv_root, since=timedelta(hours=1))
    assert "docs/runbooks/auth.md" in out
    assert out["docs/runbooks/auth.md"][0].resource_id == "vpc-abc12345"
    _clear_cache()


def test_find_invalidations_matches_full_terraform_address(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "aws_iam_role.deploy-bot", "delete", "critical",
         invalidations_root=inv_root)
    chunks = [_FakeHit("docs/auth.md",
                       "the bot is aws_iam_role.deploy-bot")]
    _clear_cache()
    out = find_invalidations_for_chunks(chunks, inv_root, since=timedelta(hours=1))
    assert out
    assert out["docs/auth.md"][0].resource_id == "deploy-bot"
    _clear_cache()


def test_find_invalidations_returns_empty_when_no_match(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-abcdef01", "modify", "high",
         invalidations_root=inv_root)
    chunks = [_FakeHit("docs/auth.md", "no resources mentioned here")]
    _clear_cache()
    out = find_invalidations_for_chunks(chunks, inv_root, since=timedelta(hours=1))
    assert out == {}
    _clear_cache()


def test_find_invalidations_empty_chunks(tmp_path: Path):
    inv_root = tmp_path / "inv"
    out = find_invalidations_for_chunks([], inv_root)
    assert out == {}


def test_find_invalidations_no_root(tmp_path: Path):
    chunks = [_FakeHit("docs/x.md", "vpc-abc12345")]
    out = find_invalidations_for_chunks(
        chunks, tmp_path / "missing", since=timedelta(hours=1)
    )
    assert out == {}


def test_find_invalidations_respects_since_window(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-abc12345", "modify", "high",
         invalidations_root=inv_root)
    chunks = [_FakeHit("docs/x.md", "vpc-abc12345")]
    _clear_cache()
    # Negative window → nothing in scope.
    out = find_invalidations_for_chunks(
        chunks, inv_root, since=timedelta(seconds=-1)
    )
    assert out == {}
    _clear_cache()


def test_find_invalidations_works_with_dict_chunks(tmp_path: Path):
    brain = tmp_path / "brain"
    brain.mkdir()
    inv_root = tmp_path / "inv"
    emit(brain, "vpc-12345678", "modify", "high",
         invalidations_root=inv_root)
    chunks = [{"path": "docs/y.md", "text": "vpc-12345678 detail"}]
    _clear_cache()
    out = find_invalidations_for_chunks(chunks, inv_root, since=timedelta(hours=1))
    assert "docs/y.md" in out
    _clear_cache()


# ---------- render_banner ----------


def _sample_event(severity: str = "high",
                  resource_type: str = "aws_vpc",
                  resource_id: str = "shared",
                  action: str = "detach") -> InvalidationEvent:
    return InvalidationEvent(
        id="x" * 32,
        timestamp=_now().isoformat(timespec="seconds"),
        source="terraform",
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        severity=severity,
        actor="alice",
        metadata={},
    )


def test_render_banner_has_required_lines():
    matches = {"docs/runbooks/auth-deploy.md": [_sample_event()]}
    body = render_banner(matches, show_severity="high")
    assert "This answer references resources with recent infra changes" in body
    assert "aws_vpc.shared" in body
    assert "docs/runbooks/auth-deploy.md" in body
    assert "HIGH" in body
    assert "Source: brain-invalidations log" in body


def test_render_banner_skips_low_severity_when_show_high():
    matches = {"docs/x.md": [_sample_event(severity="low")]}
    body = render_banner(matches, show_severity="high")
    assert body == ""


def test_render_banner_includes_low_when_show_low():
    matches = {"docs/x.md": [_sample_event(severity="low")]}
    body = render_banner(matches, show_severity="low")
    assert "LOW" in body
    assert "docs/x.md" in body


def test_render_banner_orders_critical_first():
    matches = {
        "docs/x.md": [_sample_event(severity="medium", resource_id="med")],
        "docs/y.md": [_sample_event(severity="critical", resource_id="crit")],
    }
    body = render_banner(matches, show_severity="medium")
    crit_pos = body.find("crit")
    med_pos = body.find("med")
    assert crit_pos != -1 and med_pos != -1
    assert crit_pos < med_pos


def test_render_banner_empty_matches_returns_empty():
    assert render_banner({}, show_severity="high") == ""


def test_render_banner_emits_one_row_per_page(tmp_path: Path):
    """Same event affecting two pages renders both lines."""
    ev = _sample_event(severity="high")
    matches = {
        "docs/runbooks/auth-deploy.md": [ev],
        "docs/runbooks/deploy-permissions.md": [ev],
    }
    body = render_banner(matches, show_severity="high")
    assert body.count("affecting docs/runbooks/") == 2
