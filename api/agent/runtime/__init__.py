"""Diagnostic runtime (Managed Agents path) — sub-modules.

The legacy ``api.agent.runtime_managed`` module remains as a thin shim
that re-exports the public surface of this package, so existing callers
(``api.main``, scripts, tests) keep working without import-path churn.
"""

from __future__ import annotations
