"""DEPRECATED: use ``vigil.providers.OllamaProvider``.

This module is a back-compat shim for v0.2 importers and will be removed in
v0.5. New code should import from ``vigil.providers`` directly.
"""

from __future__ import annotations

import warnings

from vigil.providers.ollama import (
    OllamaError,
    OllamaUnavailable,
)
from vigil.providers.ollama import (
    OllamaProvider as OllamaClient,
)

warnings.warn(
    "vigil.rag.ollama is deprecated; import from vigil.providers instead",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["OllamaClient", "OllamaError", "OllamaUnavailable"]
