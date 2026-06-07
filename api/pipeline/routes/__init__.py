"""Pipeline FastAPI sub-routers — split by concern.

The package `__init__.py` aggregates these into a single `router` mounted
under `/pipeline`. Each module declares its own `APIRouter()` (no prefix)
and is `include_router()`-ed by the parent — endpoint paths are unchanged
relative to the pre-split monolith.
"""
