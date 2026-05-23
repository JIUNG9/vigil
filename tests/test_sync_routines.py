"""Tests for the v0.8 sync routines.

Four routines, each with its own sub-suite. Plus shared-helper tests
for the HTML→markdown converter, the frontmatter (de)serializer, and
the allowlist gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vigil.agent import RoutineConfig
from vigil.agent._sync_common import (
    FetchedPage,
    host_in_allowlist,
    html_to_markdown,
    parse_frontmatter,
    render_frontmatter,
    slugify,
    write_doc,
)
from vigil.agent.confluence_sync import run as confluence_run
from vigil.agent.jira_sync import run as jira_run
from vigil.agent.runner import run_routine
from vigil.agent.slack_sync import run as slack_run
from vigil.agent.web_pull import run as web_run

# ---------- shared helpers ----------


def test_html_to_markdown_headings_paragraphs():
    out = html_to_markdown("<h1>Title</h1><p>Body paragraph.</p>")
    assert "# Title" in out
    assert "Body paragraph." in out


def test_html_to_markdown_lists_links_bold():
    html = (
        "<ul><li>first</li><li><b>bold</b> item</li></ul>"
        '<p>see <a href="https://example.com/x">docs</a></p>'
    )
    out = html_to_markdown(html)
    assert "- first" in out
    assert "**bold**" in out
    assert "[docs](https://example.com/x)" in out


def test_html_to_markdown_strips_scripts():
    html = "<script>alert(1)</script><p>visible</p>"
    out = html_to_markdown(html)
    assert "alert" not in out
    assert "visible" in out


def test_html_to_markdown_handles_empty_string():
    assert html_to_markdown("") == ""


def test_render_and_parse_frontmatter_roundtrip():
    meta = {"source_url": "https://example.com/x", "last_synced": "2026-05-09T00:00:00Z"}
    body = render_frontmatter(meta)
    parsed = parse_frontmatter(body + "\nfoo\n")
    assert parsed["source_url"] == "https://example.com/x"
    assert parsed["last_synced"] == "2026-05-09T00:00:00Z"


def test_render_frontmatter_keys_sorted():
    meta = {"z": "last", "a": "first", "m": "middle"}
    rendered = render_frontmatter(meta)
    # Ensure deterministic key ordering for re-sync diffs.
    a_idx = rendered.index("a:")
    m_idx = rendered.index("m:")
    z_idx = rendered.index("z:")
    assert a_idx < m_idx < z_idx


def test_host_in_allowlist_default_deny_on_empty():
    assert host_in_allowlist("https://docs.aws.amazon.com/x", []) is False


def test_host_in_allowlist_suffix_match():
    allowed = ["aws.amazon.com"]
    assert host_in_allowlist("https://docs.aws.amazon.com/x", allowed) is True
    assert host_in_allowlist("https://aws.amazon.com/", allowed) is True
    # Suffix match must require a dot boundary — never substring.
    assert host_in_allowlist("https://evil.aws.amazon.com.attacker/", allowed) is False


def test_host_in_allowlist_rejects_unknown_host():
    assert host_in_allowlist("https://evil.example/", ["docs.aws.amazon.com"]) is False


def test_slugify_basic():
    assert slugify("My Page Title") == "my-page-title"
    assert slugify("ADR-007") == "adr-007"
    assert slugify("") == "page"


def test_write_doc_dedup_preserves_mtime(tmp_path: Path):
    target = tmp_path / "imports" / "x.md"
    meta = {"source_url": "https://x.example/y", "rev": "abc"}
    write_doc(target, frontmatter=meta, body="# X\n", revision_key="rev")
    assert target.is_file()
    mtime_first = target.stat().st_mtime_ns

    # Re-sync with same revision: must not rewrite.
    path, wrote = write_doc(target, frontmatter=meta, body="# X\n", revision_key="rev")
    assert wrote is False
    assert path == target
    assert target.stat().st_mtime_ns == mtime_first


def test_write_doc_rewrites_on_revision_change(tmp_path: Path):
    target = tmp_path / "imports" / "x.md"
    write_doc(
        target,
        frontmatter={"source_url": "https://x.example/y", "rev": "v1"},
        body="# X\n",
        revision_key="rev",
    )
    _, wrote = write_doc(
        target,
        frontmatter={"source_url": "https://x.example/y", "rev": "v2"},
        body="# X v2\n",
        revision_key="rev",
    )
    assert wrote is True
    assert "v2" in target.read_text(encoding="utf-8")


# ---------- confluence_sync ----------


def test_confluence_sync_no_pages(tmp_path: Path):
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=tmp_path / "out", extra={})
    result = confluence_run(cfg)
    assert result.status == "ok"
    assert "no pages" in result.summary.lower()
    assert result.artifacts == []


def test_confluence_sync_writes_page_with_frontmatter(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "pages": [
                {
                    "space": "ENG",
                    "title": "Deploy Runbook",
                    "url": "https://acme-corp.atlassian.net/wiki/spaces/ENG/pages/1",
                    "body": "<h1>Deploy</h1><p>Step one</p>",
                    "revision": "v3",
                }
            ]
        },
    )
    result = confluence_run(cfg)
    assert result.status == "ok"
    assert len(result.artifacts) == 1
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "Deploy Runbook" in body
    assert "Step one" in body
    assert 'confluence_revision: "v3"' in body
    # Path must reflect space + slug
    assert "confluence-imports/eng/deploy-runbook.md" in str(result.artifacts[0])


def test_confluence_sync_dedup_skips_unchanged(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "pages": [
                {
                    "space": "OPS",
                    "title": "Pager",
                    "url": "https://x/y",
                    "body": "<p>Initial</p>",
                    "revision": "v1",
                }
            ]
        },
    )
    confluence_run(cfg)
    artifact = (tmp_path / "out" / "confluence-imports" / "ops" / "pager.md")
    mtime_first = artifact.stat().st_mtime_ns

    # Same revision = no rewrite
    result = confluence_run(cfg)
    assert result.status == "ok"
    assert artifact.stat().st_mtime_ns == mtime_first


def test_confluence_sync_rewrites_on_new_revision(tmp_path: Path):
    out = tmp_path / "out"
    cfg_v1 = RoutineConfig(
        brain_root=tmp_path,
        out_dir=out,
        extra={
            "pages": [
                {"space": "OPS", "title": "Pager", "url": "https://x/y", "body": "<p>v1</p>", "revision": "v1"}
            ]
        },
    )
    confluence_run(cfg_v1)
    cfg_v2 = RoutineConfig(
        brain_root=tmp_path,
        out_dir=out,
        extra={
            "pages": [
                {"space": "OPS", "title": "Pager", "url": "https://x/y", "body": "<p>v2 body</p>", "revision": "v2"}
            ]
        },
    )
    confluence_run(cfg_v2)
    body = (out / "confluence-imports" / "ops" / "pager.md").read_text(encoding="utf-8")
    assert "v2 body" in body
    assert 'confluence_revision: "v2"' in body


def test_confluence_sync_uses_injected_fetcher(tmp_path: Path):
    calls: list[str] = []

    def fake(url: str) -> FetchedPage:
        calls.append(url)
        return FetchedPage(url=url, status=200, body="<h1>From fetcher</h1>", content_type="text/html")

    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={"pages": [{"space": "ENG", "title": "Fetched", "url": "https://acme/p"}]},
    )
    result = confluence_run(cfg, fetcher=fake)
    assert calls == ["https://acme/p"]
    assert result.status == "ok"
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "From fetcher" in body


def test_confluence_sync_warns_on_fetch_error(tmp_path: Path):
    def bad(url: str) -> FetchedPage:
        return FetchedPage(url=url, status=500, body="boom")

    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={"pages": [{"url": "https://example.com/x"}]},
    )
    result = confluence_run(cfg, fetcher=bad)
    assert result.status == "warn"


def test_confluence_sync_handles_adf_html_edge_cases(tmp_path: Path):
    weird = (
        '<h2>Heading</h2>'
        '<ul><li>one</li><li>two</li></ul>'
        '<pre><code>console.log(&quot;x&quot;)</code></pre>'
        '<blockquote>quoted</blockquote>'
    )
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={"pages": [{"space": "ENG", "title": "ADF Edge", "url": "https://x/y", "body": weird, "revision": "1"}]},
    )
    result = confluence_run(cfg)
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "## Heading" in body
    assert "- one" in body
    assert "```" in body
    assert "> quoted" in body


def test_confluence_sync_records_last_synced(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={"pages": [{"space": "ENG", "title": "T", "url": "https://x/y", "body": "<p>z</p>", "revision": "1"}]},
    )
    confluence_run(cfg)
    body = (tmp_path / "out" / "confluence-imports" / "eng" / "t.md").read_text(encoding="utf-8")
    parsed = parse_frontmatter(body)
    assert parsed.get("last_synced", "").endswith("Z")
    assert parsed["source"] == "confluence"


# ---------- jira_sync ----------


def test_jira_sync_no_issues(tmp_path: Path):
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=tmp_path / "out", extra={})
    result = jira_run(cfg)
    assert result.status == "ok"
    assert "no issues" in result.summary.lower()


def test_jira_sync_writes_issue(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "issues": [
                {
                    "key": "PLAT-123",
                    "project": "PLAT",
                    "summary": "Migrate to PG16",
                    "status": "In Progress",
                    "description": "<p>Plan</p>",
                    "url": "https://acme.atlassian.net/browse/PLAT-123",
                    "updated": "2026-05-01T10:00:00Z",
                }
            ]
        },
    )
    result = jira_run(cfg)
    assert result.status == "ok"
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "PLAT-123" in body
    assert "Migrate to PG16" in body
    assert 'jira_updated: "2026-05-01T10:00:00Z"' in body
    assert "## Decision" in body


def test_jira_sync_dedup_on_same_updated(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "issues": [
                {
                    "key": "OPS-1",
                    "project": "OPS",
                    "summary": "S",
                    "status": "Open",
                    "description": "<p>d</p>",
                    "url": "https://x/y",
                    "updated": "2026-05-01T00:00:00Z",
                }
            ]
        },
    )
    jira_run(cfg)
    target = tmp_path / "out" / "jira-imports" / "ops" / "OPS-1.md"
    mtime_first = target.stat().st_mtime_ns
    jira_run(cfg)
    assert target.stat().st_mtime_ns == mtime_first


def test_jira_sync_rewrites_on_new_updated(tmp_path: Path):
    out = tmp_path / "out"
    issue_v1 = {
        "key": "OPS-1", "project": "OPS", "summary": "S", "status": "Open",
        "description": "<p>v1</p>", "url": "https://x/y", "updated": "2026-05-01T00:00:00Z",
    }
    issue_v2 = dict(issue_v1, description="<p>v2</p>", updated="2026-05-02T00:00:00Z")
    jira_run(RoutineConfig(brain_root=tmp_path, out_dir=out, extra={"issues": [issue_v1]}))
    jira_run(RoutineConfig(brain_root=tmp_path, out_dir=out, extra={"issues": [issue_v2]}))
    body = (out / "jira-imports" / "ops" / "OPS-1.md").read_text(encoding="utf-8")
    assert "v2" in body


def test_jira_sync_skips_missing_key(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path, out_dir=tmp_path / "out",
        extra={"issues": [{"summary": "no key"}]},
    )
    result = jira_run(cfg)
    # Missing key should not crash; surfaces as warn (errors > 0).
    assert result.status == "warn"


def test_jira_sync_renders_decision_record_template(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "issues": [
                {
                    "key": "ENG-9", "project": "ENG", "summary": "S", "status": "Done",
                    "description": "<p>x</p>", "url": "https://x/y", "updated": "2026-05-01T00:00:00Z",
                }
            ]
        },
    )
    jira_run(cfg)
    body = (tmp_path / "out" / "jira-imports" / "eng" / "ENG-9.md").read_text(encoding="utf-8")
    assert "## Context" in body
    assert "## Decision" in body
    assert "**Status (Jira):** Done" in body


def test_jira_sync_infers_project_from_key(tmp_path: Path):
    """If `project` is missing, derive it from KEY-NNN."""
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "issues": [
                {
                    "key": "INFRA-42", "summary": "x", "status": "Open",
                    "description": "<p>x</p>", "url": "https://x/y",
                    "updated": "2026-05-01T00:00:00Z",
                }
            ]
        },
    )
    result = jira_run(cfg)
    assert any("infra" in str(p).lower() for p in result.artifacts)


def test_jira_sync_via_runner_dispatch(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "issues": [
                {
                    "key": "X-1", "project": "X", "summary": "via runner",
                    "status": "Open", "description": "<p>d</p>",
                    "url": "https://x/y", "updated": "2026-05-01T00:00:00Z",
                }
            ]
        },
    )
    result = run_routine("jira_sync", cfg)
    assert result.name == "jira_sync"
    assert result.status == "ok"


# ---------- slack_sync ----------


def test_slack_sync_no_pins(tmp_path: Path):
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=tmp_path / "out", extra={})
    result = slack_run(cfg)
    assert result.status == "ok"


def test_slack_sync_writes_pin(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "channels": ["#oncall"],
            "pins": [
                {
                    "channel": "#oncall",
                    "ts": "1714000000.000100",
                    "user": "alice",
                    "text": "Pinned: pager rotation",
                    "permalink": "https://acme.slack.com/archives/X/p1714000000000100",
                }
            ],
        },
    )
    result = slack_run(cfg)
    assert result.status == "ok"
    body = result.artifacts[0].read_text(encoding="utf-8")
    assert "pager rotation" in body
    assert "alice" in body
    assert "1714000000-000100" in str(result.artifacts[0])


def test_slack_sync_refuses_undeclared_channel(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "channels": ["#oncall"],
            "pins": [{"channel": "#random", "ts": "1.0", "user": "u", "text": "hi"}],
        },
    )
    result = slack_run(cfg)
    assert result.status == "warn"
    assert "refused" in result.summary


def test_slack_sync_admits_all_when_channels_empty(tmp_path: Path):
    """When `channels` is empty, the routine admits any pin the runner provided.

    Rationale: the runner holds the Slack token and has already scoped
    which channels it pulls. Refusing on the agent side would only
    duplicate the runner's scoping work. Channel scoping in the agent
    is a defence-in-depth check that matters only when the runner
    pulls from a wider set than the team wants synced.
    """
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "channels": [],
            "pins": [{"channel": "#anywhere", "ts": "1.0", "user": "u", "text": "x"}],
        },
    )
    result = slack_run(cfg)
    assert result.status == "ok"
    # The pin from #anywhere should have been written.
    assert any(str(p).endswith("pin-1-0.md") for p in result.artifacts)


def test_slack_sync_dedup_on_same_ts(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "channels": ["#x"],
            "pins": [{"channel": "#x", "ts": "1.0", "user": "a", "text": "hi"}],
        },
    )
    slack_run(cfg)
    target = tmp_path / "out" / "slack-imports" / "x" / "pin-1-0.md"
    mtime_first = target.stat().st_mtime_ns
    slack_run(cfg)
    assert target.stat().st_mtime_ns == mtime_first


def test_slack_sync_renders_permalink(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "channels": ["#x"],
            "pins": [
                {
                    "channel": "#x", "ts": "1.0", "user": "a",
                    "text": "decision: use postgres",
                    "permalink": "https://acme.slack.com/archives/C123/p1000000",
                }
            ],
        },
    )
    slack_run(cfg)
    body = (tmp_path / "out" / "slack-imports" / "x" / "pin-1-0.md").read_text(encoding="utf-8")
    assert "acme.slack.com" in body


def test_slack_sync_via_runner_dispatch(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "channels": ["#x"],
            "pins": [{"channel": "#x", "ts": "1.0", "user": "a", "text": "hi"}],
        },
    )
    result = run_routine("slack_sync", cfg)
    assert result.name == "slack_sync"


# ---------- web_pull ----------


def _ok_fetcher(body: str = "<h1>T</h1><p>body</p>", url: str = "") -> Any:
    def fetch(u: str) -> FetchedPage:
        return FetchedPage(url=url or u, status=200, body=body, content_type="text/html")

    return fetch


def test_web_pull_no_urls(tmp_path: Path):
    cfg = RoutineConfig(brain_root=tmp_path, out_dir=tmp_path / "out", extra={})
    result = web_run(cfg)
    assert result.status == "ok"


def test_web_pull_empty_allowlist_refuses_everything(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={"urls": ["https://docs.aws.amazon.com/x"], "allowlist_domains": []},
    )
    result = web_run(cfg, fetcher=_ok_fetcher())
    assert result.status == "warn"
    assert "refused=1" in result.summary
    assert result.artifacts == []


def test_web_pull_admits_allowlisted_host(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "urls": ["https://docs.aws.amazon.com/eks/latest/userguide/x.html"],
            "allowlist_domains": ["docs.aws.amazon.com"],
        },
    )
    result = web_run(cfg, fetcher=_ok_fetcher())
    assert result.status == "ok"
    assert len(result.artifacts) == 1
    assert "web-imports" in str(result.artifacts[0])


def test_web_pull_refuses_off_allowlist_when_others_pass(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "urls": [
                "https://docs.aws.amazon.com/eks/x",
                "https://evil.example.com/p",
            ],
            "allowlist_domains": ["docs.aws.amazon.com"],
        },
    )
    result = web_run(cfg, fetcher=_ok_fetcher())
    assert result.status == "warn"
    assert "refused=1" in result.summary
    assert len(result.artifacts) == 1


def test_web_pull_extracts_title(tmp_path: Path):
    body = "<html><head><title>EKS user guide</title></head><body><h1>EKS</h1></body></html>"
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "urls": ["https://docs.aws.amazon.com/eks/latest/userguide/index.html"],
            "allowlist_domains": ["docs.aws.amazon.com"],
        },
    )
    result = web_run(cfg, fetcher=_ok_fetcher(body=body))
    out_text = result.artifacts[0].read_text(encoding="utf-8")
    assert "# EKS user guide" in out_text
    assert 'title: "EKS user guide"' in out_text


def test_web_pull_warns_on_status_error(tmp_path: Path):
    def fail(u: str) -> FetchedPage:
        return FetchedPage(url=u, status=503, body="")

    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "urls": ["https://kubernetes.io/docs/x"],
            "allowlist_domains": ["kubernetes.io"],
        },
    )
    result = web_run(cfg, fetcher=fail)
    assert result.status == "warn"
    assert "errors=1" in result.summary


def test_web_pull_writes_under_host_dir(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "urls": ["https://kubernetes.io/docs/concepts/x"],
            "allowlist_domains": ["kubernetes.io"],
        },
    )
    result = web_run(cfg, fetcher=_ok_fetcher())
    assert "web-imports/kubernetes-io/" in str(result.artifacts[0]).replace("\\", "/")


def test_web_pull_via_runner_dispatch(tmp_path: Path):
    cfg = RoutineConfig(
        brain_root=tmp_path,
        out_dir=tmp_path / "out",
        extra={
            "urls": ["https://kubernetes.io/x"],
            "allowlist_domains": ["kubernetes.io"],
            "fetcher": _ok_fetcher(),
        },
    )
    result = run_routine("web_pull", cfg)
    assert result.name == "web_pull"
    assert result.status == "ok"


# ---------- CLI: teammate sync ----------


def test_cli_sync_confluence_writes_artifact(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from vigil.cli import main as cli_main

    # Brain seed.
    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    cfg_dir = tmp_path / ".teammate"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        '[sync.confluence]\n'
        'pages = [\n'
        '  { space = "ENG", title = "P", url = "https://x/y", '
        'body = "<p>hi</p>", revision = "v1" }\n'
        ']\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "confluence", "--out-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    files = list((tmp_path / "out" / "confluence-imports").rglob("*.md"))
    assert len(files) == 1


def test_cli_sync_dry_run_skips_writes(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from vigil.cli import main as cli_main

    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    cfg_dir = tmp_path / ".teammate"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        '[sync.confluence]\n'
        'pages = [\n'
        '  { space = "ENG", title = "P", url = "https://x/y", '
        'body = "<p>hi</p>", revision = "v1" }\n'
        ']\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    out_dir = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["sync", "confluence", "--out-dir", str(out_dir), "--dry-run"],
    )
    assert result.exit_code == 0
    # Dry run must write nothing under out_dir.
    assert not (out_dir / "confluence-imports").exists() or not list(
        (out_dir / "confluence-imports").rglob("*.md")
    )


def test_cli_sync_unknown_routine(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from vigil.cli import main as cli_main

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync"])
    # No subcommand given — click should show usage.
    assert result.exit_code != 0 or "Usage" in result.output


def test_cli_sync_web_respects_allowlist(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from vigil.cli import main as cli_main

    (tmp_path / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")
    cfg_dir = tmp_path / ".teammate"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        '[sync.web]\n'
        'urls = ["https://evil.example.com/x"]\n'
        'allowlist_domains = ["docs.aws.amazon.com"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "web", "--out-dir", str(tmp_path / "out")])
    # Refused URLs surface as WARN. Should not write anything.
    out_dir = tmp_path / "out" / "web-imports"
    assert not out_dir.exists() or not list(out_dir.rglob("*.md"))
    assert result.exit_code in (0, 1)


# ---------- pyproject hygiene: sync routines must remain optional-friendly ----------


def test_sync_routines_lazy_import_httpx():
    """Top-level `import vigil.agent.runner` must not require httpx.

    Even if a test environment had httpx removed, the registry should
    still load. The fetcher is the one place httpx is touched; that
    import is lazy.
    """
    # A passing baseline verifies we never moved `import httpx` to the
    # top level. We can't easily simulate "httpx removed" inside the
    # test runner, but we *can* confirm the source files don't import
    # it at module load.
    from vigil.agent import _sync_common, confluence_sync, jira_sync, slack_sync, web_pull

    for module in (_sync_common, confluence_sync, jira_sync, slack_sync, web_pull):
        # Confirm the module doesn't have `httpx` as a module-level attribute.
        # (`_lazy_httpx` returns it on demand inside `default_httpx_fetcher`.)
        assert not hasattr(module, "httpx"), (
            f"{module.__name__} appears to have imported httpx at module load"
        )


@pytest.mark.parametrize("routine", ["confluence_sync", "jira_sync", "slack_sync", "web_pull"])
def test_sync_routines_registered(routine: str):
    from vigil.agent.runner import list_routines

    assert routine in list_routines()
