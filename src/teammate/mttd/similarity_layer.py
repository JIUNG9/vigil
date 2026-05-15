"""Layer 2 — similarity layer.

When an alert fires (or any 'symptom' is described), embed it via Ollama
and Qdrant-search the past-incident corpus. Return the top N most similar
historical incidents with their resolutions.

This is the high-signal half of MTTD. We deliberately do NOT do
LLM-prediction-of-novel-incidents — industry track record on that is bad.

Input is a symptom string like:
    "dp-prod-rds CPU 88% for 7m, log: idle in transaction, deploy 2h ago: PN-1834"

Output is a list of similar past incidents:
    [
      {"key": "INFRA-2391", "title": "...", "score": 0.89, "resolution": "rollback PN-1834"},
      {"key": "INCD-1102",  "title": "...", "score": 0.74, "resolution": "failover"},
    ]
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class SimilarIncident:
    key: str               # e.g. INFRA-2391
    title: str
    score: float
    path: str              # archive/jira/INFRA/INFRA-2391.md
    resolution_summary: str | None = None
    duration_min: int | None = None


class SimilarityLayer:
    def __init__(self):
        self.qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
        self.collection = os.environ.get("QDRANT_COLLECTION", "brain")
        self.ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
        self.embed_model = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        # Restrict similarity search to past-incident projects
        projects_raw = os.environ.get("MTTD_INCIDENT_PROJECTS", "INCD,INFRA")
        self.incident_projects = [p.strip() for p in projects_raw.split(",") if p.strip()]

    def find_similar(self, symptom: str, top_k: int = 3, score_floor: float = 0.6) -> list[SimilarIncident]:
        import httpx

        # Embed the symptom
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.embed_model, "prompt": symptom},
            )
            r.raise_for_status()
            vector = r.json()["embedding"]

        # Build Qdrant filter — restrict to archive/jira/<INCIDENT_PROJECTS>/
        prefix_conditions = [
            {"key": "path", "match": {"text": f"archive/jira/{p}/"}}
            for p in self.incident_projects
        ]
        filter_body = {"should": prefix_conditions} if prefix_conditions else {}

        body = {
            "vector": vector,
            "limit": top_k * 3,  # over-fetch then dedupe by doc
            "with_payload": True,
        }
        if filter_body:
            body["filter"] = filter_body

        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/search",
                json=body,
            )
            r.raise_for_status()
            hits = r.json().get("result", [])

        # Dedupe by source path — multiple chunks can match one doc
        seen: dict[str, dict] = {}
        for h in hits:
            path = h["payload"]["path"]
            if path in seen:
                continue
            if h["score"] < score_floor:
                continue
            seen[path] = h
            if len(seen) >= top_k:
                break

        results: list[SimilarIncident] = []
        for path, hit in seen.items():
            # Derive issue key from path like archive/jira/INFRA/INFRA-2391.md
            key = path.split("/")[-1].replace(".md", "")
            text = hit["payload"].get("text", "")
            # Crude title extraction — first "# KEY — title" line
            title = ""
            for line in text.splitlines()[:5]:
                if line.startswith("# ") and " — " in line:
                    title = line.split(" — ", 1)[1].strip()
                    break
            results.append(SimilarIncident(
                key=key,
                title=title or key,
                score=round(hit["score"], 3),
                path=path,
            ))

        return results
