"""Hook executable: `vigil-client-hook`.

Called by Claude Code on every tool call (PreToolUse + PostToolUse). Reads
the tool input from stdin (JSON), POSTs to the war-api `/incident/<id>/event`
endpoint.

For PreToolUse, also checks if the tool is in the soft-gate destructive list;
if so, queries the war-api for lead approval and exits non-zero (blocking the
tool call) until approval is granted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


DESTRUCTIVE_PATTERNS = [
    "kubectl delete",
    "terraform destroy",
    "terraform apply -auto-approve",  # apply WITHOUT plan-review
    "aws s3 rm",
    "aws s3 sync --delete",
    "aws iam delete",
    "aws rds delete",
    "aws elasticache delete",
    "git push --force",
    "git reset --hard origin/",
    "rm -rf /",
]


def main() -> int:
    parser = argparse.ArgumentParser(prog="vigil-client-hook")
    parser.add_argument("--phase", required=True, choices=["pre", "post"])
    parser.add_argument("--incident", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    user = os.environ.get("USER", "unknown")

    # Soft-gate destructive actions (PreToolUse only)
    if args.phase == "pre" and tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if _is_destructive(cmd):
            if not _approved(args.url, args.incident, user, cmd):
                # Block: write a clear blocking reason and exit non-zero
                print(
                    f"⚠️  BLOCKED by vigil-war: destructive action requires "
                    f"incident-lead approval. Command: {cmd[:80]}\n"
                    f"Ask the lead to approve in the war-room, then retry.",
                    file=sys.stderr,
                )
                return 1

    _post_event(args.url, args.incident, args.phase, user, tool_name, tool_input,
                payload.get("tool_response"))
    return 0


def _is_destructive(cmd: str) -> bool:
    return any(pat in cmd for pat in DESTRUCTIVE_PATTERNS)


def _approved(war_url: str, incident_id: str, user: str, cmd: str) -> bool:
    """Check the war-api for lead approval of this user's destructive action."""
    try:
        import httpx
    except ImportError:
        return True  # fail open — don't block if httpx missing
    try:
        r = httpx.post(
            f"{war_url}/incident/{incident_id}/destructive-check",
            json={"user": user, "command": cmd[:200]},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("approved", False)
    except Exception:
        return True  # fail open on network error
    return False


def _post_event(war_url: str, incident_id: str, phase: str, user: str,
                tool_name: str, tool_input: dict, tool_response) -> None:
    try:
        import httpx
    except ImportError:
        return
    try:
        httpx.post(
            f"{war_url}/incident/{incident_id}/event",
            json={
                "phase": phase,
                "user": user,
                "tool_name": tool_name,
                "tool_input_summary": str(tool_input)[:500],
                "tool_response_summary": str(tool_response)[:500] if tool_response else None,
                "ts": time.time(),
            },
            timeout=5,
        )
    except Exception:
        pass  # best-effort — never block Claude Code on telemetry


if __name__ == "__main__":
    sys.exit(main())
