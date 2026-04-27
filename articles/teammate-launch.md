---
title: "I built the Teamspace alternative for regulated teams: a Claude Code plugin that keeps every byte of your team's compliance state out of someone else's cloud"
subtitle: "Three months ago I started writing K-ISMS-P translations in a markdown file at my day job. This weekend, that scratch file became `teammate` — and it federates across a whole team via private git, no Claude Teamspace required."
author: "Jiung Gu (June Gu)"
publication: "Medium"
canonical_url: "https://github.com/placen-org/teammate"
tags:
  - sre
  - devops
  - claude-code
  - compliance
  - iso-27001
  - k-isms-p
  - obsidian
  - ollama
  - rag
status: draft
target_publish: "2026-04-26 KST evening"
---

# I built the Teamspace alternative for regulated teams: a Claude Code plugin that keeps every byte of your team's compliance state out of someone else's cloud

> Three months ago I started writing English translations of K-ISMS-P controls in a markdown file at my day job because I was tired of doing audits by hand. This weekend, that scratch file became an OSS Claude Code plugin: `teammate`. It scores any team's repo against ISO 27001 + K-ISMS-P, generates a signed audit PDF, and on day one blocks new hires from `git push origin main` on a Friday at 5 pm. **And it federates the team's compliance state via private git, not Anthropic's cloud — the Teamspace alternative for teams whose data can't leave the country.**

---

## Why "Teamspace alternative"?

Claude Teamspace is a great product for productivity-focused teams. It is also a non-starter for an entire category of users: regulated industries where data residency rules forbid sending team-internal compliance state to a US-headquartered cloud.

Korean fintech. Korean public sector. Korean healthcare. Defense. Financial trading floors. Government contractors anywhere. The list of teams who can't open Claude Teamspace is long, and it's the same list of teams that would benefit MOST from team-shared compliance tooling.

So I built one that lives entirely on the team's own infrastructure. The vault is markdown. The LLM is Ollama. The team-shared timeline is private git. Nothing leaves the team's jurisdiction — not their codebase, not their compliance state, not the prompt history of the engineer asking "is our K-ISMS-P 2.5.1 control failing?".

Comparison:

|  | Claude Teamspace | `teammate` |
|---|---|---|
| Data location | Anthropic cloud | Team's own private git host |
| Subscription | Per-seat paid | Free, OSS (MIT) |
| Audit trail | Chat history | `git log` of cryptographically dual-signed attestations (sigstore + git commits) |
| Air-gap capable | No | Yes (self-hosted GitLab, Gitea, on-prem) |
| Sovereignty | Anthropic's jurisdiction | Team's jurisdiction |
| Required infra | Claude.ai for Teams | Private git (the team already has) |
| K-ISMS-P scoring | No | First English-language OSS to ship K-ISMS-P at all |
| Production guardrails | Chat-only | Hooks that physically block dangerous git/terraform/kubectl commands |

This isn't competing with Teamspace; it's serving a different market. If your team CAN use Teamspace, by all means use it — it's a great product. If your team can't, `teammate` is the sovereign-stack alternative.

---

## The problem nobody bundles

Your first day as an SRE on a regulated team is terrifying.

You don't know the codebase. You don't know what's compliant and what isn't. You can't search your team's tribal knowledge — there's a Confluence somewhere, a Notion somewhere, a `CLAUDE.md` somewhere, and you don't know where any of them are. And you're exactly one `git push origin main` away from explaining yourself to your VP.

The OSS world treats each of those as a **separate vertical**:

- HR tools (Moxo, Enboarder) handle onboarding.
- GRC platforms (ISMS CORE, Comp AI, Scytale) handle compliance.
- Coding agents (Cline, Aider, Cody) handle productivity.
- Local-LLM tools (LLM Wiki, Smart Connections) handle personal knowledge.

Nobody bundles them. And nobody — *nobody* — speaks **K-ISMS-P** (Korea's ISMS-Personal compliance framework) in English-language OSS. I checked. Zero hits across `site:github.com K-ISMS-P`, `"K-ISMS-P scoring"`, `"Korean ISMS-P compliance"`, `"K-ISMS open source"`. The Korean fintech, healthcare, and public-sector companies who *need* this scoring have been doing it by hand or paying consultants who don't read Korean natively.

I'm a Korean SRE. I read K-ISMS-P natively. So I built the bundle.

## What teammate is

`teammate` is a Claude Code plugin. One install, day-one ready, no cloud round-trip:

```bash
claude plugin install placen-org/teammate
teammate init
```

Four pillars, with the **vault as the nucleus** and the **local LLM as the headline**:

```
                   compliance-vault/   ← the nucleus
                   (Obsidian-format markdown)
                          │
       ┌──────────────────┼──────────────────┐
       ▼                  ▼                  ▼
   pluggable        pluggable          pluggable
   scanners:        scanners:          scanners:
   score            watch              attest
   (ISO 27001       (KISA RSS,         (PDF + opt-in
    K-ISMS-P)        NVD CVE)           sigstore sign)
                          │
                          ▼
                Local LLM (Ollama, gbrain-compatible)
                + minimal RAG over the vault and
                your team's CLAUDE.md
                          │
                          ▼
                ` teammate ask "what's our K-ISMS-P posture?" `
                MCP server exposes vault as Claude Code resource
                          │
                          ▼
                Production guardrails (git pre-push,
                Claude Code PreToolUse hooks)
```

The vault is markdown. You can browse it in Obsidian as-is. Every file has YAML frontmatter. Every link is a plain markdown link.

The local LLM is Ollama (default `llama3.2:3b`, default embeddings `nomic-embed-text`). If you have it installed, `teammate ask "what's our compliance posture?"` streams a grounded, cited answer on your laptop. If you don't, the CLI falls back to keyword search and tells you how to install Ollama.

The compliance scoring runs **10 probes** with a three-tier result: `pass / partial / fail / n/a / indeterminate`. The `partial` tier exists because new SREs don't have admin scope on their team's GitHub on day one — the tool is honest about what it can verify locally vs what needs the team to grant access.

The signed PDF attestation uses **sigstore keyless** via GitHub OIDC. The auditor runs `sigstore verify-blob`, gets `OK`, and knows the PDF is exactly what teammate produced at the recorded timestamp. Pure Python, no `cosign` binary required.

## The K-ISMS-P moat

Korea's K-ISMS-P framework has roughly 80 controls across three domains: Management System, Protection Measures, and Personal Information Processing. KISA publishes the official text in Korean. There has never, to my knowledge, been an English-language OSS catalog of it. v0.1 of `teammate` ships the **top 25 highest-impact controls**, translated KO→EN by reading the Korean source — derivative work, not verbatim KISA prose, so it's MIT-publishable. Every control is cross-mapped to its ISO 27001:2022 Annex A nearest-equivalent based on professional judgment.

The full ~80-control catalog is tracked in [GitHub issue #1](https://github.com/placen-org/teammate/issues/1) — translation contributions from Korean-speaking practitioners explicitly welcome.

This is the wedge. Not "another compliance tool" — *the first English-language OSS to score K-ISMS-P at all.*

## The vault is the architectural insight I almost missed

When I started designing `teammate`, I had it as four separate pillars: setup, scoring, Obsidian RAG, guardrails. An adversarial reviewer told me to cut Obsidian/RAG entirely — said the RAG framing read as hobbyist, would dilute the recruiter signal. They were partly right. They were also wrong.

The split I missed at first: **external dynamic knowledge** (CVEs, K-ISMS amendments, ISO errata) is one job; **internal static state** (your team's compliance evidence, audit trail) is a different job. RAG over a Confluence dump conflates them. A compliance vault separates them.

So the vault stayed — but as the *evidence store*, not a knowledge base. Every scanner writes there. The local LLM RAGs over there. The MCP server exposes there. The vault is the nucleus that makes "all four pillars at once" coherent instead of bloated.

This is the kind of architecture decision that's easy to miss when you take an adversarial reviewer's framing as a verdict instead of as data.

## What's in v0.1

```
teammate/
  .claude-plugin/plugin.json              # Claude Code plugin manifest
  skills/
    init-teammate/SKILL.md
    score-compliance/SKILL.md
    attest-compliance/SKILL.md
    watch-advisories/SKILL.md
    ask-vault/SKILL.md
  hooks/
    pre-push                              # raw bash; copied to .git/hooks/
    pre-tool-use-guardrail.sh             # Claude Code PreToolUse hook
  catalogs/
    iso-27001-annex-a.yaml                # 16 controls (subset)
    k-isms-p.yaml                         # 25 controls (top-25, EN summaries)
  src/teammate/
    cli.py                                # `teammate` entry point
    init.py                               # `teammate init` orchestrator
    score.py                              # 10 probes
    vault.py                              # Obsidian-format writer
    attest.py                             # PDF + opt-in sigstore
    watch.py                              # KISA + NVD diff
    catalogs.py                           # YAML loader
    mcp_server.py                         # JSON-RPC MCP server
    rag/
      ollama.py                           # Ollama HTTP client
      index.py                            # sqlite vault index
      ask.py                              # retrieve + LLM stream
      gbrain.py                           # gbrain compatibility
  examples/                               # pre-signed attestation example
  docs/                                   # OSS hygiene + threat model + quickstart
  tests/                                  # 47 passing tests + 3 fixture repos
  .github/workflows/                      # ci, oss-hygiene, sign-example
```

47 tests pass. End-to-end works. Smoke test in CI confirms.

## What this is NOT

- Not a replacement for GitHub branch protection / RBAC / your IdP. The hooks are defense in depth; a determined user can `chmod -x` them.
- Not a real-time vulnerability scanner. `watch` polls public feeds; doesn't check whether your stack is affected.
- Not a substitute for a real audit. Mechanical scoring tells an auditor "the team has the artifact" — not "the team operates correctly." The threat model is honest about this.

## What I'd change if I weren't shipping in a weekend

Shorter list than usual:

- **Compliance-trestle / OSCAL.** Hand-rolled YAML loader is fine for v0.1, but moving to OSCAL native via IBM's compliance-trestle gives me the rest of ISO 27001 Annex A (and CIS, NIST 800-53, etc.) for free in v0.2.
- **Real GitHub admin-mode integration.** Right now `--as-admin` is wired into the CLI but the actual `gh api` calls for the partial-tier probes are stubs. Coming in v0.1.1.
- **Embedding model choice.** Default `nomic-embed-text` is good for English. Korean compliance terms (개인정보, 정보보호, 점검) are in the catalog though, so a multilingual embedder (`mxbai-embed-large` or similar) might actually retrieve better for K-ISMS-P-heavy queries. Need to A/B test.

## Why I built this

Two reasons, both honest:

1. **I was tired of doing K-ISMS-P audits by hand at my day job.** Three months ago I started a markdown file with English translations of the controls just so I could grep them. The actual scoring engine and the Claude Code plugin came later.

2. **I'm relocating to Canada in February 2027** and senior SRE recruiters look at public repos for "this person ships under their own name." This is a portfolio piece. I'm not hiding that. The Medium piece is the technical-blog companion to the LinkedIn line that says "shipped K-ISMS-P scoring as OSS."

If you read both reasons and your reaction is "fair," we're going to get along.

## How to try it

```bash
# Install Claude Code if you don't have it: https://claude.ai/code
claude plugin install placen-org/teammate
cd /path/to/your-team-repo
teammate init
teammate score
teammate ask "what's our current compliance posture?"

# To federate across the team (the Teamspace alternative):
teammate sync init git@github.com:your-org/team-vault.git
teammate sync push   # after every score run
teammate sync pull   # before every score run
```

If anything breaks, open an issue. If the K-ISMS-P translation reads weirdly to a Korean compliance practitioner, please open a PR — I'd rather have one accurate control than five rough ones.

The repo is at **github.com/placen-org/teammate**. MIT licensed. The K-ISMS-P catalog is a derivative work; provenance disclosed in `SOURCES.md`.

---

*If you're a Canadian engineering manager hiring senior SREs and you read this far, I'm at jiung.gu@placen.co.kr — happy to talk about regulated-industry production work.*
