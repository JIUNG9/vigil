"""FastAPI service for war-room state + SSE per-incident.

Mounts at /war (separately deployable from chat-api). Routes:

  POST   /incident                       — create from auto/eng/cs source
  GET    /incident                       — list (filterable by state)
  GET    /incident/<id>                  — full state (incident + preload + events)
  POST   /incident/<id>/event            — client-agent telemetry from vigil-client-hook
  POST   /incident/<id>/transition       — state transition (triage→open, open→active, etc.)
  POST   /incident/<id>/destructive-check — soft-gate approval check for client-agent hooks
  POST   /incident/<id>/resolve          — closes incident + triggers postmortem drafter
  GET    /incident/<id>/sse              — Server-Sent Events stream (chat + events + state changes)
  POST   /slack/command                  — Slack slash command dispatch (/war, /war-report, /war-list, /war-join)
  GET    /healthz                        — health probe
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import StreamingResponse, JSONResponse
    from pydantic import BaseModel
except ImportError as exc:
    raise RuntimeError("FastAPI required: pip install 'claude-vigil[war]'") from exc

log = logging.getLogger(__name__)

app = FastAPI(title="vigil-war-api", version="3.0.0")

POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateIncidentRequest(BaseModel):
    source: str                # auto / eng / cs
    title: str
    summary: str = ""
    severity: str = "medium"
    affected_service: str | None = None
    declared_by: str = ""
    skip_triage: bool = False


class TransitionRequest(BaseModel):
    target_state: str          # open / active / resolved / dismissed
    actor: str


class EventRequest(BaseModel):
    phase: str | None = None   # pre / post (for client-agent mirror)
    user: str
    tool_name: str | None = None
    tool_input_summary: str | None = None
    tool_response_summary: str | None = None
    text: str | None = None    # for plain chat events
    ts: float | None = None


class DestructiveCheckRequest(BaseModel):
    user: str
    command: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": app.version}


@app.post("/incident")
async def create_incident(req: CreateIncidentRequest):
    from vigil.war.alert_bridge import create_incident, IncidentSource
    try:
        src = IncidentSource(req.source)
    except ValueError:
        raise HTTPException(400, f"unknown source: {req.source}")
    incident = create_incident(
        source=src,
        title=req.title,
        summary=req.summary,
        severity=req.severity,
        affected_service=req.affected_service,
        declared_by=req.declared_by,
        skip_triage=req.skip_triage,
    )
    return {"id": incident.id, "state": incident.state.value}


@app.get("/incident")
async def list_incidents(state: str | None = None, limit: int = 50):
    rows = _query(
        """SELECT id, source, state, title, severity, affected_service,
                  declared_by, created_at, resolved_at
           FROM incidents
           WHERE (%s IS NULL OR state = %s)
           ORDER BY created_at DESC LIMIT %s""",
        (state, state, limit),
    )
    return {"incidents": [_row_to_dict(r) for r in rows]}


@app.get("/incident/{incident_id}")
async def get_incident(incident_id: str):
    rows = _query("SELECT * FROM incidents WHERE id = %s", (incident_id,))
    if not rows:
        raise HTTPException(404, "incident not found")
    preload = _query("SELECT * FROM incident_preload WHERE incident_id = %s", (incident_id,))
    events = _query(
        "SELECT event_type, actor, payload, created_at FROM incident_events "
        "WHERE incident_id = %s ORDER BY created_at DESC LIMIT 200",
        (incident_id,),
    )
    return {
        "incident": _row_to_dict(rows[0]),
        "preload": _row_to_dict(preload[0]) if preload else None,
        "events": [_row_to_dict(e) for e in events],
    }


@app.post("/incident/{incident_id}/event")
async def post_event(incident_id: str, req: EventRequest):
    # Build event_type from phase + tool_name (for mirror events) or use 'chat'
    if req.phase and req.tool_name:
        event_type = f"mirror_{req.phase}"
        payload = {
            "tool_name": req.tool_name,
            "tool_input_summary": req.tool_input_summary,
            "tool_response_summary": req.tool_response_summary,
        }
    else:
        event_type = "chat"
        payload = {"text": req.text or ""}

    _execute(
        """INSERT INTO incident_events (incident_id, event_type, actor, payload)
           VALUES (%s, %s, %s, %s::jsonb)""",
        (incident_id, event_type, req.user, json.dumps(payload)),
    )
    await _publish_sse(incident_id, {"type": event_type, "actor": req.user, "payload": payload})
    return {"ok": True}


@app.post("/incident/{incident_id}/transition")
async def transition_incident(incident_id: str, req: TransitionRequest):
    from vigil.war.alert_bridge import transition, IncidentState
    try:
        target = IncidentState(req.target_state)
    except ValueError:
        raise HTTPException(400, f"unknown state: {req.target_state}")
    ok = transition(incident_id, target, req.actor)
    if not ok:
        raise HTTPException(404, "incident not found")
    await _publish_sse(incident_id, {"type": "state_change", "actor": req.actor,
                                     "payload": {"new_state": target.value}})

    # If transitioning to resolved, kick off the postmortem drafter (best-effort async).
    if target == IncidentState.RESOLVED:
        from vigil.war.postmortem import draft_postmortem
        try:
            asyncio.create_task(asyncio.to_thread(draft_postmortem, incident_id))
        except Exception as exc:
            log.warning("postmortem drafter failed to schedule: %s", exc)

    return {"ok": True, "new_state": target.value}


@app.post("/incident/{incident_id}/destructive-check")
async def destructive_check(incident_id: str, req: DestructiveCheckRequest):
    """Client-agent hook calls this before a destructive command. Returns
    {approved: bool, reason: str} based on whether the incident lead has
    approved this user's pending request."""
    rows = _query(
        """SELECT payload FROM incident_events
           WHERE incident_id = %s AND event_type = 'destructive_approval'
                 AND payload->>'user' = %s
                 AND created_at > now() - interval '10 minutes'
           ORDER BY created_at DESC LIMIT 1""",
        (incident_id, req.user),
    )
    if rows:
        approved = rows[0][0].get("approved", False)
        return {"approved": approved, "reason": rows[0][0].get("reason", "")}

    # Not approved yet — register the pending request and notify lead
    _execute(
        """INSERT INTO incident_events (incident_id, event_type, actor, payload)
           VALUES (%s, 'destructive_pending', %s, %s::jsonb)""",
        (incident_id, req.user, json.dumps({"command": req.command[:200]})),
    )
    await _publish_sse(incident_id, {
        "type": "destructive_pending",
        "actor": req.user,
        "payload": {"command": req.command[:200]},
    })
    return {"approved": False, "reason": "awaiting incident-lead approval"}


@app.post("/incident/{incident_id}/resolve")
async def resolve(incident_id: str, req: TransitionRequest):
    """Convenience route: transition to resolved + trigger postmortem drafter."""
    req.target_state = "resolved"
    return await transition_incident(incident_id, req)


# SSE subscribers per-incident (in-memory; if war-api scales >1 replica, switch to Postgres LISTEN/NOTIFY)
_subscribers: dict[str, list[asyncio.Queue]] = {}


@app.get("/incident/{incident_id}/sse")
async def sse(incident_id: str) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(incident_id, []).append(queue)

    async def stream() -> AsyncIterator[str]:
        try:
            # initial snapshot
            snap = await get_incident(incident_id)
            yield f"event: snapshot\ndata: {json.dumps(snap, default=str)}\n\n"
            while True:
                ev = await queue.get()
                yield f"event: update\ndata: {json.dumps(ev, default=str)}\n\n"
        finally:
            try:
                _subscribers[incident_id].remove(queue)
            except (ValueError, KeyError):
                pass

    return StreamingResponse(stream(), media_type="text/event-stream")


async def _publish_sse(incident_id: str, event: dict) -> None:
    for q in list(_subscribers.get(incident_id, [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Slack slash commands
# ---------------------------------------------------------------------------

@app.post("/slack/command")
async def slack_command(request: Request):
    """Dispatch /war /war-report /war-list /war-join slash commands.

    Slack POSTs form-encoded body with `command`, `text`, `user_id`, etc.
    """
    form = await request.form()
    command = form.get("command", "")
    text = form.get("text", "")
    user = form.get("user_name") or form.get("user_id", "unknown")

    if command == "/war":
        from vigil.war.alert_bridge import create_incident, IncidentSource
        inc = create_incident(
            source=IncidentSource.ENG_MANUAL,
            title=text or "Engineer-declared incident",
            declared_by=user,
            skip_triage=True,
        )
        return _slack_response(f":rotating_light: *Incident {inc.id} created* — state: ACTIVE. War-room: <https://chat.vigil.placen.net/war/{inc.id}|open>")

    if command == "/war-report":
        from vigil.war.alert_bridge import create_incident, IncidentSource
        inc = create_incident(
            source=IncidentSource.CS_MANUAL,
            title=text or "CS-reported issue",
            declared_by=user,
        )
        return _slack_response(f":mag: *Triage entry {inc.id} created.* On-call will review and either open as incident or dismiss. State: TRIAGE.")

    if command == "/war-list":
        rows = _query("SELECT id, state, title FROM incidents WHERE state IN ('triage','open','active') ORDER BY created_at DESC LIMIT 10")
        if not rows:
            return _slack_response("No active war-rooms or triage entries.")
        lines = [f"• `{r[0]}` (_{r[1]}_) — {r[2]}" for r in rows]
        return _slack_response("*Active incidents:*\n" + "\n".join(lines))

    if command == "/war-join":
        if not text:
            return _slack_response("Usage: `/war-join <incident-id>`")
        return _slack_response(
            f"Run this in your terminal to join war-room `{text}`:\n"
            f"```eval \"$(vigil war join {text})\"```\n"
            f"Then start your Claude Code session as usual — actions will mirror."
        )

    return _slack_response(f"Unknown command: {command}")


def _slack_response(text: str) -> JSONResponse:
    return JSONResponse({"response_type": "in_channel", "text": text})


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _query(sql: str, params: tuple = ()) -> list[tuple]:
    import psycopg
    if not POSTGRES_DSN:
        return []
    with psycopg.connect(POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except psycopg.ProgrammingError:
                return []


def _execute(sql: str, params: tuple = ()) -> None:
    import psycopg
    if not POSTGRES_DSN:
        return
    with psycopg.connect(POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()


def _row_to_dict(row) -> dict:
    """Best-effort row→dict. Returns positional indices if names unknown."""
    if hasattr(row, "_asdict"):
        return row._asdict()
    return {f"col_{i}": v for i, v in enumerate(row)}
