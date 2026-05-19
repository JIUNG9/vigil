"""Routine 5 — pull Jira issues by JQL → markdown decision-record drafts.

Same pattern as ``confluence_sync``. Issues stage as PR drafts under
``out_dir/jira-imports/<project>/<issue-key>.md``. The runner opens
the PR; humans review.

Config (passed via ``RoutineConfig.extra``):

  jql           str            — the query the runner already resolved.
  issues        list[dict]     — pre-resolved issue records the runner
                                  pulled via MCP. Each is shaped
                                  ``{key, project, summary, status,
                                  description, url, updated}``.
  fetcher       Fetcher        — optional. Used only if a record needs
                                  follow-up HTTP for the description.

Dedup is keyed on the Jira ``updated`` timestamp (a JIRA-friendly
revision proxy).
"""

from __future__ import annotations

import time
from datetime import date as _date
from pathlib import Path
from typing import Any

from vigil.agent._sync_common import (
    Fetcher,
    default_httpx_fetcher,
    html_to_markdown,
    slugify,
    utc_now_iso,
    write_doc,
)
from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult


def _resolve_issue(entry: dict[str, Any], fetcher: Fetcher) -> dict[str, Any]:
    key = str(entry.get("key") or "")
    project = str(entry.get("project") or (key.split("-", 1)[0] if "-" in key else "unknown"))
    summary = str(entry.get("summary") or key or "untitled")
    status = str(entry.get("status") or "")
    desc = entry.get("description")
    desc_html = str(desc or "")
    url = str(entry.get("url") or "")
    updated = str(entry.get("updated") or "")

    if not desc_html and url:
        # Fall back to the public viewer URL — runners with MCP fill
        # the description directly, so this is mostly a courtesy.
        try:
            fp = fetcher(url)
            if fp.status < 400 and fp.status != 0:
                desc_html = fp.body
        except Exception:  # noqa: BLE001
            pass

    return {
        "key": key,
        "project": project,
        "summary": summary,
        "status": status,
        "description_html": desc_html,
        "url": url,
        "updated": updated,
    }


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
    fetcher: Fetcher | None = None,
) -> RoutineResult:
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)

    entries = list(config.extra.get("issues") or [])
    fetcher = fetcher or config.extra.get("fetcher") or default_httpx_fetcher

    written: list[Path] = []
    deduped: list[Path] = []
    errors: list[tuple[str, str]] = []
    base = config.out_dir / "jira-imports"

    for entry in entries:
        resolved = _resolve_issue(entry, fetcher)
        if not resolved["key"]:
            errors.append((resolved.get("url") or "?", "missing issue key"))
            continue
        project = slugify(resolved["project"], fallback="project")
        target = base / project / f"{resolved['key']}.md"
        body_md = html_to_markdown(resolved["description_html"]).strip()

        # Decision-record-style preamble — every Jira issue lands as a
        # mini-ADR draft for the team to review.
        header_lines = [
            f"# {resolved['key']}: {resolved['summary']}",
            "",
            f"- **Status (Jira):** {resolved['status'] or 'unknown'}",
            f"- **Updated:** {resolved['updated'] or 'unknown'}",
            f"- **Source:** [{resolved['key']}]({resolved['url']})" if resolved["url"] else f"- **Source:** {resolved['key']}",
            "",
            "## Context",
            "",
        ]
        body = "\n".join(header_lines) + (body_md if body_md else "_No description._\n")
        body += "\n\n## Decision\n\n_Pending review — promote to docs/decisions/ when accepted._\n"

        meta: dict[str, Any] = {
            "source": "jira",
            "source_url": resolved["url"],
            "issue_key": resolved["key"],
            "project": resolved["project"],
            "jira_status": resolved["status"],
            "last_synced": utc_now_iso(),
            "jira_updated": resolved["updated"],
        }
        path, wrote = write_doc(target, frontmatter=meta, body=body, revision_key="jira_updated")
        if wrote:
            written.append(path)
        else:
            deduped.append(path)

    status = OK if not errors else WARN
    summary_bits = [
        f"{len(entries)} issue(s)",
        f"wrote={len(written)}",
        f"deduped={len(deduped)}",
    ]
    if errors:
        summary_bits.append(f"errors={len(errors)}")
    summary = "  ".join(summary_bits) if entries else "no issues configured"

    artifacts = list(written) + list(deduped)
    return RoutineResult(
        name="jira_sync",
        status=status,
        summary=summary,
        artifacts=artifacts,
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
