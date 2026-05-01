# Services — what runs where

| Service | Repo | Owner | Tech | Region | Runbook |
|---|---|---|---|---|---|
| (replace) | github.com/your-org/svc | @handle | Python/FastAPI | us-east-1 | docs/runbooks/svc.md |

## Inventory rules

- Every service in production must have one row above.
- Every row must point at a runbook (even if the runbook is just "no on-call,
  page #platform on weekdays").
- Service owners review this file quarterly.
