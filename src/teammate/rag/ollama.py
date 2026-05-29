"""Back-compat shim for ``teammate.rag.ollama`` → ``vigil.rag.ollama``.

Re-exports every public symbol from the renamed module so v0.x importers
(``from teammate.rag.ollama import OllamaClient``) keep working. The
DeprecationWarning from ``teammate/__init__.py`` fires once per process.
"""

from __future__ import annotations

from vigil.rag.ollama import *  # noqa: F401,F403
from vigil.rag.ollama import (  # noqa: F401 — explicit re-export of common names
    OllamaClient,
    OllamaError,
    OllamaUnavailable,
)
