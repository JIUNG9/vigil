"""Tests for `teammate adopt` — mid-project file migration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from vigil.adopt import (
    ADD,
    KEEP,
    MOVE_SUGGESTED,
    REVIEW,
    SKIP_PER_ENGINEER,
    AdoptPlan,
    adopt,
)
from vigil.cli import main as cli_main

# ---------- helpers ----------


def _git_init(root: Path) -> None:
    """Initialize a clean git repo so the cleanliness gate passes.

    Stages whatever is already on disk so the working tree is clean.
    """
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@acme-corp.com",
         "commit", "--allow-empty", "-m", "init"],
        cwd=root, check=True, capture_output=True,
    )


def _git_dirty(root: Path) -> None:
    """Make the repo dirty by adding an untracked file."""
    (root / "scratch.md").write_text("# scratch\n", encoding="utf-8")


def _entries_for(plan: AdoptPlan, action: str) -> list[str]:
    return [e.path for e in plan.by_action(action)]


# ---------- discovery / classification ----------


def test_adopt_dry_run_on_empty_dir_returns_plan(tmp_path: Path):
    plan = adopt(tmp_path, dry_run=True)
    # Every bundled template file should be ADD.
    add_paths = _entries_for(plan, ADD)
    assert "CLAUDE.md" in add_paths
    assert any(p.startswith(".claude/skills/") for p in add_paths)
    assert any(p.startswith("docs/") for p in add_paths)


def test_adopt_classifies_existing_claude_md_as_keep(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True)
    assert "CLAUDE.md" in _entries_for(plan, KEEP)
    assert "CLAUDE.md" not in _entries_for(plan, ADD)


def test_adopt_classifies_canonical_skill_as_keep(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    skill = tmp_path / ".claude" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\n---\n# Demo\n", encoding="utf-8"
    )
    plan = adopt(tmp_path, dry_run=True)
    assert ".claude/skills/demo/SKILL.md" in _entries_for(plan, KEEP)


def test_adopt_classifies_wiki_md_as_move_suggested(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "payments.md").write_text("# Payments runbook\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True)
    assert "wiki/payments.md" in _entries_for(plan, MOVE_SUGGESTED)
    suggested = next(e for e in plan.by_action(MOVE_SUGGESTED)
                     if e.path == "wiki/payments.md")
    assert suggested.suggested_target == "docs/payments.md"


def test_adopt_classifies_notes_md_as_move_suggested(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "scratch.md").write_text("# scratch\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True)
    moves = _entries_for(plan, MOVE_SUGGESTED)
    assert "notes/scratch.md" in moves
    suggested = next(e for e in plan.by_action(MOVE_SUGGESTED)
                     if e.path == "notes/scratch.md")
    assert suggested.suggested_target == "knowledge/scratch.md"


def test_adopt_classifies_per_engineer_settings_as_skip(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    plan = adopt(
        tmp_path, dry_run=True, include=[".claude/settings.local.json"],
    )
    assert ".claude/settings.local.json" in _entries_for(plan, SKIP_PER_ENGINEER)


def test_adopt_excludes_default_directories(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    # node_modules should never be walked.
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "README.md").write_text("# noise\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True)
    walked = [e.path for e in plan.entries]
    assert not any("node_modules" in p for p in walked)


def test_adopt_excludes_articles_oss_etc(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / "articles").mkdir()
    (tmp_path / "articles" / "essay.md").write_text("# essay\n", encoding="utf-8")
    (tmp_path / "oss").mkdir()
    (tmp_path / "oss" / "x.md").write_text("# oss\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True)
    walked = [e.path for e in plan.entries]
    assert "articles/essay.md" not in walked
    assert "oss/x.md" not in walked


def test_adopt_user_excludes_extend_defaults(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "page.md").write_text("# w\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True, exclude=["wiki/"])
    walked = [e.path for e in plan.entries]
    assert "wiki/page.md" not in walked


def test_adopt_user_includes_extend_defaults(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "old.md").write_text("# old\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True, include=["legacy/"])
    paths = [e.path for e in plan.entries]
    assert "legacy/old.md" in paths


def test_adopt_root_level_md_classified_as_review(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True, include=["NOTES.md"])
    assert "NOTES.md" in _entries_for(plan, REVIEW)


# ---------- CLAUDE.md split suggestion ----------


def test_adopt_suggests_split_for_oversized_claude_md(tmp_path: Path):
    big_lines = ["# brain", ""]
    for i in range(8):
        big_lines.append(f"## Section {i}")
        big_lines.append("x" * 1100)
        big_lines.append("")
    (tmp_path / "CLAUDE.md").write_text("\n".join(big_lines), encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True, max_claude_md_kb=4)
    assert plan.claude_md_split_suggestion is not None
    assert len(plan.claude_md_split_suggestion) >= 2


def test_adopt_no_split_for_small_claude_md(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# tiny\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True, max_claude_md_kb=4)
    assert plan.claude_md_split_suggestion is None


# ---------- to_markdown ----------


def test_plan_to_markdown_renders_sections(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True)
    md = plan.to_markdown()
    assert "# vigil adopt — migration plan" in md
    assert "Keep — already at template path" in md
    assert "Add — template gap to fill" in md
    assert "`CLAUDE.md`" in md


def test_plan_to_markdown_renders_split_suggestion(tmp_path: Path):
    big_lines = ["# brain"]
    for i in range(8):
        big_lines.append(f"## Section {i}")
        big_lines.append("x" * 1100)
    (tmp_path / "CLAUDE.md").write_text("\n".join(big_lines), encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True, max_claude_md_kb=4)
    md = plan.to_markdown()
    assert "CLAUDE.md split suggestion" in md


# ---------- apply mode ----------


def test_adopt_apply_copies_template_gaps(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# my brain\n", encoding="utf-8")
    _git_init(tmp_path)
    adopt(tmp_path, dry_run=False, apply=True)
    # CLAUDE.md was KEEP — must not be overwritten.
    assert (tmp_path / "CLAUDE.md").read_text() == "# my brain\n"
    # Bundled SKILL.md fills a template gap.
    skill = tmp_path / ".claude" / "skills" / "example-skill" / "SKILL.md"
    assert skill.exists()
    text = skill.read_text(encoding="utf-8")
    assert "vigil_template: true" in text


def test_adopt_apply_writes_migration_md(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    _git_init(tmp_path)
    adopt(tmp_path, dry_run=False, apply=True)
    migration = tmp_path / "MIGRATION.md"
    assert migration.exists()
    assert "migration plan" in migration.read_text().lower()


def test_adopt_apply_refuses_dirty_git(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    _git_init(tmp_path)
    _git_dirty(tmp_path)
    with pytest.raises(RuntimeError, match="uncommitted"):
        adopt(tmp_path, dry_run=False, apply=True)


def test_adopt_apply_proceeds_without_git_dir(tmp_path: Path):
    """No .git → nothing to preserve → apply proceeds."""
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    adopt(tmp_path, dry_run=False, apply=True)
    skill = tmp_path / ".claude" / "skills" / "example-skill" / "SKILL.md"
    assert skill.exists()


def test_adopt_apply_merges_frontmatter_marker(tmp_path: Path):
    """Existing frontmatter keys must survive; only `teammate_template` is added."""
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    adopt(tmp_path, dry_run=False, apply=True)
    skill = tmp_path / ".claude" / "skills" / "example-skill" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    # The bundled SKILL.md already has `name:` in frontmatter.
    assert "name:" in text
    assert "vigil_template: true" in text
    # Should not have two `---` opening markers.
    assert text.count("\n---\n") <= 2


def test_adopt_apply_does_not_move_existing(tmp_path: Path):
    """A file at a non-canonical path stays put under --apply."""
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    src = tmp_path / "wiki" / "old.md"
    src.write_text("# legacy\n", encoding="utf-8")
    _git_init(tmp_path)  # stages everything currently on disk
    adopt(tmp_path, dry_run=False, apply=True)
    assert src.exists()
    # No file appeared at the suggested target.
    assert not (tmp_path / "docs" / "old.md").exists()


# ---------- dry-run is the default ----------


def test_adopt_default_is_dry_run(tmp_path: Path):
    """Calling adopt() with no flags must not write template files."""
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    adopt(tmp_path)
    skill = tmp_path / ".claude" / "skills" / "example-skill" / "SKILL.md"
    assert not skill.exists()


# ---------- CLI integration ----------


def test_cli_adopt_dry_run_writes_plan(tmp_path: Path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["adopt"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "MIGRATION-PLAN.md").exists()


def test_cli_adopt_apply_and_dry_run_conflict(tmp_path: Path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["adopt", "--apply", "--dry-run"])
    assert result.exit_code == 1
    assert "Cannot combine" in result.output


def test_cli_adopt_apply_refuses_dirty_repo(tmp_path: Path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    _git_init(tmp_path)
    _git_dirty(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["adopt", "--apply"])
    assert result.exit_code == 1
    assert "uncommitted" in result.output


def test_cli_adopt_apply_clean_repo_succeeds(tmp_path: Path, monkeypatch):
    # Need bundled template visible. Copy the actual repo's template into
    # a clean fresh dir, then init git there.
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    _git_init(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["adopt", "--apply"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "MIGRATION.md").exists()
    assert (tmp_path / ".claude" / "skills" / "example-skill" / "SKILL.md").exists()


# ---------- adopting a populated brain (integration) ----------


def test_adopt_on_populated_brain_yields_mostly_keep(populated_brain: Path):
    plan = adopt(populated_brain, dry_run=True)
    keeps = _entries_for(plan, KEEP)
    # Every bundled file should be classified as KEEP (the populated_brain
    # IS the bundled template).
    assert "CLAUDE.md" in keeps
    assert any(p.startswith(".claude/skills/") for p in keeps)
    assert any(p.startswith("docs/") for p in keeps)
    assert any(p.startswith("knowledge/") for p in keeps)
    # No ADD entries: the project already has every template path.
    assert _entries_for(plan, ADD) == []


def test_adopt_to_markdown_round_trips_through_file(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "old.md").write_text("# legacy\n", encoding="utf-8")
    plan = adopt(tmp_path, dry_run=True)
    out = tmp_path / "PLAN.md"
    out.write_text(plan.to_markdown(), encoding="utf-8")
    txt = out.read_text(encoding="utf-8")
    assert "wiki/old.md" in txt
    assert "Move suggested" in txt
