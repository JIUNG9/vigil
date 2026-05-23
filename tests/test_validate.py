"""Tests for `teammate validate` — the read-only shape checker."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from vigil.cli import main as cli_main
from vigil.validate import (
    FAIL,
    PASS,
    WARN,
    validate,
)

# ---------- helpers ----------


def _seed_minimal_brain(root: Path) -> None:
    (root / "CLAUDE.md").write_text("# brain\n\nhello.\n", encoding="utf-8")


def _seed_full_brain(root: Path) -> None:
    """A small but well-formed brain with a few canonical sections."""
    _seed_minimal_brain(root)
    (root / ".claude" / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\n---\n# Demo\n", encoding="utf-8"
    )
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "README.md").write_text("# Docs\n", encoding="utf-8")
    (root / "knowledge").mkdir(exist_ok=True)
    (root / "knowledge" / "people.md").write_text("# People\n", encoding="utf-8")


# ---------- `claude_md_present` ----------


def test_claude_md_present_pass(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["claude_md_present"].status == PASS


def test_claude_md_present_fail_when_missing(tmp_path: Path):
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["claude_md_present"].status == FAIL
    assert report.exit_code == 1
    assert report.overall == FAIL


# ---------- `claude_md_size` ----------


def test_claude_md_size_pass_under_budget(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    report = validate(tmp_path, max_claude_md_kb=4)
    by_name = {c.name: c for c in report.checks}
    assert by_name["claude_md_size"].status == PASS


def test_claude_md_size_warn_over_budget(tmp_path: Path):
    # 6 KB of content, budget 4 KB.
    big = "# brain\n" + ("x" * 6 * 1024) + "\n"
    (tmp_path / "CLAUDE.md").write_text(big, encoding="utf-8")
    report = validate(tmp_path, max_claude_md_kb=4)
    by_name = {c.name: c for c in report.checks}
    assert by_name["claude_md_size"].status == WARN
    # FAIL is absent; WARN should drive overall = WARN, exit 2.
    assert report.overall == WARN
    assert report.exit_code == 2


# ---------- `markdown_link_resolution` ----------


def test_link_resolution_pass(tmp_path: Path):
    _seed_full_brain(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\nSee [docs](docs/README.md).\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["markdown_link_resolution"].status == PASS


def test_link_resolution_fail_on_dangling(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\n[ghost](docs/missing.md)\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["markdown_link_resolution"].status == FAIL


def test_link_resolution_skips_external(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\n[google](https://google.com)\n[anchor](#section)\n"
        "[abs](/etc/hosts)\n[mail](mailto:nobody@acme-corp.com)\n",
        encoding="utf-8",
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["markdown_link_resolution"].status == PASS


def test_link_resolution_skips_images(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\n![alt](does-not-exist.png)\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    # Images are skipped; no link FAIL.
    assert by_name["markdown_link_resolution"].status == PASS


def test_link_resolution_dir_target(tmp_path: Path):
    _seed_full_brain(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\n[docs](docs/)\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["markdown_link_resolution"].status == PASS


def test_link_resolution_skips_inline_code_examples(tmp_path: Path):
    """Documented link syntax inside backticks must not trigger a FAIL."""
    _seed_minimal_brain(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\nLink syntax is `[label](target)`.\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["markdown_link_resolution"].status == PASS


def test_link_resolution_skips_fenced_code_examples(tmp_path: Path):
    """Code blocks shouldn't be scanned for links."""
    _seed_minimal_brain(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\n```\n[ghost](docs/missing.md)\n```\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["markdown_link_resolution"].status == PASS


# ---------- `orphan_files` ----------


def test_orphans_pass_when_all_canonical(tmp_path: Path):
    _seed_full_brain(tmp_path)
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["orphan_files"].status == PASS


def test_orphans_warn_for_unreferenced_markdown(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "stray.md").write_text("# Stray\n", encoding="utf-8")
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["orphan_files"].status == WARN
    assert "stray.md" in by_name["orphan_files"].details["orphans"]


def test_orphans_pass_when_referenced_from_claude_md(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "stray.md").write_text("# Stray\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(
        "# brain\n\n[stray](stray.md)\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["orphan_files"].status == PASS


# ---------- `non_canonical_paths` ----------


def test_non_canonical_warn_on_wiki(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "old.md").write_text("# Old\n", encoding="utf-8")
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["non_canonical_paths"].status == WARN


def test_non_canonical_pass_for_clean_brain(tmp_path: Path):
    _seed_full_brain(tmp_path)
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["non_canonical_paths"].status == PASS


# ---------- `binary_files_in_brain` ----------


def test_binary_files_warn_on_unknown_extension(tmp_path: Path):
    _seed_full_brain(tmp_path)
    (tmp_path / "docs" / "leaked.zip").write_bytes(b"PK\x03\x04")
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["binary_files_in_brain"].status == WARN


def test_binary_files_pass_for_images(tmp_path: Path):
    _seed_full_brain(tmp_path)
    (tmp_path / "docs" / "diagram.png").write_bytes(b"\x89PNG")
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["binary_files_in_brain"].status == PASS


# ---------- `frontmatter_parses` ----------


def test_frontmatter_pass_on_valid_yaml(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "k.md").write_text(
        "---\nowner: alice\n---\n# K\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["frontmatter_parses"].status == PASS


def test_frontmatter_fail_on_unclosed_block(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "k.md").write_text(
        "---\nowner: alice\n# never closes\n", encoding="utf-8"
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["frontmatter_parses"].status == FAIL


def test_frontmatter_fail_on_yaml_error(tmp_path: Path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "k.md").write_text(
        "---\nowner: : : not valid\n  bad indent: [\n---\n# K\n",
        encoding="utf-8",
    )
    report = validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["frontmatter_parses"].status == FAIL


# ---------- exit codes & overall ----------


def test_overall_pass_on_clean_brain(tmp_path: Path):
    _seed_full_brain(tmp_path)
    report = validate(tmp_path)
    assert report.overall == PASS
    assert report.exit_code == 0


def test_overall_fail_beats_warn(tmp_path: Path):
    # Trigger both: missing CLAUDE.md (FAIL) + non-canonical wiki (WARN).
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "x.md").write_text("# x\n", encoding="utf-8")
    report = validate(tmp_path)
    assert report.overall == FAIL
    assert report.exit_code == 1


# ---------- JSON output ----------


def test_to_json_schema(tmp_path: Path):
    _seed_full_brain(tmp_path)
    report = validate(tmp_path)
    payload = json.loads(report.to_json())
    assert payload["overall"] == PASS
    assert payload["exit_code"] == 0
    assert payload["max_claude_md_kb"] == 4
    assert isinstance(payload["checks"], list)
    assert len(payload["checks"]) == 7
    names = {c["name"] for c in payload["checks"]}
    assert {
        "claude_md_present",
        "claude_md_size",
        "markdown_link_resolution",
        "orphan_files",
        "non_canonical_paths",
        "binary_files_in_brain",
        "frontmatter_parses",
    } == names


# ---------- CLI integration ----------


def test_cli_validate_exit_0_on_clean_brain(tmp_path: Path, monkeypatch):
    _seed_full_brain(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["validate"])
    assert result.exit_code == 0


def test_cli_validate_exit_1_on_fail(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["validate"])
    assert result.exit_code == 1


def test_cli_validate_json_emits_valid_json(tmp_path: Path, monkeypatch):
    _seed_full_brain(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["validate", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["overall"] == "PASS"
