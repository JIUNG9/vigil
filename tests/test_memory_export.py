"""Tests for `teammate memory-export` — departing-engineer handover."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from click.testing import CliRunner

from vigil.cli import main as cli_main
from vigil.memory_export import export_for_handover, write_handover


def _seed_memory(memory_root: Path) -> None:
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "MEMORY.md").write_text(
        "# Memory\n\n"
        "- we deploy via ArgoCD on every merge\n"
        "- I prefer dark mode\n"
        "- see Linear project ABC for the roadmap\n"
        "- Auth service owner: alice — alice@your-org.com\n"
        "- Metrics live at db01.prod.internal:9090\n"
        "- AWS multi-account hub-spoke since 2024\n"
        "- Old project — as of 2020, deprecated\n",
        encoding="utf-8",
    )


def test_export_excludes_personal_entries(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    plan = export_for_handover(mem, user="alice", today=date(2026, 5, 7))
    texts = [e.text for e in plan.entries]
    assert not any("dark mode" in t for t in texts)


def test_export_includes_team_entries(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    plan = export_for_handover(mem, user="alice", today=date(2026, 5, 7))
    classes = {e.classification for e in plan.entries}
    assert "TEAM_RULE" in classes
    assert "TEAM_FACT" in classes
    assert "REFERENCE" in classes


def test_export_redact_replaces_email(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    plan = export_for_handover(mem, user="alice", today=date(2026, 5, 7), redact=True)
    md = plan.to_markdown()
    # Real-looking email replaced with the placeholder.
    assert "alice@your-org.com" not in md
    assert "alice.dev@acme-corp.com" in md


def test_export_redact_replaces_internal_hostname(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "# m\n\n- Production cluster API at api.cluster.corp:9000\n",
        encoding="utf-8",
    )
    plan = export_for_handover(mem, user="alice", today=date(2026, 5, 7), redact=True)
    md = plan.to_markdown()
    # Real-looking internal hostname replaced with the placeholder.
    assert "api.cluster.corp" not in md
    assert "db01.prod.internal" in md  # placeholder shape


def test_export_no_redact_keeps_verbatim(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "# m\n\n- Auth service owner: alice@your-org.com\n", encoding="utf-8"
    )
    plan = export_for_handover(mem, user="alice", today=date(2026, 5, 7), redact=False)
    md = plan.to_markdown()
    assert "alice@your-org.com" in md


def test_export_since_filter_drops_old_entries(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    plan = export_for_handover(
        mem, user="alice", today=date(2026, 5, 7), since="2023-01-01"
    )
    texts = " ".join(e.text for e in plan.entries)
    # The 2020 entry must be dropped; the 2024 entry stays.
    assert "as of 2020" not in texts
    assert "since 2024" in texts


def test_export_since_keeps_unstamped_entries(tmp_path: Path):
    """Entries with no year stamp are kept by ``--since`` (over-include
    is the right error in a leaving artifact)."""
    mem = tmp_path / "memory"
    _seed_memory(mem)
    plan = export_for_handover(
        mem, user="alice", today=date(2026, 5, 7), since="2025-01-01"
    )
    texts = " ".join(e.text for e in plan.entries)
    # No year stamp on the ArgoCD line.
    assert "ArgoCD" in texts


def test_export_includes_free_form_section(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    plan = export_for_handover(mem, user="alice", today=date(2026, 5, 7))
    md = plan.to_markdown()
    assert "how i worked" in md.lower()


def test_export_includes_user_supplied_notes(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    plan = export_for_handover(
        mem, user="alice", today=date(2026, 5, 7),
        free_form_notes="Talk to bob first.",
    )
    md = plan.to_markdown()
    assert "Talk to bob first." in md


def test_export_filename_includes_user_and_date(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    out_dir = tmp_path / "out"
    plan = export_for_handover(mem, user="alice.bob", today=date(2026, 5, 7))
    out = write_handover(plan, out_dir)
    assert out.name == "HANDOVER-alice.bob-2026-05-07.md"


def test_export_does_not_mutate_memory_root(tmp_path: Path):
    mem = tmp_path / "memory"
    _seed_memory(mem)
    before_text = (mem / "MEMORY.md").read_text(encoding="utf-8")
    plan = export_for_handover(mem, user="alice", today=date(2026, 5, 7))
    write_handover(plan, tmp_path / "out")
    after_text = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert before_text == after_text


# ---------- CLI ----------


def test_cli_memory_export_writes_file(tmp_path: Path, monkeypatch):
    mem = tmp_path / "claude-mem"
    out = tmp_path / "out"
    _seed_memory(mem)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "memory-export",
            "--memory-root", str(mem),
            "--out-dir", str(out),
            "--user", "alice",
        ],
    )
    assert result.exit_code == 0, result.output
    files = list(out.glob("HANDOVER-alice-*.md"))
    assert len(files) == 1


def test_cli_memory_export_no_redact_flag(tmp_path: Path, monkeypatch):
    mem = tmp_path / "claude-mem"
    out = tmp_path / "out"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "# m\n\n- Auth service owner: alice@your-org.com\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "memory-export",
            "--memory-root", str(mem),
            "--out-dir", str(out),
            "--user", "alice",
            "--no-redact",
        ],
    )
    assert result.exit_code == 0, result.output
    files = list(out.glob("HANDOVER-alice-*.md"))
    body = files[0].read_text(encoding="utf-8")
    assert "alice@your-org.com" in body


def test_cli_memory_export_missing_memory_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "nope"
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["memory-export", "--memory-root", str(missing)],
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
