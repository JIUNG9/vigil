"""Source importers — bulk-collect content from GitHub / Jira / Slack / Confluence.

Each importer:
- Connects to its source via that source's HTTP API
- Iterates content (issues, pages, messages, ...) with pagination + watermarks
- Converts each item to a markdown file with a standard frontmatter
- Writes to ``<brain_root>/archive/<source>/...`` (configurable)
- Updates ``<brain_root>/.teammate-sync/state.json`` with high-water marks
- Applies a redaction pass to scrub secrets/PII before write

Re-runs are incremental: each importer reads its watermark and asks the
source only for items updated since.

The importers do NOT mutate the source. They are read-only.
"""

from teammate.importers.base import ImporterBase, ImportResult
from teammate.importers.redact import redact

__all__ = ["ImporterBase", "ImportResult", "redact"]
