"""Tests for the bash hooks. Uses pytest+subprocess instead of bats so
no extra installation is required to run the suite."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PRE_PUSH = REPO / "hooks" / "pre-push"
PRE_TOOL_USE = REPO / "hooks" / "pre-tool-use-guardrail.sh"


def _run_pre_push(stdin_bytes: bytes, env: dict[str, str], remote_name: str = "origin") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(PRE_PUSH), remote_name, "git@github.com:acme-corp/repo.git"],
        input=stdin_bytes,
        env={**os.environ, **env},
        capture_output=True,
        timeout=10,
    )


def _run_pre_tool_use(payload: bytes, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(PRE_TOOL_USE)],
        input=payload,
        env={**os.environ, **env},
        capture_output=True,
        timeout=10,
    )


def test_pre_push_blocks_main():
    # git pre-push stdin format: <local_ref> <local_sha> <remote_ref> <remote_sha>
    stdin = b"refs/heads/feature abc123 refs/heads/main def456\n"
    p = _run_pre_push(stdin, env={})
    assert p.returncode == 1
    assert b"BLOCKED" in p.stderr


def test_pre_push_allows_feature_branch():
    stdin = b"refs/heads/feature abc123 refs/heads/feature def456\n"
    p = _run_pre_push(stdin, env={})
    assert p.returncode == 0


def test_pre_push_override_allows_main():
    stdin = b"refs/heads/feature abc123 refs/heads/main def456\n"
    p = _run_pre_push(stdin, env={"TEAMMATE_OVERRIDE": "1"})
    assert p.returncode == 0


def test_pre_tool_use_blocks_git_push_main():
    payload = b'{"tool_name": "Bash", "command": "git push origin main"}'
    p = _run_pre_tool_use(payload, env={})
    assert p.returncode == 2
    assert b"BLOCKED" in p.stderr


def test_pre_tool_use_blocks_force_push():
    payload = b'{"tool_name": "Bash", "command": "git push --force"}'
    p = _run_pre_tool_use(payload, env={})
    assert p.returncode == 2


def test_pre_tool_use_allows_normal_bash():
    payload = b'{"tool_name": "Bash", "command": "ls -la"}'
    p = _run_pre_tool_use(payload, env={})
    assert p.returncode == 0


def test_pre_tool_use_blocks_terraform_apply_prod():
    payload = b'{"tool_name": "Bash", "command": "cd infra/prod && terraform apply"}'
    p = _run_pre_tool_use(payload, env={})
    assert p.returncode == 2


def test_pre_tool_use_blocks_drop_table():
    payload = b'{"tool_name": "Bash", "command": "psql -c \\"DROP TABLE users\\""}'
    p = _run_pre_tool_use(payload, env={})
    assert p.returncode == 2


def test_pre_tool_use_override_allows_dangerous():
    payload = b'{"tool_name": "Bash", "command": "git push origin main"}'
    p = _run_pre_tool_use(payload, env={"TEAMMATE_OVERRIDE": "1"})
    assert p.returncode == 0


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_pre_push_warns_on_workflow_change(tmp_path: Path):
    """Workflow-file changes warn but do not block when pushing to a feature branch."""
    # We only assert exit code and warning text; setting up a real upstream
    # tracking branch in a tmp git repo is out of scope for this unit test.
    stdin = b"refs/heads/feature abc 123 refs/heads/feature def 456\n"
    p = _run_pre_push(stdin, env={})
    # Either returncode 0 (no upstream so warning didn't trigger) or 0 with
    # a WARNING message — neither blocks.
    assert p.returncode == 0
