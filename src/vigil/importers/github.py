"""GitHub importer — bulk + incremental.

Per repo: README, issues, pull requests, discussions (if enabled). Each item
gets its own markdown file under ``archive/github/<owner>/<repo>/<type>/<num-or-slug>.md``.

Env vars:
    GITHUB_PAT           required (classic PAT with repo scope, or fine-grained
                          with read access to the listed repos)
    GITHUB_ORG           org/owner to crawl (e.g. "n-fnb")
    GITHUB_REPOS         optional comma-list to restrict (e.g. "pn-app-nx-brain-api,pn-infra-gitops").
                          If unset, lists all repos in the org accessible to the PAT.
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

_API = "https://api.github.com"


def _slug(s: str, maxlen: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", s).strip("-").lower()[:maxlen] or "untitled"


class GitHubImporter(ImporterBase):
    source_name = "github"

    def __init__(self, brain_root, *, dry_run=False):
        super().__init__(brain_root, dry_run=dry_run)
        self.token = os.environ.get("GITHUB_PAT", "")
        self.org = os.environ.get("GITHUB_ORG", "")
        repos_raw = os.environ.get("GITHUB_REPOS", "")
        self.repos = [r.strip() for r in repos_raw.split(",") if r.strip()]
        if not self.token:
            raise ValueError("GITHUB_PAT env var is required")
        if not self.org:
            raise ValueError("GITHUB_ORG env var is required")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _list_repos(self, client) -> list[str]:
        if self.repos:
            return self.repos
        # Auto-discover (page 1-10, 100 per page → up to 1000 repos)
        result = []
        for page in range(1, 11):
            r = client.get(f"{_API}/orgs/{self.org}/repos",
                           params={"per_page": 100, "page": page, "type": "all"})
            if r.status_code != 200:
                log.warning("github list repos HTTP %d: %s", r.status_code, r.text[:200])
                break
            batch = r.json()
            if not batch:
                break
            result.extend(repo["name"] for repo in batch)
        return result

    def iterate(self, since: Any) -> Iterator[dict]:
        import httpx

        since_iso = None
        if since:
            try:
                since_iso = datetime.fromisoformat(str(since).replace("Z", "+00:00")).isoformat()
            except Exception:
                log.warning("cannot parse watermark %r — full scan", since)

        with httpx.Client(headers=self._headers(), timeout=30, follow_redirects=True) as client:
            repos = self._list_repos(client)
            log.info("github: crawling %d repo(s) in org %s", len(repos), self.org)
            for repo in repos:
                yield from self._iter_repo(client, repo, since_iso)

    def _iter_repo(self, client, repo: str, since_iso: str | None) -> Iterator[dict]:
        owner_repo = f"{self.org}/{repo}"

        # 1. README (always, no since-filter — it's just one item)
        r = client.get(f"{_API}/repos/{owner_repo}/readme")
        if r.status_code == 200:
            data = r.json()
            import base64
            content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
            yield {
                "_kind": "readme",
                "_owner_repo": owner_repo,
                "name": data.get("name", "README.md"),
                "html_url": data.get("html_url", ""),
                "sha": data.get("sha", ""),
                "content": content,
            }

        # 2. Issues + Pull requests share the /issues endpoint
        for state in ("open", "closed"):
            page = 1
            while True:
                params = {"state": state, "per_page": 100, "page": page, "sort": "updated", "direction": "desc"}
                if since_iso:
                    params["since"] = since_iso
                r = client.get(f"{_API}/repos/{owner_repo}/issues", params=params)
                if r.status_code != 200:
                    if r.status_code != 410:  # 410 = issues disabled on this repo
                        log.warning("github %s issues HTTP %d", owner_repo, r.status_code)
                    break
                items = r.json()
                if not items:
                    break
                for it in items:
                    it["_owner_repo"] = owner_repo
                    it["_kind"] = "pull_request" if it.get("pull_request") else "issue"
                    # Fetch comments for both issues and PRs (best-effort)
                    cr = client.get(it.get("comments_url", ""), params={"per_page": 100})
                    it["_comments"] = cr.json() if cr.status_code == 200 else []
                    yield it
                if len(items) < 100:
                    break
                page += 1

    def render(self, item: dict) -> tuple[str, dict, str]:
        owner_repo = item.get("_owner_repo", "")
        owner, repo = owner_repo.split("/", 1)
        kind = item.get("_kind", "issue")

        if kind == "readme":
            return self._render_readme(owner, repo, item)

        # issue or pull_request
        num = item.get("number", 0)
        title = item.get("title", "untitled")
        kind_dir = "pull_requests" if kind == "pull_request" else "issues"
        rel_path = f"{owner}/{repo}/{kind_dir}/{num}-{_slug(title)}.md"

        frontmatter = {
            "source": "github",
            "source_type": kind,
            "source_id": f"{owner_repo}#{num}",
            "source_url": item.get("html_url", ""),
            "title": title,
            "fetched_at": datetime.now(UTC).isoformat(),
            "last_modified": item.get("updated_at", ""),
            "author": (item.get("user") or {}).get("login", ""),
            "labels": [lbl["name"] for lbl in item.get("labels", []) if "name" in lbl],
            "extra": {
                "github_state": item.get("state", ""),
                "github_repo": owner_repo,
                "github_created_at": item.get("created_at", ""),
                "github_closed_at": item.get("closed_at", "") or "",
            },
        }

        parts = [f"# #{num} — {title}", ""]
        parts.append(f"_{item.get('html_url','')}_\n")
        body = item.get("body") or ""
        if body:
            parts.append("## Description")
            parts.append(body)
            parts.append("")

        comments = item.get("_comments") or []
        if comments:
            parts.append(f"## Comments ({len(comments)})")
            for c in comments[:50]:
                login = (c.get("user") or {}).get("login", "unknown")
                created = c.get("created_at", "")
                parts.append(f"### {login} — {created}")
                parts.append(c.get("body") or "_(empty)_")
                parts.append("")

        return rel_path, frontmatter, "\n".join(parts).strip()

    def _render_readme(self, owner: str, repo: str, item: dict) -> tuple[str, dict, str]:
        rel_path = f"{owner}/{repo}/README.md"
        frontmatter = {
            "source": "github",
            "source_type": "readme",
            "source_id": f"{owner}/{repo}/{item.get('sha','')}",
            "source_url": item.get("html_url", ""),
            "title": f"{owner}/{repo} — README",
            "fetched_at": datetime.now(UTC).isoformat(),
            "last_modified": "",  # README has no per-file mtime via API
            "author": "",
            "labels": [],
            "extra": {"github_repo": f"{owner}/{repo}", "github_sha": item.get("sha", "")},
        }
        return rel_path, frontmatter, item.get("content", "")

    def watermark(self, item: dict) -> Any:
        return item.get("updated_at", "") or ""


__all__ = ["GitHubImporter"]
