"""Participant selection for war-rooms.

Priority order (configurable):
  1. Slack user-group `@oncall-<team>` (current rotation)
  2. CODEOWNERS of the affected service in the brain repo
  3. git blame on files recently touched in the affected service (last 14d)
  4. Recent war-room peers (engineers who joined the last 5 incidents on this service)

Output: list of {user, source, priority, reason}. Caller (war_api) batches
into a single Slack message with @user1 @user2 ... rather than N DMs.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)


def propose_participants(incident) -> list[dict]:
    """Return ranked candidate participants for the war-room."""
    candidates: list[dict] = []

    # 1. Slack user-group (most authoritative)
    oncall = _slack_usergroup_members(os.environ.get("ONCALL_USER_GROUP", "@oncall-devsecops"))
    for u in oncall:
        candidates.append({"user": u, "source": "oncall", "priority": 1,
                           "reason": "current on-call rotation"})

    # 2. CODEOWNERS for the affected service (if known)
    if incident.affected_service:
        owners = _codeowners_for(incident.affected_service)
        for u in owners:
            if not any(c["user"] == u for c in candidates):
                candidates.append({"user": u, "source": "codeowners", "priority": 2,
                                   "reason": f"CODEOWNERS for {incident.affected_service}"})

    # 3. Recent committers (last 14 days) on the service
    if incident.affected_service:
        committers = _recent_committers(incident.affected_service, days=14)
        for u in committers:
            if not any(c["user"] == u for c in candidates):
                candidates.append({"user": u, "source": "git-blame", "priority": 3,
                                   "reason": f"committed to {incident.affected_service} in last 14d"})

    return candidates[:8]  # Cap at 8 to avoid notification spam


def _slack_usergroup_members(group_handle: str) -> list[str]:
    """Fetch current members of a Slack user-group."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        return []

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return []

    client = WebClient(token=bot_token)
    try:
        # First, find the group ID
        groups = client.usergroups_list(include_users=True)
        for g in groups.get("usergroups", []):
            if f"@{g['handle']}" == group_handle.lstrip("@") or g["handle"] == group_handle.lstrip("@"):
                return g.get("users", [])
    except SlackApiError as exc:
        log.warning("slack usergroups_list failed: %s", exc.response.get("error"))
    return []


def _codeowners_for(service: str) -> list[str]:
    """Parse CODEOWNERS files in brain repo for the given service."""
    brain_root = os.environ.get("TEAMMATE_BRAIN_ROOT", ".")
    candidates = [
        f"{brain_root}/CODEOWNERS",
        f"{brain_root}/.github/CODEOWNERS",
        f"{brain_root}/docs/CODEOWNERS",
    ]
    for path in candidates:
        try:
            with open(path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            continue
        # Match lines where the pattern contains the service name
        for line in lines:
            line = line.split("#")[0].strip()
            if not line or service not in line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            return [u.lstrip("@") for u in parts[1:] if u.startswith("@")]
    return []


def _recent_committers(service: str, days: int = 14) -> list[str]:
    """Most-frequent committers to files matching `service` in the last `days` days."""
    brain_root = os.environ.get("TEAMMATE_BRAIN_ROOT", ".")
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        out = subprocess.check_output(
            ["git", "-C", brain_root, "log", f"--since={since}", "--format=%an", "--", f"*{service}*"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    counts: dict[str, int] = {}
    for line in out.splitlines():
        name = line.strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)][:5]
