"""Stub parser for HONHAN BoardViewer .bdv files.

Not yet implemented. See the roadmap:
docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md
"""

from __future__ import annotations

from api.board.parser._stub import make_stub_parser
from api.board.parser.base import register

BDVParser = register(make_stub_parser(".bdv", "HONHAN BoardViewer"))
