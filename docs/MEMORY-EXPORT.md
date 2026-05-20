# `vigil memory-export` — leaving the team

When you leave a team, your `~/.claude/` memory file is full of context
the next person could use: who owns what, why we picked X over Y, the
on-call quirks the runbook never wrote down.

`memory-export` dumps the team-relevant entries as a single markdown
file you give to your successor.

## What's in the output

`HANDOVER-<user>-<date>.md` contains:

  * **Team rules** — third-person conventions ("we deploy via ArgoCD")
  * **Team facts** — concrete claims (service owners, stack, dates)
  * **References** — pointers to external resources (Linear, Confluence)
  * **Things you should know about how I worked** — a free-form
    section, blank by default, where you write the narrative the
    classifier can't capture (people to talk to, tools that saved you
    time, the one thing the team is wrong about that you didn't push
    hard enough on)

PERSONAL entries are excluded. This is a *team* handover, not a journal.

## Workflow

```bash
# 1. Generate the handover.
vigil memory-export --memory-root ~/.claude --user alice
# → wrote handover to ./HANDOVER-alice-2026-05-07.md

# 2. Open it. Fill in the "how I worked" section.

# 3. Hand it to your successor. Optionally: they run
#    `vigil memory-import` on their own machine to fold the
#    facts into their personal memory; or your team folds them
#    into the team brain in a "Handover from alice" PR.
```

## Redaction (default on)

By default, the export runs a redaction pass on each entry, replacing:

  * Email addresses → `alice.dev@acme-corp.com`
  * Internal hostnames → `db01.prod.internal`

These are placeholders. The successor will edit them with real values
when they need to. If you want the verbatim text — for example, you're
handing the file inside the same company and want real hostnames —
pass `--no-redact`:

```bash
vigil memory-export --memory-root ~/.claude --no-redact
```

## `--since` filter

If your memory has years of accumulated context and you only want
relatively recent facts, pass `--since YYYY-MM-DD`:

```bash
vigil memory-export --since 2024-01-01
```

Entries with a `since YYYY` or `as of YYYY` token older than the
filter date are dropped. Entries with no year stamp are *kept* — for a
leaving artifact, over-include is the safer error than over-prune.

## Read-only on `~/.claude/`

Like `memory-import`, this command never writes to `--memory-root`.
The output file lives in `--out-dir` (default: cwd). Verify with `ls
-la ~/.claude/` before and after if you're paranoid.
