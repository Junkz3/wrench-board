# SPDX-License-Identifier: Apache-2.0
"""Stub parser for IBM Lenovo Card Analysis Support Tool .cst files.

Not yet implemented. See the roadmap:
docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md
"""

from __future__ import annotations

from api.board.parser._stub import make_stub_parser
from api.board.parser.base import register

CSTParser = register(make_stub_parser(".cst", "IBM Lenovo CAST"))
