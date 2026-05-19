"""Daily digest routine — gather last 24h of activity, publish to Slack + Confluence.

What gets gathered (parallel):
- Confluence pages updated in the last 24h across watched spaces (CONFLUENCE_WATCHER_SPACES)
- Jira issues updated in the last 24h (JIRA_WATCHER_JQL with relative time)
- Slack messages in watched channels (TEAMMATE_DIGEST_SLACK_CHANNELS)

What gets published:
- Slack: short summary posted to TEAMMATE_DIGEST_SLACK_CHANNEL (or TEAMMATE_SLACK_CHANNELS[0])
- Confluence: full markdown digest written under TEAMMATE_DIGEST_PARENT_PAGE_ID
  - Rolling page: title "Daily Digest" (updated in place)
  - Archive child: title "Daily Digest — YYYY-MM-DD" (new each day)

The routine itself does the publishing (breaks the "routines stage files, runner
distributes" pattern) because the digest is a leaf use-case with a fixed destination
— there's no separate runner that knows about Confluence write.

Required env vars (set on the CronJob, not the dashboard):
  ATLASSIAN_API_TOKEN, ATLASSIAN_EMAIL          for Confluence/Jira read+write
  JIRA_BASE_URL, CONFLUENCE_BASE_URL            (default: placen.atlassian.net)
  CONFLUENCE_WATCHER_SPACES                     comma-separated space keys to read from
  JIRA_WATCHER_JQL                              (optional) override JQL for digest
  TEAMMATE_DIGEST_SPACE_ID                      target space numeric ID
  TEAMMATE_DIGEST_PARENT_PAGE_ID                target parent (folder/page) ID
  SLACK_BOT_TOKEN                               for chat.postMessage
  TEAMMATE_DIGEST_SLACK_CHANNEL                 channel name to post to
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vigil.agent.base import FAIL, OK, WARN, RoutineConfig, RoutineResult

log = logging.getLogger(__name__)


def run(config: RoutineConfig) -> RoutineResult:
    started = time.time()

    try:
        import httpx
    except ImportError:
        return RoutineResult(
            name="daily_digest",
            status=FAIL,
            summary="httpx not installed",
            runtime_seconds=time.time() - started,
        )

    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")

    # Gather from each source. None of these raise — they degrade to empty.
    confluence_pages = _fetch_confluence_pages(httpx) if _has_atlassian_creds() else []
    jira_issues = _fetch_jira_issues(httpx) if _has_atlassian_creds() else []
    slack_msgs = _fetch_slack_messages() if _has_slack_creds() else []

    markdown = _build_markdown(today_iso, confluence_pages, jira_issues, slack_msgs)

    # Write artifact under out_dir
    out_path = Path(config.out_dir) / f"daily-digest-{today_iso}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    publish_summary = ""
    if not config.dry_run:
        publish_summary = _publish(today_iso, markdown, httpx)

    counts = (
        f"{len(confluence_pages)} pages, {len(jira_issues)} issues, {len(slack_msgs)} msgs"
    )
    return RoutineResult(
        name="daily_digest",
        status=OK if any([confluence_pages, jira_issues, slack_msgs]) else WARN,
        summary=f"daily digest {today_iso} — {counts}. {publish_summary}".strip(),
        artifacts=[out_path],
        runtime_seconds=time.time() - started,
    )


# ---------------------------------------------------------------------------
# Source gathering
# ---------------------------------------------------------------------------

def _has_atlassian_creds() -> bool:
    return bool(os.environ.get("ATLASSIAN_API_TOKEN") and os.environ.get("ATLASSIAN_EMAIL"))


def _has_slack_creds() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN"))


def _atlassian_auth() -> tuple[str, str]:
    return (os.environ["ATLASSIAN_EMAIL"], os.environ["ATLASSIAN_API_TOKEN"])


def _fetch_confluence_pages(httpx) -> list[dict]:
    conf_url = os.environ.get("CONFLUENCE_BASE_URL", "")
    spaces_raw = os.environ.get("CONFLUENCE_WATCHER_SPACES", "")
    spaces = [s.strip() for s in spaces_raw.split(",") if s.strip()]
    if not spaces:
        return []

    since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y/%m/%d %H:%M")
    spaces_cql = ",".join(f'"{s}"' for s in spaces)
    try:
        resp = httpx.get(
            f"{conf_url}/rest/api/content/search",
            params={
                "cql": f'space IN ({spaces_cql}) AND lastmodified > "{since}" AND type = page',
                "limit": 50,
                "expand": "version,space",
            },
            auth=_atlassian_auth(),
            timeout=15,
        )
        if resp.status_code == 200:
            results = []
            for p in resp.json().get("results", []):
                results.append({
                    "id": p.get("id"),
                    "title": p.get("title", ""),
                    "space": p.get("space", {}).get("key", ""),
                    "webui": (conf_url + p.get("_links", {}).get("webui", "")),
                    "when": p.get("version", {}).get("when", ""),
                })
            return results
        log.warning("Confluence digest fetch HTTP %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Confluence digest fetch error: %s", exc)
    return []


def _fetch_jira_issues(httpx) -> list[dict]:
    jira_url = os.environ.get("JIRA_BASE_URL", "")
    jql = os.environ.get(
        "JIRA_DIGEST_JQL",
        'updated > -24h ORDER BY updated DESC',
    )
    try:
        resp = httpx.get(
            f"{jira_url}/rest/api/3/search/jql",
            params={"jql": jql, "maxResults": 50, "fields": "summary,status,assignee,updated,priority"},
            auth=_atlassian_auth(),
            timeout=15,
        )
        if resp.status_code == 200:
            results = []
            for issue in resp.json().get("issues", []):
                f = issue.get("fields", {})
                results.append({
                    "key": issue.get("key"),
                    "summary": f.get("summary", ""),
                    "status": f.get("status", {}).get("name", ""),
                    "assignee": (f.get("assignee") or {}).get("displayName") or "Unassigned",
                    "priority": (f.get("priority") or {}).get("name", ""),
                    "url": f"{jira_url}/browse/{issue.get('key')}",
                })
            return results
        log.warning("Jira digest fetch HTTP %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Jira digest fetch error: %s", exc)
    return []


def _fetch_slack_messages() -> list[dict]:
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    channels_raw = os.environ.get("TEAMMATE_DIGEST_SLACK_CHANNELS") or os.environ.get(
        "TEAMMATE_SLACK_CHANNELS", ""
    )
    channels = [c.strip() for c in channels_raw.split(",") if c.strip()]
    if not bot_token or not channels:
        return []

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return []

    client = WebClient(token=bot_token)
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).timestamp()
    results = []

    # Resolve channel name → id
    name_to_id: dict[str, str] = {}
    try:
        for page in client.conversations_list(types="public_channel,private_channel"):
            for ch in page["channels"]:
                if any(ch["name"].lstrip("#") == c.lstrip("#") for c in channels):
                    name_to_id[ch["name"]] = ch["id"]
    except Exception as exc:
        log.warning("Slack conversations.list error: %s", exc)
        return []

    for name, ch_id in name_to_id.items():
        try:
            r = client.conversations_history(channel=ch_id, oldest=str(cutoff), limit=20)
            for m in r.get("messages", []):
                if m.get("subtype"):
                    continue
                text = m.get("text", "")
                if len(text) < 30:
                    continue
                results.append({
                    "channel": name,
                    "user": m.get("user", ""),
                    "text": text[:300],
                    "ts": m.get("ts", ""),
                })
        except SlackApiError as exc:
            log.warning("Slack history error for %s: %s", name, exc.response.get("error"))
    return results


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _build_markdown(today: str, pages: list[dict], issues: list[dict], msgs: list[dict]) -> str:
    lines = [f"# Daily Digest — {today}", ""]

    lines.append(f"_Last 24 hours: {len(pages)} Confluence pages, "
                 f"{len(issues)} Jira issues, {len(msgs)} Slack messages._")
    lines.append("")

    if pages:
        lines.append("## Confluence pages updated")
        for p in pages:
            lines.append(f"- **{p['title']}** _(space `{p['space']}`)_ — [link]({p['webui']})")
        lines.append("")

    if issues:
        lines.append("## Jira issues updated")
        for i in issues:
            prio = f" `[{i['priority']}]`" if i.get("priority") else ""
            lines.append(
                f"- [{i['key']}]({i['url']}){prio} — {i['summary']} "
                f"_(status: {i['status']}, assignee: {i['assignee']})_"
            )
        lines.append("")

    if msgs:
        lines.append("## Slack discussion highlights")
        for m in msgs:
            text = m["text"].replace("\n", " ")
            lines.append(f"- _#{m['channel']}_ — {text[:200]}")
        lines.append("")

    if not (pages or issues or msgs):
        lines.append("_(no activity in the last 24 hours)_")
        lines.append("")

    lines.append("---")
    lines.append(
        f"_Generated by `vigil agent run daily_digest` at "
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}._"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def _publish(today: str, markdown: str, httpx) -> str:
    """Post to Slack + Confluence. Returns a one-line summary of what was published."""
    parts = []

    slack_result = _publish_slack(today, markdown)
    if slack_result:
        parts.append(slack_result)

    conf_result = _publish_confluence(today, markdown, httpx)
    if conf_result:
        parts.append(conf_result)

    return " ".join(parts)


def _publish_slack(today: str, markdown: str) -> str:
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("TEAMMATE_DIGEST_SLACK_CHANNEL", "")
    if not bot_token or not channel:
        return ""

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return ""

    # Take first ~30 lines as the Slack summary; full report is in Confluence
    preview = "\n".join(markdown.splitlines()[:30])
    if len(markdown.splitlines()) > 30:
        preview += "\n_… see Confluence page for the full digest._"

    client = WebClient(token=bot_token)
    try:
        client.chat_postMessage(channel=channel, text=preview, mrkdwn=True)
        return "Slack posted."
    except SlackApiError as exc:
        log.warning("Slack post error: %s", exc.response.get("error"))
        return f"Slack failed: {exc.response.get('error')}."


def _publish_confluence(today: str, markdown: str, httpx) -> str:
    if not _has_atlassian_creds():
        return ""

    space_id = os.environ.get("TEAMMATE_DIGEST_SPACE_ID", "")
    parent_id = os.environ.get("TEAMMATE_DIGEST_PARENT_PAGE_ID", "")
    if not space_id or not parent_id:
        return "Confluence skipped (no TEAMMATE_DIGEST_SPACE_ID / PARENT_PAGE_ID)."

    conf_url = os.environ.get("CONFLUENCE_BASE_URL", "")
    auth = _atlassian_auth()
    storage_body = _markdown_to_confluence_storage(markdown)

    # Strategy: rolling page (overwrite) + new archive child per day
    rolling_title = "Daily Digest"
    archive_title = f"Daily Digest — {today}"

    rolling_result = _upsert_page(httpx, conf_url, auth, space_id, parent_id, rolling_title, storage_body)
    archive_result = _create_page(httpx, conf_url, auth, space_id, parent_id, archive_title, storage_body)

    return f"Confluence: rolling={rolling_result}, archive={archive_result}."


def _upsert_page(httpx, conf_url: str, auth: tuple[str, str], space_id: str,
                 parent_id: str, title: str, body_storage: str) -> str:
    """Create page if missing, else update in place (Confluence v2 API)."""
    # Look up existing page by title under this parent
    try:
        r = httpx.get(
            f"{conf_url}/api/v2/spaces/{space_id}/pages",
            params={"title": title, "limit": 5},
            auth=auth, timeout=15,
        )
        if r.status_code == 200:
            for p in r.json().get("results", []):
                if p.get("title") == title and str(p.get("parentId", "")) == str(parent_id):
                    return _update_page(httpx, conf_url, auth, p, body_storage)
    except Exception as exc:
        log.warning("Confluence rolling page lookup error: %s", exc)

    # Not found — create new
    return _create_page(httpx, conf_url, auth, space_id, parent_id, title, body_storage)


def _create_page(httpx, conf_url: str, auth: tuple[str, str], space_id: str,
                 parent_id: str, title: str, body_storage: str) -> str:
    try:
        r = httpx.post(
            f"{conf_url}/api/v2/pages",
            json={
                "spaceId": space_id,
                "status": "current",
                "title": title,
                "parentId": parent_id,
                "body": {"representation": "storage", "value": body_storage},
            },
            auth=auth, timeout=20,
        )
        if r.status_code in (200, 201):
            return f"created({r.json().get('id')})"
        log.warning("Confluence create HTTP %d: %s", r.status_code, r.text[:200])
        return f"create-failed-{r.status_code}"
    except Exception as exc:
        log.warning("Confluence create error: %s", exc)
        return "create-error"


def _update_page(httpx, conf_url: str, auth: tuple[str, str], page: dict, body_storage: str) -> str:
    page_id = page["id"]
    current_version = page.get("version", {}).get("number", 1)
    try:
        r = httpx.put(
            f"{conf_url}/api/v2/pages/{page_id}",
            json={
                "id": page_id,
                "status": "current",
                "title": page["title"],
                "spaceId": page.get("spaceId"),
                "parentId": page.get("parentId"),
                "body": {"representation": "storage", "value": body_storage},
                "version": {"number": current_version + 1},
            },
            auth=auth, timeout=20,
        )
        if r.status_code == 200:
            return f"updated({page_id})"
        log.warning("Confluence update HTTP %d: %s", r.status_code, r.text[:200])
        return f"update-failed-{r.status_code}"
    except Exception as exc:
        log.warning("Confluence update error: %s", exc)
        return "update-error"


def _markdown_to_confluence_storage(markdown: str) -> str:
    """Minimal Markdown→Confluence storage-format converter.

    Confluence's "storage" format is XHTML-ish. We don't need a full parser —
    just enough to render the digest readably. Anything fancier (tables, code
    fences) we ship as <pre> blocks.
    """
    out = []
    in_list = False
    for line in markdown.splitlines():
        stripped = line.strip()

        if stripped.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{_esc(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{_esc(stripped[3:])}</h2>")
        elif stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_md_inline(stripped[2:])}</li>")
        elif stripped.startswith("---"):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr/>")
        elif stripped == "":
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_md_inline(stripped)}</p>")

    if in_list:
        out.append("</ul>")
    return "".join(out)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_inline(s: str) -> str:
    """Inline markdown: **bold**, _italic_, [link](url), `code`."""
    import re

    s = _esc(s)
    # [text](url) → <a href="url">text</a>
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    # **bold**
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    # `code`
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # _italic_ (not inside HTML attribute values — rough heuristic)
    s = re.sub(r"\b_([^_]+)_\b", r"<em>\1</em>", s)
    return s
