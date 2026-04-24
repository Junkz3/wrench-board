# SPDX-License-Identifier: Apache-2.0
"""Stub parser for Tebo IctView .tvw files (versions 3.0, 4.0).

Not yet implemented. See the roadmap:
docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md
"""

from __future__ import annotations

from api.board.parser._stub import make_stub_parser
from api.board.parser.base import register

TVWParser = register(make_stub_parser(".tvw", "Tebo IctView"))
