"""Confluence importer — bulk + incremental.

One markdown file per page at ``archive/confluence/<SPACE>/<page-id>-<slug>.md``.

Uses v1 ``/rest/api/content`` endpoints for listing + retrieving with body.storage
(works on Cloud and Data Center). Storage format is XHTML-ish; we run it through
a small HTML→Markdown pass.

Env vars:
    ATLASSIAN_API_TOKEN     required
    ATLASSIAN_EMAIL         required
    CONFLUENCE_BASE_URL     default https://placen.atlassian.net/wiki
    CONFLUENCE_SPACES       comma-separated keys (e.g. "GOUR,FNBNOW")
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from vigil.importers.base import ImporterBase

log = logging.getLogger(__name__)

_SLUG = re.compile(r"[^a-zA-Z0-9가-힣-]+")


def _slug(s: str, maxlen: int = 60) -> str:
    return _SLUG.sub("-", s).strip("-")[:maxlen].lower() or "untitled"


class ConfluenceImporter(ImporterBase):
    source_name = "confluence"

    def __init__(self, brain_root, *, dry_run=False):
        super().__init__(brain_root, dry_run=dry_run)
        self.base = os.environ.get("CONFLUENCE_BASE_URL", "")
        self.auth = (
            os.environ.get("ATLASSIAN_EMAIL", ""),
            os.environ.get("ATLASSIAN_API_TOKEN", ""),
        )
        spaces_raw = os.environ.get("CONFLUENCE_SPACES", "")
        self.spaces = [s.strip() for s in spaces_raw.split(",") if s.strip()]
        if not self.base:
            raise ValueError("CONFLUENCE_BASE_URL env var is required (e.g. https://your-org.atlassian.net/wiki)")
        if not self.spaces:
            raise ValueError("CONFLUENCE_SPACES env var is required (comma-separated space keys)")
        if not self.auth[0] or not self.auth[1]:
            raise ValueError("ATLASSIAN_EMAIL + ATLASSIAN_API_TOKEN env vars are required")

    def iterate(self, since: Any) -> Iterator[dict]:
        import httpx

        # CQL: filter by spaces + (optionally) lastmodified
        spaces_cql = ",".join(f'"{s}"' for s in self.spaces)
        clauses = [f"space IN ({spaces_cql})", "type = page"]
        if since:
            try:
                dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
                cql_date = dt.strftime("%Y/%m/%d %H:%M")
                clauses.append(f'lastmodified > "{cql_date}"')
            except Exception:
                log.warning("cannot parse watermark %r — doing a full scan", since)

        # ORDER BY lastmodified ASC so watermark-resume works:
        # latest-processed = max watermark, no overlap on next run.
        cql = " AND ".join(clauses) + " ORDER BY lastmodified ASC"
        log.info("confluence: CQL=%r", cql)

        with httpx.Client(auth=self.auth, timeout=30) as client:
            start = 0
            page_size = 25
            total = 0
            while True:
                resp = client.get(
                    f"{self.base}/rest/api/content/search",
                    params={
                        "cql": cql,
                        "limit": page_size,
                        "start": start,
                        "expand": "body.storage,version,space,history.lastUpdated",
                    },
                )
                if resp.status_code != 200:
                    log.error("confluence search HTTP %d: %s", resp.status_code, resp.text[:300])
                    return
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break
                for page in results:
                    total += 1
                    yield page
                log.info("confluence: paged %d (running total %d)", len(results), total)
                if len(results) < page_size:
                    break
                start += page_size

    def render(self, page: dict) -> tuple[str, dict, str]:
        page_id = page.get("id", "")
        title = page.get("title", "untitled")
        space_key = (page.get("space") or {}).get("key", "unknown")
        webui = self.base + (page.get("_links", {}).get("webui") or page.get("_links", {}).get("tinyui") or "")

        rel_path = f"{space_key}/{page_id}-{_slug(title)}.md"

        last_modified = ""
        version = page.get("version", {}) or {}
        if version.get("when"):
            last_modified = version["when"]

        frontmatter = {
            "source": "confluence",
            "source_type": "page",
            "source_id": page_id,
            "source_url": webui,
            "title": title,
            "fetched_at": datetime.now(UTC).isoformat(),
            "last_modified": last_modified,
            "author": (version.get("by") or {}).get("displayName", ""),
            "labels": [],
            "extra": {
                "confluence_space": space_key,
                "confluence_version": version.get("number", 1),
            },
        }

        storage = ((page.get("body") or {}).get("storage") or {}).get("value", "")
        body_md = _confluence_storage_to_markdown(storage)
        body = f"# {title}\n\n_{webui}_\n\n{body_md}"

        return rel_path, frontmatter, body

    def watermark(self, page: dict) -> Any:
        return (page.get("version") or {}).get("when", "")


# ---------------------------------------------------------------------------
# Confluence storage (XHTML-ish) → Markdown
# ---------------------------------------------------------------------------

def _confluence_storage_to_markdown(html: str) -> str:
    if not html:
        return ""

    s = html

    # Custom Confluence macros — keep their text, drop the wrapper
    s = re.sub(r"<ac:rich-text-body>(.*?)</ac:rich-text-body>", r"\1", s, flags=re.S)
    s = re.sub(r"<ac:structured-macro[^>]*name=\"(?P<n>code|noformat)\"[^>]*>(?P<inner>.*?)</ac:structured-macro>",
               lambda m: f"\n```\n{_strip_tags(m.group('inner'))}\n```\n", s, flags=re.S)
    s = re.sub(r"<ac:structured-macro[^>]*>.*?</ac:structured-macro>", "", s, flags=re.S)
    s = re.sub(r"<ac:image[^>]*ri:filename=\"([^\"]+)\"[^>]*/?>", r"![image](\1)", s)

    # Standard HTML
    for n in range(6, 0, -1):
        s = re.sub(rf"<h{n}[^>]*>(.*?)</h{n}>",
                   lambda m, _n=n: "#" * _n + " " + m.group(1).strip() + "\n",
                   s, flags=re.S | re.I)
    s = re.sub(r"<li[^>]*>(.*?)</li>", lambda m: f"- {m.group(1).strip()}\n", s, flags=re.S | re.I)
    s = re.sub(r"</?[uo]l[^>]*>", "", s, flags=re.I)
    s = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
               lambda m: f"[{m.group(2).strip()}]({m.group(1)})", s, flags=re.S | re.I)
    s = re.sub(r"<pre[^>]*>(.*?)</pre>",
               lambda m: f"\n```\n{_strip_tags(m.group(1))}\n```\n", s, flags=re.S | re.I)
    s = re.sub(r"<code[^>]*>(.*?)</code>",
               lambda m: f"`{_strip_tags(m.group(1))}`", s, flags=re.S | re.I)
    s = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", s, flags=re.S | re.I)
    s = re.sub(r"<(em|i)[^>]*>(.*?)</\1>", r"*\2*", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<p[^>]*>", "", s, flags=re.I)
    s = _strip_tags(s)

    s = (s.replace("&amp;", "&").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&quot;", '"')
         .replace("&#39;", "'").replace("&nbsp;", " "))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


__all__ = ["ConfluenceImporter"]
