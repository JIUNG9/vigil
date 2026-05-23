"""Tests for `teammate scaffold` and `teammate init`."""

from __future__ import annotations

from pathlib import Path

from vigil.brain import Brain
from vigil.init import scaffold, step_brain, step_pre_push


def test_scaffold_creates_template_files(tmp_path: Path):
    target = tmp_path / "new-brain"
    result = scaffold(target, team_name="acme-platform")
    assert result["status"] == "ok"
    assert (target / "CLAUDE.md").exists()
    assert (target / ".claude" / "skills" / "example-skill" / "SKILL.md").exists()
    assert (target / "knowledge" / "people.md").exists()


def test_scaffold_substitutes_team_name(tmp_path: Path):
    target = tmp_path / "new-brain"
    scaffold(target, team_name="acme-platform")
    claude_md = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert "acme-platform" in claude_md
    assert "TEAM-NAME" not in claude_md.split("\n", 1)[0]  # not in title line


def test_scaffold_refuses_non_empty_target(tmp_path: Path):
    target = tmp_path / "new-brain"
    target.mkdir()
    (target / "existing.txt").write_text("hi")
    result = scaffold(target)
    assert result["status"] == "failed"
    assert "not empty" in result["detail"]


def test_step_brain_detects_populated_brain(populated_brain: Path):
    result = step_brain(populated_brain)
    assert result["status"] == "ok"
    assert "Brain detected" in result["detail"]


def test_step_brain_fails_on_empty_dir(tmp_path: Path):
    result = step_brain(tmp_path)
    assert result["status"] == "failed"
    assert "No CLAUDE.md found" in result["detail"]


def test_brain_after_scaffold_is_queryable(tmp_path: Path):
    target = tmp_path / "new-brain"
    scaffold(target, team_name="acme")
    brain = Brain(target)
    assert brain.exists()
    stats = brain.stats()
    assert stats["total"] >= 5


# ---------- v0.9 pre-push hook ----------


def test_step_pre_push_skips_when_no_git(tmp_path: Path):
    """No .git/ → skip (don't fail). Engineers run init before git init sometimes."""
    result = step_pre_push(tmp_path)
    assert result["status"] == "skipped"


def test_step_pre_push_installs_into_git_hooks(tmp_path: Path):
    target = tmp_path / "new-brain"
    scaffold(target, team_name="acme")
    (target / ".git" / "hooks").mkdir(parents=True)
    result = step_pre_push(target)
    assert result["status"] == "ok"
    hook_path = target / ".git" / "hooks" / "pre-push"
    assert hook_path.exists()
    assert hook_path.stat().st_mode & 0o111  # executable bit set
