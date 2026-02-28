"""teammate — battle buddy for new SREs joining regulated teams.

Pluggable Obsidian-format vault as the nucleus, with local LLM (Ollama-backed
RAG) on top. Compliance scoring, advisory watching, and signed attestation
are pluggable scanners that all write to the vault. Production guardrail
hooks block dangerous git/infrastructure actions on day 1.

This package is OSS for any team to adopt — config-only changes, no code
edits required to deploy at any company. Enforced by oss-hygiene CI.
"""

__version__ = "0.1.0"
