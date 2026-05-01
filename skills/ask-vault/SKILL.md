---
name: ask-vault
description: Ask a question about the team's brain (CLAUDE.md, skills, rules, runbooks, ADRs, knowledge files). Streams a grounded answer from a local LLM (Ollama) with cited markdown paths. Falls back to keyword search if Ollama isn't running. Everything happens locally — no cloud round-trip.
---

# /ask-vault

The headline pillar. The new engineer queries the team brain in plain English
and gets an answer grounded in the team's own markdown — running on their
laptop, no cloud.

## When to invoke

- The user asks a question about how the team works, what a service does, who
  owns what, what the on-call procedure is, what an old ADR decided.
- Examples that should route here:
  - "What's our deploy procedure?"
  - "Who owns the auth service?"
  - "Why did we choose Postgres over MongoDB?"
  - "How do we handle on-call rotation?"
  - "What's the test convention for new code?"

## Behavior

1. Indexes (or re-uses the existing index of) every `.md` file in the
   team-brain repo: `CLAUDE.md`, `.claude/skills/*/SKILL.md`,
   `.claude/rules/*.md`, `docs/**/*.md`, `knowledge/**/*.md`.
   The index lives in `.teammate-cache/vault.sqlite` (sqlite-vec).

2. Embeds the user's query via Ollama (`nomic-embed-text` by default) and
   runs cosine similarity against the indexed chunks.

3. Falls back to BM25-ish keyword scoring if Ollama isn't running OR no
   embeddings exist.

4. Builds a context block of the top-k chunks (default 6) and streams an
   answer from Ollama (`llama3.2:3b` by default). System prompt enforces:

   - Cite file paths in `[brackets]` for every fact
   - Refuse to make up facts not in the chunks
   - Be terse — engineers don't need preamble

5. If Ollama isn't running, returns the matching file paths instead of a
   synthesized answer. Tells the user how to start Ollama.

## Run

```bash
teammate ask "what's our deploy procedure?"
teammate ask "who owns the auth service?"
teammate ask --top-k 10 "summarize the ADRs from this quarter"
teammate ask --rebuild "force a re-index, then answer"
```

## Output (Ollama running)

```
The team's deploy procedure for production services:

1. Open a PR against the service repo's main branch [docs/runbooks/deploy.md].
2. Confirm tests are green in CI [.claude/rules/test.md].
3. Get review approval from a CODEOWNER [docs/runbooks/deploy.md].
4. Merge to main; CD pipeline rolls out to staging automatically.
5. After 30 minutes of staging soak time, promote to prod via the manual
   `gh workflow run promote-prod` step [docs/runbooks/deploy.md].

The auth service has additional steps (token rotation pre-deploy)
documented at [docs/runbooks/auth-deploy.md].
```

## Output (Ollama down)

```
Local LLM (Ollama) not running — returning matching files instead of a
synthesized answer.

- docs/runbooks/deploy.md#chunk0       (score=2.105)
- docs/runbooks/auth-deploy.md#chunk0   (score=1.443)
- .claude/rules/test.md#chunk1          (score=0.822)

Start Ollama (`ollama serve`) and re-run for a synthesized answer.
```

## Privacy

Everything happens locally. The query, the brain content, and the answer
never leave the user's laptop. No telemetry. No cloud round-trip.
