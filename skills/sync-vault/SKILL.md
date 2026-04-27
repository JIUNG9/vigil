---
name: sync-vault
description: Federate the team's compliance vault via a private git repository the team already owns. Each engineer's signed attestations sync to a shared team timeline without ever touching cloud infrastructure outside the team's own git host. The Teamspace alternative for regulated teams.
---

# /sync-vault

Claude Teamspace gives a team shared workspace state in Anthropic's cloud.
Regulated teams (Korean fintech, public sector, defense, anyone subject to
data-residency rules) cannot put their compliance state in someone else's
cloud. They CAN put it in a private git repository they already own.

`teammate sync` turns the local `compliance-vault/` into a separate git
checkout pointing at a private team-vault repo. Every engineer's local
score writes there as usual; `sync push` ships their attestations to the
team timeline; `sync pull` rebases everyone else's attestations into the
local vault. Dual-signed audit trail (git commit by engineer, PDF by
sigstore/Fulcio).

## When to invoke

- The user mentions "share the vault with my team", "federate", "Teamspace
  but private", "team-wide compliance state", "shared audit trail".
- After `teammate init` on a fresh laptop where the team already has a
  team-vault repo set up.

## Behavior

### `teammate sync init <git-url>`

- Removes the local-only `.gitignore` from `compliance-vault/` (the vault is
  now meant to be tracked).
- Initializes `compliance-vault/.git/` as a separate git working tree.
- Adds `<git-url>` as `origin`.
- Pulls existing team content if the remote has any; if empty, populates
  on the first push.
- Writes `compliance-vault/.teammate-sync.json` with the remote URL +
  branch + initialization timestamp.

### `teammate sync push [-m MESSAGE]`

- `git add -A` everything in `compliance-vault/`.
- Commits with a default message that surfaces the engineer's email +
  timestamp ("vault: attestation push from alice@acme.com @
  2026-04-27T13:30:00Z"). Override with `-m`.
- Pushes to the remote branch.

### `teammate sync pull`

- `git pull --rebase` so the team timeline stays linear.
- Conflict resolution is the user's responsibility (rare — most
  attestations live in distinct timestamped files so there's no overlap).

### `teammate sync status`

- Shows: remote URL, branch, ahead/behind counts, dirty flag, last
  local commit. Useful before pushing to see what's about to land.

## Run

```bash
# One-time per laptop: bind the local vault to the team's private repo
teammate sync init git@github.com:acme-corp/team-vault.git

# After every score run, push your attestations to the team
teammate score
teammate sync push

# Before scoring, get the team's latest state
teammate sync pull

# Anytime: see where you are
teammate sync status
```

## Why this beats Teamspace for regulated teams

| | Teamspace | teammate sync |
|---|---|---|
| Data location | Anthropic cloud | Team's own private git host |
| Subscription | Per-seat paid | Free, OSS |
| Audit trail | Chat history | Cryptographically dual-signed (git + sigstore) |
| Air-gap capable | No | Yes (use a self-hosted GitLab / Gitea) |
| Sovereignty | Anthropic's jurisdiction | Team's jurisdiction |
| Required infra | Claude.ai for Teams | Private git (the team already has) |

## What this is NOT

- Not a real-time collaboration layer. There's no "sees what other
  engineers are typing right now." It's an attestation aggregator.
- Not a substitute for branch protection on the team-vault repo. The
  team should require PR review on the vault repo too if compliance
  history matters for audit.
- Not a replacement for centralized compliance tooling (Comp AI,
  Hicomply, etc.). It's the local artifact layer below those.

## Conflict handling

`git pull --rebase` will refuse to apply a conflicting change if two
engineers wrote to the same file at the same minute (rare — most files
are timestamped). When that happens, the user resolves the conflict by
hand the same way they would in any git workflow: edit the conflicted
file, `git add`, `git rebase --continue` from inside `compliance-vault/`.

The vault format intentionally minimizes this: per-control evidence
files (`controls/<framework>/<id>.md`) are overwritten, not appended.
History files (`history/<timestamp>.md`) include source/timestamp in
filename so concurrent runs don't collide. Real conflicts are rare.
