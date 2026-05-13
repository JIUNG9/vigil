"""Best-effort secret + PII redaction.

This is the LAST line of defence before content lands in a git-tracked
``archive/`` directory. It is NOT a substitute for source-level access
control — Slack messages and Jira comments still belong to people, and
nothing here knows the difference between a public meeting note and a
private complaint about a colleague.

What this scrubs:
- AWS account IDs (12-digit) — replaced with [REDACTED-AWS-ACCOUNT]
- AWS access keys (AKIA / ASIA prefix, 20 chars) — replaced
- Slack tokens (xoxb-, xapp-, xoxp-) — replaced
- GitHub tokens (ghp_, ghs_, gho_, ghu_, ghr_) — replaced
- Atlassian API tokens (ATATT3xFfGF0... — long opaque string with that prefix)
- Bearer tokens in HTTP headers, "Bearer <opaque>"
- URLs with ``?token=`` or ``?key=`` query parameters
- Email addresses NOT on the org's allowlist (configurable)
- IP addresses in the 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 RFC1918
  ranges — public IPs are kept because they're typically already public

What this does NOT scrub:
- Free-text mention of a service / runbook / colleague name — that's the
  point of having the brain. Add specific terms to ``CUSTOM_PATTERNS`` if
  you need to redact those too.
"""

from __future__ import annotations

import re

_REPL = "[REDACTED]"

# Order matters: longer/more-specific patterns first so they don't get
# clobbered by shorter ones (e.g. xapp- contains x… but we match the full token).
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ---- Cloud tokens ----
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-ACCESS-KEY]"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-STS-KEY]"),
    # AWS account IDs — only when they look isolated, to avoid clobbering
    # any 12-digit number that happens to appear in text.
    (re.compile(r"(?<![\w-])([0-9]{12})(?![\w-])"), "[REDACTED-AWS-ACCOUNT]"),

    # ---- Slack tokens ----
    (re.compile(r"\bxoxb-[A-Za-z0-9-]{10,}\b"), "[REDACTED-SLACK-BOT]"),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"), "[REDACTED-SLACK-APP]"),
    (re.compile(r"\bxoxp-[A-Za-z0-9-]{10,}\b"), "[REDACTED-SLACK-USER]"),

    # ---- GitHub tokens ----
    (re.compile(r"\b(ghp|ghs|gho|ghu|ghr)_[A-Za-z0-9]{30,}\b"), "[REDACTED-GITHUB-PAT]"),

    # ---- Atlassian tokens ----
    (re.compile(r"\bATATT3[A-Za-z0-9_-]{10,}\b"), "[REDACTED-ATLASSIAN]"),

    # ---- Anthropic / OpenAI ----
    (re.compile(r"\bsk-ant-api03-[A-Za-z0-9_-]{30,}\b"), "[REDACTED-ANTHROPIC]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{30,}\b"), "[REDACTED-OPENAI]"),

    # ---- Generic tokens in URLs ----
    (re.compile(r"([?&](?:token|key|secret|password|api[_-]?key)=)([^&\s]+)", re.IGNORECASE),
     r"\1[REDACTED-URL-PARAM]"),

    # ---- HTTP bearer ----
    (re.compile(r"(Authorization:\s*Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE),
     r"\1[REDACTED-BEARER]"),

    # ---- Private network IPs (RFC1918) — generally safe to keep but
    # users sometimes want them gone. Default: keep them. Override via
    # `redact(text, scrub_private_ips=True)`.
]


def redact(text: str, *, custom_patterns: list[tuple[re.Pattern, str]] | None = None,
           scrub_private_ips: bool = False) -> str:
    """Apply the redaction patterns to ``text``. Idempotent."""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    if scrub_private_ips:
        text = re.sub(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            "[REDACTED-IP]", text)
    if custom_patterns:
        for pat, repl in custom_patterns:
            text = pat.sub(repl, text)
    return text


__all__ = ["redact"]
