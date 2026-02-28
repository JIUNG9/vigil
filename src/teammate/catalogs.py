"""Compliance catalog loader + Probe-Control mapping.

Reads YAML catalogs (currently ISO 27001 Annex A and K-ISMS-P) from
``catalogs/``. Exposes a uniform Control type and the mapping that probes
use to declare which controls they exercise.

The probe -> controls mapping is hardcoded in ``PROBE_CONTROL_MAP`` rather
than embedded in catalog YAML. Reason: the same probe can satisfy multiple
controls across multiple frameworks, and the mapping is the author's
professional judgment, not normative catalog data. Keep them separate so
catalog updates don't churn probe code and vice versa.

ASCII-art summary of the data flow:

    catalogs/*.yaml                   src/teammate/catalogs.py
    -----------------                 ---------------------------
    iso-27001-annex-a.yaml ─────┐
                                ├─►   load_all_catalogs()
    k-isms-p.yaml ──────────────┘             │
                                              ▼
                                       Catalog (frozen)
                                              │
                                              ▼
                              PROBE_CONTROL_MAP (probe -> [Control...])
                                              │
                                              ▼
                                  consumed by score.py + vault.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

# ---------- public types ----------


@dataclass(frozen=True, slots=True)
class Control:
    """One catalog control. Framework-agnostic shape."""

    framework: str
    id: str
    title: str
    summary: str
    severity: str  # one of: low, medium, high, critical
    # Cross-references to other frameworks. K-ISMS-P controls list ISO refs;
    # ISO controls don't (yet) list K-ISMS-P refs. The mapping flows one way.
    iso_27001: tuple[str, ...] = field(default_factory=tuple)
    # Theme/domain tag from the catalog (e.g. "Organizational" for ISO,
    # "Protection Measures" for K-ISMS-P). Used for grouping in the score
    # output table and the vault evidence directory layout.
    domain: str = ""


@dataclass(frozen=True, slots=True)
class Catalog:
    """Loaded catalog with metadata + controls dict keyed by control ID."""

    framework: str
    version: str
    updated: str
    source: str
    license_note: str
    controls: dict[str, Control]


# ---------- loader ----------


def find_catalogs_dir(start: Path | None = None) -> Path:
    """Find the ``catalogs/`` directory by walking up from start.

    Resolution order:
    1. ``$TEAMMATE_CATALOGS_DIR`` env var if set.
    2. Walk up from ``start`` (default: current working directory) looking
       for a directory named ``catalogs/`` that contains at least one YAML.
    3. Fall back to the package-shipped catalogs (sibling of ``src/teammate``).

    This lets a team override catalogs locally (e.g. add internal controls)
    without forking the package.
    """
    import os

    env_override = os.environ.get("TEAMMATE_CATALOGS_DIR")
    if env_override:
        p = Path(env_override).resolve()
        if p.is_dir():
            return p
        raise FileNotFoundError(f"TEAMMATE_CATALOGS_DIR={env_override} does not exist")

    cwd = Path(start or Path.cwd()).resolve()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "catalogs"
        if candidate.is_dir() and any(candidate.glob("*.yaml")):
            return candidate

    # Package-shipped fallback: <repo-root>/catalogs/ relative to this file.
    pkg_root = Path(__file__).resolve().parent.parent.parent
    fallback = pkg_root / "catalogs"
    if fallback.is_dir():
        return fallback

    raise FileNotFoundError(
        "No catalogs/ directory found. Set TEAMMATE_CATALOGS_DIR or run "
        "teammate from a repository checkout that includes catalogs/."
    )


def load_catalog(path: Path) -> Catalog:
    """Load a single catalog YAML into the Catalog dataclass."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected top-level mapping, got {type(raw).__name__}")
    required = {"framework", "version", "updated", "source", "license_note", "controls"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"{path}: missing keys {sorted(missing)}")

    controls: dict[str, Control] = {}
    for entry in raw["controls"]:
        if "id" not in entry or "title" not in entry or "summary" not in entry:
            raise ValueError(f"{path}: control missing required field: {entry}")
        cid = str(entry["id"])
        if cid in controls:
            raise ValueError(f"{path}: duplicate control id {cid!r}")
        iso_refs_raw = entry.get("iso_27001", [])
        iso_refs = tuple(str(x) for x in iso_refs_raw)
        domain = entry.get("domain") or entry.get("theme") or ""
        controls[cid] = Control(
            framework=raw["framework"],
            id=cid,
            title=entry["title"].strip(),
            summary=entry["summary"].strip(),
            severity=entry.get("severity", "medium"),
            iso_27001=iso_refs,
            domain=domain,
        )

    return Catalog(
        framework=raw["framework"],
        version=raw["version"],
        updated=raw["updated"],
        source=raw["source"],
        license_note=raw["license_note"],
        controls=controls,
    )


def load_all_catalogs(catalogs_dir: Path | None = None) -> dict[str, Catalog]:
    """Load every catalog YAML in ``catalogs_dir``, keyed by framework name."""
    d = catalogs_dir or find_catalogs_dir()
    found: dict[str, Catalog] = {}
    for yaml_path in sorted(d.glob("*.yaml")):
        cat = load_catalog(yaml_path)
        if cat.framework in found:
            raise ValueError(
                f"Duplicate framework {cat.framework!r}: "
                f"{found[cat.framework]} and {yaml_path}"
            )
        found[cat.framework] = cat
    return found


# ---------- probe -> control mapping ----------

# Each probe id maps to a list of (framework, control_id) tuples. A probe can
# satisfy multiple controls across frameworks. See the design doc's
# "Control -> Probe Mapping" section for the rationale behind each pairing.
PROBE_CONTROL_MAP: dict[str, list[tuple[str, str]]] = {
    "codeowners-exists": [
        ("iso-27001", "A.5.2"),
        ("k-isms-p", "2.1.3"),
    ],
    "branch-protection": [
        ("iso-27001", "A.8.32"),
        ("iso-27001", "A.5.31"),
        ("k-isms-p", "2.6.1"),
    ],
    "secrets-scan": [
        ("iso-27001", "A.8.24"),
        ("iso-27001", "A.5.10"),
        ("k-isms-p", "2.5.1"),
    ],
    "tf-state-encryption": [
        ("iso-27001", "A.8.24"),
        ("k-isms-p", "2.7.4"),
    ],
    "dependency-pinning": [
        ("iso-27001", "A.8.30"),
        ("k-isms-p", "2.10.2"),
    ],
    "oss-hygiene-workflow": [
        ("iso-27001", "A.5.36"),
        ("k-isms-p", "2.11.5"),
    ],
    "pre-commit-config": [
        ("iso-27001", "A.8.25"),
        ("k-isms-p", "2.10.5"),
    ],
    "license-present": [
        ("iso-27001", "A.5.32"),
        ("k-isms-p", "2.11.4"),
    ],
    "security-md-present": [
        ("iso-27001", "A.5.34"),
        ("k-isms-p", "2.13.1"),
    ],
    "dependabot-or-renovate": [
        ("iso-27001", "A.8.30"),
        ("iso-27001", "A.8.8"),
        ("k-isms-p", "2.10.3"),
    ],
}


def controls_for_probe(
    probe_id: str, catalogs: dict[str, Catalog]
) -> list[Control]:
    """Resolve a probe id to its referenced Control objects."""
    out: list[Control] = []
    for framework, control_id in PROBE_CONTROL_MAP.get(probe_id, []):
        cat = catalogs.get(framework)
        if cat is None:
            continue  # framework not loaded; skip silently
        ctrl = cat.controls.get(control_id)
        if ctrl is None:
            # Catalog drift: probe maps to a control id not present in the
            # loaded catalog. Surface this loudly during dev; in prod this
            # would be a configuration bug.
            raise KeyError(
                f"probe {probe_id!r} maps to {framework}:{control_id!r} but "
                f"that control is not in the loaded catalog (catalog version "
                f"{cat.version}). Update PROBE_CONTROL_MAP or the catalog."
            )
        out.append(ctrl)
    return out


def all_referenced_controls(
    catalogs: dict[str, Catalog],
) -> set[tuple[str, str]]:
    """Set of (framework, control_id) pairs referenced by any probe."""
    seen: set[tuple[str, str]] = set()
    for refs in PROBE_CONTROL_MAP.values():
        seen.update(refs)
    return seen


def unreferenced_controls(catalogs: dict[str, Catalog]) -> list[Control]:
    """Catalogued controls that no probe currently exercises.

    These show up as ``n/a`` in score output. Useful for the v0.1.x roadmap:
    each new probe expansion can target a previously-unreferenced control.
    """
    referenced = all_referenced_controls(catalogs)
    out: list[Control] = []
    for cat in catalogs.values():
        for ctrl in cat.controls.values():
            if (cat.framework, ctrl.id) not in referenced:
                out.append(ctrl)
    return out


# ---------- convenience entry point for CLI/tests ----------


def load_default() -> dict[str, Catalog]:
    """Load whatever catalogs the running process can see, with helpful errors."""
    return load_all_catalogs()


__all__ = [
    "Catalog",
    "Control",
    "PROBE_CONTROL_MAP",
    "all_referenced_controls",
    "controls_for_probe",
    "find_catalogs_dir",
    "load_all_catalogs",
    "load_catalog",
    "load_default",
    "unreferenced_controls",
]
