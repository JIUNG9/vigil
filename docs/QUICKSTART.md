# Quickstart — 90 seconds

You just joined a team that uses vigil. Here's the fast path.

## Step 1 — Install Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | sh
```

## Step 2 — Install vigil

```bash
pip install vigil
# or:
claude plugin install JIUNG9/vigil
```

## Step 3 — Clone the team-brain

```bash
git clone git@github.com:<your-org>/team-brain.git ~/team-brain
cd ~/team-brain
```

## Step 4 — Run init

```bash
vigil init
```

You'll see a four-line summary. If anything fails, the message tells you what to do.

## Step 5 — Install Ollama (recommended)

```bash
brew install ollama
ollama serve &
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

## Step 6 — Ask the brain

```bash
vigil ask "what does this team do?"
vigil ask "what's our deploy procedure?"
```

You're done. Five-minute total wall-clock from zero to a working local-LLM Q&A
over your team's brain.

## Daily flow

```bash
vigil ask "..."          # whenever you're unsure
git pull                    # when teammates update the brain
vigil init               # re-runs the index (incremental)

# When YOU update the brain:
echo "..." >> docs/runbooks/new-procedure.md
git commit -am "runbook: new procedure"
git push                    # CI rebuilds the index for everyone else
```

## When something goes wrong

- **`vigil init` says "no CLAUDE.md"?** You're not in the team-brain repo. Check `pwd`.
- **Ollama not answering?** `curl http://localhost:11434/api/tags` should return JSON. If not, `ollama serve`.
- **Index empty?** Run `vigil index --rebuild` to force a clean rebuild.
- **`vigil ask` returns just file paths, no answer?** Ollama isn't running. Start it; vigil falls back to keyword search when it's down.
