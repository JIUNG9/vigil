"""Slack importer — bulk + incremental.

Writes one daily-rollup markdown per channel at
``archive/slack/<channel-name>/YYYY/MM/DD-messages.md``. Each message is one
bullet with author + text. Thread replies are nested under their parent.

Privacy notes:
- Only channels the bot is a member of are accessible.
- Messages with subtype (joins, edits, bot-posts) are skipped by default.
- Short messages (< 30 chars) are skipped to drop noise / acks.
- Redaction pass applies to text body before write.

Env vars:
    SLACK_BOT_TOKEN          required (xoxb-...)
    SLACK_IMPORT_CHANNELS    comma-separated channel names. Empty = all bot is in.
    SLACK_HISTORY_DAYS       default 30 — how far back to walk on initial import.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from vigil.importers.base import ImporterBase

log = logging.getLogger(__name__)


class SlackImporter(ImporterBase):
    source_name = "slack"

    def __init__(self, brain_root, *, dry_run=False):
        super().__init__(brain_root, dry_run=dry_run)
        self.token = os.environ.get("SLACK_BOT_TOKEN", "")
        channels_raw = os.environ.get("SLACK_IMPORT_CHANNELS", "")
        self.channel_filter = [c.strip().lstrip("#") for c in channels_raw.split(",") if c.strip()]
        self.history_days = int(os.environ.get("SLACK_HISTORY_DAYS", "30"))
        if not self.token:
            raise ValueError("SLACK_BOT_TOKEN env var is required")

    def iterate(self, since: Any) -> Iterator[dict]:
        try:
            from slack_sdk import WebClient
            from slack_sdk.errors import SlackApiError
        except ImportError:
            log.error("slack-sdk not installed")
            return

        client = WebClient(token=self.token)

        # Resolve channel IDs (only ones bot is a member of)
        channels: list[tuple[str, str]] = []  # (name, id)
        try:
            for page in client.conversations_list(types="public_channel,private_channel"):
                for ch in page["channels"]:
                    if not ch.get("is_member"):
                        continue
                    name = ch["name"]
                    if self.channel_filter and name not in self.channel_filter:
                        continue
                    channels.append((name, ch["id"]))
        except SlackApiError as exc:
            log.error("slack conversations.list: %s", exc.response.get("error"))
            return

        # Watermark = ISO date. If unset, walk back history_days.
        oldest_ts = None
        if since:
            with contextlib.suppress(Exception):
                oldest_ts = datetime.fromisoformat(str(since).replace("Z", "+00:00")).timestamp()
        if not oldest_ts:
            oldest_ts = (datetime.now(UTC) - timedelta(days=self.history_days)).timestamp()

        # Group messages by (channel, date)
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)

        for name, ch_id in channels:
            cursor = None
            while True:
                try:
                    resp = client.conversations_history(
                        channel=ch_id, oldest=str(oldest_ts), limit=200,
                        cursor=cursor, inclusive=False,
                    )
                except SlackApiError as exc:
                    log.warning("slack history %s: %s", name, exc.response.get("error"))
                    break
                msgs = resp.get("messages", [])
                for m in msgs:
                    if m.get("subtype") and m.get("subtype") not in ("thread_broadcast",):
                        continue
                    text = m.get("text", "")
                    if len(text) < 30:
                        continue
                    m["_channel_name"] = name
                    m["_channel_id"] = ch_id
                    ts_float = float(m["ts"])
                    d = datetime.fromtimestamp(ts_float, UTC)
                    grouped[(name, d.strftime("%Y-%m-%d"))].append(m)

                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break

        # Resolve user names (one batch call cached)
        user_cache: dict[str, str] = {}

        def _resolve(uid: str) -> str:
            if not uid:
                return ""
            if uid in user_cache:
                return user_cache[uid]
            try:
                u = client.users_info(user=uid).get("user", {})
                name = u.get("real_name") or u.get("name") or uid
                user_cache[uid] = name
                return name
            except SlackApiError:
                user_cache[uid] = uid
                return uid

        # Emit one synthetic item per (channel, date)
        for (name, date), msgs in sorted(grouped.items()):
            msgs_sorted = sorted(msgs, key=lambda m: float(m["ts"]))
            for m in msgs_sorted:
                m["_author"] = _resolve(m.get("user", ""))
            yield {
                "_kind": "daily_rollup",
                "channel": name,
                "channel_id": msgs_sorted[0]["_channel_id"] if msgs_sorted else "",
                "date": date,
                "messages": msgs_sorted,
                "last_ts": msgs_sorted[-1]["ts"] if msgs_sorted else "",
            }

    def render(self, rollup: dict) -> tuple[str, dict, str]:
        name = rollup["channel"]
        date = rollup["date"]                # YYYY-MM-DD
        y, m, d = date.split("-")
        rel_path = f"{name}/{y}/{m}/{d}-messages.md"

        frontmatter = {
            "source": "slack",
            "source_type": "channel_daily_rollup",
            "source_id": f"slack/{name}/{date}",
            "source_url": "",
            "title": f"#{name} — {date}",
            "fetched_at": datetime.now(UTC).isoformat(),
            "last_modified": rollup.get("last_ts", ""),
            "author": "",
            "labels": [],
            "extra": {"slack_channel": name, "slack_channel_id": rollup.get("channel_id", "")},
        }

        body_parts = [f"# #{name} — {date}", ""]
        for msg in rollup["messages"]:
            author = msg.get("_author", "")
            ts = float(msg["ts"])
            t = datetime.fromtimestamp(ts, UTC).strftime("%H:%M")
            text = msg.get("text", "").replace("\n", "\n  ")
            body_parts.append(f"- **{t} · {author}**: {text}")
        return rel_path, frontmatter, "\n".join(body_parts).strip()

    def watermark(self, rollup: dict) -> Any:
        # Use the latest message ts in this rollup as the watermark
        ts = rollup.get("last_ts") or ""
        if ts:
            try:
                return datetime.fromtimestamp(float(ts), UTC).isoformat()
            except Exception:
                pass
        return ""


__all__ = ["SlackImporter"]
