"""Stub parser for PCB Repair Tool .fz boardview files.

Not yet implemented. See the roadmap for when/why to promote to DONE:
docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md
"""

from __future__ import annotations

from api.board.parser._stub import make_stub_parser
from api.board.parser.base import register

FZParser = register(make_stub_parser(".fz", "PCB Repair Tool"))
