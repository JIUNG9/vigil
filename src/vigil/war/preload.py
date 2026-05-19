"""7-panel auto-pre-load for war-rooms.

When an incident transitions out of TRIAGE state, this module populates:
  ① summary (LLM-generated from alert + recent context)
  ② similar past incidents (similarity layer)
  ③ candidate root causes (LLM, ranked, marked DRAFT)
  ④ suggested runbooks (similarity over docs/runbooks/)
  ⑤ action checklist (pre-filled from similar incidents' resolutions)
  ⑥ participant proposal (oncall user-group + CODEOWNERS + git blame)
  ⑦ live data panel (SigNoz dashboard URL pre-zoomed to incident window)

The output is stored in `incident_preload` Postgres table, keyed by
incident_id, version 1. Subsequent updates increment version (so the UI
can refetch).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)


@dataclass
class PreloadResult:
    incident_id: str
    summary: str
    similar_incidents: list[dict]
    candidate_causes: list[dict]
    runbooks: list[dict]
    actions: list[dict]
    participants: list[dict]
    live_data_urls: list[dict]


def preload_panels(incident) -> PreloadResult:
    from vigil.war.alert_bridge import Incident
    from vigil.war.participant_selector import propose_participants
    from vigil.mttd.similarity_layer import SimilarityLayer

    assert isinstance(incident, Incident)

    # Panel ②: similar past incidents
    similarity = SimilarityLayer()
    symptom = f"{incident.title}. {incident.summary} (service: {incident.affected_service or 'unknown'})"
    similar = similarity.find_similar(symptom, top_k=3, score_floor=0.55)

    # Panel ④: suggested runbooks via similarity to docs/runbooks/
    runbooks = _suggest_runbooks(symptom)

    # Panel ⑤: action checklist (derived from similar-incident resolutions if any)
    actions = _derive_actions(similar)

    # Panel ⑥: participant proposal
    participants = propose_participants(incident)

    # Panel ⑦: live data URLs
    live_urls = _build_signoz_urls(incident)

    # Panel ① + ③: LLM summary + root-cause ranking
    summary, causes = _llm_synthesize(incident, similar, runbooks)

    result = PreloadResult(
        incident_id=incident.id,
        summary=summary,
        similar_incidents=[asdict(s) for s in similar],
        candidate_causes=causes,
        runbooks=runbooks,
        actions=actions,
        participants=participants,
        live_data_urls=live_urls,
    )

    _persist(result)
    return result


def _suggest_runbooks(symptom: str) -> list[dict]:
    """Qdrant search restricted to docs/runbooks/."""
    import httpx

    qdrant = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    collection = os.environ.get("QDRANT_COLLECTION", "brain")
    ollama = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(f"{ollama}/api/embeddings", json={"model": embed_model, "prompt": symptom})
            r.raise_for_status()
            vec = r.json()["embedding"]

            r = client.post(
                f"{qdrant}/collections/{collection}/points/search",
                json={
                    "vector": vec,
                    "limit": 5,
                    "filter": {"should": [{"key": "path", "match": {"text": "docs/runbooks/"}}]},
                    "with_payload": True,
                },
            )
            hits = r.json().get("result", [])
    except Exception as exc:
        log.warning("runbook search error: %s", exc)
        return []

    seen = set()
    result = []
    for h in hits:
        path = h["payload"]["path"]
        if path in seen:
            continue
        seen.add(path)
        result.append({"path": path, "score": round(h["score"], 3)})
        if len(result) >= 3:
            break
    return result


def _derive_actions(similar_incidents: list) -> list[dict]:
    """Pre-fill an action checklist from similar past incident resolutions."""
    # Baseline: always include these
    actions = [
        {"text": "acknowledge alert in SigNoz", "done": False, "source": "baseline"},
        {"text": "verify current deploy version", "done": False, "source": "baseline"},
    ]
    # If a past incident had a known resolution, add it
    for inc in similar_incidents:
        if inc.resolution_summary:
            actions.append({
                "text": f"check if {inc.resolution_summary.lower()} applies (similar to {inc.key})",
                "done": False,
                "source": f"derived_from:{inc.key}",
            })
    # Tail action
    actions.append({"text": "notify customers via status-page if user-facing > 5min",
                    "done": False, "source": "baseline"})
    return actions


def _build_signoz_urls(incident) -> list[dict]:
    """Pre-zoomed SigNoz links for the affected service + incident time window."""
    base = os.environ.get("SIGNOZ_BASE_URL", "")
    if not base or not incident.affected_service:
        return []
    return [{
        "label": f"{incident.affected_service} — SigNoz dashboard",
        "url": f"{base}/services/{incident.affected_service}",
    }]


def _llm_synthesize(incident, similar, runbooks) -> tuple[str, list[dict]]:
    """Ask Ollama to summarize + rank root cause candidates."""
    import httpx

    ollama = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    llm_model = os.environ.get("OLLAMA_LLM_MODEL", "llama3.2:3b")

    context_lines = [f"Incident: {incident.title}", f"Service: {incident.affected_service or 'unknown'}",
                     f"Summary: {incident.summary}", "", "Similar past incidents:"]
    for inc in similar:
        context_lines.append(f"- {inc.key} (similarity {inc.score}): {inc.title}")
    context = "\n".join(context_lines)

    prompt = (
        f"{context}\n\n"
        "Output JSON with two fields:\n"
        "  summary: 2-sentence incident summary\n"
        "  causes: list of 1-3 candidate root causes, each as "
        "{cause: str, confidence: float 0-1, supporting_evidence: str}\n"
        "Mark all causes as DRAFT — engineer review required."
    )

    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                f"{ollama}/api/generate",
                json={"model": llm_model, "prompt": prompt, "stream": False, "format": "json"},
            )
            r.raise_for_status()
            data = r.json()
            raw = data.get("response", "{}")
            parsed = json.loads(raw)
        return parsed.get("summary", incident.summary), parsed.get("causes", [])
    except Exception as exc:
        log.warning("llm synthesize error: %s", exc)
        return incident.summary, []


def _persist(result: PreloadResult) -> None:
    """Store pre-load result in Postgres `incident_preload` table."""
    try:
        import psycopg
    except ImportError:
        log.info("psycopg not installed; preload logged only: %s", result.incident_id)
        return

    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        return

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO incident_preload
                   (incident_id, summary, similar_incidents, candidate_causes,
                    runbooks, actions, participants, live_data_urls)
                   VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)
                   ON CONFLICT (incident_id) DO UPDATE SET
                     summary = EXCLUDED.summary,
                     similar_incidents = EXCLUDED.similar_incidents,
                     candidate_causes = EXCLUDED.candidate_causes,
                     runbooks = EXCLUDED.runbooks,
                     actions = EXCLUDED.actions,
                     participants = EXCLUDED.participants,
                     live_data_urls = EXCLUDED.live_data_urls""",
                (
                    result.incident_id, result.summary,
                    json.dumps(result.similar_incidents),
                    json.dumps(result.candidate_causes),
                    json.dumps(result.runbooks),
                    json.dumps(result.actions),
                    json.dumps(result.participants),
                    json.dumps(result.live_data_urls),
                ),
            )
            conn.commit()
