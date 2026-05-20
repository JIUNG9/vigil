# The adapter — personal-vs-team layout

**v0.6.0**

## The problem

Every engineer's laptop has its own filesystem layout. One has notes in
`~/notes/runbooks/`, another keeps a `~/wiki/` they've grown for years, a
third puts everything under `~/Documents/work/`. The team's brain, by
contrast, has a fixed canonical shape: `docs/runbooks/`, `docs/wiki/`,
`knowledge/`, `CLAUDE.md`.

Without a translation layer the engineer faces a lose-lose:

- Restructure all their personal notes to match the team — the kind of
  yak-shave that gets postponed forever.
- Accept that `vigil` doesn't see anything outside the brain repo.

The adapter is the seam. One file per laptop, no code changes.

## What v0.6 ships (MVP scope)

Two responsibilities:

1. **Path translation** — map personal globs to canonical brain paths.
2. **CLAUDE.md section precedence** — when both team and personal
   `CLAUDE.md` exist, team wins by default. The user opts specific H2
   sections into "personal overrides team" mode.

That's it. Skill-collision namespacing, vocabulary aliases, reverse path
translation, auto-detection of personal layouts — all deferred to v0.7.
The advisor's argument was decisive: we don't have 2-3 real adopters yet,
so building the full design would be a strawman. We ship the load-bearing
20% now and expand once usage tells us what to expand.

## File location and precedence

The adapter file is named `.vigil-adapter.toml` and may live at one
of two locations:

| Path | Role |
|---|---|
| `~/.vigil-adapter.toml` | Per-engineer config. Wins on conflict. |
| `<brain-root>/.vigil-adapter.toml` | Team-shipped fallback. Optional. |

When both exist, the home file's keys override the brain file's keys
section by section: `[paths]` rules are unioned with home winning on
collision; the home `personal_overrides_team` list replaces the brain's
list outright. Diagnostic: `vigil adapter show` prints which source
the effective config came from (`home` / `brain` / `merged`).

## Schema

```toml
# ~/.vigil-adapter.toml
[paths]
"~/notes/runbooks/*.md" = "docs/runbooks/{}"
"~/wiki/*.md"           = "docs/wiki/{}"

[claude_md]
personal_overrides_team = ["Personal preferences", "My editor config"]
```

### `[paths]`

Each key is a personal glob pattern; each value is the canonical
brain-relative path it maps to. Rules are evaluated in declaration order;
the **first match wins**.

- Globs may use `~` (expanded against `$HOME`).
- Each glob has **exactly one `*`**. Multi-`*` patterns are silently
  ignored in the MVP.
- The substitution `{}` in the value is filled with what the `*` matched
  *plus the suffix that followed it in the glob*. So
  `"~/notes/runbooks/*.md" = "docs/runbooks/{}"` rewrites
  `~/notes/runbooks/auth.md` to `docs/runbooks/auth.md` — you don't
  repeat `.md` in the template.

The returned path is relative; the caller decides what to root it
against.

### `[claude_md]`

```toml
[claude_md]
personal_overrides_team = ["Personal preferences", "My editor config"]
```

`personal_overrides_team` is a list of H2 section headers (the part after
`## `). The merge rule:

- The team's `CLAUDE.md` is the base, copied verbatim.
- For each H2 in the personal `CLAUDE.md` whose header is in the list:
  - If the team has a section with the same header, **replace** it.
  - If the team does not, **append** the personal section to the end.
- All other personal sections — the ones NOT listed — are **dropped**.
  The team brain owns the canonical content.

This rule is easy to misread. A worked example:

```markdown
# team CLAUDE.md
## Onboarding
Follow the checklist in /docs/onboarding/.

## Personal preferences
We default to vim.

## Deploy
Use the runbook in /docs/runbooks/deploy.md.
```

```markdown
# personal CLAUDE.md
## Personal preferences
I use emacs with evil mode and these settings: …

## Random thoughts
This section is dropped by the merge rule.
```

```toml
[claude_md]
personal_overrides_team = ["Personal preferences"]
```

Result:

```markdown
## Onboarding
Follow the checklist in /docs/onboarding/.

## Personal preferences
I use emacs with evil mode and these settings: …

## Deploy
Use the runbook in /docs/runbooks/deploy.md.
```

`Random thoughts` was dropped because it wasn't on the override list.
`Personal preferences` was replaced because it was. `Onboarding` and
`Deploy` came through unchanged because the personal file didn't touch
them.

## CLI

```bash
# show the effective adapter (or "no adapter configured")
vigil adapter show

# write a starter file (~/.vigil-adapter.toml by default)
vigil adapter init                  # writes to home
vigil adapter init --scope brain    # writes to <brain-root>
vigil adapter init --force          # overwrite existing

# check that every [paths] rule still matches real files
vigil adapter validate
```

`adapter init` looks at `~` for common personal-notes directories
(`notes/`, `wiki/`, `runbooks/`, `personal-brain/`) and surfaces them as
**commented suggestions** in the starter file. It never auto-writes
rules — the user uncomments and tunes.

`adapter validate` returns exit 2 with one warning per dangling rule:

```
adapter has 1 warning(s):
  - rule matches no files: '~/wiki/*.md' → no candidates on disk
```

This is read-only: it doesn't touch the brain or the adapter file.

## When to add an adapter

Add one when:

- Your personal markdown lives outside the team-brain repo and you'd
  benefit from `vigil ask` reaching into it (future v0.7 work, not
  v0.6).
- Your `CLAUDE.md` has a section you'd be happy to hide from the team
  but want active locally (editor config, personal vocabulary aliases).

Skip it when:

- You already keep all your work-context inside the team-brain repo.
  The adapter is dead weight in that case.
- Your personal layout matches the team's. Move on; do something useful.

## When NOT to add an adapter

- **Don't translate company-confidential paths into the team brain.** The
  adapter is a per-engineer overlay; it never publishes anything. But the
  resulting paths flow into local audit logs and `vigil ask`. If a
  personal path is sensitive, leave it outside the adapter's reach.
- **Don't use it as a search-replace for vocabulary.** That's a v0.7
  feature; don't try to abuse `[paths]` to rewrite text content. It maps
  paths only.

## What's coming in v0.7

The full design needs real adopter data. Likely candidates:

- **Skill-collision namespacing.** When the team and engineer both ship
  `auth-deploy.md` skills, choose which wins or alias one.
- **Vocabulary aliases.** "When I say `ENG`, the team's brain says
  `Engineering`."
- **Reverse path translation.** Team-brain paths shown to the user
  rendered back into the engineer's layout.

We'll add them when 2-3 teams have asked for the same thing in the same
way, not before.
