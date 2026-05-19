"""Routine 4 — pull Confluence pages → markdown → stage PR.

The runner reads an artifacts list and stages a PR with the new /
updated files. Humans review. The agent never auto-merges.

Config (passed via ``RoutineConfig.extra``):

  pages         list[dict]   — entries shaping {space, title, url, body, revision}
                              The runner / CLI fills this in from
                              ``examples/sync-routines.json`` or
                              ``.vigil/config.toml`` ``[sync.confluence]``.
  fetcher       callable     — optional. Default: httpx GET. The runner
                              swaps in an MCP-backed fetcher when the
                              user has Atlassian MCP configured.

Dedup: if a target file already exists with the same
``confluence_revision`` frontmatter value, the routine skips writing.
mtime is preserved.
"""

from __future__ import annotations

import time
from datetime import date as _date
from pathlib import Path
from typing import Any

from vigil.agent._sync_common import (
    FetchedPage,
    Fetcher,
    default_httpx_fetcher,
    html_to_markdown,
    slugify,
    utc_now_iso,
    write_doc,
)
from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult


def _entry_to_page(entry: dict[str, Any], fetcher: Fetcher) -> dict[str, Any]:
    """Resolve one config entry into a page dict.

    Two shapes supported:
      1. Already-resolved: ``{space, title, body, revision, url}`` — the
         runner has already pulled it via MCP.
      2. URL-only: ``{url}`` — fetch via ``fetcher`` and infer space /
         title from the response.
    """
    if "body" in entry:
        return {
            "space": str(entry.get("space") or "unknown"),
            "title": str(entry.get("title") or entry.get("url") or "untitled"),
            "url": str(entry.get("url") or ""),
            "body_html": str(entry.get("body") or ""),
            "revision": str(entry.get("revision") or ""),
        }
    url = str(entry.get("url") or "")
    if not url:
        return {
            "space": "unknown",
            "title": "untitled",
            "url": "",
            "body_html": "",
            "revision": "",
            "error": "missing url",
        }
    fp: FetchedPage = fetcher(url)
    if fp.status >= 400 or fp.status == 0:
        return {
            "space": str(entry.get("space") or "unknown"),
            "title": str(entry.get("title") or url),
            "url": url,
            "body_html": "",
            "revision": "",
            "error": f"fetch status {fp.status}",
        }
    return {
        "space": str(entry.get("space") or "unknown"),
        "title": str(entry.get("title") or url),
        "url": url,
        "body_html": fp.body,
        "revision": str(entry.get("revision") or fp.headers.get("etag", "") or ""),
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

    entries = list(config.extra.get("pages") or [])
    fetcher = fetcher or config.extra.get("fetcher") or default_httpx_fetcher

    written: list[Path] = []
    deduped: list[Path] = []
    errors: list[tuple[str, str]] = []
    base = config.out_dir / "confluence-imports"

    for entry in entries:
        resolved = _entry_to_page(entry, fetcher)
        if "error" in resolved:
            errors.append((resolved.get("url") or "?", resolved["error"]))
            continue
        space = slugify(resolved["space"], fallback="space")
        title_slug = slugify(resolved["title"], fallback="page")
        target = base / space / f"{title_slug}.md"
        body_md = html_to_markdown(resolved["body_html"]).strip()
        body = f"# {resolved['title']}\n\n{body_md}\n" if body_md else f"# {resolved['title']}\n"
        meta: dict[str, Any] = {
            "source": "confluence",
            "source_url": resolved["url"],
            "space": resolved["space"],
            "title": resolved["title"],
            "last_synced": utc_now_iso(),
            "confluence_revision": resolved["revision"],
        }
        path, wrote = write_doc(target, frontmatter=meta, body=body, revision_key="confluence_revision")
        if wrote:
            written.append(path)
        else:
            deduped.append(path)

    status = OK if not errors else WARN
    summary_bits = [
        f"{len(entries)} page(s)",
        f"wrote={len(written)}",
        f"deduped={len(deduped)}",
    ]
    if errors:
        summary_bits.append(f"errors={len(errors)}")
    summary = "  ".join(summary_bits) if entries else "no pages configured"

    artifacts = list(written) + list(deduped)
    return RoutineResult(
        name="confluence_sync",
        status=status,
        summary=summary,
        artifacts=artifacts,
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
