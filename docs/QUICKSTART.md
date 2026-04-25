# Quickstart — 90 seconds

You just joined the team. Your manager pointed you at this README. Here's the fast path.

## Step 1 — Install Claude Code (if you don't have it)

```bash
# Mac/Linux
curl -fsSL https://claude.ai/install.sh | sh
```

## Step 2 — Install teammate

```bash
claude plugin install placen-org/teammate
```

## Step 3 — Run init

```bash
cd /path/to/your-team-repo
teammate init
```

You'll see a five-line summary. If anything fails, the message tells you exactly what to do.

## Step 4 — Get a baseline score

```bash
teammate score
```

Output is a terse table. The first run is your day-1 baseline.

## Step 5 — (Optional but worth it) install Ollama

```bash
# Mac
brew install ollama
ollama serve &
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

Now `teammate ask` works:

```bash
teammate ask "what's our current K-ISMS-P posture?"
```

You'll get a streamed answer grounded in the vault on your laptop, no cloud.

## Optional Claude Code wiring

If you want the `PreToolUse` guardrail active inside Claude Code, add to `.claude/settings.json` in the team repo:

```json
{
  "hooks": {
    "PreToolUse": ".claude-plugins/teammate/hooks/pre-tool-use-guardrail.sh"
  }
}
```

## Daily flow

```bash
teammate score             # before lunch, after a refactor
teammate ask "..."         # whenever you're unsure
teammate watch             # weekly cron
teammate score --sign      # before audit window
```

## When you forget

```bash
teammate --help
teammate score --help
```

## When something goes wrong

- **Hooks not running?** `cat .git/hooks/pre-push` should show our script. Re-run `teammate init`.
- **Ollama not answering?** `curl http://localhost:11434/api/tags` should return JSON. If not, `ollama serve`.
- **Vault empty?** Run `teammate score` — the vault populates on the first run.
- **Indexing failed?** `teammate ask --rebuild` forces a clean index.

## What to do next

- Read `docs/SECURITY.md` for the threat model on signed attestations.
- Read `docs/OSS_HYGIENE.md` if you're going to contribute back.
- Check `compliance-vault/latest.md` after a `score` run — that's your team's posture in markdown.
