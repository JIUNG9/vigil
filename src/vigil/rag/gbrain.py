"""gbrain compatibility layer.

If ``gbrain`` binary is on PATH, ``vigil init`` offers to register the
team's vault as a gbrain source. The two systems coexist:

  - **gbrain** handles cross-machine sync, MCP serving, embedding budget
    accounting, and trust policy across many sources (org-wide).
  - **vigil's mini-RAG** handles the local query path even if gbrain
    isn't installed.

This module is intentionally small: detection + a single registration call.
gbrain has its own setup wizard for the heavy lifting.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple


class GBrainStatus(NamedTuple):
    available: bool
    binary_path: str | None
    version: str | None
    notes: str


def detect() -> GBrainStatus:
    """Return whether gbrain is installed on this machine."""
    binary = shutil.which("gbrain")
    if not binary:
        return GBrainStatus(
            available=False,
            binary_path=None,
            version=None,
            notes="No `gbrain` binary on PATH. vigil's built-in mini-RAG will run instead.",
        )
    try:
        out = subprocess.check_output(
            [binary, "--version"],
            stderr=subprocess.STDOUT,
            timeout=5,
        ).decode().strip()
    except (subprocess.SubprocessError, OSError) as exc:
        return GBrainStatus(
            available=True,
            binary_path=binary,
            version=None,
            notes=f"`gbrain` found but `--version` failed: {exc}. Treating as available anyway.",
        )
    return GBrainStatus(
        available=True,
        binary_path=binary,
        version=out,
        notes=f"gbrain detected ({out}). vigil will register the vault as a source on init.",
    )


def register_vault(vault_path: Path, dry_run: bool = False) -> tuple[bool, str]:
    """Register ``vault_path`` as a gbrain source.

    The exact subcommand depends on gbrain's installed version. v1+ uses
    ``gbrain source add <path> --kind markdown``. We try that first and
    fall back to a no-op + explanatory message if the subcommand is absent.

    Returns (success, message).
    """
    binary = shutil.which("gbrain")
    if not binary:
        return False, "gbrain not installed; nothing to register."
    cmd = [binary, "source", "add", str(vault_path), "--kind", "markdown"]
    if dry_run:
        return True, f"Would run: {' '.join(cmd)}"
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=15)
        return True, f"Registered {vault_path} as a gbrain source."
    except subprocess.CalledProcessError as exc:
        msg = exc.output.decode("utf-8", errors="ignore") if exc.output else ""
        if "no such command" in msg.lower() or "unknown command" in msg.lower():
            return (
                False,
                "gbrain version on this machine doesn't expose `source add`. "
                "Either upgrade gbrain or run gbrain's own setup wizard separately.",
            )
        return False, f"gbrain registration failed: {msg.strip() or exc}"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"gbrain registration error: {exc}"


__all__ = ["GBrainStatus", "detect", "register_vault"]
