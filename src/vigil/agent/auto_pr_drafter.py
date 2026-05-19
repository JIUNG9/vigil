"""Routine 11 — for HIGH-severity events, draft an auto-PR to the brain.

Triggered when a HIGH-severity invalidation is added to the
invalidations repo. For each affected brain page, the agent:

  1. Reads the current page content.
  2. Builds an LLM prompt: page + invalidation event details +
     "rewrite this page to reflect the new state, preserving all
     sections, only updating affected steps".
  3. Calls the configured ``LLMProvider``.
  4. Writes the rewritten page to ``out_dir/draft-prs/`` with frontmatter
     pointing back at the original page and the invalidation id.

The runner then opens a PR (one PR per invalidation, all affected pages
as separate commits) and tags the page owner per ``knowledge/people.md``.
The PR is **always a draft**, never auto-merged. Humans review.

Inputs (``RoutineConfig.extra``):

  invalidation       dict           — required. Same shape as
                                       ``targeted_radar`` accepts.
  affected_pages     list[str]      — repo-relative md paths to redraft.
                                       When omitted, the routine walks
                                       the brain itself looking for
                                       pages that reference the resource.
  provider           LLMProvider    — optional kwarg (test injection).
                                       When ``None``, the routine falls
                                       back to ``load_llm_provider`` against
                                       ``.vigil/config.toml``. Still
                                       ``None`` after that → return WARN
                                       with a "no LLM provider configured"
                                       summary; no draft is written.

Output: ``out_dir/draft-prs/<page-slug>-<invalidation-id>.md`` per page.

Severity gate: severities below HIGH are dropped — the routine returns
status=OK and a clear summary saying "below severity floor".

Hard rule: the draft markdown contains a ``requires_review: true``
frontmatter key. The runner refuses to mark a PR ready-for-review on
files carrying that flag — the v0.5 contract.
"""

from __future__ import annotations

import time
from datetime import date as _date
from pathlib import Path
from typing import Any

# We share the frontmatter helpers with the v0.8 sync routines so the
# draft frontmatter format is identical to other agent-staged files.
from vigil.agent._sync_common import render_frontmatter, slugify
from vigil.agent.base import OK, WARN, RoutineConfig, RoutineResult
from vigil.invalidations import extract_resource_ids
from vigil.providers import LLMProvider

# ---------- LLM call ----------


def _build_prompt(
    *,
    relpath: str,
    page_text: str,
    invalidation: dict[str, Any],
) -> str:
    """Build the rewrite prompt — deterministic, easy to diff in PR review.

    The prompt is intentionally narrow: keep all sections, only update
    affected steps, never invent infra state. The LLM result still goes
    through human review — this prompt is the first guardrail.
    """
    rid = invalidation.get("resource_id", "")
    rtype = invalidation.get("resource_type", "")
    full = f"{rtype}.{rid}" if rtype else rid
    return (
        "You are a careful technical-writing assistant. "
        "Rewrite the markdown page below to reflect the new infrastructure "
        "state described in the invalidation event.\n\n"
        "**Hard rules:**\n"
        "1. Preserve every section heading and the overall structure.\n"
        "2. Only update steps and references that are directly affected.\n"
        "3. Never invent resource ids, ARNs, account numbers, or commands "
        "that were not in the original page or the event.\n"
        "4. When a step is no longer correct and you don't know the "
        "replacement, leave a clearly-marked TODO instead of guessing.\n\n"
        f"**Page path:** `{relpath}`\n\n"
        f"**Invalidation event:**\n"
        f"- resource: `{full}`\n"
        f"- action: {invalidation.get('action', '')}\n"
        f"- severity: {invalidation.get('severity', '')}\n"
        f"- timestamp: {invalidation.get('timestamp', '')}\n"
        f"- source: {invalidation.get('source', '')}\n\n"
        "**Current page content:**\n\n"
        "```markdown\n"
        f"{page_text}\n"
        "```\n\n"
        "Output the full rewritten markdown only — no preamble, no "
        "code-fence wrapper around the answer."
    )


def _call_provider(provider: LLMProvider, prompt: str) -> str:
    """Concatenate streaming chunks into a single string."""
    chunks: list[str] = []
    for piece in provider.generate(prompt, stream=False):
        chunks.append(str(piece))
    return "".join(chunks).strip() + "\n"


# ---------- page discovery ----------


def _walk_brain_md(brain_root: Path) -> list[Path]:
    out: list[Path] = []
    for sub in ("docs", "knowledge", ".claude/skills"):
        base = brain_root / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".md", ".markdown"}:
                out.append(path)
    return out


def _discover_affected_pages(
    brain_root: Path, invalidation: dict[str, Any]
) -> list[str]:
    """Find pages that reference the invalidated resource."""
    rid = str(invalidation.get("resource_id") or "")
    rtype = str(invalidation.get("resource_type") or "")
    full = f"{rtype}.{rid}" if rtype else rid
    keys = {k for k in (rid, full) if k}
    if not keys:
        return []
    out: list[str] = []
    for path in _walk_brain_md(brain_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ids = extract_resource_ids(text)
        if any(k in ids or k in text for k in keys):
            try:
                out.append(str(path.relative_to(brain_root)))
            except ValueError:
                out.append(str(path))
    return sorted(out)


# ---------- routine ----------


def _provider_or_none(provider: LLMProvider | None) -> LLMProvider | None:
    """Resolve the provider — explicit kwarg wins, then config, then None."""
    if provider is not None:
        return provider
    try:
        from vigil.config import load_config
        from vigil.providers import load_llm_provider

        cfg = load_config()
        return load_llm_provider(cfg.llm)
    except Exception:  # noqa: BLE001 — drafting is opt-in; degrade silently
        return None


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
    provider: LLMProvider | None = None,
) -> RoutineResult:
    """Stage one rewritten draft per affected brain page."""
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)
    drafts_dir = config.out_dir / "draft-prs"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    invalidation = dict(config.extra.get("invalidation") or {})
    if not invalidation.get("resource_id"):
        return RoutineResult(
            name="auto_pr_drafter",
            status=WARN,
            summary="no invalidation supplied (extra.invalidation missing)",
            artifacts=[],
            runtime_seconds=time.perf_counter() - started,
        )

    severity_floor = str(config.extra.get("severity_floor", "high")).lower()
    sev = str(invalidation.get("severity", "")).lower()
    from vigil.impact import severity_at_least
    if not severity_at_least(sev, severity_floor):
        return RoutineResult(
            name="auto_pr_drafter",
            status=OK,
            summary=f"severity {sev or '?'} below floor ({severity_floor}); "
                    "no drafts staged",
            artifacts=[],
            runtime_seconds=time.perf_counter() - started,
        )

    pages = list(config.extra.get("affected_pages") or [])
    if not pages:
        pages = _discover_affected_pages(config.brain_root, invalidation)

    if not pages:
        return RoutineResult(
            name="auto_pr_drafter",
            status=OK,
            summary="no affected brain pages found",
            artifacts=[],
            runtime_seconds=time.perf_counter() - started,
        )

    llm = _provider_or_none(provider)
    if llm is None:
        return RoutineResult(
            name="auto_pr_drafter",
            status=WARN,
            summary="no LLM provider configured — drafts not written",
            artifacts=[],
            runtime_seconds=time.perf_counter() - started,
        )

    inv_id = str(invalidation.get("id") or "no-id")
    written: list[Path] = []
    errors: list[tuple[str, str]] = []
    for relpath in pages:
        page_path = config.brain_root / relpath
        try:
            page_text = page_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append((relpath, f"read failed: {exc}"))
            continue
        prompt = _build_prompt(
            relpath=relpath,
            page_text=page_text,
            invalidation=invalidation,
        )
        try:
            answer = _call_provider(llm, prompt)
        except Exception as exc:  # noqa: BLE001 — surface, never crash
            errors.append((relpath, f"llm error: {exc}"))
            continue
        meta: dict[str, Any] = {
            "original_path": relpath,
            "invalidation_id": inv_id,
            "severity": "high",
            "requires_review": True,
            "action": str(invalidation.get("action", "")),
            "resource": (
                f"{invalidation.get('resource_type','')}."
                f"{invalidation.get('resource_id','')}"
            ).strip("."),
            "drafted_at": today.isoformat(),
        }
        slug = slugify(relpath.replace("/", "-").replace(".md", ""), fallback="page")
        out_path = drafts_dir / f"{slug}-{slugify(inv_id, fallback='inv')}.md"
        out_path.write_text(
            render_frontmatter(meta) + "\n" + answer.rstrip() + "\n",
            encoding="utf-8",
        )
        written.append(out_path)

    status = OK if not errors else WARN
    summary = f"{len(written)} draft(s) for invalidation {inv_id}"
    if errors:
        summary += f" ({len(errors)} error(s))"
    return RoutineResult(
        name="auto_pr_drafter",
        status=status,
        summary=summary,
        artifacts=written,
        runtime_seconds=time.perf_counter() - started,
    )


__all__ = ["run"]
