"""Per-engineer adapter — translate personal layout into the team's canonical layout.

Why this exists
---------------

Every engineer's laptop is a one-of-a-kind bird's nest. Some keep notes in
``~/notes/runbooks/``; some have a ``~/wiki/`` they've been growing for years.
The team's brain repo, on the other hand, has a fixed canonical shape:
``docs/runbooks/``, ``docs/wiki/``, etc. Without a translation layer the
engineer has to either (a) restructure their personal notes to match the team
or (b) accept that ``vigil ask`` doesn't see their local context.

Neither is acceptable. The adapter is the seam.

MVP scope (v0.6)
----------------

Two responsibilities:

  1. **Path translation.** Map personal globs to canonical brain paths so
     non-canonical locations can still be picked up by the indexer / hooks.
  2. **CLAUDE.md section precedence.** When both team and personal CLAUDE.md
     exist, team wins by default. The user can list H2 section headers under
     ``[claude_md]`` ``personal_overrides_team`` to override specific sections
     (rare; usually personal editor preferences, not team rules).

What's deferred to v0.7
-----------------------

Skill-collision namespacing, vocabulary aliases, reverse path translation,
auto-detection of personal layouts. The advisor was emphatic: the full
adapter design without 2-3 real adopters' patterns is a strawman. We ship
the load-bearing 20% now, expand once the data tells us what to expand.

Schema
------

::

    # ~/.vigil-adapter.toml  OR  <brain-root>/.vigil-adapter.toml
    [paths]
    "~/notes/runbooks/*.md" = "docs/runbooks/{}"
    "~/wiki/*.md"           = "docs/wiki/{}"

    [claude_md]
    personal_overrides_team = ["Personal preferences", "My editor config"]

Each ``[paths]`` key is a glob with a single ``*``. The ``{}`` in the value
is filled with whatever the ``*`` matched. Multi-``*`` patterns are not
supported in the MVP.

Precedence
----------

If both ``<brain-root>/.vigil-adapter.toml`` and ``~/.vigil-adapter.toml``
exist, the **home file wins** — that's the per-engineer override of the
team-shipped fallback. Per the spec: "adapter is per-engineer."
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Filename used for both the per-engineer (home) and per-brain (root) variants.
ADAPTER_FILENAME = ".vigil-adapter.toml"


@dataclass(frozen=True)
class Adapter:
    """Effective adapter rules after precedence resolution.

    Attributes:
        paths: ordered mapping of personal-glob -> canonical-template. Order
               is the order the rules were declared in the file (Python 3.7+
               dicts preserve insertion order); first match wins.
        personal_override_sections: H2 header names (without leading ``##``)
               where a personal CLAUDE.md gets to override the team's
               same-named section.
        source: ``"home" | "brain" | "merged"`` — diagnostic only.
    """

    paths: dict[str, str] = field(default_factory=dict)
    personal_override_sections: list[str] = field(default_factory=list)
    source: str = "none"

    def translate_path(self, personal_path: Path) -> Path | None:
        """Map a personal path to a canonical brain-relative path.

        Returns ``None`` if no rule matches. The returned path is always
        relative — the caller decides what to root it against.
        """
        # Normalise ``~`` so the user can write ``"~/notes/*.md"`` and the
        # match still works against the absolute path on disk.
        candidate = str(personal_path)
        candidate_expanded = os.path.expanduser(candidate)
        for glob, template in self.paths.items():
            glob_expanded = os.path.expanduser(glob)
            matched = _match_glob(glob_expanded, candidate_expanded)
            if matched is None:
                continue
            return Path(template.replace("{}", matched))
        return None

    def merge_claude_md(self, team_md: str, personal_md: str) -> str:
        """Merge personal CLAUDE.md into team's, honouring section precedence.

        The base is the team's CLAUDE.md verbatim. For each H2 section in the
        personal file whose header is listed in ``personal_override_sections``:

          - If the team has the same H2 section, replace it.
          - If the team does not, append the personal section to the end.

        Personal sections NOT in the list are dropped — the team brain owns
        the canonical content. This matches the documented "team wins by
        default" rule.
        """
        if not personal_md.strip():
            return team_md
        personal_sections = _split_h2(personal_md)
        team_sections, team_order, team_preamble = _parse_with_preamble(team_md)
        for header, body in personal_sections.items():
            if header not in self.personal_override_sections:
                continue
            if header in team_sections:
                team_sections[header] = body
            else:
                team_sections[header] = body
                team_order.append(header)
        # Reassemble in the original order, with new sections appended.
        rebuilt: list[str] = []
        if team_preamble:
            rebuilt.append(team_preamble.rstrip("\n"))
        for header in team_order:
            rebuilt.append(f"## {header}")
            body = team_sections[header].strip("\n")
            if body:
                rebuilt.append(body)
        return "\n\n".join(rebuilt).rstrip() + "\n"


# ---------- glob matching ----------


def _match_glob(glob: str, candidate: str) -> str | None:
    """Match ``candidate`` against ``glob`` and return the substitution.

    The substitution semantics follow the spec example::

        "~/notes/runbooks/*.md" = "docs/runbooks/{}"
                            ^                     ^
                            └────── captured ─────┘

    The captured value is the basename — what ``*`` matched, **plus** the
    suffix that followed it in the glob (e.g. ``auth.md``). This way the
    user doesn't have to repeat the extension in the template.

    ``*`` matches a single path segment — i.e. one or more characters with
    NO path separator (``/``). This prevents ``~/notes/*.md`` from
    accidentally rewriting ``~/notes/subdir/file.md`` and leaking
    ``subdir/`` into the team-side path. Recursive globbing (``**``) is
    deferred to v0.7 alongside the rest of the adapter expansion.

    Supports a single ``*`` per glob. Multi-``*`` rules return None.
    """
    if glob.count("*") != 1:
        return None
    star_pos = glob.index("*")
    prefix = re.escape(glob[:star_pos])
    suffix = re.escape(glob[star_pos + 1 :])
    # Non-greedy single-segment match: at least one non-separator char.
    pattern = f"^{prefix}([^/]+){suffix}$"
    m = re.match(pattern, candidate)
    if m is None:
        return None
    star_match = m.group(1)
    # Re-attach the literal suffix so the template can use ``{}`` for the
    # whole basename, not just the stem.
    return star_match + glob[star_pos + 1 :]


# ---------- CLAUDE.md section parsing ----------

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_h2(text: str) -> dict[str, str]:
    """Return ``{header: body}`` for every H2 section, preamble dropped."""
    sections, _, _ = _parse_with_preamble(text)
    return sections


def _parse_with_preamble(text: str) -> tuple[dict[str, str], list[str], str]:
    """Return ``(sections_by_header, header_order, preamble_text)``.

    ``preamble_text`` is everything before the first H2.
    """
    matches = list(_H2_RE.finditer(text))
    if not matches:
        return {}, [], text
    sections: dict[str, str] = {}
    order: list[str] = []
    preamble = text[: matches[0].start()]
    for i, m in enumerate(matches):
        header = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].lstrip("\n")
        sections[header] = body
        order.append(header)
    return sections, order, preamble


# ---------- TOML loading ----------


def _read_toml(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _adapter_from_data(data: dict, source: str) -> Adapter:
    paths_section = data.get("paths") or {}
    if not isinstance(paths_section, dict):
        paths_section = {}
    paths: dict[str, str] = {}
    for k, v in paths_section.items():
        if isinstance(k, str) and isinstance(v, str):
            paths[k] = v
    claude_section = data.get("claude_md") or {}
    overrides_raw = (
        claude_section.get("personal_overrides_team", [])
        if isinstance(claude_section, dict)
        else []
    )
    overrides: list[str] = [
        str(s) for s in overrides_raw if isinstance(s, str)
    ]
    return Adapter(
        paths=paths,
        personal_override_sections=overrides,
        source=source,
    )


def load_adapter(brain_root: Path) -> Adapter | None:
    """Resolve the effective adapter, or ``None`` if neither file exists.

    Precedence: ``~/.vigil-adapter.toml`` (per-engineer) wins over
    ``<brain_root>/.vigil-adapter.toml`` (team-shipped fallback). When
    both exist, the home file's keys override the brain file's keys
    section by section — paths from home + brain are unioned with home
    winning on collision; ``personal_overrides_team`` from home replaces
    the brain's list outright.
    """
    home_path = Path.home() / ADAPTER_FILENAME
    brain_path = brain_root / ADAPTER_FILENAME
    home_data = _read_toml(home_path)
    brain_data = _read_toml(brain_path)
    if home_data is None and brain_data is None:
        return None
    if home_data is None:
        return _adapter_from_data(brain_data or {}, source="brain")
    if brain_data is None:
        return _adapter_from_data(home_data, source="home")
    # Merge: brain is base, home overrides.
    base = _adapter_from_data(brain_data, source="brain")
    over = _adapter_from_data(home_data, source="home")
    merged_paths = dict(base.paths)
    merged_paths.update(over.paths)
    # If the home file has any ``[claude_md]`` table at all, its list wins
    # outright — even when explicitly empty. Only when home has no
    # ``[claude_md]`` section do we fall back to the brain's list.
    home_has_claude_md = isinstance(home_data.get("claude_md"), dict)
    overrides = (
        over.personal_override_sections
        if home_has_claude_md
        else base.personal_override_sections
    )
    return Adapter(
        paths=merged_paths,
        personal_override_sections=overrides,
        source="merged",
    )


# ---------- CLI helpers ----------


_STARTER_ADAPTER = """\
# vigil adapter — per-engineer translation between your personal layout
# and the team brain's canonical layout. See docs/ADAPTER.md.
#
# This file lives at one of:
#   ~/.vigil-adapter.toml         (per-engineer, wins on conflict)
#   <brain-root>/.vigil-adapter.toml  (team-shipped fallback)
#
# Path translation: map your personal globs to canonical brain paths.
# Each key has exactly ONE ``*``; the value uses ``{}`` as the
# substitution placeholder.
[paths]
# "~/notes/runbooks/*.md" = "docs/runbooks/{}"
# "~/wiki/*.md"           = "docs/wiki/{}"

# CLAUDE.md merge: when both team and personal CLAUDE.md exist, team wins
# by default. List H2 section headers below to let your personal version
# override the team's section of the same name. Sections not listed here
# are dropped — the team brain owns the canonical content.
[claude_md]
personal_overrides_team = []
"""


def _detect_non_canonical_dirs(home: Path) -> list[str]:
    """Look for directories under ``~`` that smell like personal-notes layouts.

    Best-effort. We don't auto-write rules for them — the user does.
    """
    candidates: list[str] = []
    for name in ("notes", "wiki", "runbooks", "personal-brain"):
        p = home / name
        if p.is_dir():
            candidates.append(name)
    return candidates


def starter_adapter_text(home: Path | None = None) -> str:
    """Return the body of a starter adapter file. Detects common dirs.

    The body always starts from the canonical template; if any of the
    well-known directory names are found under ``home``, we surface them
    as commented examples the user can uncomment.
    """
    home = home or Path.home()
    suggestions = _detect_non_canonical_dirs(home)
    if not suggestions:
        return _STARTER_ADAPTER
    extra: list[str] = ["", "# Detected on this laptop — uncomment to enable:"]
    for name in suggestions:
        extra.append(f'# "~/{name}/*.md" = "docs/{name}/{{}}"')
    extra.append("")
    return _STARTER_ADAPTER.replace(
        "[paths]\n",
        "[paths]\n" + "\n".join(extra) + "\n",
    )


def write_starter_adapter(target: Path, *, home: Path | None = None) -> Path:
    """Write a starter adapter file to ``target``. Caller chose the location."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(starter_adapter_text(home=home), encoding="utf-8")
    return target


def validate_adapter(adapter: Adapter, *, home: Path | None = None) -> list[str]:
    """Return a list of warning strings about dangling rules.

    A warning is emitted when a rule's source glob matches no real files
    in either ``home`` (for ``~``-anchored globs) or any reachable absolute
    path. Returns an empty list when everything matches.
    """
    home = home or Path.home()
    warnings: list[str] = []
    import glob as _glob

    for personal_glob in adapter.paths:
        expanded = os.path.expanduser(personal_glob)
        if "*" not in expanded:
            warnings.append(f"rule has no glob (`*`): {personal_glob!r}")
            continue
        matches = _glob.glob(expanded, recursive=False)
        if not matches:
            warnings.append(
                f"rule matches no files: {personal_glob!r} → no candidates on disk"
            )
    return warnings


__all__ = [
    "ADAPTER_FILENAME",
    "Adapter",
    "load_adapter",
    "starter_adapter_text",
    "validate_adapter",
    "write_starter_adapter",
]
