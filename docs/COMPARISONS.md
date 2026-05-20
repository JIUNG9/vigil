# How vigil Compares to Obsidian, gbrain, and Cloud RAG (Notion / Confluence)

vigil is one point in a four-corner design space. The other three corners — Obsidian, gbrain, and Cloud RAG — are each genuinely good at something vigil is not. This document names those strengths honestly. If we're wrong about which corner you're in, we'd rather you find out here than after `vigil init`.

For the one-screen capability matrix, see the [stack comparison matrix](https://github.com/JIUNG9/vigil) referenced throughout the vigil docs. This page is the prose version, with explicit "pick the other tool instead" guidance per alternative.

## Obsidian

Obsidian is a local-first markdown notebook with a graph view, backlinks, and a deep plugin ecosystem. It treats your notes as plain `.md` files in a folder you own.

**What it does well.** The single-user UX is best in class. Graph view, canvas, daily notes, the plugin marketplace — all of it is polished and offline-first. Sovereignty is real: your vault is just a folder on disk. There is nothing to install on a server, nothing to log into, nothing that phones home.

**The architectural property vigil has that Obsidian does not.** Team federation as a first-class operation. Obsidian Sync exists as a paid add-on, but the underlying model is "one vault, one user." There is no PR workflow for docs, no `git blame` for "who wrote this runbook and why," and onboarding a vigil means handing them a copy of your vault. vigil's substrate — a private git repo plus a local index per laptop — gives you branch / PR / merge / blame for documentation by construction.

**When you should pick Obsidian instead of vigil:**

- You're the only author and there is no team to federate with.
- The graph view is the point — you want a visual exocortex, not a CLI Q&A tool.
- You want a rich plugin ecosystem (Dataview, Templater, Excalidraw) that vigil intentionally does not ship.

## gbrain

gbrain is a single-user AI-curated knowledge graph backed by a local Postgres (PGLite or Supabase). It ingests your notes, builds a graph, and exposes it to coding agents over MCP.

**What it does well.** First-class local AI: ingest, embeddings, graph traversal, and timeline tracking are all native, not bolted on. The graph model is richer than vigil's flat-markdown-plus-vectors approach — gbrain knows about entities, relationships, and versions out of the box. For a single engineer who wants an AI-managed second brain that other agents can call, it is the strongest option in the space.

**The architectural property vigil has that gbrain does not.** Team federation. gbrain is one brain per user. There is no merge-conflict story, no PR review for the graph, no "your vigil updated the deploy runbook, pull to get it" workflow. vigil is interoperable with gbrain (it auto-detects gbrain on init and complements it), but the federation layer — `git push` / `git pull` against a shared private repo — is vigil's, not gbrain's.

**When you should pick gbrain instead of vigil:**

- The brain has exactly one author and you want AI to manage the graph for you.
- You need rich entity / relationship / timeline modeling beyond flat markdown.
- You're already running gbrain and a CLI Q&A tool over a shared repo would be redundant.

## Cloud RAG (Notion AI, Confluence AI, Atlassian Teamspace)

Cloud RAG is the vendor-hosted pattern: your team's documents live in someone else's database, indexed by their pipeline, queried by their LLM. SSO, real-time co-edit, and a polished web UI come included.

**What it does well.** Onboarding is genuinely zero-friction — share a workspace URL and they're in. Real-time co-edit, comment threads, and a rich block editor are well beyond what vigil offers. For non-sensitive documentation in a team that already trusts the vendor, Cloud RAG is the path of least resistance and the right answer.

**The architectural property vigil has that Cloud RAG does not.** Sovereignty. Your team's source of truth never leaves infrastructure you control. The brain is a private git repo on your git host; the LLM runs on the engineer's laptop via Ollama; the index is a sqlite file. There is no vendor ToS surface, no cloud exfil risk, no audit log that lives in someone else's tenant. `git log` and `git blame` are the audit trail, and they are yours.

**When you should pick Cloud RAG instead of vigil:**

- The team values real-time collaborative editing over sovereignty, and the data isn't regulated.
- Onboarding non-engineers (PMs, designers, support) matters more than CLI ergonomics.
- You're already paying for the vendor and the brain is a small slice of a larger workspace you're not going to migrate.

## Closing

The full architecture rationale — sqlite-vec, Ollama, git as the federation layer, markdown as the format, what we explicitly chose not to use — lives in the README under [Why this architecture](../README.md#why-this-architecture). Source and issues: [github.com/JIUNG9/vigil](https://github.com/JIUNG9/vigil).
