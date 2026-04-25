"""Advisory feed watcher — pluggable scanner that diffs vs last run.

v0.1 ships two feed adapters:

  - **KISA RSS** (Korea Internet & Security Agency) — Korean-language
    advisory feed. We pull the standard public RSS via ``feedparser``.
    No API key required. Rate-limit-friendly polling (default daily).
  - **NVD CVE 2.0 API** — JSON, paginated. Stdlib ``urllib`` + ``json``.
    NVD encourages API-key registration for sustained use; v0.1 polls
    unauthenticated within free-tier limits.

Each adapter returns a list of dict items with keys:
    title, link, published (ISO date), summary, source.

The watcher writes new-since-last-run items into ``compliance-vault/advisories/<ts>.md``
and a one-line summary into ``compliance-vault/history/<ts>-advisory.md``.
``compliance-vault/.teammate-watch-state.json`` tracks the last-seen item id
per source so the next run only emits diffs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from teammate.vault import Vault

# ---------- feed adapters ----------


def fetch_kisa_rss(
    url: str = "https://www.kisa.or.kr/rss/notice.xml",
    timeout_s: float = 15.0,
) -> list[dict[str, Any]]:
    """Pull KISA's public notices RSS via feedparser.

    The exact RSS endpoint URL has changed historically. ``url`` is
    parameterized so users can point at the current one in their config
    without a code change. Default is the long-running ``/rss/notice.xml``
    path; if KISA renames it, the override is one config entry.
    """
    try:
        import feedparser
    except ImportError:
        return []

    parsed = feedparser.parse(url)
    if parsed.bozo and parsed.entries == []:
        return []
    out: list[dict[str, Any]] = []
    for entry in parsed.entries:
        out.append(
            {
                "id": getattr(entry, "id", "") or getattr(entry, "link", ""),
                "title": getattr(entry, "title", "(no title)"),
                "link": getattr(entry, "link", ""),
                "published": getattr(entry, "published", "") or getattr(entry, "updated", ""),
                "summary": getattr(entry, "summary", ""),
                "source": "kisa",
            }
        )
    return out


def fetch_nvd_recent(
    days: int = 7,
    timeout_s: float = 30.0,
    results_per_page: int = 200,
) -> list[dict[str, Any]]:
    """Fetch CVEs published in the last N days from NVD's JSON 2.0 API.

    Unauthenticated. NVD asks for ``User-Agent`` identification — we send
    one. Pagination handled within a single page since v0.1 caps results
    at ``results_per_page`` (200 is generous; a quiet week is ~50 CVEs).
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    base = "https://services.nvd.nist.gov/rest/json/cves/2.0"
    params = (
        f"pubStartDate={start.strftime('%Y-%m-%dT00:00:00.000')}"
        f"&pubEndDate={end.strftime('%Y-%m-%dT23:59:59.999')}"
        f"&resultsPerPage={results_per_page}"
    )
    url = f"{base}?{params}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "claude-teammate/0.1 (+https://github.com/placen-org/teammate)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return []

    out: list[dict[str, Any]] = []
    for vuln in data.get("vulnerabilities", []):
        cve = vuln.get("cve", {})
        cve_id = cve.get("id", "")
        descriptions = cve.get("descriptions", [])
        # Prefer English description; fall back to whatever's first.
        en = next(
            (d["value"] for d in descriptions if d.get("lang") == "en"),
            descriptions[0]["value"] if descriptions else "",
        )
        published = cve.get("published", "")
        out.append(
            {
                "id": cve_id,
                "title": cve_id,
                "link": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                "published": published,
                "summary": en[:400],
                "source": "nvd",
            }
        )
    return out


# ---------- state tracking ----------


def _state_path(vault_root: Path) -> Path:
    return vault_root / ".teammate-watch-state.json"


def load_state(vault_root: Path) -> dict[str, list[str]]:
    p = _state_path(vault_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {k: list(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(vault_root: Path, state: dict[str, list[str]]) -> None:
    p = _state_path(vault_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def diff_against_state(
    items: list[dict[str, Any]],
    state: dict[str, list[str]],
    source: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (new_items, updated_seen_ids) for ``source``."""
    seen = set(state.get(source, []))
    new_items = [it for it in items if it["id"] and it["id"] not in seen]
    # Cap stored ids per source to avoid unbounded growth — keep most recent ~500.
    refreshed = list(seen | {it["id"] for it in items if it["id"]})
    refreshed = refreshed[-500:]
    return new_items, refreshed


# ---------- top-level run ----------


def run(
    vault_root: Path,
    *,
    sources: list[str] | None = None,
    nvd_days: int = 7,
) -> dict[str, dict[str, Any]]:
    """Run all configured feed adapters, write diffs to vault, return summary."""
    requested = sources or ["kisa", "nvd"]
    vault = Vault(vault_root)
    vault.ensure_layout()
    state = load_state(vault_root)
    summary: dict[str, dict[str, Any]] = {}

    if "kisa" in requested:
        kisa_items = fetch_kisa_rss()
        new, refreshed = diff_against_state(kisa_items, state, "kisa")
        if new:
            vault.write_advisory_diff(source="kisa", new_items=new)
        state["kisa"] = refreshed
        summary["kisa"] = {
            "fetched": len(kisa_items),
            "new": len(new),
            "first_new_title": new[0]["title"] if new else "",
        }

    if "nvd" in requested:
        nvd_items = fetch_nvd_recent(days=nvd_days)
        new, refreshed = diff_against_state(nvd_items, state, "nvd")
        if new:
            vault.write_advisory_diff(source="nvd", new_items=new)
        state["nvd"] = refreshed
        summary["nvd"] = {
            "fetched": len(nvd_items),
            "new": len(new),
            "first_new_title": new[0]["title"] if new else "",
        }

    save_state(vault_root, state)
    return summary


__all__ = [
    "diff_against_state",
    "fetch_kisa_rss",
    "fetch_nvd_recent",
    "load_state",
    "run",
    "save_state",
]
