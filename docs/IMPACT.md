# Event-driven invalidation (v0.9)

> The brain is correct on Tuesday. Production changes on Wednesday. The brain is now wrong, and nobody knows.

This is the failure mode the v0.9 invalidation layer fixes. Up to v0.8,
teammate solved the **brain-vs-brain** freshness problem (orphan files,
dead links, stale runbooks; `teammate validate`). v0.9 adds the
**brain-vs-infra** layer: a hook that catches cloud mutations and tells
every engineer's `teammate ask` session about them.

The thesis: there are two freshness problems, and they need different
solutions.

| Problem | Symptom | v0.x layer |
| --- | --- | --- |
| Brain-vs-brain | A runbook references `auth-service-old`, which was renamed three weeks ago | `teammate validate` (CI shape check) |
| Brain-vs-infra | A runbook references `vpc-abc123`, which was detached two hours ago | `teammate impact` (event-driven) |

A CI check runs every push; an EventBridge rule fires every API call.
The cadences are different. The data sources are different. Mashing them
into a single nightly job that "freshens the brain" produces something
that's late on both fronts and clean on neither.

## The three commands

### `teammate impact preview`

Pre-`terraform apply` hook. Given the resources you're about to change,
walk the brain (`docs/`, `knowledge/`, `.claude/skills/`) and list every
page that mentions them. Then read the brain-invalidations repo for
events touching the same resources within the recency window. If a
HIGH-severity event exists for a touched resource, exit 2 (block).

```bash
teammate impact preview \
    --resource aws_vpc.shared \
    --resource aws_iam_role.deploy-bot \
    --state-path terraform.tfstate \
    --severity high \
    --recency 24
```

The block semantics live in one place: `severity_at_least(actual_severity, --severity)`.
Anything below threshold is informational. The exit code drives a
terraform pre-apply wrapper:

```bash
#!/usr/bin/env bash
# wrap-apply.sh
set -euo pipefail
resources=$(terraform show -json plan.tfplan | jq -r '.resource_changes[].address')
args=()
for r in $resources; do args+=(--resource "$r"); done
teammate impact preview "${args[@]}" --severity high
terraform apply plan.tfplan
```

### `teammate impact emit`

Post-`terraform apply` hook. Writes a structured event to the
brain-invalidations repo:

```bash
teammate impact emit \
    --resource aws_vpc.shared \
    --action detach \
    --severity high \
    --source terraform \
    --actor "$USER"
```

File layout:

```
<brain-invalidations>/
  invalidations/
    2026/
      05/
        09/
          aws-vpc-shared-detach-1715241600.json
```

Grep-friendly. Gitable. Commit messages double as the audit log.
`<repo>/.github/workflows/notify.yml` can fan out to Slack / PagerDuty /
email — the repo is the **single source of truth**, derived
notifications hang off it.

### `teammate impact list`

Read-only table view:

```bash
teammate impact list --since 24h --severity high
```

Driven by the same `read_recent_invalidations` function the runtime
banner uses. One read path, one cache, one set of bugs.

## Schema

```json
{
  "id": "ab12...",
  "timestamp": "2026-05-09T14:00:00+00:00",
  "source": "cloudtrail",
  "resource_type": "aws_vpc",
  "resource_id": "vpc-abc12345",
  "action": "detach",
  "severity": "high",
  "actor": "arn:aws:iam::000000000000:user/alice",
  "metadata": {
    "event_name": "DetachVpcCidrBlock",
    "aws_region": "us-east-1",
    "account": "000000000000"
  }
}
```

Severity ladder: `low < medium < high < critical`. The
recommended policy:

| Severity | Use for |
| --- | --- |
| low | Tag changes, descriptive metadata edits, snapshot creations |
| medium | Permission changes, traffic-shaping, parameter group changes |
| high | Resource detachments, security-group rule changes, IAM policy detachments |
| critical | Resource deletions, role deletions, cluster destroys |

## Runtime integration with `teammate ask`

When `teammate ask` retrieves chunks, it now extracts AWS resource ids
from the chunk text (cheap regex — `vpc-[0-9a-f]{8,17}`,
`aws_<type>.<name>`, ARN), looks them up against the
brain-invalidations repo (60-second session cache), and **prepends a
banner** when any retrieved chunk references a recently-mutated
resource:

```
⚠️  This answer references resources with recent infra changes:
   • aws_vpc.shared (vpc-abc123) — detach 2 hours ago, severity: HIGH
     affecting docs/runbooks/auth-deploy.md
   • aws_iam_role.deploy-bot — modify 9 hours ago, severity: MEDIUM
     affecting docs/runbooks/deploy-permissions.md

The retrieved runbooks may be stale. Verify against current infra state
before acting. Source: brain-invalidations log.
─────────────────────────────────────────────
```

Default policy: only HIGH and above surface as a banner. LOW / MEDIUM
events count toward the audit JSONL (`invalidations_matched` field) so
the trail is honest, but don't visibly interrupt the engineer. Tunable
via:

```toml
# .teammate/config.toml
[invalidations]
enabled = true
show_severity = "high"
recency_window_hours = 168  # one week
# Override the auto-detected location:
# repo_path = "/Users/alice/work/brain-invalidations"
```

## The no-daemon argument

We considered three architectures:

1. **Daemon polling** — engineer's laptop runs a sidecar that polls the
   invalidations repo every minute.
2. **Push notifications** — central server holds open sockets to every
   engineer's machine, pushes events as they happen.
3. **Lazy fetch at command time** — `teammate ask` reads the repo at
   query time, 60s cache.

Option 3 won. The k8s-controller analogy:

> A reconcile loop doesn't push state to consumers. It writes to etcd
> and lets every interested party watch — or, in this case, lazy-read
> on demand. The source of truth is the persisted store, not the
> messenger.

A daemon adds a process that has to start on login, has to handle
network errors silently, has to log somewhere, has to be uninstalled
when the engineer leaves. None of that earns its keep when the engineer
queries the brain a few times an hour. A 60-second cache absorbs
duplicate calls inside one session; a fresh `teammate ask` picks up
events emitted in the last minute, which is faster than any
push-notification system the team would actually deploy.

## Wiring into terraform

The two-line wrapper covers most teams:

```bash
# bin/tf-apply
set -euo pipefail
RESOURCES=$(terraform show -json "$1" | jq -r '.resource_changes[] | select(.change.actions[] | inside(["update","delete","create"])) | .address')
ARGS=$(printf -- '--resource %s ' $RESOURCES)
teammate impact preview $ARGS --severity high || exit $?
terraform apply "$1"
# On success, emit a single rolled-up event for the apply.
teammate impact emit \
    --resource "$(printf '%s\n' $RESOURCES | head -1)" \
    --action "$(jq -r '.terraform_version' < "$1" >/dev/null && echo modify)" \
    --severity medium \
    --source terraform \
    --actor "$USER"
```

A `pre-apply` hook in Atlantis or Terragrunt is a one-liner. If your
workflow is GitOps-ish (`terraform apply` runs in CI on merge), wire
this into the pipeline rather than every engineer's laptop.

## Wiring into CloudTrail

Three components:

1. **CloudTrail trail** — multi-region, your team probably has one. Out
   of scope for the teammate module.
2. **EventBridge rule** — filters CloudTrail events to the API names you
   care about (`DetachVpcCidrBlock`, `DeleteRole`, `ModifyDBInstance`, …).
3. **Lambda** — translates the event into the `InvalidationEvent` shape
   and commits to the brain-invalidations repo via the GitHub Contents
   API.

`examples/infra/aws-cloudtrail-hook/` ships steps 2 and 3 as a
self-contained Terraform module. Drop it into your platform repo, fill
in `aws_account_id` and `github_invalidations_repo`, `terraform apply`.

## What v0.9 deliberately does NOT do

- **No auto-PR drafting.** When a HIGH event lands, the agent does not
  open a PR against the brain to update affected runbooks. That's
  v0.10's `auto_pr_drafter` routine — and it's deliberately separated
  because the safety bar is different. Reading invalidation events is
  cheap; mutating the brain unprompted needs human review for every
  draft.
- **No cross-cloud.** AWS resource extraction only. GCP / Azure
  patterns will land via `[invalidations.extra_patterns]` config — the
  spec is sketched for v0.10.
- **No event-deletion / TTL.** The invalidations repo grows monotonically.
  At realistic volumes (single-digit events per day per AWS account), a
  year of events fits comfortably under 1 MB. If your account is noisier
  than that, gzip + monthly rollup is the v0.10 plan.

## Why a separate repo

The events need to be:

- **Shared** — every engineer's laptop reads the same source.
- **Audited** — when something breaks, the on-call asks "who detached
  the VPC and when?" and the answer is `git log`.
- **Append-only** — by social convention, not technical lock. A
  rebase that drops events would cover up an incident.
- **Cheap to fan out** — Slack notifications, dashboards, weekly
  digests are GitHub Actions on the events repo. Adding a new sink does
  not need teammate code changes.

A separate repo earns all four. A subdirectory inside the team brain
would force every engineer to fetch the events along with their
markdown — needless network traffic — and would couple the brain's
commit cadence to the event cadence.
