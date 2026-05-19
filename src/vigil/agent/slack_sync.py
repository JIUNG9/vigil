"""Routine 6 — pull pinned messages from declared Slack channels.

Format: each pinned message becomes a small decision-record-style
markdown file the runner can stage as a PR draft.

Config (passed via ``RoutineConfig.extra``):

  channels      list[str]    — channel names declared in
                                 ``[sync.slack] channels`` of the
                                 user's ``.vigil/config.toml``.
  pins          list[dict]   — pre-resolved pin records the runner
                                 pulled via Slack MCP. Shape:
                                 ``{channel, ts, user, text, permalink}``.

The agent does NOT call Slack directly. The runner provides
already-resolved pin records; tests inject the same shape.
"""

from __future__ import annotations

import time
from datetime import date as _date
from pathlib import Path
from typing import Any

from vigil.agent._sync_common import (
    slugify,
    utc_now_iso,
    write_doc,
)
from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult


def _channel_slug(name: str) -> str:
    """``#oncall`` / ``oncall`` → ``oncall``."""
    return slugify((name or "").lstrip("#"), fallback="channel")


def _ts_id(ts: str) -> str:
    """Slack message ts ``"1714000000.001234"`` → ``"1714000000-001234"``."""
    return (ts or "0").replace(".", "-")


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
) -> RoutineResult:
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)

    declared: list[str] = list(config.extra.get("channels") or [])
    pins: list[dict[str, Any]] = list(config.extra.get("pins") or [])

    # Channel-scoped admission. When `channels` is non-empty, only
    # pins from those channels are admitted; pins from undeclared
    # channels are refused (surfaces as `refused=N` in the summary).
    # When `channels` is empty, the routine admits every pin the
    # runner provided — the runner is what holds the Slack token and
    # has already scoped which channels it pulls from. Refusing here
    # would just duplicate the runner's scoping work.
    declared_slugs = {_channel_slug(c) for c in declared}

    written: list[Path] = []
    deduped: list[Path] = []
    refused: list[str] = []
    base = config.out_dir / "slack-imports"

    for pin in pins:
        channel_raw = str(pin.get("channel") or "")
        channel = _channel_slug(channel_raw)
        if not channel:
            refused.append("(missing channel)")
            continue
        if declared_slugs and channel not in declared_slugs:
            refused.append(channel_raw)
            continue
        ts = str(pin.get("ts") or "")
        target = base / channel / f"pin-{_ts_id(ts)}.md"
        text = str(pin.get("text") or "").strip()
        user = str(pin.get("user") or "unknown")
        permalink = str(pin.get("permalink") or "")

        body = (
            f"# Pinned in #{channel}\n\n"
            f"- **Author:** {user}\n"
            f"- **Slack ts:** `{ts or 'unknown'}`\n"
            + (f"- **Permalink:** {permalink}\n\n" if permalink else "\n")
            + "## Message\n\n"
            + (text or "_(empty pin)_")
            + "\n\n## Why this matters\n\n"
            + "_Drafted by `slack_sync`. Promote into `docs/decisions/` if "
            "this is a real decision; close otherwise._\n"
        )
        meta: dict[str, Any] = {
            "source": "slack",
            "channel": channel_raw,
            "slack_ts": ts,
            "author": user,
            "permalink": permalink,
            "last_synced": utc_now_iso(),
        }
        path, wrote = write_doc(target, frontmatter=meta, body=body, revision_key="slack_ts")
        if wrote:
            written.append(path)
        else:
            deduped.append(path)

    status = OK if not refused else WARN
    summary_bits = [
        f"{len(pins)} pin(s)",
        f"wrote={len(written)}",
        f"deduped={len(deduped)}",
    ]
    if refused:
        summary_bits.append(f"refused={len(refused)}")
    summary = "  ".join(summary_bits) if pins or declared else "no channels configured"

    artifacts = list(written) + list(deduped)
    return RoutineResult(
        name="slack_sync",
        status=status,
        summary=summary,
        artifacts=artifacts,
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
