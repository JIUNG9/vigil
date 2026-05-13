"""Importer base class + shared frontmatter schema.

Every imported markdown file has a frontmatter that lets the indexer and
later readers know where the content came from and when. Standard fields:

    ---
    source: github | jira | slack | confluence
    source_type: issue | pull_request | discussion | page | message | readme
    source_id: <stable id from the source>
    source_url: <https://...> for human navigation
    title: <human-readable title>
    fetched_at: 2026-05-13T07:00:00Z          # when teammate pulled it
    last_modified: 2026-05-12T14:00:00Z       # when the source last changed
    author: <username or email if available>
    labels: [tag1, tag2]                       # source-specific tags
    extra:                                     # free-form per source
      jira_status: In Progress
      jira_priority: High
    ---
    <markdown body>

Each importer subclass implements:
    - source_name (class attr): "github" / "jira" / ...
    - iterate(state, since): yields parsed items
    - render(item): returns (relative_path, frontmatter_dict, body_md)
    - watermark(item): returns the value to store for incremental sync

Importers are read-only on their source. They write to
``<brain_root>/archive/<source>/`` and bump ``<brain_root>/.teammate-sync/state.json``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from teammate.importers.redact import redact

log = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Outcome of a single importer run."""
    source: str
    written: int = 0
    skipped: int = 0
    errors: int = 0
    new_watermark: Any = None
    artifacts: list[Path] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.source}: wrote {self.written}, skipped {self.skipped}, "
            f"errors {self.errors}, watermark={self.new_watermark}"
        )


class ImporterBase(ABC):
    """Subclasses implement iterate() + render() + watermark()."""

    source_name: str = ""

    def __init__(self, brain_root: Path, *, dry_run: bool = False,
                 archive_subdir: str | None = None):
        self.brain_root = Path(brain_root)
        self.dry_run = dry_run
        sub = archive_subdir or self.source_name
        self.archive_root = self.brain_root / "archive" / sub
        self.state_path = self.brain_root / ".teammate-sync" / "state.json"

    # ----- subclass surface -----

    @abstractmethod
    def iterate(self, since: Any) -> Iterator[dict]:
        """Yield raw items from the source, optionally filtered by `since`."""

    @abstractmethod
    def render(self, item: dict) -> tuple[str, dict, str]:
        """Return (relative_path, frontmatter_dict, markdown_body).

        relative_path is relative to ``self.archive_root`` and uses forward slashes.
        Subclass must ensure the path is stable (so re-runs overwrite, not duplicate).
        """

    @abstractmethod
    def watermark(self, item: dict) -> Any:
        """Return the watermark value of ``item`` (e.g. last_modified str).

        After a run, the maximum watermark seen is persisted so the next
        run only fetches items newer than that.
        """

    # ----- run loop -----

    def run(self) -> ImportResult:
        state = self._load_state()
        since = state.get(self.source_name, {}).get("watermark")
        log.info("%s: starting from watermark=%r dry_run=%s", self.source_name, since, self.dry_run)

        result = ImportResult(source=self.source_name)
        max_watermark = since

        for item in self.iterate(since):
            try:
                rel_path, fm, body = self.render(item)
                wm = self.watermark(item)
                if wm and (max_watermark is None or str(wm) > str(max_watermark)):
                    max_watermark = wm
                if self._write(rel_path, fm, body):
                    result.written += 1
                    result.artifacts.append(self.archive_root / rel_path)
                else:
                    result.skipped += 1
            except Exception as exc:
                log.warning("%s: render/write error: %s", self.source_name, exc)
                result.errors += 1

        result.new_watermark = max_watermark
        if not self.dry_run and max_watermark is not None:
            state.setdefault(self.source_name, {})["watermark"] = str(max_watermark)
            state[self.source_name]["last_run"] = datetime.now(UTC).isoformat()
            self._save_state(state)

        log.info("%s done — %s", self.source_name, result)
        return result

    # ----- helpers -----

    def _write(self, rel_path: str, frontmatter: dict, body: str) -> bool:
        """Write the markdown file. Returns True if changed/created, False if identical."""
        target = self.archive_root / rel_path
        body_redacted = redact(body)
        fm_yaml = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        content = f"---\n{fm_yaml}\n---\n\n{body_redacted}\n"

        if target.exists() and target.read_text(encoding="utf-8") == content:
            return False

        if self.dry_run:
            log.info("[dry-run] would write %s (%d bytes)", target, len(content))
            return True

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("cannot parse state file %s: %s — starting fresh", self.state_path, exc)
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )


__all__ = ["ImporterBase", "ImportResult"]
