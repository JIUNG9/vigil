# When sources disagree — the contradiction detector

**v0.6.0**

## What this is

When `vigil ask` retrieves the top-k chunks for a query, two of them
might say opposite things. A naive synthesis-by-LLM path will happily
blend "use PG13" and "use PG16" into "use PG14" — a half-truth that
costs you at 3 AM. The contradiction detector surfaces the conflict
instead of erasing it.

The output looks like this:

```
**Two sources disagree on this:**

- `[runbooks/auth-pg.md]` says: "Auth runs on PostgreSQL 13."
- `[runbooks/db-policy.md]` says: "All services migrated to PostgreSQL 16."
  (parameter_drift: Numeric/version drift: ['13'] vs ['16'])

Resolve manually before acting. Continuing with synthesis below.

[the LLM-synthesised answer follows]
```

Both chunks still go into the LLM's context, so the synthesised answer
is informed by both perspectives. The user sees the disagreement up
top — they decide.

## Two phases

```
              top-k retrieved chunks
                       │
                       ▼
              ┌───────────────────┐
              │  Phase 1:         │  free, runs by default,
              │  heuristic        │  best-effort.
              └────────┬──────────┘
                       │ candidate pairs
                       ▼
              ┌───────────────────┐
              │  Phase 2:         │  opt-in via config; only runs on
              │  LLM judge        │  pairs Phase 1 already flagged.
              └────────┬──────────┘
                       │
                       ▼
                contradictions[]
```

### Phase 1 — heuristic

For each pair of chunks above the score floor:

1. Split each chunk into sentences (crude regex; markdown is messy).
2. For every sentence pair, check if they share a 4-token n-gram —
   crude proxy for "talking about the same subject."
3. If yes:
   - **parameter_drift** — both sentences contain numeric tokens
     (versions, counts, ports) and the sets don't intersect.
   - **procedure_conflict** — exactly one of the two sentences carries
     a strong negation word (`not`, `never`, `do not`, `disable`, …).

Phase 1 is intentionally crude. It's the screen, not the verdict.

### Phase 2 — LLM judge (opt-in)

When `[contradiction] use_llm_judge = true` and an LLM provider is
configured, every Phase 1 candidate is sent to the LLM with a tight
yes/no prompt:

```
Verdict format: YES: <conflict summary>
                NO: <reason they don't conflict>
                UNSURE: <reason you can't tell>
```

Only `YES` is treated as a contradiction. `NO` / `UNSURE` / malformed
all drop the heuristic finding. With Phase 2 ON, **every** confirmed
contradiction has been judged — when the per-query call budget is
exhausted, remaining unjudged candidates are dropped rather than
surfaced.

## Cost ceiling

With `k=6` chunks the maximum candidate pair count is `6 * 5 / 2 = 15`.
Phase 1 prunes aggressively — typical realistic workloads see 0-3
candidates per query.

When Phase 2 is on:

- `[contradiction] max_llm_calls = 3` (default) caps LLM calls per query.
- Same-file pairs are skipped (they're not "two sources").
- Each pair gets at most one judge call (we cache by `(label_a, label_b)`).

So the worst case under default config is 3 extra small LLM calls per
query, all against the locally-running model. Negligible cost on a
laptop; tunable if you're paying per token.

## Configuration

```toml
# .vigil/config.toml
[contradiction]
use_llm_judge = false   # default — Phase 1 only
score_floor   = 0.5     # only pair chunks at or above this score
max_llm_calls = 3       # per-query budget when Phase 2 is on
```

The `score_floor` here is independent of the global confidence
threshold (Guard 1). They serve different purposes:

- Guard 1's `score_threshold` decides whether to answer at all.
- The contradiction `score_floor` decides whether a chunk is
  "high-confidence enough" to be worth checking against another for
  conflicts.

In keyword-fallback mode, scores top out around 0.1 — well below the
default 0.5 floor. The detector is effectively embedding-mode-only
unless you explicitly lower `score_floor` in the config. That asymmetry
is intentional (the contradiction detector is most useful when the
retriever is trustworthy enough to decide which chunks are "really"
about the same subject). See `docs/CONFIDENCE.md` for the full
discussion of mode differences.

## When this is useful (and when it's noise)

**Useful when:**

- Two runbooks describe overlapping procedures with different parameters.
- A migration is in progress and the brain hasn't caught up everywhere.
- An ADR says one thing and a runbook says another.

**Noise when:**

- Different sources cover different services, both correctly. The
  heuristic can false-positive when two unrelated chunks share a
  generic 4-gram. Turn on Phase 2 (LLM judge) to filter these.
- The brain has a lot of historical content that disagrees with current
  state. Either prune the history or add a `[archived]` frontmatter tag
  and exclude it from the index.

## What's NOT a contradiction

- Two chunks that simply discuss different aspects of the same system.
- Differences in wording — a single fact stated two ways.
- Differences in formatting — bullet vs numbered list.

Phase 1 will sometimes flag these. Phase 2's prompt is calibrated to
filter them out: "Two excerpts that simply discuss different aspects of
the same system are NOT a contradiction."

## Performance

Phase 1 runs in microseconds — pure regex + n-gram hash lookups.
Phase 2's per-pair LLM call is bounded by your local model's latency
(typically 1-3 s on a laptop with `llama3.2:3b`). The default budget
caps cost predictably.

## Surfaced where

The contradiction prefix appears at the top of `vigil ask` output
(before the LLM-synthesised answer), and is visible to MCP clients
through the same `answer()` path. It's also recorded in the audit log
(see `docs/CONFIDENCE.md`):

```json
{"ts": "...", "query": "...", "contradictions": 2, ...}
```

That makes it easy to grep for "queries that surfaced a conflict" and
prioritise fixing them upstream.
