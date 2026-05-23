"""Tests for `teammate memory-import`.

The load-bearing safety property: every entry in the draft starts as
SKIP. No matter how the heuristic classifies the entry — no matter how
clean its redaction flags — the checkbox in the rendered draft is
unchecked. Several tests verify that explicitly so the next person to
touch the renderer can't accidentally invert the default.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from click.testing import CliRunner

from vigil.cli import main as cli_main
from vigil.memory_import import (
    PERSONAL,
    REFERENCE,
    TEAM_FACT,
    TEAM_RULE,
    classify,
    discover_memory_files,
    find_redaction_flags,
    harvest_user_memory,
    parse_memory_file,
    write_plan,
)

# ---------- classify ----------


def test_classify_personal_first_person():
    assert classify("I prefer dark mode") == PERSONAL
    assert classify("My role is SRE") == PERSONAL
    assert classify("for me, this is a hard pass") == PERSONAL


def test_classify_team_rule_third_person():
    assert classify("we deploy via ArgoCD on every merge") == TEAM_RULE
    assert classify("Team uses Terraform 1.10+") == TEAM_RULE
    assert classify("convention is conventional commits") == TEAM_RULE


def test_classify_team_fact_concrete():
    assert classify("Auth service owner: alice") == TEAM_FACT
    assert classify("AWS multi-account hub-spoke since 2024") == TEAM_FACT
    assert classify("Production cluster runs Kubernetes 1.33") == TEAM_FACT


def test_classify_reference_pointers():
    assert classify("see Linear project ABC for the roadmap") == REFERENCE
    assert classify("Confluence: page on incident response") == REFERENCE


def test_classify_personal_wins_over_team_marker():
    """`I prefer that we deploy via X` is fundamentally personal."""
    assert classify("I prefer that we deploy via blue/green") == PERSONAL


def test_classify_default_is_personal():
    """When no signal fires, default is PERSONAL — opt-in stays the bar."""
    assert classify("a thing happened today") == PERSONAL
    assert classify("") == PERSONAL


def test_classify_strips_list_markers():
    """Bullets / hyphens shouldn't blunt the heuristic."""
    assert classify("- we deploy via ArgoCD") == TEAM_RULE
    assert classify("* Team uses Terraform") == TEAM_RULE


# ---------- redaction flags ----------


def test_redaction_flags_email():
    flags = find_redaction_flags("contact: alice@your-org.com for access")
    assert any("email" in f for f in flags)


def test_redaction_flags_internal_hostname():
    flags = find_redaction_flags("metrics live at db01.prod.internal:9090")
    assert any("hostname" in f for f in flags)


def test_redaction_flags_employer_pattern():
    flags = find_redaction_flags("we deploy on acme-corp internal infra")
    assert any("employer" in f for f in flags)


def test_redaction_flags_clean_entry():
    """Clean entry: no flags."""
    flags = find_redaction_flags("we deploy via ArgoCD on every merge")
    assert flags == []


# ---------- parse_memory_file ----------


def test_parse_memory_file_skips_headings_and_blanks(tmp_path: Path):
    f = tmp_path / "MEMORY.md"
    f.write_text(
        "# heading\n\n"
        "- we deploy via ArgoCD\n"
        "- I prefer dark mode\n"
        "\n"
        "## subheading\n"
        "team uses Terraform 1.10+\n",
        encoding="utf-8",
    )
    entries = parse_memory_file(f)
    assert len(entries) == 3
    classes = [e.classification for e in entries]
    assert TEAM_RULE in classes
    assert PERSONAL in classes


def test_parse_memory_file_records_line_numbers(tmp_path: Path):
    f = tmp_path / "MEMORY.md"
    f.write_text("# h\n\nfirst entry\nsecond entry\n", encoding="utf-8")
    entries = parse_memory_file(f)
    # Lines 3 and 4 are entries (line 1 = heading, line 2 = blank).
    assert {e.line for e in entries} == {3, 4}


# ---------- discover ----------


def test_discover_finds_memory_md_and_topic_files(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("# top\n", encoding="utf-8")
    (tmp_path / "feedback_writing.md").write_text("# fb\n", encoding="utf-8")
    (tmp_path / "project_oss.md").write_text("# proj\n", encoding="utf-8")
    (tmp_path / "reference_links.md").write_text("# ref\n", encoding="utf-8")
    (tmp_path / "unrelated.md").write_text("# nope\n", encoding="utf-8")
    found = discover_memory_files(tmp_path)
    names = {p.name for p in found}
    assert "MEMORY.md" in names
    assert "feedback_writing.md" in names
    assert "project_oss.md" in names
    assert "reference_links.md" in names
    assert "unrelated.md" not in names


def test_discover_returns_empty_for_missing_root(tmp_path: Path):
    missing = tmp_path / "nope"
    assert discover_memory_files(missing) == []


def test_discover_falls_back_to_claude_code_nested_layout(tmp_path: Path):
    """Real ~/.claude has projects/<id>/memory/MEMORY.md, not MEMORY.md
    at the root. The CLI default points at ~/.claude — we must dig in."""
    claude_root = tmp_path / "claude-home"
    proj = claude_root / "projects" / "-Users-foo-bar"
    mem = proj / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# m\n\n- we deploy via X\n", encoding="utf-8")
    (mem / "feedback_voice.md").write_text("# fb\n", encoding="utf-8")
    found = discover_memory_files(claude_root)
    names = [p.name for p in found]
    assert "MEMORY.md" in names
    assert "feedback_voice.md" in names


def test_harvest_works_from_nested_claude_root(tmp_path: Path):
    """End-to-end: pointing at a Claude Code-shaped ~/.claude root
    surfaces real entries, not a silent empty plan."""
    claude_root = tmp_path / "claude-home"
    brain = tmp_path / "brain"
    proj = claude_root / "projects" / "-Users-foo-bar"
    (proj / "memory").mkdir(parents=True)
    (proj / "memory" / "MEMORY.md").write_text(
        "# m\n\n- we deploy via ArgoCD\n- I prefer dark mode\n",
        encoding="utf-8",
    )
    plan = harvest_user_memory(
        claude_root, brain, user="foo", today=date(2026, 5, 7),
    )
    assert len(plan.entries) == 2  # not zero — discovery worked


# ---------- harvest_user_memory ----------


def _seed_memory(memory_root: Path) -> None:
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "MEMORY.md").write_text(
        "# Memory\n\n"
        "- we deploy via ArgoCD on every merge\n"
        "- I prefer dark mode\n"
        "- see Linear project ABC for the roadmap\n"
        "- Auth service owner: alice — alice@your-org.com\n",
        encoding="utf-8",
    )


def test_harvest_classifies_every_entry(tmp_path: Path):
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    _seed_memory(mem)
    plan = harvest_user_memory(mem, brain, user="alice", today=date(2026, 5, 7))
    classes = {e.classification for e in plan.entries}
    assert TEAM_RULE in classes
    assert PERSONAL in classes
    assert REFERENCE in classes
    assert TEAM_FACT in classes


def test_harvest_force_skip_drops_entries(tmp_path: Path):
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    _seed_memory(mem)
    plan = harvest_user_memory(
        mem, brain, user="alice", today=date(2026, 5, 7),
        force_skip=["dark mode"],
    )
    texts = [e.text for e in plan.entries]
    assert not any("dark mode" in t for t in texts)


# ---------- draft format — REVERSED SAFETY BIAS ----------


def test_draft_every_box_is_unchecked_by_default(tmp_path: Path):
    """The load-bearing test: nothing is auto-imported.

    Even a TEAM_RULE with zero redaction flags must produce ``[ ]``,
    not ``[x]``. Inverting this is the most likely accidental
    regression — pin it here.
    """
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    _seed_memory(mem)
    plan = harvest_user_memory(mem, brain, user="alice", today=date(2026, 5, 7))
    md = plan.to_markdown()
    # Every "IMPORT THIS" line is unchecked.
    import_lines = [line for line in md.splitlines() if "IMPORT THIS" in line]
    assert import_lines, "draft should surface at least one entry"
    for line in import_lines:
        assert "[ ]" in line, f"unchecked default broken: {line}"
        assert "[x]" not in line.lower()


def test_draft_clean_entry_with_no_redaction_flags_still_unchecked(tmp_path: Path):
    """Specifically: a TEAM_RULE entry with zero flags is still SKIP."""
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "# m\n\n- we deploy via ArgoCD on every merge\n", encoding="utf-8"
    )
    plan = harvest_user_memory(mem, brain, user="bob", today=date(2026, 5, 7))
    assert len(plan.entries) == 1
    assert plan.entries[0].redaction_flags == []
    md = plan.to_markdown()
    assert "[ ] IMPORT THIS" in md


def test_draft_lists_redaction_flags_when_present(tmp_path: Path):
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "# m\n\n- contact alice@your-org.com\n", encoding="utf-8"
    )
    plan = harvest_user_memory(mem, brain, user="bob", today=date(2026, 5, 7))
    md = plan.to_markdown()
    assert "Redaction flags" in md
    assert "email" in md


def test_draft_filename_includes_user_and_date(tmp_path: Path):
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    _seed_memory(mem)
    plan = harvest_user_memory(mem, brain, user="alice.bob", today=date(2026, 5, 7))
    out = write_plan(plan)
    assert out.name == "MEMORY-IMPORT-alice.bob-2026-05-07.md"
    assert (brain / "pending-imports").is_dir()


def test_draft_explains_default_skip_in_intro(tmp_path: Path):
    """The intro must make the SKIP-by-default contract obvious."""
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    _seed_memory(mem)
    plan = harvest_user_memory(mem, brain, user="alice", today=date(2026, 5, 7))
    md = plan.to_markdown()
    assert "SKIP" in md
    assert "never auto-imports" in md.lower() or "never auto-import" in md.lower()


def test_draft_section_order(tmp_path: Path):
    """Team buckets first; PERSONAL last (it's the noise bucket)."""
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    _seed_memory(mem)
    plan = harvest_user_memory(mem, brain, user="alice", today=date(2026, 5, 7))
    md = plan.to_markdown()
    rule_pos = md.find("Team rules")
    fact_pos = md.find("Team facts")
    ref_pos = md.find("References")
    pers_pos = md.find("Personal")
    assert rule_pos < fact_pos < ref_pos < pers_pos


# ---------- read-only on ~/.claude ----------


def test_harvest_does_not_mutate_memory_root(tmp_path: Path):
    """Writes happen to brain_root only — never to memory_root."""
    mem = tmp_path / "memory"
    brain = tmp_path / "brain"
    _seed_memory(mem)
    before_files = sorted(p.name for p in mem.iterdir())
    before_text = (mem / "MEMORY.md").read_text(encoding="utf-8")

    plan = harvest_user_memory(mem, brain, user="alice", today=date(2026, 5, 7))
    write_plan(plan)

    after_files = sorted(p.name for p in mem.iterdir())
    after_text = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert before_files == after_files
    assert before_text == after_text


# ---------- CLI ----------


def test_cli_memory_import_default_skip(tmp_path: Path, monkeypatch):
    mem = tmp_path / "claude-mem"
    brain = tmp_path / "brain"
    brain.mkdir()
    _seed_memory(mem)
    monkeypatch.chdir(brain)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["memory-import", "--memory-root", str(mem), "--user", "alice"],
    )
    assert result.exit_code == 0, result.output
    assert "wrote draft" in result.output.lower()
    drafts = list((brain / "pending-imports").glob("MEMORY-IMPORT-alice-*.md"))
    assert len(drafts) == 1
    body = drafts[0].read_text(encoding="utf-8")
    # Every box still unchecked when invoked through the CLI.
    assert re.search(r"\[\s\]\s*IMPORT THIS", body)
    assert "[x]" not in body.lower()


def test_cli_memory_import_missing_memory_root(tmp_path: Path, monkeypatch):
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.chdir(brain)
    missing = tmp_path / "nope"
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["memory-import", "--memory-root", str(missing)],
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
