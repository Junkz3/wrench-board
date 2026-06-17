"""Deterministic device-identity facet extraction (T9a, the "carnet").

Pulls structured ids out of free device text — board number, Apple model, EMC,
codename — plus the whole label as a searchable marketing facet. Pure + free (no
LLM): these formats are stable and cover the majority of repair inputs; the
ambiguous remainder is enriched by Scout upstream.

Mirrors the cloud's ``domain/services/deviceAliasing.js`` so the alias-matching
rules (normalization, strong-vs-soft kinds) are identical on both sides of the
``/internal/device-registry`` contract.
"""
from __future__ import annotations

import re
import unicodedata

# Physically board-unique kinds: at most one identity may own a given strong
# alias (the anti-poison invariant, enforced cloud-side). Soft kinds
# (apple_model, codename, marketing) may fan out across sibling boards (cousins).
STRONG_KINDS = frozenset({"board", "emc"})

# Non-alphanumeric boundaries (NOT \b) so an underscore/hyphen separates tokens —
# `\b` treats `_` as a word char, so `A1286_820-2533` would hide the A-number.
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"

# Apple-style logic-board number, e.g. 820-2533 / 821-01234. (Apple-centric for
# V1; other vendors' board ids are left to Scout.)
_BOARD_RE = re.compile(_B + r"(8\d{2}-\d{3,5})" + _E)
# Apple model identifier, e.g. A1286 / A2179.
_APPLE_MODEL_RE = re.compile(_B + r"(A\d{3,4})" + _E)
# EMC number, e.g. "EMC 2353" / "EMC3164" → normalized to "EMC <digits>".
_EMC_RE = re.compile(_B + r"EMC\s*(\d{3,4})" + _E)
# Apple-ish board codename, e.g. K19i / N71m (letter + 2-3 digits + trailing
# lowercase). Conservative on purpose; codename is a SOFT kind so a stray hit
# only adds a searchable alias, it can never trigger a poison merge.
_CODENAME_RE = re.compile(_B + r"([A-Z]\d{2,3}[a-z])" + _E)


def slugify_label(label: str) -> str:
    """Directory-safe slug — byte-identical to ``orchestrator._slugify`` so a
    fallback (no strong id) canonical key matches the legacy pack-dir naming."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(label or "").strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "unknown-device"


def normalize_token(value: str) -> str:
    """Canonical match form: lowercase, fold accents, non-alphanumeric → single
    space, trim. Applied identically on write and lookup (cloud parity)."""
    s = unicodedata.normalize("NFKD", str(value or "")).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _append_unique(out: list[dict], kind: str, value: str) -> None:
    if not value:
        return
    if any(f["kind"] == kind and f["value"] == value for f in out):
        return
    out.append({"value": value, "kind": kind})


def extract_facets(text: str) -> list[dict]:
    """Extract [{value, kind}] facets from free device text. Order: board,
    apple_model, emc, codename, then the whole label as marketing."""
    text = str(text or "")
    facets: list[dict] = []
    for m in _BOARD_RE.finditer(text):
        _append_unique(facets, "board", m.group(1))
    for m in _APPLE_MODEL_RE.finditer(text):
        _append_unique(facets, "apple_model", m.group(1))
    for m in _EMC_RE.finditer(text):
        _append_unique(facets, "emc", f"EMC {m.group(1)}")
    for m in _CODENAME_RE.finditer(text):
        _append_unique(facets, "codename", m.group(1))
    label = text.strip()
    if label:
        _append_unique(facets, "marketing", label)
    return facets
