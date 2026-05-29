"""Back-compat shim for ``teammate.rag`` → ``vigil.rag``.

See ``teammate/__init__.py`` for the rename history. New code: import
from ``vigil.rag`` directly.
"""

from __future__ import annotations

from vigil.rag import *  # noqa: F401,F403 — re-export all public symbols
