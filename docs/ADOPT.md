# `vigil adopt` — mid-project file migration

Most teams don't start with vigil. They have a `docs/` site, a `runbooks/`
folder, a `wiki/` someone abandoned, and the inevitable root-level `NOTES.md`.
`vigil adopt` walks the project, classifies what's already there, and
fills gaps from the bundled team-brain template.

It is the bridge between "we have markdown all over the place" and "we have
a brain vigil can index."

## What it does

1. **Walks** the project from the brain root (default: cwd).
2. **Classifies** every included file into one of five buckets:
   - `KEEP` — already at a canonical path (`CLAUDE.md`, `.claude/skills/`, `docs/`, `knowledge/`, …)
   - `MOVE_SUGGESTED` — looks like a runbook/doc but lives at a non-canonical path (`wiki/foo.md`, `notes/foo.md`)
   - `REVIEW` — heterogeneous root-level markdown the tool can't confidently classify
   - `ADD` — template path is empty in the project; bundled file fills the gap
   - `SKIP_PER_ENGINEER` — never team-shareable (`.claude/settings*.json`)
3. **Suggests a CLAUDE.md split** when the file exceeds the size budget.
4. **Writes** a human-readable plan to `MIGRATION-PLAN.md`.
5. **Optionally applies** template gap files when `--apply` is passed.

## Usage

```bash
# Dry run (default) — writes MIGRATION-PLAN.md, touches nothing else.
vigil adopt

# Apply mode — copies ADD entries, writes MIGRATION.md summary.
vigil adopt --apply

# Customize the include / exclude scope.
vigil adopt --include legacy-docs/ --exclude wiki/
```

## Discovery rules

### Default include

- `CLAUDE.md`
- `docs/`
- `knowledge/`
- `runbooks/`
- `wiki/`
- `notes/`
- `.claude/skills/`
- `.claude/rules/`
- `.claude/commands/`

`--include` *extends* this list. Use `--exclude` to remove a default-included
path you don't want adopt to scan.

### Default exclude

- `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `.vigil-cache`
- Hidden directories matching `^\.[a-z]+/` (except `.claude/` and `.github/`)
- Author/personal output dirs: `articles/`, `oss/`, `applications/`,
  `portfolio/`, `interview-prep/`, `invest/`, `resume/`, `safe-poc/`
- Build outputs: `dist/`, `build/`

The excludes protect against accidentally scanning the assistant's working
directory if a user runs adopt at `$HOME` by mistake.

## Apply semantics

`--apply` only **adds** template gap files. It does not:

- Move existing files.
- Merge file contents.
- Modify CLAUDE.md.

When the same path exists in both project and template, the project's version
wins. We never auto-merge text — humans do that in their own commit.

Every template file copied in via `--apply` gets a `vigil_template: true`
key merged into its YAML frontmatter so future tooling can spot which files
came from the template versus team-authored content.

## The git-cleanliness gate

`--apply` refuses to run when `git status --porcelain` is non-empty. The
brain's git history is the audit trail. CI must never auto-mutate it; if
mistakes happen, `git revert` must be the recovery, not `git stash pop`.

If there is no `.git` directory the gate is open — nothing to preserve. This
makes `adopt --apply` work on fresh, not-yet-initialized projects.

## CLAUDE.md split suggestion

When CLAUDE.md exceeds `--max-claude-md-kb` (default 4 KB), the plan
includes a heuristic split: walk the H2 boundaries, group H2s into chunks
of < 2 KB each, suggest one `.claude/rules/<topic>.md` per chunk. The split
is a *suggestion*; adopt does not perform it. A human re-reads the file,
chooses the split points, and commits the result.

## Plan format

`MIGRATION-PLAN.md` renders one section per action bucket, each entry as
`- relative/path → suggested/target — reason`. The PR-comment workflow
(`brain-ci.yml`) posts this same file back to the PR via
`gh pr comment $PR_NUMBER --body-file MIGRATION-PLAN.md`.

## Why no auto-move

The tool can guess that `wiki/payments-runbook.md` belongs at
`docs/payments-runbook.md`. It cannot guess whether your team *wants* it
there. Maybe the wiki copy is stale and the canonical version is in a third
repo. Maybe `payments` is a service name and the runbook should live under
`docs/services/payments/`. Move *suggestions* are surfaced; humans
execute them in a reviewable commit.

## Examples

### Brand-new project, fresh skeleton

```bash
mkdir my-team-brain && cd my-team-brain
git init -b main
vigil adopt --apply         # populates everything from the template
git add -A && git commit -m "init: team brain via vigil adopt"
```

### Existing project with scattered docs

```bash
cd ~/work/legacy-platform-docs
vigil adopt                 # dry-run, writes MIGRATION-PLAN.md
# Read the plan. Decide which moves are right.
git mv wiki/payments-runbook.md docs/runbooks/payments.md
git commit -m "docs: move wiki runbook into docs/runbooks/"
vigil adopt --apply         # now fill template gaps
```

### CI usage

The bundled `brain-ci.yml` runs `vigil adopt --dry-run` on every PR and
posts the plan as a PR comment. Reviewers see, in the PR conversation,
exactly what would change if the team ran `--apply`.

## Exit codes

`vigil adopt` exits 0 on success, 1 on failure (e.g. dirty git tree under
`--apply`, or filesystem error during copy).

## See also

- `vigil validate` — `docs/VALIDATE.md`
- The bundled template — `templates/team-brain-skeleton/`
