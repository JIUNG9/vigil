---
name: example-skill
description: Template skill — replace with a real one. When invoked, this skill describes (in plain English) what Claude should do, when to invoke it, and what success looks like for THIS team's specific workflow.
---

# /example-skill

Replace this file with a real team skill. The format is a YAML frontmatter block
followed by a SKILL.md body. Claude reads BOTH the frontmatter `description` (used
for routing decisions) and the body (used as the actual behavior contract).

## When to invoke

Concrete trigger phrases. Examples:

- "deploy the auth service" → invoke this skill
- "rotate the prod database password" → invoke this skill

## Behavior

Step-by-step instructions, written for Claude to read and follow:

1. Verify the on-call engineer is in `#oncall-pager`.
2. Run the pre-deploy checklist in `docs/runbooks/deploy.md`.
3. Use `teammate ask "what changed in this service since last deploy?"` to surface
   the diff summary.
4. Confirm the rollback plan is documented in the PR.

## Run

```bash
# example invocation
teammate ask "is the auth service safe to deploy right now?"
```

## What this skill is NOT

- Not a full deploy automation. It's the contextual checklist Claude uses to help
  the engineer make a confident decision.
