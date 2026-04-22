"""Stub parser for generic .cad boardview files (BoardViewer 2.1.0.8 umbrella).

Not yet implemented. See the roadmap:
docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md
"""

from __future__ import annotations

from api.board.parser._stub import make_stub_parser
from api.board.parser.base import register

CADParser = register(make_stub_parser(".cad", "generic .cad"))
