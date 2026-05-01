# CLAUDE.md — TEAM-NAME

This file is loaded by Claude Code at the start of every session in this repository
(or in any repo where this team-brain is registered as a context source). Edit it to
encode the rules, conventions, and tribal knowledge that every engineer on the team
should agree on.

## Who we are

- **Team:** TEAM-NAME (e.g., Platform Engineering at acme-corp)
- **What we own:** the canonical list of services, systems, and processes the team is
  responsible for. Update `knowledge/services.md` and `knowledge/people.md` as
  authoritative sources.

## How we work

Add the rules every engineer should follow. Keep them direct and actionable. Examples:

- We commit using conventional commits (`feat:`, `fix:`, `chore:`, etc.).
  See `.claude/rules/commit.md` for the full convention.
- We never push directly to `main`. Use a feature branch + PR.
- We require code review on every PR. CODEOWNERS file enforces approvers.
- We pin dependencies. Lockfiles must be committed.
- We document architecture decisions in `knowledge/decisions/` as ADRs.

## How we use teammate

This repo IS the team brain. Each engineer:

1. Clones the repo locally.
2. Runs `teammate init` (one time per laptop) — sets up Ollama, builds the local
   sqlite-vec index of every markdown file in this repo.
3. Uses `teammate ask "<question>"` to query the brain locally with the team's
   own LLM. Answers cite the markdown files they came from.
4. When a piece of tribal knowledge needs to land in the brain: write it as
   markdown, commit, push, PR. The CI workflow re-builds the canonical index
   automatically and attaches it as a release artifact.

## What goes in this repo (and what doesn't)

- **Goes in:** rules, conventions, runbooks, on-call procedures, ADRs, onboarding
  walkthroughs, service catalog, "who owns what."
- **Stays out:** secrets (use a real secret store), application code (lives in
  service repos), customer data (PII has its own handling rules per
  `.claude/rules/privacy.md` if you have one).

## Skills

Team-specific Claude Code skills live in `.claude/skills/`. Each skill is a folder
with a `SKILL.md` describing when Claude should invoke it. See
`.claude/skills/example-skill/SKILL.md` for the canonical template.

## Pointers

- New on the team? Start at `docs/onboarding/README.md`.
- On call? `docs/runbooks/README.md` is the index.
- Designing something? Check `knowledge/decisions/` for prior ADRs first.
- Need to know who owns service X? `knowledge/services.md`.

---

*This file replaces ad-hoc Slack DMs and outdated Confluence pages with a single,
version-controlled, locally-queryable source of truth. Edit it as your team learns.*
