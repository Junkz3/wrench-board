"""Cross-cutting validators shared by the pipeline route modules.

Path-segment safety for `device_slug` and `repair_id` is the only common
need across the split — both reach the filesystem unsanitised in several
endpoints and must be rejected at the HTTP boundary before any disk I/O.
"""

from __future__ import annotations

import re

from fastapi import HTTPException

# Repair ids are generated via `uuid.uuid4().hex[:12]` → 12 hex chars. We
# keep the validator permissive enough to accept legacy / manually-seeded
# ids (short alphanumeric + `._-`) while rejecting anything that could
# escape the `memory/{slug}/repairs/{repair_id}/` subtree when used as a
# filesystem path segment.
_REPAIR_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _validate_repair_id(repair_id: str) -> str:
    """Return `repair_id` when safe to use as a path segment, else raise 400.

    Rejects empty, `..`, anything with `/` or `\\`, and anything outside
    the `[A-Za-z0-9._-]` alphabet. Path traversal defense in depth for
    the measurement HTTP routes that append `repair_id` into a filesystem
    path without further sanitisation.
    """
    if not repair_id or repair_id in {".", ".."} or not _REPAIR_ID_RE.match(repair_id):
        raise HTTPException(status_code=400, detail={"reason": "invalid_repair_id"})
    return repair_id


def _validate_slug(slug: str) -> str:
    """Reject inputs that aren't already canonical kebab-case slugs.

    The GET routes happily slugify user input, but the ingestion POST needs
    stricter guarantees: the slug becomes a directory name under memory_root
    and a non-canonical value like "../evil" or "bad..slug" must never reach
    disk. Consecutive dots are rejected even though the character class allows
    a single `.` — `..` is a path-traversal marker by any reasonable reading.
    """
    if not _SLUG_RE.fullmatch(slug) or ".." in slug:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid device_slug {slug!r} — must match "
                "^[a-z0-9][a-z0-9._-]*$ with no '..' sequences."
            ),
        )
    return slug
