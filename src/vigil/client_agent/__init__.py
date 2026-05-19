"""Client agent — pipx-installable package for engineers' laptops.

When an engineer joins a war-room, this package activates:

1. **Mirror**: PreToolUse + PostToolUse hooks in `~/.claude/settings.json` POST
   every Claude Code tool call to the war-room API. Other participants see
   each engineer's actions live in the war-room UI.

2. **Mediate**: a local MCP server (subprocess) exposes war-room tools
   (`warroom_context`, `warroom_post`, `warroom_alert`) so Claude can read
   the current incident state and report progress back.

3. **Soft-gate**: when a destructive action is detected (`kubectl delete`,
   `terraform destroy`, `aws s3 rm`, etc.), the PreToolUse hook checks with
   the war-room API; if the incident lead hasn't approved, the action is
   blocked.

Lifecycle:
    pipx install claude-vigil-client
    vigil war join <incident_id>     # activates hooks + MCP, exports env
    # ... work happens ...
    vigil war leave                  # deactivates everything
"""

from vigil.client_agent.cli import main

__all__ = ["main"]
