# `vigil validate` — read-only structural check

Validation is what CI runs on every push. It must be:

1. **Read-only.** No mutations, no side effects, no network calls.
2. **Cheap.** Walk the markdown, run regexes, parse YAML — that's it.
3. **Stable in CI.** Fixed exit codes:
   - `0` — all PASS
   - `1` — at least one FAIL (broken brain — block the merge)
   - `2` — at least one WARN (cosmetic — surface but don't block)

The checks are intentionally narrow. We're not enforcing prose quality.
We're catching the structural mistakes that turn a brain into a junk drawer.

## Usage

```bash
vigil validate
vigil validate --json                 # machine-readable
vigil validate --max-claude-md-kb 8   # raise the size budget
```

## The seven checks

### `claude_md_present`

**Status:** `FAIL` if `<brain_root>/CLAUDE.md` is missing, else `PASS`.

CLAUDE.md is the contract: it's the file Claude Code loads at session start.
A brain without a root CLAUDE.md is structurally broken.

### `claude_md_size`

**Status:** `WARN` when CLAUDE.md exceeds `--max-claude-md-kb` (default 4 KB).

A bloated CLAUDE.md is a smell — model context is finite, and rules deserve
their own files in `.claude/rules/`. The WARN is loud enough to spot, soft
enough not to block.

### `markdown_link_resolution`

**Status:** `FAIL` on the first unresolved internal link.

Walks every `.md` file, finds `[label](target)` references, and resolves
them as paths relative to the source file. External links (`http(s)://`,
`mailto:`, anchor-only `#foo`, absolute `/path`), images (`![alt](src)`),
and links that escape the brain root are skipped. Everything else must
resolve to a file or directory inside the brain.

### `orphan_files`

**Status:** `WARN` with a list of orphan paths.

A markdown file is "reachable" if it's in a canonical section
(`.claude/skills/`, `.claude/rules/`, `.claude/commands/`, `docs/`,
`knowledge/`, `runbooks/`) or if `CLAUDE.md` links to it directly.
Files that are neither are orphans — they exist, but nobody finds them.

### `non_canonical_paths`

**Status:** `WARN` when markdown lives under `wiki/`, `notes/`, or
`wiki-archive/`.

These paths are tolerated by `iter_markdown` but they're not where the
brain template puts content. The WARN nudges teams to migrate via
`vigil adopt`'s `MOVE_SUGGESTED` entries.

### `binary_files_in_brain`

**Status:** `WARN` with a count.

Anything under `docs/` or `knowledge/` that isn't a markdown file or a
known image suffix (`.png`, `.svg`, `.jpg`, `.jpeg`, `.gif`, `.webp`).
Binary blobs in the brain are usually accidental commits — a PDF dragged
into a runbook folder, a `.zip` that should have been a Release artifact.

### `frontmatter_parses`

**Status:** `FAIL` on the first parse error.

Every markdown file that opens with `---` must have closing `---` and a
YAML body that parses. A broken frontmatter block silently corrupts the
brain index — the parser fails open, the file's metadata vanishes, and
search loses the file. Catching this in CI is much cheaper than catching
it in a stale-result complaint six weeks later.

## Exit codes & CI integration

```bash
vigil validate
echo $?    # 0 PASS  |  1 FAIL  |  2 WARN
```

In a GitHub Actions workflow:

```yaml
- name: Run validate
  run: vigil validate
```

A non-zero exit fails the job. If you want to allow WARNs to pass, gate on
the JSON output instead:

```yaml
- name: Run validate
  run: |
    vigil validate --json > validate.json
    code=$?
    if [ "$code" -eq 1 ]; then exit 1; fi
    # WARN-only is OK.
    exit 0
```

## JSON schema

```jsonc
{
  "brain_root": "/path/to/brain",
  "max_claude_md_kb": 4,
  "overall": "PASS" | "WARN" | "FAIL",
  "exit_code": 0 | 1 | 2,
  "checks": [
    {
      "name": "claude_md_present",
      "status": "PASS",
      "summary": "...",
      "details": { "...": "..." }
    },
    ...
  ]
}
```

The `name` field is stable across versions; CI tooling can grep on it.

## What validate does NOT check

- Prose quality, grammar, consistency.
- Whether links go to the *right* file (only that they resolve).
- Whether the team's actual rules are good rules.
- Whether `knowledge/services.md` is up-to-date.

That's a human review concern. validate is the seatbelt, not the driver.

## See also

- `vigil adopt` — `docs/ADOPT.md`
- `vigil doctor` — runtime diagnostic (config, reachability, models, index)
