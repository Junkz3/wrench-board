"""Stub parser for BoardView R5.0 .gr files.

Not yet implemented. See the roadmap:
docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md
"""

from __future__ import annotations

from api.board.parser._stub import make_stub_parser
from api.board.parser.base import register

GRParser = register(make_stub_parser(".gr", "BoardView R5.0"))
