"""Tests for the Brain (read-only view over a team-brain repo)."""

from __future__ import annotations

from pathlib import Path

from teammate.brain import Brain, BrainEntry


def test_brain_exists_when_claude_md_present(populated_brain: Path):
    assert Brain(populated_brain).exists()


def test_brain_does_not_exist_in_empty_dir(tmp_path: Path):
    assert not Brain(tmp_path).exists()


def test_iter_markdown_yields_seeded_files(populated_brain: Path):
    brain = Brain(populated_brain)
    relpaths = {e.relpath for e in brain.iter_markdown()}
    assert "CLAUDE.md" in relpaths
    assert ".claude/skills/example-skill/SKILL.md" in relpaths
    assert ".claude/rules/commit.md" in relpaths
    assert "docs/onboarding/README.md" in relpaths
    assert "knowledge/people.md" in relpaths


def test_classify_section(populated_brain: Path):
    brain = Brain(populated_brain)
    by_section: dict[str, list[BrainEntry]] = {}
    for e in brain.iter_markdown():
        by_section.setdefault(e.section, []).append(e)
    assert any(e.relpath == "CLAUDE.md" for e in by_section.get("claude", []))
    assert any(".claude/skills/" in e.relpath for e in by_section.get("skills", []))
    assert any(".claude/rules/" in e.relpath for e in by_section.get("rules", []))
    assert any(e.relpath.startswith("docs/") for e in by_section.get("docs", []))
    assert any(e.relpath.startswith("knowledge/") for e in by_section.get("knowledge", []))


def test_stats_returns_per_section_counts(populated_brain: Path):
    s = Brain(populated_brain).stats()
    assert s["total"] >= 5
    assert s["claude"] == 1
    assert s["skills"] >= 1


def test_skips_vendor_dirs(populated_brain: Path):
    """Markdown files inside .git/ etc. should not be enumerated."""
    (populated_brain / ".git").mkdir()
    (populated_brain / ".git" / "should-be-skipped.md").write_text("# nope\n")
    (populated_brain / "node_modules").mkdir()
    (populated_brain / "node_modules" / "also-skip.md").write_text("# nope\n")
    relpaths = {e.relpath for e in Brain(populated_brain).iter_markdown()}
    assert ".git/should-be-skipped.md" not in relpaths
    assert "node_modules/also-skip.md" not in relpaths


def test_frontmatter_parsing(populated_brain: Path):
    """Files with YAML frontmatter expose it via .frontmatter."""
    skill = populated_brain / ".claude" / "skills" / "example-skill" / "SKILL.md"
    brain = Brain(populated_brain)
    entry = next(e for e in brain.iter_markdown() if e.path == skill.resolve())
    assert "name" in entry.frontmatter
    assert entry.frontmatter["name"] == "example-skill"


def test_title_falls_back_to_first_h1(populated_brain: Path):
    """Files without a title in frontmatter should use the first H1 in the body."""
    p = populated_brain / "knowledge" / "people.md"
    brain = Brain(populated_brain)
    entry = next(e for e in brain.iter_markdown() if e.path == p.resolve())
    assert entry.title.startswith("People")
