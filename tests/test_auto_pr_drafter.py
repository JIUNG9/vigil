"""Tests for the v0.10 ``auto_pr_drafter`` routine.

The LLM is mocked — we never call a real backend in tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

from vigil.agent import RoutineConfig
from vigil.agent.auto_pr_drafter import run as drafter_run
from vigil.agent.base import OK, WARN
from vigil.providers.base import LLMProvider


class FakeLLM(LLMProvider):
    """Records prompts and returns a canned answer."""

    def __init__(self, answer: str = "# Rewritten page\n\nNew content.\n") -> None:
        self._answer = answer
        self.prompts: list[str] = []
        self.up = True

    def generate(self, prompt: str, system: str | None = None,
                 *, stream: bool = True) -> Iterator[str]:
        self.prompts.append(prompt)
        yield self._answer

    def is_up(self) -> bool:
        return self.up

    @property
    def model_id(self) -> str:
        return "fake-model"


def _seed(root: Path) -> None:
    (root / "docs" / "runbooks").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "runbooks" / "auth-deploy.md").write_text(
        "# Auth deploy\n\nUses aws_vpc.shared (vpc-abc12345).\n",
        encoding="utf-8",
    )


def test_auto_pr_drafter_warn_no_invalidation(tmp_path: Path):
    _seed(tmp_path)
    out = tmp_path / "agent-out"
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=out, extra={})
    result = drafter_run(cfg, provider=FakeLLM())
    assert result.status == WARN
    assert "no invalidation" in result.summary.lower()


def test_auto_pr_drafter_skips_below_severity_floor(tmp_path: Path):
    _seed(tmp_path)
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-low",
        "resource_type": "aws_vpc",
        "resource_id": "vpc-abc12345",
        "severity": "medium",
        "action": "modify",
        "timestamp": "2026-05-09T00:00:00+00:00",
    }
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=out,
                        extra={"invalidation": inv})
    result = drafter_run(cfg, provider=FakeLLM(), today=date(2026, 5, 9))
    assert result.status == OK
    assert "below floor" in result.summary
    assert result.artifacts == []


def test_auto_pr_drafter_warn_no_provider(tmp_path: Path):
    """When no provider is configured (and none injected), return WARN."""
    _seed(tmp_path)
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-1",
        "resource_type": "aws_vpc",
        "resource_id": "vpc-abc12345",
        "severity": "high",
        "action": "detach",
        "timestamp": "2026-05-09T00:00:00+00:00",
    }
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=out,
                        extra={"invalidation": inv})
    # provider=None and no .teammate config → degrade with WARN.
    result = drafter_run(cfg, provider=None, today=date(2026, 5, 9))
    assert result.status == WARN
    assert "no LLM provider" in result.summary


def test_auto_pr_drafter_writes_draft_with_frontmatter(tmp_path: Path):
    _seed(tmp_path)
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-42",
        "resource_type": "aws_vpc",
        "resource_id": "vpc-abc12345",
        "severity": "high",
        "action": "detach",
        "timestamp": "2026-05-09T00:00:00+00:00",
        "source": "test",
    }
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=out,
                        extra={"invalidation": inv})
    fake = FakeLLM("# Auth deploy (rewritten)\n\nNew content.\n")
    result = drafter_run(cfg, provider=fake, today=date(2026, 5, 9))
    assert result.status == OK
    drafts = list((out / "draft-prs").glob("*.md"))
    assert drafts, "expected at least one draft"
    body = drafts[0].read_text(encoding="utf-8")
    assert "requires_review" in body
    assert "true" in body
    assert "invalidation_id" in body
    assert "inv-42" in body
    assert "original_path" in body
    assert "auth-deploy.md" in body
    # The LLM answer must be present after frontmatter.
    assert "Auth deploy (rewritten)" in body
    # Provider was actually called.
    assert fake.prompts, "the provider should have been invoked"
    assert "vpc-abc12345" in fake.prompts[0]


def test_auto_pr_drafter_no_affected_pages(tmp_path: Path):
    """When no page references the resource, return OK with empty drafts."""
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "unrelated.md").write_text(
        "# Unrelated\n\nNo VPC mentions here.\n", encoding="utf-8",
    )
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-nopage",
        "resource_type": "aws_vpc",
        "resource_id": "vpc-doesnotexist",
        "severity": "high",
        "action": "detach",
        "timestamp": "2026-05-09T00:00:00+00:00",
    }
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=out,
                        extra={"invalidation": inv})
    result = drafter_run(cfg, provider=FakeLLM(), today=date(2026, 5, 9))
    assert result.status == OK
    assert "no affected" in result.summary.lower()


def test_auto_pr_drafter_uses_explicit_affected_pages(tmp_path: Path):
    _seed(tmp_path)
    # Add a second page.
    (tmp_path / "docs" / "second.md").write_text(
        "# Second\n\nReferences vpc-abc12345 too.\n", encoding="utf-8",
    )
    out = tmp_path / "agent-out"
    inv = {
        "id": "inv-explicit",
        "resource_type": "aws_vpc",
        "resource_id": "vpc-abc12345",
        "severity": "high",
        "action": "detach",
        "timestamp": "2026-05-09T00:00:00+00:00",
    }
    cfg = RoutineConfig(
        brain_root=tmp_path, out_dir=out,
        extra={
            "invalidation": inv,
            "affected_pages": ["docs/runbooks/auth-deploy.md"],  # only one
        },
    )
    fake = FakeLLM()
    result = drafter_run(cfg, provider=fake, today=date(2026, 5, 9))
    assert result.status == OK
    assert len(result.artifacts) == 1, "should respect explicit page list"
    assert len(fake.prompts) == 1
