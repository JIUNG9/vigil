---
name: init-teammate
description: Set up teammate on this laptop in an already-cloned team-brain repo. Detects Ollama, builds the local sqlite-vec index of every markdown file, optionally registers gbrain. For team leads creating a NEW team-brain repo, use `teammate scaffold` first instead.
---

# /init-teammate

One-command per-laptop setup for an engineer who just cloned the team-brain repo.

## When to invoke

- A new engineer just cloned the team's brain repo and wants to start using it.
- The user mentions "set up teammate", "first run on this laptop", "I just joined".

## Two distinct flows

- **Team lead setting up the brain for the first time:** `teammate scaffold <dir>`.
  Creates a fresh team-brain repo from the bundled template. Run once per
  organization. Outputs a skeleton ready to commit + push to a private remote.

- **Engineer joining an existing team brain (this skill):** `teammate init`.
  Run inside an already-cloned brain repo. Detects + indexes + sets up local
  query path. Run once per laptop.

## Behavior

1. **Brain detection.** Confirms `CLAUDE.md` exists at the cwd (i.e., this is
   a team-brain repo). If not, refuses with the hint to run `teammate scaffold`
   first.

2. **Ollama detection.** Checks `localhost:11434`. If running, lists models
   and flags any missing required ones. If not running, prints install link
   (https://ollama.com/download) + the two pull commands. Does NOT auto-install.

3. **gbrain detection.** Checks if `gbrain` binary is on PATH. Optional
   register-as-source pass with `--register-gbrain`.

4. **Local index build.** Runs the indexer over every markdown file in the brain
   (CLAUDE.md, .claude/, docs/, knowledge/). Writes to `.teammate-cache/vault.sqlite`
   (sqlite-vec). Re-uses Ollama embeddings if available; falls back to keyword
   scoring if not.

## Run

```bash
cd /path/to/team-brain
teammate init
# or, with gbrain registration:
teammate init --register-gbrain
```

## Output

Per-step status table:

```
teammate init —
  ✓ brain: detected at /path/to/team-brain: 23 markdown files (1 CLAUDE.md, 5 skills, 3 rules, 9 docs, 5 knowledge)
  ✓ ollama: up. Required models present: llama3.2:3b, nomic-embed-text
  · gbrain: not on PATH; built-in mini-RAG will handle queries
  ✓ index: indexed 23 files with embeddings (0 unchanged)
```

## What blocks a successful init

- No `CLAUDE.md` at the cwd. The user is in the wrong directory, OR they are
  the team lead and need `teammate scaffold` first.

## What does NOT block

- Ollama not running. `init` still exits zero; keyword search will work.
- gbrain not installed. Built-in mini-RAG replaces it.
