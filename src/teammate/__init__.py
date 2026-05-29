"""Back-compat shim for the ``teammate`` import path (renamed → ``vigil``).

The package was renamed at v5.0.0 (2026-05-19). Anything importing
``teammate.*`` still works, but emits a single DeprecationWarning the
first time it's used in a process. New code should import ``vigil.*``.

This shim is a thin alias layer: every submodule re-exports the
corresponding ``vigil.*`` module. The submodule files are named to match
the original v0.x layout; they call ``importlib.import_module`` on the
real ``vigil.*`` package and assign all public symbols back into the
shim namespace.

Removal: scheduled for v6.0 (TBD — at least 90 days after v5.0.0).
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "The 'teammate' package has been renamed to 'vigil'. Importing "
    "'teammate.*' still works as a back-compat shim through the v5.x line, "
    "but will be removed in v6.0. Migrate imports to 'vigil.*'.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export the version from the canonical vigil package so consumers that
# read ``teammate.__version__`` continue to work.
try:
    from vigil import __version__ as __version__  # noqa: PLC0414 — explicit re-export
except ImportError:
    __version__ = "unknown"
