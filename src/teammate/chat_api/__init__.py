"""FastAPI service for the chat UI + Slack listener.

Exposes:
  GET  /healthz              health probe
  GET  /ask?q=...            Server-Sent Events: streaming LLM answer with citations
  POST /search               JSON: top-K chunks with scores per source
  GET  /feed                 JSON: today's digest + recent jobs
  GET  /index-status         JSON: doc count, coverage, last build
  POST /reindex              triggers a K8s Job; returns 200 + job id (or existing job if active)

Auth: trusts X-Forwarded-User header (set by oauth2-proxy or your ingress).
"""

from teammate.chat_api.main import app

__all__ = ["app"]
