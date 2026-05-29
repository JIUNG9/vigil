"""War-room (MTTR) module.

When an alert fires (auto MTTD) or an engineer / CS reports an incident
manually, this module:

1. Persists an incident row to Postgres (state machine: triage → open → active → resolved)
2. Runs the 7-panel auto-pre-load:
     ① summary  ② similar past  ③ root-cause candidates  ④ runbooks
     ⑤ action checklist  ⑥ participant proposal  ⑦ live data link
3. Proposes participants (Slack user-group + CODEOWNERS + git blame)
4. Sends batched Slack DM with war-room link
5. Streams events to subscribers via SSE
6. Drafts postmortem on resolve

Three creation paths converge on `create_incident()`:
- auto from MTTD detector
- engineer manual (/war slash command, or UI)
- CS / non-engineer (/war-report → triage gate)
"""

from vigil.war.alert_bridge import IncidentSource, create_incident
from vigil.war.preload import preload_panels

__all__ = ["create_incident", "preload_panels", "IncidentSource"]
