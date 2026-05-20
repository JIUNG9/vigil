# `vigil memory-import` — pull team-relevant facts out of personal memory

`~/.claude/` is where Claude Code stores per-user memory. Over time it
accumulates facts that *are* team-relevant — service ownership, why
the team picked X over Y, on-call quirks — alongside facts that aren't —
"I prefer dark mode," "my role is SRE."

`memory-import` stages the team-relevant facts as a draft on the team
brain. The user reviews the draft and opts in per entry to import.

## The reversed safety bias

The default for every entry in the draft is **SKIP**. To import an
entry, the user edits the draft file and checks its `[ ] IMPORT THIS`
box. There is no auto-import path. Even when the heuristic flags an
entry as obviously team-relevant with zero redaction concerns, the box
in the draft starts unchecked.

This is intentional. The naive design — "auto-import; user redacts
later" — has the safety property backwards. It's easy to miss a
redaction in an auto-imported entry; it's hard to accidentally check
a box on a draft you're reading line-by-line.

So:

  * Classification proposes; the human disposes.
  * Redaction is a *confirmation* step, not a *bypass* step.
  * `~/.claude/` is read-only. The command never writes there.

## Memory-root layouts

`memory-import` understands two layouts:

  1. **Direct** — you pass `--memory-root /path/to/dir` and that dir
     contains `MEMORY.md` directly. Used in tests, fixtures, and any
     custom per-team setup.
  2. **Claude Code's default** — `~/.claude/projects/<project-id>/memory/MEMORY.md`.
     If `--memory-root ~/.claude` doesn't have `MEMORY.md` directly,
     the harvester digs one level deeper into `projects/<id>/memory/`
     and re-anchors to the first project-memory dir it finds.

Multi-project users should pass `--memory-root ~/.claude/projects/<id>/memory`
explicitly to pick the right project.

## Workflow

```bash
# 1. From inside the team brain repo:
vigil memory-import --memory-root ~/.claude --user alice
# → wrote draft to <brain>/pending-imports/MEMORY-IMPORT-alice-2026-05-07.md

# 2. Open the draft in your editor. For each entry, decide:
#    - Leave the box unchecked: SKIP
#    - Check the box: IMPORT (and fold the entry into the right
#      brain page in your next commit)

# 3. Commit only the brain pages you actually edited.
#    The draft itself is gitignored by default — `pending-imports/`
#    is in the team-brain template's .gitignore.
```

## Classification

The harvester runs heuristics on each non-blank, non-heading line of
`MEMORY.md` (and sibling `feedback_*.md` / `project_*.md` /
`reference_*.md` files):

| Class | Marker | Default |
|---|---|---|
| `TEAM_RULE` | "we deploy", "team uses", "convention is" | surfaced as candidate; **box unchecked** |
| `TEAM_FACT` | service ownership, stack, dates | surfaced as candidate; **box unchecked** |
| `REFERENCE` | "see Linear project X", Confluence pointers | surfaced as candidate; **box unchecked** |
| `PERSONAL` | "I prefer", "my role", "for me" | surfaced last; **box unchecked** |

Classification is a heuristic. PERSONAL wins ties — a sentence like "I
prefer that we deploy via X" is fundamentally personal even though it
contains "we". The whole point is that SKIP is the default, so the
heuristic erring toward PERSONAL costs nothing.

## Redaction pre-pass

After classification, the harvester scans each entry for:

  * Email addresses (`alice@your-org.com`)
  * Internal-looking hostnames (`db01.prod.internal`, `api.cluster.corp`)
  * Employer-name patterns (configurable)

Matches are **flagged** in the draft, not rewritten. Each entry's flag
list shows up under "Redaction flags". The user redacts in the draft
before checking the import box.

## Where the draft lives

`<brain_root>/pending-imports/MEMORY-IMPORT-<user>-<date>.md`

The team-brain template ships a `.gitignore` that excludes
`pending-imports/`. Teams who prefer drafts in PR review can remove
that line locally. Either way, the draft is on the team's own filesystem
— nothing leaves.

## Read-only on `~/.claude/`

The command opens, reads, and parses files under `--memory-root`. It
never writes there. The OS could enforce that with a chmod; we don't
require it. The contract is in code: `harvest_user_memory()` returns a
data structure; only `write_plan()` writes, and only to `brain_root /
"pending-imports"`.

If you want to verify, watch the directory: `ls -la ~/.claude/` before
and after the command. It should be byte-identical.

## What v0.5 does NOT do

There is no `vigil memory-import --apply`. v0.5 stops at the draft.
"Apply" is the engineer's editor: open the brain page where the entry
belongs (e.g. `knowledge/services.md` for an ownership fact), paste,
commit. The diff in `git log` is the audit trail.

A future version may add an `--apply` path that reads checked entries
and proposes edits to specific brain pages. The same default-skip
contract will hold: every proposed edit will need an explicit confirm
before it lands.
