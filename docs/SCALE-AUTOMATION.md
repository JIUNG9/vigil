# SCALE-AUTOMATION

> v0.10 ships four new agent routines and `vigil brain-pulse` — the
> set of moves that lets the team brain stay accurate at 100+ engineers
> without a single daemon.

## 1. The scale problem — manual sync doesn't work past ~10 engineers

Up to v0.9 the team-brain story was complete on every axis but one:
**reach**. The brain knew when it was wrong (v0.9 invalidations), it
could refuse to answer when confidence was low (v0.6 guards), and it
produced PR-staged drafts when MCP sources changed (v0.8). What it did
not have was a way to put the right information in front of the right
engineer at the right time.

For a five-person team, hallway conversation closes that gap. For a
fifty-person team, hallway conversation is a coin flip — half the
people who needed to know the VPC was detached at 2 AM wake up at 9 AM
to find the runbook is wrong, and a postmortem.

The naive fix is "send all events to all engineers." That dies on day
one. Engineers ignore noisy channels, and a noisy channel is worse
than no channel because it teaches engineers to ignore the system that
will, later, send them the page that matters.

The actual fix is targeting: derive, from data the team already
generates (commits, PRs, ownership pages), which engineer is most
likely to need a given event — and only send to them. Workload then
scales with **events**, not **engineers**.

## 2. The agent-as-controller architecture (k8s controller analogy)

Each v0.10 routine is a Kubernetes-style controller. It:

- Watches a small, well-typed input (an invalidation event, a PR
  webhook, a daily timer).
- Reads adjacent state (the brain repo, `git log`, the invalidations
  log, `knowledge/people.md`).
- Produces a desired-state artifact (a digest, a comment, a draft PR).
- Exits. No daemon. No persistent memory beyond the file it just
  wrote.

The runner — `/schedule`, GitHub Actions, or local cron — is the
analog of the Kubernetes API server. It carries the tokens, posts the
comments, opens the PRs. The agent never holds credentials. This split
keeps the trust surface narrow and lets the same routines run under
totally different runners with no code change.

```
┌─────────────────────┐    event    ┌──────────────────┐    artifact    ┌─────────┐
│ invalidations repo  │ ─────────►  │ vigil routine │ ─────────────► │ runner  │
│ (CloudTrail / TF)   │             │  (read-only)     │                │ (gh/PD) │
└─────────────────────┘             └──────────────────┘                └─────────┘
                                            ▲                                  │
                                            │ git log + brain reads            │
                                            └──────────────────────────────────┘
                                                                     posts/sends
```

## 3. The four new routines — purpose, trigger, inputs, outputs, scope

### `invalidation_digest` — daily per-engineer email

- **Trigger.** Daily cron.
- **Inputs.** `knowledge/people.md`; `git log --author=<email>` over
  the last 7 days; recent invalidations from the invalidations repo
  (default last 24h).
- **Output.** One markdown file per engineer at
  `out_dir/digests/<engineer-id>-YYYY-MM-DD.md` listing the
  invalidations that touch a brain page they recently edited. The
  runner emails / Slacks them; the routine never holds tokens.
- **Scope.** Per-engineer. Engineers with no relevant matches receive
  no file (a single `_empty-…md` breadcrumb is left so the runner
  knows the routine ran).

### `targeted_radar` — git-history-based notification routing

- **Trigger.** Per-event, when a HIGH-severity invalidation lands.
- **Heuristics.**
  - +50 declared owner of the resource per `knowledge/services.md`.
  - +30 per brain page the engineer authored / edited within 90 days
    that references the resource.
  - +25 open-PR author touching the resource. (The agent has no
    tokens; the runner injects `open_prs` via `RoutineConfig.extra`.)
- **Output.** `out_dir/radar/<invalidation-id>.json` — top-N (default
  5) engineers with `engineer_id`, `score`, and `reasons` (each signal
  is annotated). The runner uses the list to direct-message the right
  people instead of broadcasting.

### `pr_review_assist` — auto-comment on infra PRs

- **Trigger.** `pull_request` (opened / synchronize).
- **Inputs.** PR file list (and optional patches). The routine
  extracts AWS resource ids from each changed file using the same
  regex that backs `vigil ask`'s invalidation banner, walks the
  brain looking for pages that mention any of those resources, and
  cross-references with recent invalidations.
- **Output.** `out_dir/pr-comments/pr-<number>.md` — a markdown
  comment ready for `gh pr comment`. Lists every affected brain page
  with last-update date plus any recent invalidation events on the
  same resources.

### `auto_pr_drafter` — drafts auto-PRs for HIGH events

- **Trigger.** Per-event, severity ≥ HIGH.
- **Inputs.** The invalidation event; affected brain pages
  (auto-discovered by walking the brain or supplied via
  `extra.affected_pages`); a configured `LLMProvider` (test injection
  via the `provider=` kwarg, real injection via
  `load_llm_provider` from config).
- **Output.** Per page, a markdown file at
  `out_dir/draft-prs/<slug>-<invalidation-id>.md` with frontmatter
  (`original_path`, `invalidation_id`, `severity`, `requires_review:
  true`). The runner opens **one PR per invalidation**, with each
  affected page as its own commit, tags the page owner per
  `knowledge/people.md`, and **always opens it as a draft**. No
  auto-merge, ever.

## 4. `vigil brain-pulse` — the engineer's morning ritual

```text
Brain Pulse — last 24h
─────────────────────────────────────────────
  user: alice@team

  Resources YOU worked on with recent invalidations:    [3]
     - aws_vpc.vpc-abc123 — 2h ago  severity: HIGH
       affecting: docs/runbooks/auth-deploy.md
     - aws_iam_role.deploy-bot — 9h ago  severity: MEDIUM
       affecting: docs/runbooks/deploy-permissions.md
     - ...

  Brain page changes (last 24h):                         [8]
     - alice@team: docs/runbooks/eks-cluster-rotation.md (modified)
     - bob@team:   knowledge/decisions/0009-rds-encryption.md (new)
     ... (3 more — use --since 7d for full week)

  Pending PR-staged drafts (auto_pr_drafter):            [2]
     - docs/runbooks/auth-deploy.md (invalidation inv-abc, severity high)
     run `gh pr review --request <id>` to triage.

  Filtered as not-relevant-to-you:                       [47]

Today's recommended actions:
  1. Verify docs/runbooks/auth-deploy.md reflects new state of aws_vpc.vpc-abc123
  2. Review staged draft auth-deploy-inv-abc.md for docs/runbooks/auth-deploy.md

Run `vigil ask "..."` to dig deeper.
─────────────────────────────────────────────
```

- `--user EMAIL` — overrides `git config user.email`.
- `--since 7d` — widens the window. Default `24h`. Accepts `s/m/h/d`.
- `--json` — emits a stable JSON shape for scripting:

  ```json
  {
    "user_email": "alice@team",
    "since": "24h",
    "targeted":         [{"resource": "...", "page": "...", "severity": "high"}],
    "brain_changes":    [{"sha": "...", "author": "...", "path": "...", "kind": "modified"}],
    "pending_drafts":   [{"path": "...", "original_path": "...", "invalidation_id": "..."}],
    "filtered_count":   47,
    "recommended_actions": [...]
  }
  ```

`brain-pulse` is read-only. With no brain, no invalidations, and no
agent staging dir present, it emits an empty report and exits 0.

## 5. The hard rules that still hold

- **Agent never auto-merges.** `auto_pr_drafter` writes drafts
  carrying `requires_review: true`; the runner marks every PR as a
  draft. Humans approve. (v0.5 contract.)
- **PR-staging only.** Every routine writes into `out_dir/`; the
  brain itself is read-only. (v0.5 contract.)
- **No daemon.** Every routine is a finite function. It runs, writes,
  exits.
- **Idempotent.** Re-running the same routine with the same inputs
  produces the same artifacts. Frontmatter dedup keys (e.g.
  `confluence_revision` in v0.8, `invalidation_id` here) protect
  against churn.

## 6. Trust split

| Component             | Read access | Write access | Tokens |
|-----------------------|-------------|--------------|--------|
| Agent routines        | brain repo, invalidations repo, `git log` | `out_dir/` only | none |
| Runner (cron / `/schedule`) | `out_dir/` | github / slack / email APIs | yes |
| `vigil brain-pulse` | brain repo, invalidations repo, `out_dir/` | none | none |

The split is the same one v0.5 introduced and v0.8 kept: the agent has
read-only on the brain and write-only on the staging dir. The runner
holds the tokens. Compromising the agent leaks no credentials.

## 7. Why this scales sub-linearly

The classic team-knowledge tools scale O(engineers) — at minimum, you
notify everyone on every event. v0.10's routines scale O(events).

- An invalidation event triggers `targeted_radar` (one run) and
  `auto_pr_drafter` (one run, opening one PR with N commits).
- A PR triggers `pr_review_assist` (one run, posting one comment).
- A day triggers `invalidation_digest` (one run, producing N digest
  files where N ≤ engineers and typically N ≪ engineers).

Adding the 51st engineer adds zero baseline work. It only adds work
when that 51st engineer's `git log` matches an event — exactly the
case where they should be notified anyway. The team brain that wins
at 3 AM is the one willing to say "I don't know"; the team brain that
**stays useful** at 100 engineers is the one that knows who to tell
when it just learned something new.
