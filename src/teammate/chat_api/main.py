"""FastAPI app for teammate v1.

Run with:
    uvicorn teammate.chat_api.main:app --host 0.0.0.0 --port 8000

Env vars:
  QDRANT_URL              default http://qdrant:6333
  QDRANT_COLLECTION       default brain
  OLLAMA_URL              default http://ollama:11434
  OLLAMA_LLM_MODEL        default llama3.2:3b
  OLLAMA_EMBED_MODEL      default nomic-embed-text
  TEAMMATE_NAMESPACE      default teammate-agent
  TEAMMATE_TOP_K          default 6
  TEAMMATE_SCORE_FLOOR    default 0.5
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import StreamingResponse, JSONResponse
    from pydantic import BaseModel
except ImportError as exc:
    raise RuntimeError("FastAPI required: pip install 'claude-teammate[chat-api]'") from exc

import httpx

app = FastAPI(title="teammate-chat-api", version="1.0.0")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "brain")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
LLM_MODEL = os.environ.get("OLLAMA_LLM_MODEL", "llama3.2:3b")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
NAMESPACE = os.environ.get("TEAMMATE_NAMESPACE", "teammate-agent")
TOP_K = int(os.environ.get("TEAMMATE_TOP_K", "6"))
SCORE_FLOOR = float(os.environ.get("TEAMMATE_SCORE_FLOOR", "0.5"))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None
    filters: dict | None = None
    source_weights: dict[str, float] | None = None  # per-source weight multiplier


class SearchHit(BaseModel):
    path: str
    text: str
    score: float
    source: str | None = None  # derived from path (jira / confluence / etc.)


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    by_source: dict[str, dict[str, float]]  # {source: {count, avg_score}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{OLLAMA_URL}/api/embeddings",
                              json={"model": EMBED_MODEL, "prompt": text})
        r.raise_for_status()
        return r.json()["embedding"]


async def _qdrant_search(vector: list[float], top_k: int, filters: dict | None = None) -> list[dict]:
    body: dict = {"vector": vector, "limit": top_k, "with_payload": True}
    if filters:
        body["filter"] = filters
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search", json=body)
        r.raise_for_status()
        return r.json().get("result", [])


def _source_from_path(path: str) -> str:
    # Path patterns: archive/jira/..., archive/confluence/..., docs/..., knowledge/...
    if path.startswith("archive/"):
        parts = path.split("/")
        return parts[1] if len(parts) > 1 else "archive"
    if path.startswith("docs/"):
        return "docs"
    if path.startswith("knowledge/"):
        return "knowledge"
    if path.startswith("decisions/"):
        return "decisions"
    return "other"


def _apply_source_weights(hits: list[dict], weights: dict[str, float] | None) -> list[dict]:
    if not weights:
        return hits
    for h in hits:
        src = _source_from_path(h["payload"]["path"])
        w = weights.get(src, 1.0)
        h["score"] = h["score"] * w
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits


def _by_source_stats(hits: list[dict]) -> dict[str, dict[str, float]]:
    """Aggregate per-source count + avg score for the UI confidence badges."""
    grouped: dict[str, list[float]] = {}
    for h in hits:
        src = _source_from_path(h["payload"]["path"])
        grouped.setdefault(src, []).append(h["score"])
    return {
        src: {"count": len(scores), "avg_score": round(sum(scores) / len(scores), 3)}
        for src, scores in grouped.items()
    }


def _build_prompt(query: str, hits: list[dict]) -> str:
    """Construct the LLM prompt with retrieved chunks + grounding instruction."""
    ctx = "\n\n".join(
        f"[{h['payload']['path']}]\n{h['payload']['text']}"
        for h in hits
    )
    return (
        "You are teammate, an assistant grounded in the team's brain repo.\n"
        "Answer ONLY using the chunks below. Cite the source path in [brackets] "
        "after every claim. If chunks don't contain the answer, say 'I don't know.'\n\n"
        f"CHUNKS:\n{ctx}\n\nQUESTION: {query}\n\nANSWER:"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": app.version}


@app.post("/search")
async def search(req: SearchRequest) -> SearchResponse:
    vec = await _embed(req.query)
    raw = await _qdrant_search(vec, req.top_k or TOP_K, req.filters)
    weighted = _apply_source_weights(raw, req.source_weights)
    # Apply score floor
    kept = [h for h in weighted if h["score"] >= SCORE_FLOOR]
    return SearchResponse(
        hits=[SearchHit(
            path=h["payload"]["path"],
            text=h["payload"]["text"],
            score=h["score"],
            source=_source_from_path(h["payload"]["path"]),
        ) for h in kept],
        by_source=_by_source_stats(kept),
    )


async def _stream_llm(prompt: str) -> AsyncIterator[str]:
    """Stream LLM tokens via Ollama /api/generate."""
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_URL}/api/generate",
            json={"model": LLM_MODEL, "prompt": prompt, "stream": True},
        ) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "response" in chunk:
                    yield chunk["response"]
                if chunk.get("done"):
                    return


@app.get("/ask")
async def ask(q: str = Query(..., min_length=1)) -> StreamingResponse:
    """Server-Sent Events: streams the LLM answer + final citations event."""

    async def event_stream() -> AsyncIterator[str]:
        # Phase 1: retrieve
        vec = await _embed(q)
        hits = await _qdrant_search(vec, TOP_K)
        kept = [h for h in hits if h["score"] >= SCORE_FLOOR]
        if not kept:
            yield f"event: error\ndata: {json.dumps({'msg': 'No matching chunks above score floor.'})}\n\n"
            return

        # Phase 2: send retrieved meta to UI
        meta = {
            "by_source": _by_source_stats(kept),
            "citations": [{"path": h["payload"]["path"], "score": round(h["score"], 3)} for h in kept],
        }
        yield f"event: meta\ndata: {json.dumps(meta)}\n\n"

        # Phase 3: stream answer
        prompt = _build_prompt(q, kept)
        async for token in _stream_llm(prompt):
            yield f"event: token\ndata: {json.dumps({'t': token})}\n\n"

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/index-status")
async def index_status() -> dict:
    """Coverage stats per source."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{QDRANT_URL}/collections/{COLLECTION}")
        if r.status_code != 200:
            raise HTTPException(status_code=503, detail="qdrant unreachable")
        info = r.json().get("result", {})
        return {
            "vectors_count": info.get("vectors_count"),
            "points_count": info.get("points_count"),
            "indexed_vectors_count": info.get("indexed_vectors_count"),
            "status": info.get("status"),
            "config": {"vector_size": 768, "distance": "Cosine"},
        }


@app.post("/reindex")
async def reindex(request: Request) -> JSONResponse:
    """Create a K8s Job from teammate-reindex CronJob template. Idempotent via label check."""
    try:
        from kubernetes import client as k8s_client, config as k8s_config
    except ImportError:
        raise HTTPException(503, "kubernetes client not installed")

    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()

    batch = k8s_client.BatchV1Api()
    # Active-job check: if a reindex is running, return its name so the UI can subscribe
    jobs = batch.list_namespaced_job(NAMESPACE, label_selector="teammate-routine=reindex")
    active = [j for j in jobs.items if j.status.active and j.status.active > 0]
    if active:
        return JSONResponse({
            "status": "already-running",
            "job_name": active[0].metadata.name,
            "started_by": active[0].metadata.labels.get("requested-by", "unknown"),
        })

    # Otherwise: create a new Job
    cj = batch.read_namespaced_cron_job("teammate-reindex", NAMESPACE)
    job_name = f"reindex-manual-{int(time.time())}"
    requested_by = request.headers.get("X-Forwarded-User", "anonymous")
    job = k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(
            name=job_name,
            namespace=NAMESPACE,
            labels={"teammate-routine": "reindex", "requested-by": requested_by},
        ),
        spec=cj.spec.job_template.spec,
    )
    batch.create_namespaced_job(NAMESPACE, job)
    return JSONResponse({"status": "created", "job_name": job_name, "started_by": requested_by})


@app.get("/feed")
async def feed() -> dict:
    """Today's digest + recent triggered jobs. UI Feed tab consumes this."""
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        batch = k8s_client.BatchV1Api()
        jobs = batch.list_namespaced_job(NAMESPACE, label_selector="teammate-routine")
        recent_jobs = sorted(
            jobs.items,
            key=lambda j: j.metadata.creation_timestamp,
            reverse=True,
        )[:20]
        return {
            "recent_jobs": [{
                "name": j.metadata.name,
                "routine": j.metadata.labels.get("teammate-routine"),
                "triggered_by": j.metadata.labels.get("triggered-by"),
                "status": (
                    "succeeded" if (j.status.succeeded and j.status.succeeded > 0)
                    else "failed" if (j.status.failed and j.status.failed > 0)
                    else "running"
                ),
                "started_at": j.metadata.creation_timestamp.isoformat(),
            } for j in recent_jobs],
        }
    except Exception as exc:
        log.warning("feed error: %s", exc)
        return {"recent_jobs": [], "error": str(exc)}
