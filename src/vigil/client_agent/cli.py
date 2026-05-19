"""CLI: `vigil war join/leave/status`.

`join <incident_id>`:
  - Writes session hook config to ~/.claude/settings.json (PreToolUse + PostToolUse)
  - Exports TEAMMATE_INCIDENT_ID and TEAMMATE_WAR_API_URL as shell vars
  - Adds the vigil MCP server to the Claude Code MCP config
  - Prints "✅ joined war-room <id>"

`leave`:
  - Removes the hook config
  - Unsets the env vars
  - Removes the MCP server entry
  - Prints "👋 left war-room"

`status`:
  - Shows current incident, hooks active/inactive, MCP active/inactive

This file is small on purpose — the heavy lifting (HTTP calls, MCP server)
is in sibling modules.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


HOOK_NAME = "vigil-war-mirror"
MCP_NAME = "vigil-war"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vigil-war", description="vigil war-room client agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    join = sub.add_parser("join", help="join a war-room (activate hooks + MCP)")
    join.add_argument("incident_id")
    join.add_argument("--war-api-url", default=os.environ.get("TEAMMATE_WAR_API_URL",
                                                              "https://chat.vigil.your-domain.net"))

    sub.add_parser("leave", help="leave the war-room (deactivate hooks + MCP)")
    sub.add_parser("status", help="show current war-room state")

    args = parser.parse_args(argv)
    if args.cmd == "join":
        return _cmd_join(args.incident_id, args.war_api_url)
    if args.cmd == "leave":
        return _cmd_leave()
    if args.cmd == "status":
        return _cmd_status()
    return 1


def _cmd_join(incident_id: str, war_api_url: str) -> int:
    settings_path = _claude_settings_path()
    settings = _load_settings(settings_path)

    # 1. Install hooks
    settings.setdefault("hooks", {})
    settings["hooks"]["PreToolUse"] = settings["hooks"].get("PreToolUse", [])
    settings["hooks"]["PostToolUse"] = settings["hooks"].get("PostToolUse", [])

    hook_cmd_pre = f"vigil-client-hook --phase pre --incident {incident_id} --url {war_api_url}"
    hook_cmd_post = f"vigil-client-hook --phase post --incident {incident_id} --url {war_api_url}"

    if not any(h.get("hooks", [{}])[0].get("command", "") == hook_cmd_pre for h in settings["hooks"]["PreToolUse"]):
        settings["hooks"]["PreToolUse"].append({
            "name": HOOK_NAME, "hooks": [{"type": "command", "command": hook_cmd_pre}]
        })
    if not any(h.get("hooks", [{}])[0].get("command", "") == hook_cmd_post for h in settings["hooks"]["PostToolUse"]):
        settings["hooks"]["PostToolUse"].append({
            "name": HOOK_NAME, "hooks": [{"type": "command", "command": hook_cmd_post}]
        })

    # 2. Add MCP server
    settings.setdefault("mcpServers", {})
    settings["mcpServers"][MCP_NAME] = {
        "command": "vigil-war-mcp",
        "args": [],
        "env": {
            "TEAMMATE_INCIDENT_ID": incident_id,
            "TEAMMATE_WAR_API_URL": war_api_url,
        },
    }

    _save_settings(settings_path, settings)

    # 3. Print shell snippet for env vars (caller `eval`s this)
    print(f"export TEAMMATE_INCIDENT_ID={incident_id}")
    print(f"export TEAMMATE_WAR_API_URL={war_api_url}")
    print(f"echo '✅ joined war-room {incident_id}'", file=sys.stderr)
    return 0


def _cmd_leave() -> int:
    settings_path = _claude_settings_path()
    settings = _load_settings(settings_path)

    for phase in ("PreToolUse", "PostToolUse"):
        if phase in settings.get("hooks", {}):
            settings["hooks"][phase] = [h for h in settings["hooks"][phase] if h.get("name") != HOOK_NAME]

    if MCP_NAME in settings.get("mcpServers", {}):
        del settings["mcpServers"][MCP_NAME]

    _save_settings(settings_path, settings)
    print("unset TEAMMATE_INCIDENT_ID")
    print("unset TEAMMATE_WAR_API_URL")
    print("echo '👋 left war-room'", file=sys.stderr)
    return 0


def _cmd_status() -> int:
    settings_path = _claude_settings_path()
    settings = _load_settings(settings_path)
    active = any(
        h.get("name") == HOOK_NAME
        for phase in ("PreToolUse", "PostToolUse")
        for h in settings.get("hooks", {}).get(phase, [])
    )
    mcp_active = MCP_NAME in settings.get("mcpServers", {})
    incident = os.environ.get("TEAMMATE_INCIDENT_ID", "(none)")
    print(f"Active incident: {incident}")
    print(f"Hooks installed: {'yes' if active else 'no'}")
    print(f"MCP server installed: {'yes' if mcp_active else 'no'}")
    return 0


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
