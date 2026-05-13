"""Jira importer — bulk + incremental.

One markdown file per issue at ``archive/jira/<PROJECT>/<KEY>.md``.
Each issue's most useful comments are appended after the description.

Env vars:
    ATLASSIAN_API_TOKEN   required
    ATLASSIAN_EMAIL       required (basic-auth username)
    JIRA_BASE_URL         default https://placen.atlassian.net
    JIRA_PROJECTS         comma-separated, e.g. "NEXUS,PLAT,PLATFORM"
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from teammate.importers.base import ImporterBase

log = logging.getLogger(__name__)

_SLUG = re.compile(r"[^a-zA-Z0-9-]+")


def _slug(s: str, maxlen: int = 60) -> str:
    return _SLUG.sub("-", s).strip("-").lower()[:maxlen] or "untitled"


class JiraImporter(ImporterBase):
    source_name = "jira"

    def __init__(self, brain_root, *, dry_run=False):
        super().__init__(brain_root, dry_run=dry_run)
        self.base = os.environ.get("JIRA_BASE_URL", "")
        self.auth = (
            os.environ.get("ATLASSIAN_EMAIL", ""),
            os.environ.get("ATLASSIAN_API_TOKEN", ""),
        )
        projects_raw = os.environ.get("JIRA_PROJECTS", "")
        self.projects = [p.strip() for p in projects_raw.split(",") if p.strip()]
        if not self.base:
            raise ValueError("JIRA_BASE_URL env var is required (e.g. https://your-org.atlassian.net)")
        if not self.projects:
            raise ValueError("JIRA_PROJECTS env var is required (comma-separated project keys)")
        if not self.auth[0] or not self.auth[1]:
            raise ValueError("ATLASSIAN_EMAIL + ATLASSIAN_API_TOKEN env vars are required")

    def iterate(self, since: Any) -> Iterator[dict]:
        import httpx

        # JQL: filter by projects + (optionally) updated > since
        clauses = [f"project IN ({','.join(self.projects)})"]
        if since:
            # since is an ISO-ish string; format as JQL date+time
            try:
                dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
                jql_date = dt.strftime("%Y-%m-%d %H:%M")
                clauses.append(f'updated > "{jql_date}"')
            except Exception:
                log.warning("cannot parse watermark %r — doing a full scan", since)

        jql = " AND ".join(clauses) + " ORDER BY updated DESC"
        log.info("jira: JQL=%r", jql)

        next_page_token = None
        total = 0
        with httpx.Client(auth=self.auth, timeout=30) as client:
            while True:
                params = {
                    "jql": jql,
                    "maxResults": 50,
                    "fields": "*navigable,comment",
                    "expand": "renderedFields",
                }
                if next_page_token:
                    params["nextPageToken"] = next_page_token

                resp = client.get(f"{self.base}/rest/api/3/search/jql", params=params)
                if resp.status_code != 200:
                    log.error("jira search HTTP %d: %s", resp.status_code, resp.text[:300])
                    return

                data = resp.json()
                issues = data.get("issues", [])
                if not issues:
                    break
                for issue in issues:
                    total += 1
                    yield issue
                log.info("jira: paged %d issues (running total %d)", len(issues), total)

                next_page_token = data.get("nextPageToken")
                if not next_page_token:
                    break

    def render(self, issue: dict) -> tuple[str, dict, str]:
        key = issue["key"]
        project = key.split("-")[0]
        f = issue.get("fields", {})
        title = f.get("summary", "") or "untitled"

        rel_path = f"{project}/{key}.md"

        frontmatter = {
            "source": "jira",
            "source_type": "issue",
            "source_id": key,
            "source_url": f"{self.base}/browse/{key}",
            "title": title,
            "fetched_at": datetime.now(UTC).isoformat(),
            "last_modified": f.get("updated", ""),
            "author": ((f.get("reporter") or {}).get("displayName") or ""),
            "labels": f.get("labels", []),
            "extra": {
                "jira_status": (f.get("status") or {}).get("name", ""),
                "jira_priority": (f.get("priority") or {}).get("name", ""),
                "jira_issuetype": (f.get("issuetype") or {}).get("name", ""),
                "jira_assignee": (f.get("assignee") or {}).get("displayName", ""),
                "jira_created": f.get("created", ""),
            },
        }

        body_parts = [f"# {key} — {title}", ""]

        # Description (renderedFields has HTML; fields.description is ADF JSON)
        rendered = issue.get("renderedFields", {})
        desc_html = rendered.get("description", "") or ""
        if desc_html:
            body_parts.append("## Description")
            body_parts.append(_html_to_markdown(desc_html))
            body_parts.append("")

        # Comments (top 30, oldest first for readability)
        comments = (f.get("comment") or {}).get("comments", []) or []
        if comments:
            body_parts.append("## Comments")
            for c in comments[:30]:
                author = (c.get("author") or {}).get("displayName") or "unknown"
                created = c.get("created", "")
                (rendered.get("comment", {}).get("comments", []) or [])
                # rendered comments align by index in some Jira versions
                body_parts.append(f"### {author} — {created}")
                body_text = c.get("body", "")
                if isinstance(body_text, dict):
                    body_text = _adf_to_text(body_text)
                body_parts.append(str(body_text))
                body_parts.append("")

        return rel_path, frontmatter, "\n".join(body_parts).strip()

    def watermark(self, issue: dict) -> Any:
        return (issue.get("fields") or {}).get("updated", "")


# ---------------------------------------------------------------------------
# HTML → Markdown (tiny, best-effort)
# ---------------------------------------------------------------------------

def _html_to_markdown(html: str) -> str:
    """Very rough HTML → Markdown. Good enough for Jira-rendered descriptions."""
    s = html
    # Headers
    for n in range(6, 0, -1):
        s = re.sub(rf"<h{n}[^>]*>(.*?)</h{n}>",
                   lambda m, _n=n: "#" * _n + " " + m.group(1).strip() + "\n",
                   s, flags=re.S | re.I)
    # Lists
    s = re.sub(r"<li[^>]*>(.*?)</li>", lambda m: f"- {m.group(1).strip()}\n", s, flags=re.S | re.I)
    s = re.sub(r"</?[uo]l[^>]*>", "", s, flags=re.I)
    # Links
    s = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
               lambda m: f"[{m.group(2).strip()}]({m.group(1)})", s, flags=re.S | re.I)
    # Code
    s = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: f"\n```\n{_strip_tags(m.group(1))}\n```\n", s, flags=re.S | re.I)
    s = re.sub(r"<code[^>]*>(.*?)</code>", lambda m: f"`{_strip_tags(m.group(1))}`", s, flags=re.S | re.I)
    # Bold / italic
    s = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", s, flags=re.S | re.I)
    s = re.sub(r"<(em|i)[^>]*>(.*?)</\1>", r"*\2*", s, flags=re.S | re.I)
    # Paragraphs / breaks
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<p[^>]*>", "", s, flags=re.I)
    # Strip remaining tags
    s = _strip_tags(s)
    # Decode entities
    s = (s.replace("&amp;", "&").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&quot;", '"')
         .replace("&#39;", "'").replace("&nbsp;", " "))
    # Collapse > 2 consecutive blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _adf_to_text(adf: dict) -> str:
    """Atlassian Document Format → plain text. Walks the doc tree."""
    parts: list[str] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
            return
        for child in node.get("content", []) or []:
            walk(child)
        # Add line breaks for block-level
        if node.get("type") in ("paragraph", "heading", "listItem"):
            parts.append("\n")

    walk(adf)
    return "".join(parts).strip()


__all__ = ["JiraImporter"]
