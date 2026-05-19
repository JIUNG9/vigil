"""Routine 1 — weekly brain-health digest.

Calls ``vigil validate --json`` and ``vigil doctor --json`` as
subprocesses so the digest reflects the same code paths a CI run would
exercise. Adds a one-week ``git log`` summary plus file-count and
oversize-CLAUDE.md warnings.

The output is a markdown file with a clearly delimited "POST TO SLACK"
chunk. The runner extracts that chunk and posts it to Slack; the rest
of the file stays on disk as the audit-trail copy.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date as _date
from pathlib import Path

from vigil.agent.base import FAIL, OK, WARN, RoutineConfig, RoutineResult

# Subprocess form: invoke vigil via ``python -m vigil.cli`` so we
# don't depend on the ``vigil`` script being on PATH inside whatever
# runner ends up calling us. Tests patch this list to avoid the real
# subprocess.
_TEAMMATE_CMD: tuple[str, ...] = (sys.executable, "-m", "vigil.cli")

_DIGEST_DELIMITER = "<!-- POST TO SLACK START -->"
_DIGEST_DELIMITER_END = "<!-- POST TO SLACK END -->"


def _run_subcommand(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    """Run ``vigil <args...>`` and return ``(rc, stdout, stderr)``.

    Never raises on non-zero exit — validate exits 1 on FAIL and 2 on
    WARN by design. We surface the rc to the caller so the digest can
    still report on a brain that has issues.
    """
    try:
        result = subprocess.run(
            list(_TEAMMATE_CMD) + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"subprocess error: {exc}"
    return result.returncode, result.stdout, result.stderr


def _git_log_oneline(brain_root: Path, since: str = "1 week ago") -> list[str]:
    """Return ``git log`` lines for the past week. Empty list if no git."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since}", "--oneline"],
            cwd=str(brain_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _count_markdown(brain_root: Path) -> int:
    """How many markdown files live in the brain right now."""
    try:
        return sum(1 for p in brain_root.rglob("*.md") if p.is_file())
    except OSError:
        return 0


def _claude_md_size_kb(brain_root: Path) -> float | None:
    p = brain_root / "CLAUDE.md"
    if not p.is_file():
        return None
    try:
        return p.stat().st_size / 1024.0
    except OSError:
        return None


# ---------- routine ----------


def run(
    config: RoutineConfig,
    *,
    today: _date | None = None,
    max_claude_md_kb: int = 4,
) -> RoutineResult:
    started = time.perf_counter()
    today = today or _date.today()
    config.out_dir.mkdir(parents=True, exist_ok=True)

    # 1. validate
    v_rc, v_stdout, v_stderr = _run_subcommand(
        ["validate", "--json"], cwd=config.brain_root
    )
    validate_payload: dict | None = None
    if v_stdout.strip():
        try:
            validate_payload = json.loads(v_stdout)
        except json.JSONDecodeError:
            validate_payload = None

    # 2. doctor
    d_rc, d_stdout, d_stderr = _run_subcommand(
        ["doctor", "--json"], cwd=config.brain_root
    )
    doctor_payload: dict | None = None
    if d_stdout.strip():
        try:
            doctor_payload = json.loads(d_stdout)
        except json.JSONDecodeError:
            doctor_payload = None

    git_log = _git_log_oneline(config.brain_root)
    md_count = _count_markdown(config.brain_root)
    claude_size = _claude_md_size_kb(config.brain_root)
    oversize = (
        claude_size is not None and claude_size > max_claude_md_kb
    )

    # 3. roll up status
    status = OK
    summary_parts: list[str] = []
    if validate_payload:
        v_overall = validate_payload.get("overall", "PASS")
        summary_parts.append(f"validate={v_overall}")
        if v_overall == "FAIL":
            status = FAIL
        elif v_overall == "WARN" and status != FAIL:
            status = WARN
    elif v_rc != 0:
        status = WARN
        summary_parts.append(f"validate rc={v_rc}")
    if doctor_payload:
        d_counts = _doctor_status_counts(doctor_payload)
        summary_parts.append(
            f"doctor pass={d_counts['PASS']}/warn={d_counts['WARN']}/fail={d_counts['FAIL']}"
        )
        if d_counts["FAIL"] > 0:
            status = FAIL
        elif d_counts["WARN"] > 0 and status != FAIL:
            status = WARN
    elif d_rc != 0:
        if status != FAIL:
            status = WARN
        summary_parts.append(f"doctor rc={d_rc}")
    if oversize and status != FAIL:
        status = WARN
        summary_parts.append(f"CLAUDE.md {claude_size:.1f}KB > {max_claude_md_kb}KB")

    out_name = f"weekly-digest-{today.isoformat()}.md"
    out_path = config.out_dir / out_name
    out_path.write_text(
        _render_digest(
            today=today,
            brain_root=config.brain_root,
            validate_payload=validate_payload,
            validate_rc=v_rc,
            validate_stderr=v_stderr,
            doctor_payload=doctor_payload,
            doctor_rc=d_rc,
            doctor_stderr=d_stderr,
            git_log=git_log,
            md_count=md_count,
            claude_size_kb=claude_size,
            max_claude_md_kb=max_claude_md_kb,
        ),
        encoding="utf-8",
    )

    summary = "  ".join(summary_parts) or "ran"
    return RoutineResult(
        name="weekly_digest",
        status=status,
        summary=summary,
        artifacts=[out_path],
        runtime_seconds=time.perf_counter() - started,
    )


def _doctor_status_counts(payload: dict) -> dict[str, int]:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for c in payload.get("checks", []) or []:
        s = str(c.get("status", ""))
        if s in counts:
            counts[s] += 1
    return counts


# ---------- renderer ----------


def _render_digest(
    *,
    today: _date,
    brain_root: Path,
    validate_payload: dict | None,
    validate_rc: int,
    validate_stderr: str,
    doctor_payload: dict | None,
    doctor_rc: int,
    doctor_stderr: str,
    git_log: list[str],
    md_count: int,
    claude_size_kb: float | None,
    max_claude_md_kb: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# Weekly brain digest — {today.isoformat()}")
    lines.append("")
    lines.append(f"- Brain root: `{brain_root}`")
    lines.append(f"- Markdown files: {md_count}")
    if claude_size_kb is not None:
        flag = (
            " (over budget)" if claude_size_kb > max_claude_md_kb else ""
        )
        lines.append(f"- CLAUDE.md: {claude_size_kb:.1f} KB / budget {max_claude_md_kb} KB{flag}")
    else:
        lines.append("- CLAUDE.md: (missing)")
    lines.append("")

    # The Slack-ready chunk lives between delimiters so the runner can
    # extract it without re-parsing the whole markdown.
    lines.append(_DIGEST_DELIMITER)
    lines.append("")
    lines.append(_render_slack_chunk(
        today=today,
        validate_payload=validate_payload,
        doctor_payload=doctor_payload,
        md_count=md_count,
        git_log=git_log,
    ))
    lines.append("")
    lines.append(_DIGEST_DELIMITER_END)
    lines.append("")

    lines.append("## validate")
    lines.append("")
    if validate_payload:
        lines.append(f"Overall: **{validate_payload.get('overall', '?')}**")
        lines.append("")
        for c in validate_payload.get("checks", []) or []:
            lines.append(f"- `[{c.get('status')}]` `{c.get('name')}` — {c.get('summary')}")
    else:
        lines.append(f"Could not parse validate output (rc={validate_rc}).")
        if validate_stderr.strip():
            lines.append("")
            lines.append("```")
            lines.append(validate_stderr.strip())
            lines.append("```")
    lines.append("")

    lines.append("## doctor")
    lines.append("")
    if doctor_payload:
        counts = _doctor_status_counts(doctor_payload)
        lines.append(
            f"PASS={counts['PASS']}  WARN={counts['WARN']}  FAIL={counts['FAIL']}"
        )
        lines.append("")
        for c in doctor_payload.get("checks", []) or []:
            lines.append(f"- `[{c.get('status')}]` `{c.get('name')}` — {c.get('summary')}")
    else:
        lines.append(f"Could not parse doctor output (rc={doctor_rc}).")
        if doctor_stderr.strip():
            lines.append("")
            lines.append("```")
            lines.append(doctor_stderr.strip())
            lines.append("```")
    lines.append("")

    lines.append("## Brain commits in the past week")
    lines.append("")
    if git_log:
        for entry in git_log:
            lines.append(f"- `{entry}`")
    else:
        lines.append("_No git history (or empty for the past week)._")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_slack_chunk(
    *,
    today: _date,
    validate_payload: dict | None,
    doctor_payload: dict | None,
    md_count: int,
    git_log: list[str],
) -> str:
    """The chunk the runner will lift verbatim into Slack."""
    parts: list[str] = []
    parts.append(f"*Team brain — weekly digest ({today.isoformat()})*")
    parts.append("")
    if validate_payload:
        parts.append(
            f"validate: *{validate_payload.get('overall', '?')}* — "
            f"{len(validate_payload.get('checks', []) or [])} checks"
        )
    else:
        parts.append("validate: could not run")
    if doctor_payload:
        counts = _doctor_status_counts(doctor_payload)
        parts.append(
            f"doctor: PASS={counts['PASS']}  WARN={counts['WARN']}  FAIL={counts['FAIL']}"
        )
    else:
        parts.append("doctor: could not run")
    parts.append(f"markdown files: {md_count}")
    parts.append(f"commits this week: {len(git_log)}")
    return "\n".join(parts)


def extract_slack_chunk(digest_text: str) -> str | None:
    """Pull the Slack-ready section out of a rendered digest. None if missing."""
    start = digest_text.find(_DIGEST_DELIMITER)
    end = digest_text.find(_DIGEST_DELIMITER_END)
    if start == -1 or end == -1 or end <= start:
        return None
    body = digest_text[start + len(_DIGEST_DELIMITER) : end]
    return body.strip()


__all__ = ["extract_slack_chunk", "run"]
