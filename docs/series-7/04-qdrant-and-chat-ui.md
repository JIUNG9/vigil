# From Per-Pod SQLite to a k8s-Native Qdrant + Streaming Chat UI

**Tags:** `Kubernetes` `Qdrant` `FastAPI` `Server-Sent Events` `Vector DB` `RAG`

---

> Part 4 of "Building teammate." How we replaced 12 agent routines each rebuilding their own SQLite vector index (45 min per Job) with a single persistent Qdrant deployment + a streaming chat UI that ships per-source confidence badges.

---

## The Problem

The original teammate design was **local-first by philosophy**. Every engineer's laptop had its own SQLite-vec index, rebuilt on demand. Every Kubernetes agent Job had its own ephemeral index, built in an `emptyDir` and discarded when the pod exited. The brain itself was a git repo; the index was a pure function of the brain content.

For ~100 markdown files, this was fine. `teammate init` embedded the whole corpus in 10 seconds.

After we shipped the four-source importers (part 3 of this series), the brain went from ~100 files to **24,853**. Embedding the whole corpus now takes ~30-45 minutes on cluster-local Ollama. And every cron run of the agent routines was doing it from scratch. Twelve routines × 30 minutes = six hours of wasted Ollama time per night.

The fix is obvious: build the index **once**, persist it, let everyone read.

The less-obvious choice is *how*. Specifically:

- Which vector store?
- Who writes to it?
- How do consumers read concurrently?
- How do we present per-source confidence in the chat answers without leaking ranking complexity to the UI?

---

## Decision Matrix: Vector Stores at ~25k Docs Scale

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| sqlite-vec on a PVC | Zero new deps. One file. ~50 MB binary. | Writer lock; no per-source metadata filtering at scale; doesn't scale past 1 writer. | Works, but feels like outgrowing it on day 1 |
| pgvector | Mature; SQL filtering; backups built-in. | Need to run Postgres (HA, backups, tuning). Adds a dependency we'd have to operate. | Best if you already run Postgres |
| **Qdrant** | Purpose-built. Helm chart. Snapshots. Metadata filters. gRPC + REST. | New service to operate. | **Best fit for vector-DB scale + ops maturity** |
| Weaviate | Strong schema. | Heavier (Java baseline). Schema friction. | Overkill |
| Chroma | Python-native. Simple. | Smaller community, less proven at our scale. | OK, but Qdrant is the safer bet |

We picked Qdrant. Key reasons:

1. **Native metadata filtering** — `filter: {source: "jira", project: "INFRA"}` is one of the most important features for unified search across four sources. sqlite-vec can do this, but only with custom indexes and per-source query hacks.
2. **Snapshot API** — backup via a single HTTP call to S3. Disaster recovery becomes a cron job.
3. **Concurrent reads, lock-free** — Qdrant handles MVCC internally. Our chat-api can scale horizontally without coordinating with the indexer.
4. **One Helm chart** — `helm upgrade --install qdrant qdrant/qdrant` deploys it. We're not building a vector DB; we're integrating one.

---

## The Single-Writer, Multi-Reader Pattern

Here's the load profile:

| Operation | Frequency | Latency target |
|---|---|---|
| **Index build** (full corpus) | Nightly + manual rebuild | minutes (acceptable) |
| **Index update** (incremental, per changed doc) | Hourly | seconds |
| **Search** (chat answers) | ~200/day | <100ms |
| **Search** (agent routines, similarity, MTTD) | ~5000/day | <100ms |

That's a 25× read:write ratio. The right model is:

- **One indexer Deployment** (replicas: 1, strategy: Recreate). Single writer. No coordination needed because there's literally only one.
- **N stateless reader services** (chat-api Deployment, agent CronJobs, war-room API) that all hit Qdrant via HTTP for search.

Qdrant handles concurrent reads natively — no lock contention.

```python
# The indexer is conceptually a loop. Real implementation skips
# already-embedded chunks via per-doc SHA tracking.
class Indexer:
    def run(self) -> IndexResult:
        result = IndexResult()
        self.ensure_collection()
        for path in self._iter_markdown():
            source_sha = self._sha(path)
            if self._already_indexed(path, source_sha):
                result.docs_unchanged += 1
                continue
            chunks = list(self._chunk_file(path))
            vectors = self._embed_batch([c["text"] for c in chunks])
            self._upsert(path, source_sha, chunks, vectors)
            result.docs_embedded += 1
        return result
```

The per-doc SHA gate is what makes nightly runs cheap. First run: 30 minutes to embed everything. Every subsequent run: ~3 minutes for the handful of docs that actually changed.

---

## Idempotency via Deterministic Point IDs

Qdrant points need integer or UUID IDs. We generate them deterministically from `(path, chunk_idx)`:

```python
pid = hashlib.md5(f"{rel_path}:{chunk_idx}".encode()).hexdigest()
point_id = int(pid[:15], 16)  # 60-bit int, well within Qdrant's range
```

This means re-running the indexer with no source changes is a no-op — the same chunk produces the same ID, and Qdrant's upsert is idempotent. Critically, when we DO re-index a changed doc, we explicitly DELETE all prior chunks for that path first:

```python
client.post(
    f"{qdrant_url}/collections/brain/points/delete",
    json={"filter": {"must": [{"key": "path", "match": {"value": rel}}]}},
)
# Then upsert the new chunks.
```

Without the delete, an edit that *shortens* a file would leave orphan chunks (from the longer prior version) in the index, polluting future search results. Took me one annoying afternoon to figure out why search was returning text that no longer existed in the brain.

---

## The Chat API: FastAPI + Server-Sent Events

The chat-api is the new public surface. Three endpoints carry the load:

| Endpoint | Purpose |
|---|---|
| `POST /search` | Top-K chunks with per-source aggregated confidence — JSON |
| `GET /ask?q=...` | Server-Sent Events: streams the LLM answer with citations meta |
| `POST /reindex` | Triggers an indexer Job (idempotent — joins an active rebuild if one is running) |

### Why SSE, not WebSocket?

The chat answer is one-way: server pushes tokens to client. SSE is perfect — auto-reconnect built into the browser, plain HTTP, no special infra. WebSocket would be overkill, with bidirectional framing we don't need and stricter proxy/auth requirements.

### The SSE stream shape

```
event: meta
data: {"by_source": {"jira": {"count": 4, "avg_score": 0.82},
                     "conf": {"count": 2, "avg_score": 0.71}},
       "citations": [{"path": "archive/jira/INFRA-2391.md", "score": 0.89}, ...]}

event: token
data: {"t": "The"}

event: token
data: {"t": " cost spike"}

# ... many more token events ...

event: done
data: {}
```

The `meta` event arrives first, so the UI can render citation badges immediately. Then tokens stream in as Ollama produces them. The UI appends each token to the in-progress message bubble.

### Per-source confidence in the answer

This is the feature users asked for. When the LLM produces an answer, the user wants to know:

> "How much should I trust this? Is it grounded in good sources, or is it a guess?"

The chat-api computes this from the retrieved chunks before generation:

```python
def _by_source_stats(hits):
    grouped = {}
    for h in hits:
        src = _source_from_path(h["payload"]["path"])
        grouped.setdefault(src, []).append(h["score"])
    return {
        src: {"count": len(scores), "avg_score": round(sum(scores) / len(scores), 3)}
        for src, scores in grouped.items()
    }
```

The UI renders this as per-source pill badges:

```
retrieved from: jira 0.82  conf 0.71  slack 0.43
```

And per-paragraph badges within the answer body, derived from the LLM's `[citation/path]` tags. A paragraph citing only Slack with avg 0.43 gets a yellow "low confidence" badge. A paragraph citing Jira at 0.89 gets green.

### User-tunable source weights

Engineers don't all trust the four sources equally. Slack is noisy; Confluence is more curated. The Settings tab in the UI lets each engineer set a per-source weight multiplier (0-1.5) and a per-source minimum-score floor.

The chat-api accepts these in the search request:

```python
class SearchRequest(BaseModel):
    query: str
    source_weights: dict[str, float] | None = None
```

And applies them after Qdrant returns the raw scores:

```python
def _apply_source_weights(hits, weights):
    for h in hits:
        src = _source_from_path(h["payload"]["path"])
        h["score"] *= weights.get(src, 1.0)
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits
```

This is a small implementation but a big UX shift. It changes the chat from "the system's idea of what's relevant" to "your team's idea of what's relevant."

---

## Concurrent Rebuilds, Surfaced in the UI

When two users click "Rebuild Index" within 30 seconds, we don't want two indexers fighting. K8s itself is the lock:

```python
@app.post("/reindex")
async def reindex(request: Request):
    batch = k8s_client.BatchV1Api()
    jobs = batch.list_namespaced_job(NS, label_selector="teammate-routine=reindex")
    active = [j for j in jobs.items if j.status.active and j.status.active > 0]
    if active:
        return {
            "status": "already-running",
            "job_name": active[0].metadata.name,
            "started_by": active[0].metadata.labels.get("requested-by", "unknown"),
        }
    # ... create new Job ...
```

The UI translates `already-running` into a banner: *"Rebuild in progress — started by alice 38s ago. ETA 6m 12s. 📡 2 subscribers watching live."*

This is more honest UX than silently no-op'ing the second click. It teaches the engineer that the system is **shared**, not personal.

---

## The K8s Manifests

The Qdrant deployment is a StatefulSet with a 20 GB PVC. The indexer is `replicas: 1, strategy: Recreate`. The chat-api is `replicas: 2` (rolling updates fine because reads are stateless). The Ingress goes through ALB with cert-manager and SSO via oauth2-proxy.

Full manifests are in [`examples/k8s/`](https://github.com/JIUNG9/teammate/tree/main/examples/k8s):

- `qdrant/qdrant.yaml` — StatefulSet + Service + PVC
- `chat-api/deployment.yaml` — chat-api Deployment + Service + Ingress + indexer Deployment

The indexer's command is intentionally dumb:

```bash
while true; do
  git -C /etc/teammate/brain pull --rebase
  teammate index
  sleep 300
done
```

A `git pull` every 5 minutes, then a re-index. Idempotent re-index returns immediately if nothing changed. No more complex scheduler than the Linux `sleep` command. The whole pipeline is "git is the queue."

---

## Results

After 30 days running:

| Metric | Before (per-pod SQLite) | After (Qdrant) |
|---|---|---|
| Per-job index build time | 30-45 min | 0 (read from Qdrant) |
| Total Ollama embedding load / day | ~6 hours | ~3 min (delta only) |
| Search P50 latency | 80 ms (in-pod sqlite) | 22 ms (Qdrant network) |
| Search P99 latency | 200 ms | 60 ms |
| Number of search consumers | 1 per pod | unlimited |
| Vector DB storage | duplicated per pod | 1.4 GB (single canonical copy) |
| Chat answer T50 (first token) | n/a (no chat UI before) | 600 ms |
| Chat answer T50 (full answer) | n/a | 4.2 s |

The most surprising number isn't latency — it's the **Ollama load reduction**. Going from 6 hours of embedding to 3 minutes freed the inference server for actual LLM work. Chat-answer throughput tripled because Ollama wasn't constantly embedding the same documents in different pods.

---

## What I'd Do Differently

1. **Build the per-doc SHA gate on day 1.** Without it, you re-embed everything on every run, which makes the cost analysis look worse than it has to.

2. **Don't ship the chat-api without `/reindex` joining an active job.** The "two users both click rebuild" pattern is the kind of bug that's invisible until you have two users. Better to bake the dedup in from version 0.

3. **Lead with confidence badges in the UI, not as a "later" feature.** Engineers don't trust answers that don't tell them how much to trust them. Per-source confidence is what made teammate go from "neat demo" to "I actually rely on this."

---

## Try It Yourself

```bash
# Deploy Qdrant
helm repo add qdrant https://qdrant.to/helm
helm upgrade --install qdrant qdrant/qdrant -n teammate-agent

# Deploy the indexer + chat-api (from this repo's examples/k8s/)
kubectl apply -f https://raw.githubusercontent.com/JIUNG9/teammate/main/examples/k8s/chat-api/deployment.yaml

# Hit the API
curl https://chat.teammate.your-domain.net/healthz
curl -N "https://chat.teammate.your-domain.net/ask?q=what+is+our+RDS+rotation+procedure"
```

OSS source: https://github.com/JIUNG9/teammate/tree/main/src/teammate/chat_api

---

*Part 4 of "Building teammate." [← Part 3: Importers](./03-importers-25k-docs.md) · [Next: MTTD before MTTR →](./05-mttd-similarity-search.md)*
