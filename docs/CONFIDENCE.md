# The four confidence guards

**v0.6.0**

## Why this exists

A team-brain tool that synthesises something whenever asked is worse than
one that refuses to bluff. At 3 AM, "I don't know" is the most important
output your AI can give you. The launch article promised this would land
in v0.4; v0.6 is when it actually integrates with the retrieval path.

Four guards. Each one trades a thin slice of recall for a meaningful
slice of trust:

| # | Guard | What it does |
|---|---|---|
| 1 | Score threshold | Refuse to synthesise when top-k max < floor. |
| 2 | Citation guard | Strip paragraphs the LLM emitted without a `[file]` citation. |
| 3 | Audit JSONL | One line per retrieval; weekly rotation. |
| 4 | Per-action floor | Different floors for `ask` / agent routines / future mutations. |

## Guard 1 — score threshold

Default: `0.5`. When the highest-scoring chunk is below the floor, the
LLM is **not** invoked. The user gets:

```
I don't know — the closest match scored 0.31, below the floor of 0.50.
Closest file: `docs/runbooks/auth-deploy.md`. Consider rewording the
query, or run `teammate index --rebuild` if you expected this to be in
the brain.
```

That's the entire response. No synthesis, no speculation, no half-truth.

### Embedding mode vs keyword mode

The threshold is meaningful only when retrieval used **embeddings**.
Cosine similarity sits in `[0, 1]` (well, `[-1, 1]` in theory; for our
texts it stays positive); 0.5 is roughly "this is about the same topic."

The keyword-fallback path computes a custom BM25-ish score that is
unbounded and density-normalised. A "0.5 floor" is meaningful for cosine
and arbitrary for keyword. So:

> **In keyword mode, the threshold gate is disabled.** The audit log
> records `retrieval_mode: "keyword"` and `below_threshold: false`. The
> fallback file-listing is returned as before.

That asymmetry is documented here so you don't get a surprise when
Ollama is down and the brain still returns answers without honouring
the floor. If you want strict enforcement, run with embeddings.

### Configuration

```toml
[confidence]
score_threshold = 0.5  # default; raise to be stricter
```

Or per-action via Guard 4 (below).

## Guard 2 — citation guard (enforced)

The system prompt includes:

> Every paragraph in your answer MUST cite at least one source file in
> [brackets] using its path (e.g. `[docs/runbooks/auth-deploy.md]`).
> Paragraphs without a bracketed citation will be stripped before the
> user sees them.

The streaming output is buffered per-paragraph. When a paragraph closes
(`\n\n`), it's emitted verbatim if it contains a citation, else replaced
with `(uncited claim removed)\n\n`.

### Granularity is paragraph-level, not sentence-level

A paragraph with one citation at the end satisfies the rule. We don't
attempt sentence-level enforcement — that would be overkill and would
break legitimate multi-sentence paragraphs.

### Short-answer flush

Some models don't emit a closing `\n\n` for short answers. We flush the
residual buffer at end-of-stream and apply the same citation check, so
a short reply without an explicit blank-line separator still gets
inspected. Short answers without a citation become
`(uncited claim removed)`.

### Bracketed vs parenthesised

The detector accepts both `[docs/x.md]` and `(docs/x.md)`. Some models
reach for parentheses despite the prompt; we don't fight them.

## Guard 3 — audit JSONL

Lives at `<brain-root>/.teammate-cache/audit.jsonl`. One line per
retrieval. Schema:

```json
{
  "ts": "2026-05-08T10:00:00+00:00",
  "action": "ask",
  "query": "what's our deploy procedure?",
  "k": 6,
  "max_score": 0.74,
  "min_score": 0.31,
  "chunks_used": ["docs/runbooks/deploy.md", "docs/onboarding/README.md"],
  "llm_provider": "OllamaLLMProvider",
  "llm_model": "llama3.2:3b",
  "answer_length_chars": 1242,
  "below_threshold": false,
  "retrieval_mode": "embedding",
  "contradictions": 0
}
```

### Rotation

Weekly. The active file rotates to `audit-YYYY-Wnn.jsonl` (ISO week)
when the active file's mtime falls in a different ISO week than the
current write. **Lazy** — there's no daemon. A 3-week-quiet brain that
wakes up still gets exactly one rotation on the next write, with the
old file's contents preserved under the appropriate week stamp.

If an archive with the target week stamp already exists (e.g. a previous
rotation made it), the active file's contents are appended rather than
clobbering. No data is lost.

### CLI

```bash
teammate audit                                    # last 20 records
teammate audit --since 2026-05-01                 # ISO date filter
teammate audit --query-grep deploy                # regex on query field
teammate audit --json                             # raw JSONL on stdout
teammate audit --limit 100                        # widen the window
```

The output is human-readable by default:

```
2026-05-08T10:00:00+00:00  ask                       max=0.74  k=6  mode=embedding  contradictions=0  what's our deploy procedure?
2026-05-08T10:05:12+00:00  ask                       max=0.31  k=6  mode=embedding  contradictions=0 *BELOW*  obscure-thing-not-in-brain
```

## Guard 4 — per-action confidence floor

Different actions deserve different confidence floors. Reading the brain
is one thing; opening an issue based on retrieval is another. Defaults:

| Action | Floor |
|---|---|
| `ask` (read-only Q&A) | 0.5 |
| `agent.weekly_digest` (reports) | 0.5 |
| `agent.orphan_triage` (proposes issues) | 0.6 |
| `agent.pr_migration_plan` (PR comment) | 0.65 |
| `execute` (any future mutation — reserved) | 0.85 |

Override via TOML:

```toml
[confidence.action_floors]
ask = 0.6
agent.orphan_triage = 0.7
```

The floor passed to `answer()` overrides the global `score_threshold`.
The agent's `RoutineConfig` carries an `action_floor` field; future
routines that touch retrieval will plumb it through.

### Why higher floors for action-taking routines?

Read-only `ask` can tolerate a marginal answer — the user reads it and
decides. A routine that opens a GitHub issue based on retrieval gets to
be wrong out loud, in the team's tracker. The trust budget is smaller;
the floor needs to be higher.

The `execute` floor of 0.85 is reserved for any v0.7+ work that has
mutating side effects. We're not shipping that yet, but the slot is
booked.

## Putting it all together

```
ask "..."                                                            
   │                                                                 
   ▼                                                                 
retrieve top-k                                                       
   │                                                                 
   ▼                                                                 
[Guard 4] resolve action_floor for "ask"                             
   │                                                                 
   ▼                                                                 
[Guard 1] is mode "embedding" AND max_score < floor?                 
   ├── yes → emit "I don't know", append audit, stop.                
   └── no  →                                                         
        │                                                            
        ▼                                                            
   contradiction detector → "Two sources disagree" prefix (optional) 
        │                                                            
        ▼                                                            
   build prompt with the citation rule in SYSTEM_PROMPT              
        │                                                            
        ▼                                                            
   [Guard 2] LLM stream wrapped in citation_guard()                  
        │                                                            
        ▼                                                            
   user                                                              
        │                                                            
        ▼                                                            
   [Guard 3] append one line to audit.jsonl                          
```

## When you'd want to tune these

- **Score threshold too aggressive.** You see a lot of "I don't know"
  on queries that should match. Either re-index (`teammate index
  --rebuild`) — your embedding stamp may not match the configured
  provider — or lower the floor in `[confidence] score_threshold`.
- **Citation guard stripping good answers.** Your local model is
  ignoring the citation rule. Try a larger model, or relax the prompt
  in `rag/ask.py` for your fork.
- **Audit log too noisy.** Add `.teammate-cache/audit*.jsonl` to your
  `.gitignore`. (The default template already does.)
- **Per-action floor too tight.** Routine X is producing useful work
  that is being suppressed. Lower its floor in
  `[confidence.action_floors]` rather than dropping the global one.

## What confidence guards don't do

- **They don't make the brain right.** If the brain says PG13 in the
  wrong place, the guards won't catch it. The contradiction detector
  helps when the brain disagrees with itself; the guards don't help
  when the brain is uniformly wrong.
- **They don't replace human review.** "Confidence high enough to
  answer" is not "answer correct." A 0.85 cosine similarity to a chunk
  doesn't mean the chunk is right.
- **They don't anonymise the audit log.** Queries are stored verbatim.
  If your team types secrets into queries, that's already a different
  problem and the audit log isn't where to fix it.
