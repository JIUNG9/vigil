"""Shared helpers for the v0.8 sync routines.

The four sync routines (confluence, jira, slack, web) all do the same
shape of work:

  1. Pull a list of "things" (pages / issues / channels / URLs) from the
     routine config.
  2. Fetch each thing through a pluggable fetcher (default: ``httpx``;
     the runner can swap in an MCP-backed one).
  3. Convert HTML → markdown with a small hand-rolled converter — no
     extra dependency.
  4. Write a frontmatter-prefixed markdown file under ``out_dir`` so the
     runner can stage it as a PR.

This module owns the bits that don't change across routines: the
``FetchedPage`` dataclass, the default fetcher, the HTML→markdown
helper, and the frontmatter helpers. Each routine then layers its own
config schema and target-path logic on top.

Hard rules — same as the rest of ``vigil.agent``:

  * Routines are read-only on the brain. They never edit existing
    files outside ``out_dir``.
  * No auto-merge. The runner stages files; humans review.
  * Empty allowlist → refuse everything. Default-deny.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class FetchedPage:
    """A normalized response from any fetcher (httpx, MCP, fixture)."""

    url: str
    status: int
    body: str
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)


# A fetcher takes a URL and returns a FetchedPage. The default fetcher
# uses httpx; tests inject a fake; the runner can plug in an MCP-backed
# fetcher to route Atlassian / Slack URLs through the user's configured
# MCP servers.
Fetcher = Callable[[str], FetchedPage]


def _lazy_httpx():
    """Import ``httpx`` only on demand.

    ``httpx`` is in the ``[rag]`` optional extra, not the core install.
    OSS users running ``pip install claude-vigil`` (no extras) get
    keyword-only retrieval; the sync routines also gracefully say
    "install claude-vigil[rag]" instead of crashing at import time.
    """
    try:
        import httpx  # type: ignore[import-not-found]

        return httpx
    except ImportError as exc:  # pragma: no cover — exercised when extras missing
        raise RuntimeError(
            "sync routines require the [rag] extra: "
            "pip install 'claude-vigil[rag]'"
        ) from exc


def default_httpx_fetcher(url: str, *, timeout: float = 10.0) -> FetchedPage:
    """Default fetcher used when the runner doesn't inject one."""
    httpx = _lazy_httpx()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            return FetchedPage(
                url=str(resp.url),
                status=resp.status_code,
                body=resp.text or "",
                content_type=resp.headers.get("content-type", ""),
                headers={k.lower(): v for k, v in resp.headers.items()},
            )
    except Exception as exc:  # noqa: BLE001 — surface as a 0-status page
        return FetchedPage(url=url, status=0, body=f"fetch failed: {exc}", content_type="")


# ---------- HTML → markdown ----------

# Hand-rolled converter. We support the subset that Confluence / Jira /
# generic doc pages use most: headings, paragraphs, lists, links, bold,
# italic, code, blockquote. Anything we don't understand falls through
# to plain text. The point is "good enough to PR-review", not "perfect
# round-trip".

_BLOCK_TAG_RE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_scripts_and_styles(s: str) -> str:
    return _BLOCK_TAG_RE.sub("", s)


def _heading(text: str, level: int) -> str:
    level = max(1, min(level, 6))
    return f"\n\n{'#' * level} {text.strip()}\n\n"


def _link(text: str, href: str) -> str:
    text = text.strip() or href
    return f"[{text}]({href})"


def html_to_markdown(html_text: str) -> str:
    """Convert a small HTML / ADF-rendered subset to markdown.

    Not a full parser — the tests use realistic but small inputs. The
    implementation is regex-driven so we don't grow a BeautifulSoup
    dependency.
    """
    if not html_text:
        return ""
    s = _strip_scripts_and_styles(html_text)
    # Normalize self-closing brs / hrs to plain newlines / dividers
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<\s*hr\s*/?\s*>", "\n\n---\n\n", s, flags=re.IGNORECASE)

    # Headings (h1..h6)
    def _h_repl(m: re.Match[str]) -> str:
        return _heading(_inner(m.group(2)), int(m.group(1)))

    s = re.sub(r"<\s*h([1-6])[^>]*>(.*?)<\s*/\s*h\1\s*>", _h_repl, s, flags=re.IGNORECASE | re.DOTALL)

    # Bold / italic / inline code — order matters (process strong first)
    s = re.sub(r"<\s*(strong|b)[^>]*>(.*?)<\s*/\s*\1\s*>", r"**\2**", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<\s*(em|i)[^>]*>(.*?)<\s*/\s*\1\s*>", r"*\2*", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<\s*code[^>]*>(.*?)<\s*/\s*code\s*>", r"`\1`", s, flags=re.IGNORECASE | re.DOTALL)

    # Pre / code blocks
    def _pre_repl(m: re.Match[str]) -> str:
        body = re.sub(r"<[^>]+>", "", m.group(1))
        body = html.unescape(body).rstrip()
        return f"\n\n```\n{body}\n```\n\n"

    s = re.sub(r"<\s*pre[^>]*>(.*?)<\s*/\s*pre\s*>", _pre_repl, s, flags=re.IGNORECASE | re.DOTALL)

    # Blockquote
    def _bq_repl(m: re.Match[str]) -> str:
        body = _inner(m.group(1)).strip()
        return "\n\n" + "\n".join(f"> {ln}" for ln in body.splitlines() or [""]) + "\n\n"

    s = re.sub(r"<\s*blockquote[^>]*>(.*?)<\s*/\s*blockquote\s*>", _bq_repl, s, flags=re.IGNORECASE | re.DOTALL)

    # Anchors
    def _a_repl(m: re.Match[str]) -> str:
        href = m.group(1)
        text = _inner(m.group(2))
        return _link(text, href)

    s = re.sub(
        r'<\s*a[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)<\s*/\s*a\s*>',
        _a_repl,
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Lists — flatten ul/ol to bullet / numbered list. We don't support
    # nested lists faithfully; we render bullets at the same depth.
    def _ul_repl(m: re.Match[str]) -> str:
        items = re.findall(r"<\s*li[^>]*>(.*?)<\s*/\s*li\s*>", m.group(1), re.IGNORECASE | re.DOTALL)
        rendered = "\n".join(f"- {_inner(item).strip()}" for item in items)
        return f"\n\n{rendered}\n\n"

    def _ol_repl(m: re.Match[str]) -> str:
        items = re.findall(r"<\s*li[^>]*>(.*?)<\s*/\s*li\s*>", m.group(1), re.IGNORECASE | re.DOTALL)
        rendered = "\n".join(f"{i + 1}. {_inner(item).strip()}" for i, item in enumerate(items))
        return f"\n\n{rendered}\n\n"

    s = re.sub(r"<\s*ul[^>]*>(.*?)<\s*/\s*ul\s*>", _ul_repl, s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<\s*ol[^>]*>(.*?)<\s*/\s*ol\s*>", _ol_repl, s, flags=re.IGNORECASE | re.DOTALL)

    # Paragraphs
    s = re.sub(r"<\s*p[^>]*>(.*?)<\s*/\s*p\s*>", lambda m: "\n\n" + _inner(m.group(1)).strip() + "\n\n", s, flags=re.IGNORECASE | re.DOTALL)

    # Strip remaining tags
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)

    # Collapse 3+ blank lines to 2
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def _inner(text: str) -> str:
    """Strip nested simple tags from inline content."""
    return re.sub(r"<[^>]+>", "", text or "")


# ---------- frontmatter ----------


def render_frontmatter(meta: dict[str, Any]) -> str:
    """Render a stable YAML-ish frontmatter block.

    Hand-rolled — every value is rendered as a quoted string. The keys
    are emitted in sorted order so file diffs stay deterministic across
    re-syncs.
    """
    if not meta:
        return ""
    lines = ["---"]
    for k in sorted(meta.keys()):
        v = meta[k]
        rendered = _yaml_value(v)
        lines.append(f"{k}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_yaml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract the simple `key: "value"` pairs from a frontmatter block."""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}
    block = m.group(1)
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        v = raw.strip()
        if v.startswith('"') and v.endswith('"') and len(v) >= 2:
            v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[key.strip()] = v
    return out


# ---------- common file-write helper ----------


def write_doc(
    out_path: Path,
    *,
    frontmatter: dict[str, Any],
    body: str,
    revision_key: str | None = None,
) -> tuple[Path, bool]:
    """Write ``out_path`` with frontmatter + body. Returns ``(path, wrote)``.

    Dedup contract: if ``revision_key`` is provided AND the existing
    file already has the same value for that key, do not rewrite. The
    file's mtime is preserved so downstream watchers (CI cache, file
    watchers) don't re-trigger on no-op syncs.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    new_value = frontmatter.get(revision_key) if revision_key else None
    if revision_key and new_value is not None and out_path.is_file():
        try:
            existing = out_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        existing_meta = parse_frontmatter(existing)
        if str(existing_meta.get(revision_key, "")) == str(new_value):
            return out_path, False
    text = render_frontmatter(frontmatter) + "\n" + (body.rstrip() + "\n")
    out_path.write_text(text, encoding="utf-8")
    return out_path, True


# ---------- allowlist ----------


def host_in_allowlist(url: str, allowlist: list[str]) -> bool:
    """``True`` iff the URL's host matches any allowlist entry.

    Empty allowlist refuses everything (default-deny). Entries match
    by suffix so ``"docs.aws.amazon.com"`` admits
    ``"https://docs.aws.amazon.com/foo"`` but not
    ``"https://evil.docs.aws.amazon.com.example/"``.
    """
    if not allowlist:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for entry in allowlist:
        e = (entry or "").lower().strip()
        if not e:
            continue
        if host == e or host.endswith("." + e):
            return True
    return False


def utc_now_iso() -> str:
    """ISO-8601 timestamp suitable for ``last_synced`` frontmatter."""
    return _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, *, fallback: str = "page") -> str:
    """Lower-case, kebab-case slug suitable for a filename."""
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or fallback


__all__ = [
    "FetchedPage",
    "Fetcher",
    "default_httpx_fetcher",
    "host_in_allowlist",
    "html_to_markdown",
    "parse_frontmatter",
    "render_frontmatter",
    "slugify",
    "utc_now_iso",
    "write_doc",
]
