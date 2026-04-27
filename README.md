# teammate

> Battle buddy for new SREs joining regulated teams. **Pluggable Obsidian vault + local LLM (Ollama, gbrain-compatible) + ISO 27001 / K-ISMS-P compliance scanners + production guardrail hooks + git-backed team federation.** One install, day-one ready. No cloud round-trip, no API keys, no Anthropic-cloud lock-in.

> **The Teamspace alternative for teams who can't put compliance state in someone else's cloud.** Sync the vault across the team via private git — every attestation cryptographically signed, every audit trail reproducible from `git log`.

[![CI](https://github.com/placen-org/teammate/actions/workflows/ci.yml/badge.svg)](https://github.com/placen-org/teammate/actions/workflows/ci.yml)
[![OSS Hygiene](https://github.com/placen-org/teammate/actions/workflows/oss-hygiene.yml/badge.svg)](https://github.com/placen-org/teammate/actions/workflows/oss-hygiene.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What is this?

Your first day as an SRE on a regulated team is terrifying. You don't know the codebase. You don't know what's compliant and what isn't. You can't search your team's tribal knowledge. And you're exactly one `git push origin main` away from explaining yourself to your VP.

`teammate` is a Claude Code plugin that bundles four things into one install:

1. **A pluggable Obsidian vault** — every scanner writes evidence here. Your team's compliance state lives in markdown, locally, browsable in Obsidian as-is.
2. **A local LLM + RAG** — Ollama-backed (`llama3.2:3b` by default), with a built-in mini-RAG over the vault and your team's `CLAUDE.md`. Ask `teammate ask "what's our K-ISMS-P posture?"` and get a grounded, cited answer on your laptop. gbrain-compatible if you have it; not required.
3. **Pluggable compliance scanners** — ISO 27001:2022 Annex A + **K-ISMS-P** (the first English-language OSS to score against Korea's ISMS-Personal framework). 10 probes, three-tier results (pass / partial / fail), opt-in signed PDF attestations.
4. **Production guardrails** — a `pre-push` hook that blocks direct pushes to `main`, plus a Claude Code `PreToolUse` hook that catches `terraform apply` on prod paths, `kubectl` mutations against prod contexts, `DROP TABLE` SQL, and other day-1 footguns.

Everything runs locally. No telemetry. No cloud calls. Designed to be installed by a new hire in their first hour and used every day after.

```
                    ┌──────────────────────────────────────┐
                    │   compliance-vault/   (the nucleus)  │
                    │   — Obsidian-format markdown         │
                    │   — version-control or local-only    │
                    └──────────┬───────────────────────────┘
                               │
       ┌─────────────┬─────────┼──────────────┬──────────────────┐
       ▼             ▼         ▼              ▼                  ▼
   ┌────────┐   ┌─────────┐ ┌────────┐  ┌────────────┐    ┌──────────┐
   │ score  │   │  watch  │ │ attest │  │ ask-vault  │    │ MCP      │
   │ probes │   │ (RSS,   │ │ (PDF + │  │ (Ollama +  │    │ server   │
   │        │   │ NVD)    │ │  sig)  │  │  RAG)      │    │ (vault   │
   └────────┘   └─────────┘ └────────┘  └────────────┘    │ resource)│
       ▲             ▲         ▲              ▲          └──────────┘
       │             │         │              │                  ▲
       └─────────────┴─────────┴──────────────┴──────────────────┘
                                  │
                       Claude Code plugin spec
                                  │
                       hooks/ (pre-push, PreToolUse)
                       skills/  (5 skill files)
                       .claude-plugin/plugin.json
```

## Install

One line, after [Claude Code](https://claude.ai/code) is set up:

```bash
claude plugin install placen-org/teammate
```

Then on the first run, `/init-teammate` (or `teammate init` in your shell):

```bash
teammate init
```

That command:

- Scaffolds `compliance-vault/` (with a `.gitignore` so it stays local-only by default)
- Installs the `pre-push` hook into `.git/hooks/` (refuses to clobber any existing hook unless you pass `--force`)
- Detects [Ollama](https://ollama.com/download) and tells you which models to pull (`llama3.2:3b`, `nomic-embed-text`)
- Detects [gbrain](https://gstack.dev) if you have it and offers to register the vault as a source
- Builds the initial vault index

Total runtime: ~10 seconds. Total dependencies you actually have to install: zero (Ollama + gbrain are optional).

## Use

### Ask the vault anything

```bash
teammate ask "what's our current K-ISMS-P posture and which controls are failing?"
```

Streams a grounded answer from your local LLM, citing `compliance-vault/` markdown files for every fact. Works offline. If Ollama isn't running, falls back to keyword search and tells you how to start it.

### Score the repo

```bash
teammate score
```

Output:

```
teammate score — overall: 73.3%  (pass=11 partial=4 fail=0 n/a=0 indet=0)
target: /Users/.../my-team-repo
commit: abc123def

probe                   result                  framework:control       severity
----------------------  ----------------------  ----------------------  ----------------------
codeowners-exists       pass                    iso-27001:A.5.2         medium
codeowners-exists       pass                    k-isms-p:2.1.3          medium
branch-protection       partial                 iso-27001:A.8.32        high
secrets-scan            pass                    iso-27001:A.8.24        critical
tf-state-encryption     partial                 iso-27001:A.8.24        critical
dependency-pinning      pass                    iso-27001:A.8.30        high
oss-hygiene-workflow    pass                    iso-27001:A.5.36        medium
pre-commit-config       pass                    iso-27001:A.8.25        medium
license-present         pass                    iso-27001:A.5.32        medium
security-md-present     pass                    iso-27001:A.5.34        high
dependabot-or-renovate  partial                 iso-27001:A.8.30        high
```

`partial` results promote to `pass`/`fail` when you re-run with `--as-admin` and a `GITHUB_TOKEN` that has `admin:repo` scope. The `partial` tier exists because new SREs almost never have repo-admin on day 1 — the tool is honest about what it can verify locally vs what needs the team's GitHub.

### Generate a signed audit PDF

```bash
teammate score --sign
```

Opens an interactive sigstore keyless OAuth flow (browser popup), then writes a `.pdf`, `.sig`, and `.crt` into `compliance-vault/attestations/`. An external auditor can verify with:

```bash
sigstore verify-blob compliance-vault/attestations/2026-04-26-1030.pdf \
  --signature compliance-vault/attestations/2026-04-26-1030.pdf.sig \
  --certificate compliance-vault/attestations/2026-04-26-1030.pdf.crt \
  --certificate-identity https://github.com/placen-org/teammate/.github/workflows/sign-example.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

If verification exits zero, the auditor knows the PDF is exactly what teammate produced at the recorded timestamp, untampered. See `docs/SECURITY.md` for the threat model — it's important to understand what the signature does and doesn't prove.

### Watch external advisories

```bash
teammate watch
```

Pulls KISA RSS + NVD CVE 2.0 feeds, diffs against the last run, writes new items into `compliance-vault/advisories/<timestamp>.md`. Run weekly or via cron.

### Federate the vault across the team (the Teamspace alternative)

Claude Teamspace gives a team shared workspace state in Anthropic's cloud. Regulated teams can't put compliance state there. They CAN put it in a private git repo they already own. `teammate sync` does that:

```bash
# One-time per laptop: bind to the team's private vault repo
teammate sync init git@github.com:acme-corp/team-vault.git

# After every score run, push your attestations to the team
teammate score
teammate sync push

# Before scoring, pull the team's latest state
teammate sync pull

# See where you are
teammate sync status
```

Every attestation is cryptographically dual-signed (git commit by the engineer, PDF body by sigstore/Fulcio). The team timeline is just `git log` — `git blame` tells you who attested what, when, against which commit. Works on private GitHub, GitHub Enterprise, GitLab self-hosted, Gitea, anything that speaks git. Nothing leaves the team's jurisdiction.

|  | Claude Teamspace | `teammate sync` |
|---|---|---|
| Data location | Anthropic cloud | Team's own private git host |
| Subscription | Per-seat paid | Free, OSS |
| Audit trail | Chat history | `git log` of cryptographically dual-signed attestations |
| Air-gap capable | No | Yes (self-hosted GitLab, Gitea, etc.) |
| Sovereignty | Anthropic's jurisdiction | Team's jurisdiction |
| Required infra | Claude.ai for Teams | Private git (the team already has) |

## Why does this exist

A new SRE on day 1 of a regulated team has four problems at once:

1. They don't know the codebase or the compliance posture.
2. They have no auditable evidence of what the team's compliance state is on the day they joined.
3. They're one keystroke away from breaking production.
4. They can't keep up with new advisories.

The OSS world treats each as a separate vertical. HR tools handle onboarding. GRC platforms handle compliance. Coding agents handle productivity. Local-LLM tools handle personal KM. Nobody has bundled them as one experience for the new-SRE-on-day-1 persona — and nobody speaks **K-ISMS-P** (Korea's ISMS-Personal compliance framework) in English-language OSS at all.

`teammate` is that bundle. The vault is the nucleus, the local LLM makes it queryable, the scanners are pluggable, the guardrails are real.

## What this is NOT

- **Not a replacement for GitHub branch protection / RBAC / your IdP.** Hooks are defense in depth. A determined user can `chmod -x` them.
- **Not a real-time vulnerability scanner.** `watch` polls public feeds; it doesn't check whether your stack is affected. Pair with your CVE/SBOM tooling.
- **Not a substitute for a real audit.** Mechanical scoring tells an auditor "the team has the artifact" — not "the team operates correctly." See `docs/SECURITY.md`.

## Configuration

Most things are env-var configurable so a team can override without forking:

| Variable | Default | What it does |
|---|---|---|
| `TEAMMATE_VAULT_ROOT` | `./compliance-vault` | Vault location |
| `TEAMMATE_CATALOGS_DIR` | repo `catalogs/` | Override compliance catalogs (add internal controls) |
| `TEAMMATE_HOOKS_DIR` | repo `hooks/` | Override bundled hooks |
| `TEAMMATE_FORCE_INIT` | `0` | Allow `init` to overwrite an existing pre-push hook |
| `TEAMMATE_OVERRIDE` | `0` | Bypass guardrail hooks for one push (use sparingly) |
| `TEAMMATE_PROTECTED_BRANCHES` | `main master production prod release` | Branches the pre-push hook blocks |
| `TEAMMATE_ADMIN_MODE` | `0` | With `GITHUB_TOKEN`, promotes partial results via `gh api` |

## Project structure

```
teammate/
  .claude-plugin/plugin.json          # Claude Code plugin manifest
  skills/                             # 6 skill files
    init-teammate/SKILL.md
    score-compliance/SKILL.md
    attest-compliance/SKILL.md
    watch-advisories/SKILL.md
    ask-vault/SKILL.md
    sync-vault/SKILL.md               # the Teamspace-alternative federation
  hooks/
    pre-push                          # raw bash, copied to .git/hooks/
    pre-tool-use-guardrail.sh         # Claude Code PreToolUse hook
  catalogs/
    iso-27001-annex-a.yaml            # 16 controls (ISO 27001:2022)
    k-isms-p.yaml                     # 25 controls (K-ISMS-P 2.x, EN summaries)
  src/teammate/
    cli.py                            # `teammate` entry point (click)
    init.py                           # `teammate init` orchestrator
    score.py                          # 10 probes + score aggregation
    vault.py                          # Obsidian-format markdown writer
    attest.py                         # PDF + opt-in sigstore signing
    watch.py                          # KISA RSS + NVD JSON 2.0 diff
    sync.py                           # git-backed team vault federation
    catalogs.py                       # YAML loader + Probe-Control mapping
    mcp_server.py                     # JSON-RPC MCP server (vault as resource)
    rag/
      __init__.py
      ollama.py                       # Ollama HTTP client
      index.py                        # sqlite-backed vault index
      ask.py                          # retrieve + LLM orchestration
      gbrain.py                       # gbrain compatibility layer
  examples/
    attestation.pdf                   # pre-signed example (signed in CI)
    attestation.pdf.sig
    attestation.pdf.crt
  docs/
    OSS_HYGIENE.md                    # the no-employer-names rule
    SECURITY.md                       # threat model + verification
    QUICKSTART.md                     # 90-second read
  .github/workflows/
    ci.yml                            # pytest + ruff
    oss-hygiene.yml                   # blocks employer names in source
    sign-example.yml                  # workflow_dispatch sigstore signer
  tests/                              # pytest + bats
  pyproject.toml
  README.md
  SECURITY.md
  LICENSE
  SOURCES.md                          # K-ISMS-P provenance
```

## Contributing

This is OSS for any team to adopt. The OSS hygiene workflow blocks any commit that hardcodes employer names, personal emails, or non-placeholder AWS account IDs in source code. See `docs/OSS_HYGIENE.md` for the policy and the playbook for adding new patterns.

Issues and PRs welcome. The full ~80-control K-ISMS-P catalog is tracked in [issue #1](https://github.com/placen-org/teammate/issues/1) — translation contributions especially appreciated from Korean-speaking compliance practitioners.

## License

MIT — see [LICENSE](LICENSE). The K-ISMS-P catalog is a derivative work; see [SOURCES.md](SOURCES.md) for provenance and the legal note on KISA's official framework.
