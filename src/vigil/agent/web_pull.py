"""Routine 7 — generic HTTP fetch + HTML→markdown with allowlist.

Config (``RoutineConfig.extra``):

  urls                 list[str]   — URLs to ingest.
  allowlist_domains    list[str]   — host suffixes admitted. Empty list
                                      refuses everything (default-deny).
  fetcher              Fetcher     — optional. Default: httpx GET.

Outputs land at ``out_dir/web-imports/<host>/<slug>.md``. The frontmatter
records ``source_url`` and ``last_synced``.

Refusals are a hard no — the routine never silently downgrades a
disallowed URL. They surface in the result summary so the runner can
report them.
"""

from __future__ import annotations

import re
import time
from datetime import date as _date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from vigil.agent._sync_common import (
    Fetcher,
    default_httpx_fetcher,
    host_in_allowlist,
    html_to_markdown,
    slugify,
    utc_now_iso,
    write_doc,
)
from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult

_TITLE_RE = re.compile(r"<\s*title[^>]*>(.*?)<\s*/\s*title\s*>", re.IGNORECASE | re.DOTALL)


def _extract_title(html_text: str, fallback: str) -> str:
    m = _TITLE_RE.search(html_text or "")
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if title:
            return title
    return fallback


def _path_slug(url: str) -> str:
    """Use the URL path's last meaningful segment as the slug."""
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    candidate = parts[-1] if parts else parsed.netloc
    # Strip trailing extensions like .html / .htm
    candidate = re.sub(r"\.(html|htm|php|aspx?)$", "", candidate, flags=re.IGNORECASE)
    return slugify(candidate, fallback="page")


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
    fetcher: Fetcher | None = None,
) -> RoutineResult:
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = [str(u) for u in (config.extra.get("urls") or []) if u]
    allowlist: list[str] = [str(d) for d in (config.extra.get("allowlist_domains") or [])]
    fetcher = fetcher or config.extra.get("fetcher") or default_httpx_fetcher

    written: list[Path] = []
    deduped: list[Path] = []
    refused: list[str] = []
    errors: list[tuple[str, str]] = []
    base = config.out_dir / "web-imports"

    for url in urls:
        if not host_in_allowlist(url, allowlist):
            refused.append(url)
            continue
        try:
            fp = fetcher(url)
        except Exception as exc:  # noqa: BLE001
            errors.append((url, f"fetcher raised: {exc}"))
            continue
        if fp.status >= 400 or fp.status == 0:
            errors.append((url, f"status {fp.status}"))
            continue
        # Re-validate after redirects — fp.url may differ from the originally
        # requested URL when the fetcher follows 30x. A page on an allowed
        # host can 302 to a disallowed host; we treat that as a refusal.
        if fp.url != url and not host_in_allowlist(fp.url, allowlist):
            refused.append(f"{url} -> {fp.url} (redirect off allowlist)")
            continue

        host = (urlparse(url).hostname or "unknown").lower()
        target = base / slugify(host, fallback="host") / f"{_path_slug(url)}.md"
        title = _extract_title(fp.body, fallback=url)
        body_md = html_to_markdown(fp.body).strip()
        body = f"# {title}\n\n{body_md}\n" if body_md else f"# {title}\n"
        meta: dict[str, Any] = {
            "source": "web",
            "source_url": url,
            "host": host,
            "title": title,
            "last_synced": utc_now_iso(),
            "fetched_status": fp.status,
        }
        # Use the URL itself as the dedup key — re-pulling the same URL
        # rewrites the file (web pages don't expose a stable revision
        # like Confluence does). Test patches can override by injecting
        # a fetcher that returns a steady etag and forcing a key.
        revision_key = "etag" if fp.headers.get("etag") else None
        if revision_key:
            meta["etag"] = fp.headers["etag"]
        path, wrote = write_doc(target, frontmatter=meta, body=body, revision_key=revision_key)
        if wrote:
            written.append(path)
        else:
            deduped.append(path)

    status = OK if not (refused or errors) else WARN
    summary_bits = [
        f"{len(urls)} url(s)",
        f"wrote={len(written)}",
        f"deduped={len(deduped)}",
    ]
    if refused:
        summary_bits.append(f"refused={len(refused)}")
    if errors:
        summary_bits.append(f"errors={len(errors)}")
    summary = "  ".join(summary_bits) if urls else "no urls configured"

    artifacts = list(written) + list(deduped)
    return RoutineResult(
        name="web_pull",
        status=status,
        summary=summary,
        artifacts=artifacts,
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
