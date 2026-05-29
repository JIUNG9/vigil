"""Tests for the v0.6 adapter pattern (MVP)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from vigil.adapter import (
    ADAPTER_FILENAME,
    Adapter,
    load_adapter,
    starter_adapter_text,
    validate_adapter,
    write_starter_adapter,
)

# ---------- TOML parsing ----------


def test_load_adapter_returns_none_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    (tmp_path / "fake-home").mkdir()
    assert load_adapter(tmp_path) is None


def test_load_adapter_reads_brain_root_only(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    (tmp_path / "fake-home").mkdir()
    (tmp_path / ADAPTER_FILENAME).write_text(
        '[paths]\n"~/notes/runbooks/*.md" = "docs/runbooks/{}"\n'
        '[claude_md]\npersonal_overrides_team = ["My editor config"]\n',
        encoding="utf-8",
    )
    a = load_adapter(tmp_path)
    assert a is not None
    assert a.source == "brain"
    assert a.paths == {"~/notes/runbooks/*.md": "docs/runbooks/{}"}
    assert a.personal_override_sections == ["My editor config"]


def test_load_adapter_reads_home_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    (home / ADAPTER_FILENAME).write_text(
        '[paths]\n"~/wiki/*.md" = "docs/wiki/{}"\n',
        encoding="utf-8",
    )
    a = load_adapter(tmp_path)
    assert a is not None
    assert a.source == "home"
    assert a.paths == {"~/wiki/*.md": "docs/wiki/{}"}


def test_load_adapter_home_overrides_brain(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    (home / ADAPTER_FILENAME).write_text(
        '[paths]\n"~/wiki/*.md" = "docs/wiki/{}"\n'
        '[claude_md]\npersonal_overrides_team = ["personal"]\n',
        encoding="utf-8",
    )
    (tmp_path / ADAPTER_FILENAME).write_text(
        '[paths]\n"~/wiki/*.md" = "docs/legacy/{}"\n'
        '"~/notes/*.md" = "docs/notes/{}"\n'
        '[claude_md]\npersonal_overrides_team = ["team-default"]\n',
        encoding="utf-8",
    )
    a = load_adapter(tmp_path)
    assert a is not None
    assert a.source == "merged"
    # Home wins on collision; brain-only keys survive.
    assert a.paths["~/wiki/*.md"] == "docs/wiki/{}"
    assert a.paths["~/notes/*.md"] == "docs/notes/{}"
    # Home's section list replaces brain's wholesale.
    assert a.personal_override_sections == ["personal"]


def test_load_adapter_home_empty_list_clears_brain(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    # Home explicitly clears the override list.
    (home / ADAPTER_FILENAME).write_text(
        "[claude_md]\npersonal_overrides_team = []\n", encoding="utf-8"
    )
    (tmp_path / ADAPTER_FILENAME).write_text(
        '[claude_md]\npersonal_overrides_team = ["team-default"]\n',
        encoding="utf-8",
    )
    a = load_adapter(tmp_path)
    assert a is not None
    # Empty list at home replaces the brain's list — "home wins outright".
    assert a.personal_override_sections == []


def test_load_adapter_tolerates_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    (tmp_path / ADAPTER_FILENAME).write_text("not = valid = toml ===", encoding="utf-8")
    # Read fails -> returns None as if no file existed.
    assert load_adapter(tmp_path) is None


def test_load_adapter_drops_non_string_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    (tmp_path / ADAPTER_FILENAME).write_text(
        '[paths]\n"~/notes/*.md" = "docs/notes/{}"\n'
        '[claude_md]\npersonal_overrides_team = ["sec1", 7, "sec2"]\n',
        encoding="utf-8",
    )
    a = load_adapter(tmp_path)
    assert a is not None
    assert a.personal_override_sections == ["sec1", "sec2"]


# ---------- path translation ----------


def test_translate_path_basic_match():
    a = Adapter(paths={"~/notes/runbooks/*.md": "docs/runbooks/{}"})
    out = a.translate_path(Path("~/notes/runbooks/auth.md"))
    assert out == Path("docs/runbooks/auth.md")


def test_translate_path_expands_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = Adapter(paths={"~/wiki/*.md": "docs/wiki/{}"})
    abs_path = tmp_path / "wiki" / "ownership.md"
    out = a.translate_path(abs_path)
    assert out == Path("docs/wiki/ownership.md")


def test_translate_path_no_match_returns_none():
    a = Adapter(paths={"~/wiki/*.md": "docs/wiki/{}"})
    assert a.translate_path(Path("/elsewhere/foo.md")) is None


def test_translate_path_first_rule_wins():
    # When BOTH rules match the same single-segment path, the first rule
    # wins by dict-insertion order. (This is the path-translation MVP
    # contract; v0.7 may add explicit priority annotations.)
    a = Adapter(
        paths={
            "~/notes/*.md": "docs/notes/{}",
            "~/notes/*": "docs/everything/{}",
        }
    )
    out = a.translate_path(Path("~/notes/auth.md"))
    assert out == Path("docs/notes/auth.md")


def test_translate_path_glob_does_not_cross_path_separator():
    # ``*`` matches a single path segment — it must NOT eat ``/`` and leak
    # subdirectories into the team-side path. Codex caught this on v0.6
    # review.
    a = Adapter(paths={"~/notes/*.md": "docs/notes/{}"})
    # Direct child of ~/notes → matches, single-segment substitution.
    direct = a.translate_path(Path("~/notes/auth.md"))
    assert direct == Path("docs/notes/auth.md")
    # Nested under ~/notes/runbooks/ → must NOT match the simple glob.
    nested = a.translate_path(Path("~/notes/runbooks/auth.md"))
    assert nested is None


def test_translate_path_rejects_multi_star():
    a = Adapter(paths={"~/*/runbooks/*.md": "docs/runbooks/{}"})
    # MVP doesn't support multi-``*`` patterns; rule is silently skipped.
    assert a.translate_path(Path("~/team/runbooks/x.md")) is None


def test_translate_path_no_star_in_glob():
    a = Adapter(paths={"~/special.md": "docs/special.md"})
    # No-glob rules don't crash; they just never match.
    assert a.translate_path(Path("~/special.md")) is None


# ---------- CLAUDE.md merge ----------


def test_merge_claude_md_team_only_when_no_personal():
    a = Adapter()
    team = "## Onboarding\n\nWelcome.\n"
    out = a.merge_claude_md(team, "")
    assert out.strip() == team.strip()


def test_merge_claude_md_drops_personal_sections_not_in_overrides():
    a = Adapter(personal_override_sections=["My editor config"])
    team = "# Brain\n\n## Onboarding\n\nFollow this.\n\n## Deploy\n\nrunbook.\n"
    personal = "## Onboarding\n\nMy take.\n\n## Random thoughts\n\nignore me.\n"
    merged = a.merge_claude_md(team, personal)
    # Onboarding is NOT in overrides → team wins, personal version dropped.
    assert "Follow this" in merged
    assert "My take" not in merged
    assert "ignore me" not in merged


def test_merge_claude_md_personal_overrides_named_section():
    a = Adapter(personal_override_sections=["Personal preferences"])
    team = (
        "# Brain\n\n## Onboarding\n\nFollow this.\n\n"
        "## Personal preferences\n\nteam default.\n"
    )
    personal = "## Personal preferences\n\nmy way.\n"
    merged = a.merge_claude_md(team, personal)
    assert "my way." in merged
    assert "team default." not in merged
    # Other team sections preserved.
    assert "Follow this." in merged


def test_merge_claude_md_appends_when_team_lacks_section():
    a = Adapter(personal_override_sections=["My editor config"])
    team = "# Brain\n\n## Onboarding\n\nFollow this.\n"
    personal = "## My editor config\n\nuse emacs.\n"
    merged = a.merge_claude_md(team, personal)
    assert "Follow this" in merged
    assert "use emacs" in merged
    # Appended at the end, not mixed into Onboarding.
    onboard_idx = merged.index("Follow this")
    editor_idx = merged.index("use emacs")
    assert onboard_idx < editor_idx


def test_merge_claude_md_preserves_preamble():
    a = Adapter()
    team = "# Brain\n\nIntro paragraph here.\n\n## Onboarding\n\nbody.\n"
    out = a.merge_claude_md(team, "")
    assert "Intro paragraph here." in out
    assert "## Onboarding" in out


# ---------- starter file generation ----------


def test_starter_adapter_text_includes_template():
    text = starter_adapter_text(home=Path("/nonexistent-home"))
    assert "[paths]" in text
    assert "[claude_md]" in text
    assert "personal_overrides_team" in text


def test_starter_adapter_text_surfaces_detected_dirs(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "wiki").mkdir()
    text = starter_adapter_text(home=tmp_path)
    assert "Detected on this laptop" in text
    assert '"~/notes/*.md"' in text
    assert '"~/wiki/*.md"' in text


def test_write_starter_adapter_writes_file(tmp_path):
    target = tmp_path / "x" / ADAPTER_FILENAME
    written = write_starter_adapter(target, home=tmp_path)
    assert written.exists()
    assert written.read_text(encoding="utf-8").startswith("# vigil adapter")


# ---------- validate ----------


def test_validate_adapter_clean_when_files_exist(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "runbook.md").write_text("hi", encoding="utf-8")
    a = Adapter(paths={f"{tmp_path}/notes/*.md": "docs/notes/{}"})
    warnings = validate_adapter(a, home=tmp_path)
    assert warnings == []


def test_validate_adapter_flags_dangling_rule(tmp_path):
    a = Adapter(paths={f"{tmp_path}/missing/*.md": "docs/missing/{}"})
    warnings = validate_adapter(a, home=tmp_path)
    assert len(warnings) == 1
    assert "no candidates on disk" in warnings[0]


def test_validate_adapter_flags_no_glob():
    a = Adapter(paths={"/abs/no-star.md": "docs/no-star.md"})
    warnings = validate_adapter(a)
    assert any("no glob" in w for w in warnings)


# ---------- CLI integration ----------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TEAMMATE_BRAIN_ROOT", str(brain))
    return home, brain


def _run_cli(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "vigil.cli"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_adapter_show_no_config(cli_env):
    home, brain = cli_env
    rc, out, _ = _run_cli(["adapter", "show"], cwd=brain)
    assert rc == 0
    assert "no adapter configured" in out


def test_cli_adapter_show_with_config(cli_env):
    home, brain = cli_env
    (home / ADAPTER_FILENAME).write_text(
        '[paths]\n"~/notes/*.md" = "docs/notes/{}"\n', encoding="utf-8"
    )
    rc, out, _ = _run_cli(["adapter", "show"], cwd=brain)
    assert rc == 0
    assert "[paths]" in out
    assert "~/notes/*.md" in out


def test_cli_adapter_init_writes_to_home(cli_env):
    home, brain = cli_env
    rc, out, _ = _run_cli(["adapter", "init", "--scope", "home"], cwd=brain)
    assert rc == 0
    assert (home / ADAPTER_FILENAME).exists()


def test_cli_adapter_init_refuses_overwrite(cli_env):
    home, _brain = cli_env
    target = home / ADAPTER_FILENAME
    target.write_text("# existing\n", encoding="utf-8")
    rc, _out, err = _run_cli(["adapter", "init", "--scope", "home"], cwd=home)
    assert rc == 1
    assert "already exists" in err


def test_cli_adapter_init_force_overwrites(cli_env):
    home, brain = cli_env
    target = home / ADAPTER_FILENAME
    target.write_text("# existing\n", encoding="utf-8")
    rc, _out, _err = _run_cli(
        ["adapter", "init", "--scope", "home", "--force"], cwd=brain
    )
    assert rc == 0
    assert "vigil adapter" in target.read_text(encoding="utf-8")


def test_cli_adapter_validate_no_config(cli_env):
    _home, brain = cli_env
    rc, out, _ = _run_cli(["adapter", "validate"], cwd=brain)
    assert rc == 0
    assert "nothing to validate" in out
