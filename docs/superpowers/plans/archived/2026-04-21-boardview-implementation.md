# Boardview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the boardview panel for the `microsolder-agent` workbench — a Canvas-2D board viewer driven by Claude Opus via tool calls, with a from-scratch OpenBoardView `.brd` parser, an anti-hallucination validator, and drag-drop support.

**Architecture:** Python 3.11 + FastAPI + Pydantic on the backend (`api/board/`, `api/tools/boardview.py`, `api/session/`) ; vanilla JS + Canvas 2D on the frontend (`web/boardview/`). Data flows : `.brd` → parser → immutable `Board` model → session state → tool handlers (validated refdes) → WebSocket events → store → renderer.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pytest, Canvas 2D API (vanilla JS), WebSocket (native). No build step on the frontend.

**Design doc:** `docs/superpowers/specs/2026-04-21-boardview-design.md`

---

## Conventions for every task

- **TDD:** every Python component starts with a failing test. Frontend code is validated by the E2E manual checklist at the end (Canvas rendering is not unit-testable without a headless browser — out of scope for hackathon).
- **Commits:** every task ends with a commit. Small, focused messages in English, Conventional Commits flavor (`feat:`, `test:`, `refactor:`, `chore:`).
- **Before starting:** this project is not yet a git repo. Task 0 initializes it. If it is already a repo by the time a subagent picks up a later task, `git status` first and skip Task 0.
- **Line length & style:** see `pyproject.toml` (ruff). Run `make lint` and `make format` before commit.
- **Environment:** commands assume CWD = `/home/alex/Documents/hackathon-microsolder`.

---

## Task 0: Initialize git repo and first commit baseline

**Files:**
- Modify: repo root (`git init`).

- [ ] **Step 1: Check repo state**

Run: `git status`
Expected: `fatal: not a git repository` → proceed. If it's already a repo, skip to Task 1.

- [ ] **Step 2: Initialize repo and configure**

```bash
git init
git branch -M main
```

- [ ] **Step 3: Stage current state and commit**

```bash
git add -A
git status   # sanity check — no .venv, no .superpowers
git commit -m "chore: initial project baseline (scaffold from hackathon starter)"
```

Expected: a single baseline commit on `main`.

---

## Task 1: Create test fixtures directory and minimal `.brd`

**Files:**
- Create: `tests/board/__init__.py`
- Create: `tests/board/fixtures/__init__.py`
- Create: `tests/board/fixtures/minimal.brd`

**Context:** The minimal fixture is the contract for the parser. Two parts (one top, one bottom), four pins, one net. We'll write tests against this exact structure.

- [ ] **Step 1: Create the fixture file**

Create `tests/board/fixtures/minimal.brd` with exact contents:

```
str_length: 1024 512
var_data: 4 2 4 1
Format:
0 0
1000 0
1000 500
0 500
Parts:
R1 5 2
C1 10 4
Pins:
100 100 -99 1 +3V3
100 200 -99 1 GND
400 100 1 2 +3V3
400 200 -99 2 GND
Nails:
1 400 100 1 +3V3
```

Contract this encodes:
- Outline: 1000×500 mils rectangle (4 format points).
- `R1` : part index 1, `type_layer=5` → SMD + Top, `end_of_pins=2` → owns pins 0-1 (0-indexed internally).
- `C1` : part index 2, `type_layer=10` → through-hole + Bottom, `end_of_pins=4` → owns pins 2-3.
- Pin 3 (1-based: 3rd) has `probe=1`, matches nail 1 → `+3V3` net.
- One nail on top side.

- [ ] **Step 2: Create the empty `__init__.py` files**

```bash
touch tests/board/__init__.py tests/board/fixtures/__init__.py
```

- [ ] **Step 3: Commit**

```bash
git add tests/board/
git commit -m "test: add minimal .brd fixture for parser tests"
```

---

## Task 2: Pydantic data model — `Point`, `Layer`, `Pin`, `Part`

**Files:**
- Create: `api/board/model.py`
- Create: `tests/board/test_model.py`

- [ ] **Step 1: Write the failing test**

Create `tests/board/test_model.py`:

```python
"""Pydantic model for Board and its components."""

from api.board.model import Layer, Point, Pin, Part


def test_layer_bitflag():
    assert Layer.TOP.value == 1
    assert Layer.BOTTOM.value == 2
    assert (Layer.TOP | Layer.BOTTOM) == Layer.BOTH


def test_point_is_integer_mils():
    p = Point(x=100, y=200)
    assert p.x == 100
    assert p.y == 200


def test_part_bbox_from_two_points():
    part = Part(
        refdes="U7",
        layer=Layer.TOP,
        is_smd=True,
        bbox=(Point(x=0, y=0), Point(x=100, y=50)),
        pin_refs=[0, 1, 2, 3],
    )
    assert part.refdes == "U7"
    assert part.bbox[1].x == 100


def test_pin_with_optional_net():
    pin = Pin(
        part_refdes="U7",
        index=1,
        pos=Point(x=10, y=20),
        net=None,
        probe=None,
        layer=Layer.TOP,
    )
    assert pin.net is None
    assert pin.probe is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/board/test_model.py -v`
Expected: ImportError on `api.board.model` → `Layer` / `Point` / etc.

- [ ] **Step 3: Implement the model**

Create `api/board/model.py`:

```python
"""Board data model — Pydantic v2 immutable types."""

from __future__ import annotations

from enum import IntFlag

from pydantic import BaseModel


class Layer(IntFlag):
    TOP = 1
    BOTTOM = 2
    BOTH = TOP | BOTTOM


class Point(BaseModel):
    x: int  # mils (1 unit = 0.025 mm, per OBV convention)
    y: int


class Pin(BaseModel):
    part_refdes: str
    index: int
    pos: Point
    net: str | None = None
    probe: int | None = None
    layer: Layer


class Part(BaseModel):
    refdes: str
    layer: Layer
    is_smd: bool
    bbox: tuple[Point, Point]  # (min, max)
    pin_refs: list[int]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/board/test_model.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/model.py tests/board/test_model.py
git commit -m "feat(board): add Pydantic model for Point, Layer, Pin, Part"
```

---

## Task 3: Data model — `Net`, `Nail`, `Board` with private indexes

**Files:**
- Modify: `api/board/model.py`
- Modify: `tests/board/test_model.py`

- [ ] **Step 1: Write the failing test (append)**

Append to `tests/board/test_model.py`:

```python
from api.board.model import Board, Nail, Net


def _sample_board() -> Board:
    pins = [
        Pin(part_refdes="R1", index=1, pos=Point(x=0, y=0), net="+3V3", layer=Layer.TOP),
        Pin(part_refdes="R1", index=2, pos=Point(x=10, y=0), net="GND", layer=Layer.TOP),
    ]
    parts = [
        Part(
            refdes="R1", layer=Layer.TOP, is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=5)), pin_refs=[0, 1],
        ),
    ]
    nets = [
        Net(name="+3V3", pin_refs=[0], is_power=True, is_ground=False),
        Net(name="GND", pin_refs=[1], is_power=False, is_ground=True),
    ]
    return Board(
        board_id="test", file_hash="sha256:deadbeef", source_format="brd",
        outline=[Point(x=0, y=0), Point(x=100, y=0), Point(x=100, y=50), Point(x=0, y=50)],
        parts=parts, pins=pins, nets=nets, nails=[],
    )


def test_net_flags():
    n = Net(name="+3V3", pin_refs=[0, 1], is_power=True, is_ground=False)
    assert n.is_power is True
    assert n.is_ground is False


def test_nail_model():
    nail = Nail(probe=1, pos=Point(x=100, y=200), layer=Layer.TOP, net="+3V3")
    assert nail.probe == 1


def test_board_indexes_built_after_construction():
    board = _sample_board()
    assert board.part_by_refdes("R1") is not None
    assert board.part_by_refdes("R1").refdes == "R1"
    assert board.part_by_refdes("missing") is None
    assert board.net_by_name("+3V3").is_power is True


def test_board_is_json_serializable_without_private_indexes():
    board = _sample_board()
    dumped = board.model_dump()
    # private indexes must not leak into serialization
    assert "_refdes_index" not in dumped
    assert "_net_index" not in dumped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/board/test_model.py -v`
Expected: 4 new tests fail on `Net` / `Nail` / `Board` not defined.

- [ ] **Step 3: Implement the rest of the model (append to `api/board/model.py`)**

```python
class Net(BaseModel):
    name: str
    pin_refs: list[int]
    is_power: bool = False
    is_ground: bool = False


class Nail(BaseModel):
    probe: int
    pos: Point
    layer: Layer
    net: str


class Board(BaseModel):
    board_id: str
    file_hash: str
    source_format: str
    outline: list[Point]
    parts: list[Part]
    pins: list[Pin]
    nets: list[Net]
    nails: list[Nail]

    # private indexes, built by model_post_init — excluded from serialization
    _refdes_index: dict[str, Part]
    _net_index: dict[str, Net]

    def model_post_init(self, __context) -> None:
        object.__setattr__(self, "_refdes_index", {p.refdes: p for p in self.parts})
        object.__setattr__(self, "_net_index", {n.name: n for n in self.nets})

    def part_by_refdes(self, refdes: str) -> Part | None:
        return self._refdes_index.get(refdes)

    def net_by_name(self, name: str) -> Net | None:
        return self._net_index.get(name)
```

Add to the imports at top: `from typing import Any` is not needed (we use `__context`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/board/test_model.py -v`
Expected: 8 passed total.

- [ ] **Step 5: Commit**

```bash
git add api/board/model.py tests/board/test_model.py
git commit -m "feat(board): add Net, Nail, Board with private refdes/net indexes"
```

---

## Task 4: Parser ABC and extension dispatch

**Files:**
- Create: `api/board/parser/__init__.py`
- Create: `api/board/parser/base.py`
- Create: `tests/board/test_parser_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/board/test_parser_base.py`:

```python
from pathlib import Path

import pytest

from api.board.parser.base import (
    BoardParser,
    UnsupportedFormatError,
    parser_for,
)


def test_parser_for_unknown_extension_raises(tmp_path: Path):
    p = tmp_path / "nope.xyz"
    p.write_bytes(b"irrelevant")
    with pytest.raises(UnsupportedFormatError):
        parser_for(p)


def test_parser_for_brd_returns_brd_parser(tmp_path: Path):
    from api.board.parser.brd import BRDParser  # noqa: F401  (registers via import)
    p = tmp_path / "mini.brd"
    p.write_text("str_length: 0\nvar_data: 0 0 0 0\n")
    parser = parser_for(p)
    assert isinstance(parser, BoardParser)
    assert ".brd" in parser.extensions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/board/test_parser_base.py -v`
Expected: ImportError on `api.board.parser.base`.

- [ ] **Step 3: Implement the ABC**

Create `api/board/parser/__init__.py`:

```python
"""Board parsers — one implementation per file format."""
```

Create `api/board/parser/base.py`:

```python
"""Abstract base and format dispatch for board file parsers."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

from api.board.model import Board


class BoardParserError(Exception):
    """Base class for parser errors."""


class UnsupportedFormatError(BoardParserError):
    """Raised when no parser is registered for a file's extension."""


class InvalidBoardFile(BoardParserError):
    """Raised when a file is recognized but malformed or refused."""


class ObfuscatedFileError(InvalidBoardFile):
    """Raised on OBV-signature obfuscated files — we refuse to decode."""


class MalformedHeaderError(InvalidBoardFile):
    def __init__(self, field: str):
        super().__init__(f"malformed header block: {field}")
        self.field = field


class PinPartMismatchError(InvalidBoardFile):
    def __init__(self, pin_index: int):
        super().__init__(f"pin {pin_index} references an unknown part")
        self.pin_index = pin_index


class BoardParser(ABC):
    """Abstract parser. One subclass per file format."""

    extensions: tuple[str, ...] = ()

    def parse_file(self, path: Path) -> Board:
        raw = path.read_bytes()
        file_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        return self.parse(raw, file_hash=file_hash, board_id=path.stem)

    @abstractmethod
    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board: ...


_REGISTRY: dict[str, type[BoardParser]] = {}


def register(parser_cls: type[BoardParser]) -> type[BoardParser]:
    """Decorator : register a parser by its extensions."""
    for ext in parser_cls.extensions:
        _REGISTRY[ext.lower()] = parser_cls
    return parser_cls


def parser_for(path: Path) -> BoardParser:
    ext = path.suffix.lower()
    cls = _REGISTRY.get(ext)
    if cls is None:
        raise UnsupportedFormatError(f"no parser registered for extension {ext!r}")
    return cls()
```

- [ ] **Step 4: Run test (it will still fail on `BRDParser` import)**

The test imports `api.board.parser.brd.BRDParser` which doesn't exist yet. The test for unsupported extension works ; brd-specific test will be fixed in Task 5. Acceptable to commit base now with the first test passing.

Run: `.venv/bin/pytest tests/board/test_parser_base.py::test_parser_for_unknown_extension_raises -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/board/parser/ tests/board/test_parser_base.py
git commit -m "feat(board): add BoardParser ABC with extension dispatch and error types"
```

---

## Task 5: `.brd` parser — header (var_data + Format outline)

**Files:**
- Create: `api/board/parser/brd.py`
- Create: `tests/board/test_brd_parser.py`

- [ ] **Step 1: Write the failing test**

Create `tests/board/test_brd_parser.py`:

```python
"""Parser for OpenBoardView .brd (Test_Link) format."""

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import (
    MalformedHeaderError,
    ObfuscatedFileError,
    PinPartMismatchError,
)
from api.board.parser.brd import BRDParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_parses_minimal_outline():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    assert board.board_id == "minimal"
    assert board.source_format == "brd"
    assert len(board.outline) == 4
    assert board.outline[0].x == 0
    assert board.outline[0].y == 0
    assert board.outline[2].x == 1000
    assert board.outline[2].y == 500


def test_rejects_obfuscated_file(tmp_path: Path):
    f = tmp_path / "obf.brd"
    # OBV obfuscation signature: 0x23 0xe2 0x63 0x28 at byte 0
    f.write_bytes(b"\x23\xe2\x63\x28" + b"\x00" * 64)
    with pytest.raises(ObfuscatedFileError):
        BRDParser().parse_file(f)


def test_malformed_header_raises(tmp_path: Path):
    f = tmp_path / "bad.brd"
    f.write_text("str_length: 0\nvar_data: not-a-number 2 4 1\n")
    with pytest.raises(MalformedHeaderError):
        BRDParser().parse_file(f)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/board/test_brd_parser.py -v`
Expected: ImportError on `api.board.parser.brd`.

- [ ] **Step 3: Implement the parser — header + outline only**

Create `api/board/parser/brd.py`:

```python
"""OpenBoardView .brd (Test_Link) parser — written from scratch.

Format reference: the OBV codebase documents the .brd format ; we reimplement
from the specification, no code copied. See the design doc §7 for field layout.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser.base import (
    BRDParser as _,  # sentinel import to ensure we override below
    BoardParser,
    InvalidBoardFile,
    MalformedHeaderError,
    ObfuscatedFileError,
    PinPartMismatchError,
    register,
)

_OBF_SIGNATURE = b"\x23\xe2\x63\x28"


@dataclass
class _Header:
    num_format: int
    num_parts: int
    num_pins: int
    num_nails: int


@register
class BRDParser(BoardParser):
    extensions = (".brd",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if raw.startswith(_OBF_SIGNATURE):
            raise ObfuscatedFileError("file uses OBV XOR obfuscation — refused")

        text = raw.decode("utf-8", errors="replace")
        if "str_length:" not in text or "var_data:" not in text:
            raise InvalidBoardFile("unknown encoding or not a .brd Test_Link file")

        lines = _lines(text)
        header = _parse_header(lines)
        outline = _parse_outline(lines, header.num_format)

        # Parts/Pins/Nails — implemented in later tasks
        parts: list[Part] = []
        pins: list[Pin] = []
        nets: list[Net] = []
        nails: list[Nail] = []

        return Board(
            board_id=board_id,
            file_hash=file_hash,
            source_format="brd",
            outline=outline,
            parts=parts,
            pins=pins,
            nets=nets,
            nails=nails,
        )


def _lines(text: str) -> list[str]:
    """Return stripped non-empty lines."""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _parse_header(lines: list[str]) -> _Header:
    for ln in lines:
        if ln.startswith("var_data:"):
            parts = ln.split()[1:]  # skip "var_data:"
            if len(parts) != 4:
                raise MalformedHeaderError("var_data")
            try:
                return _Header(*(int(p) for p in parts))
            except ValueError as exc:
                raise MalformedHeaderError("var_data") from exc
    raise MalformedHeaderError("var_data")


def _parse_outline(lines: list[str], n: int) -> list[Point]:
    try:
        idx = lines.index("Format:")
    except ValueError:
        if n == 0:
            return []
        raise MalformedHeaderError("Format")
    pts: list[Point] = []
    for raw in lines[idx + 1 : idx + 1 + n]:
        try:
            x, y = raw.split()
            pts.append(Point(x=int(x), y=int(y)))
        except ValueError as exc:
            raise MalformedHeaderError("Format") from exc
    if len(pts) != n:
        raise MalformedHeaderError("Format")
    return pts
```

Note the `from api.board.parser.base import BRDParser as _` is a leftover — remove it. Final imports should be clean:

```python
from api.board.parser.base import (
    BoardParser,
    InvalidBoardFile,
    MalformedHeaderError,
    ObfuscatedFileError,
    PinPartMismatchError,
    register,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/board/test_brd_parser.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/parser/brd.py tests/board/test_brd_parser.py
git commit -m "feat(brd-parser): parse var_data header and Format outline; refuse obfuscated"
```

---

## Task 6: `.brd` parser — Parts block

**Files:**
- Modify: `api/board/parser/brd.py`
- Modify: `tests/board/test_brd_parser.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_parses_parts_block_with_layer_bits():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    assert len(board.parts) == 2
    r1 = board.part_by_refdes("R1")
    c1 = board.part_by_refdes("C1")
    assert r1 is not None
    assert r1.layer == Layer.TOP
    assert r1.is_smd is True
    assert c1.layer == Layer.BOTTOM
    assert c1.is_smd is False  # type_layer 10 has bit 0x8 without 0x4 → TH + bottom
```

(Verify the byte layout: `type_layer=5` = `0b0101` → Top + SMD ; `type_layer=10` = `0b1010` → Bottom + through-hole. Cross-check against `.brd` format reference.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/board/test_brd_parser.py::test_parses_parts_block_with_layer_bits -v`
Expected: FAIL (parts list is empty).

- [ ] **Step 3: Implement Parts parsing**

Modify `api/board/parser/brd.py`. Replace the line `parts: list[Part] = []` with a call to a new helper, and add the helper function:

```python
# In parse():
parts_raw = _parse_parts(lines, header.num_parts)
# parts_raw is list[tuple[refdes, type_layer, end_of_pins]]
# we need pins to compute bbox ; we'll patch parts after pins are parsed (Task 7).
# For now, create parts with placeholder bbox and empty pin_refs.
parts = [
    Part(
        refdes=r,
        layer=_layer_from_bits(t),
        is_smd=_is_smd_from_bits(t),
        bbox=(Point(x=0, y=0), Point(x=0, y=0)),
        pin_refs=[],
    )
    for r, t, _ in parts_raw
]
```

Add at the bottom of the module:

```python
def _parse_parts(lines: list[str], n: int) -> list[tuple[str, int, int]]:
    try:
        # Header marker can be "Parts:" or "Pins1:" historically
        idx = next(i for i, ln in enumerate(lines) if ln in ("Parts:", "Pins1:"))
    except StopIteration:
        if n == 0:
            return []
        raise MalformedHeaderError("Parts")
    out: list[tuple[str, int, int]] = []
    for raw in lines[idx + 1 : idx + 1 + n]:
        toks = raw.split()
        if len(toks) < 3:
            raise MalformedHeaderError("Parts")
        try:
            name = toks[0]
            type_layer = int(toks[1])
            end_of_pins = int(toks[2])
        except ValueError as exc:
            raise MalformedHeaderError("Parts") from exc
        out.append((name, type_layer, end_of_pins))
    if len(out) != n:
        raise MalformedHeaderError("Parts")
    return out


def _layer_from_bits(type_layer: int) -> Layer:
    # Single-bit scheme : bit 0x2 set → bottom layer, else top.
    # Validated against the fixture : 5 (0b0101) → top, 10 (0b1010) → bottom.
    return Layer.BOTTOM if (type_layer & 0x2) else Layer.TOP


def _is_smd_from_bits(type_layer: int) -> bool:
    # Bit 0x4 set → SMD. Validated: 5 → SMD, 10 → through-hole.
    return bool(type_layer & 0x4)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/board/test_brd_parser.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/parser/brd.py tests/board/test_brd_parser.py
git commit -m "feat(brd-parser): parse Parts block with layer/SMD bitfield"
```

---

## Task 7: `.brd` parser — Pins block with part linkage and bbox

**Files:**
- Modify: `api/board/parser/brd.py`
- Modify: `tests/board/test_brd_parser.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_parses_pins_block_with_bbox():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    assert len(board.pins) == 4

    # R1 owns pins 0, 1 at (100,100) and (100,200)
    r1 = board.part_by_refdes("R1")
    pins_r1 = [board.pins[i] for i in r1.pin_refs]
    assert len(pins_r1) == 2
    assert pins_r1[0].pos.x == 100
    assert r1.bbox[0].x == 100 and r1.bbox[0].y == 100
    assert r1.bbox[1].x == 100 and r1.bbox[1].y == 200

    # C1 at (400, 100) and (400, 200), bottom layer — its pin 0 has probe=1
    c1 = board.part_by_refdes("C1")
    pins_c1 = [board.pins[i] for i in c1.pin_refs]
    assert pins_c1[0].probe == 1
    assert pins_c1[1].probe is None
    assert pins_c1[0].layer == Layer.BOTTOM


def test_pin_part_mismatch_raises(tmp_path: Path):
    bad = tmp_path / "mismatch.brd"
    bad.write_text(
        "str_length: 0\n"
        "var_data: 4 1 1 0\n"
        "Format:\n0 0\n10 0\n10 10\n0 10\n"
        "Parts:\nR1 5 1\n"
        "Pins:\n5 5 -99 99 NET\n"  # part_idx=99 but only 1 part
    )
    with pytest.raises(PinPartMismatchError):
        BRDParser().parse_file(bad)
```

- [ ] **Step 2: Run tests**

Expected: FAIL — pins list is empty, bbox is all zeros.

- [ ] **Step 3: Implement pins + bbox patching**

Modify `parse()` to replace the `pins: list[Pin] = []` line with:

```python
pins, parts = _parse_pins_and_patch_parts(lines, header.num_pins, parts_raw, parts)
```

Add the helpers:

```python
def _parse_pins_and_patch_parts(
    lines: list[str],
    num_pins: int,
    parts_raw: list[tuple[str, int, int]],
    parts: list[Part],
) -> tuple[list[Pin], list[Part]]:
    try:
        idx = next(i for i, ln in enumerate(lines) if ln in ("Pins:", "Pins2:"))
    except StopIteration:
        if num_pins == 0:
            return [], parts
        raise MalformedHeaderError("Pins")

    pin_lines = lines[idx + 1 : idx + 1 + num_pins]
    if len(pin_lines) != num_pins:
        raise MalformedHeaderError("Pins")

    # Compute pin ownership : part k owns pins [prev_end, parts_raw[k].end_of_pins).
    # prev_end starts at 0 ; end is exclusive.
    pin_refs_by_part: list[list[int]] = [[] for _ in parts_raw]
    prev_end = 0
    for k, (_, _, end) in enumerate(parts_raw):
        pin_refs_by_part[k] = list(range(prev_end, end))
        prev_end = end

    pins: list[Pin] = []
    for i, raw in enumerate(pin_lines):
        toks = raw.split()
        if len(toks) < 4:
            raise MalformedHeaderError("Pins")
        try:
            x = int(toks[0])
            y = int(toks[1])
            probe = int(toks[2])
            part_idx = int(toks[3])  # 1-based
        except ValueError as exc:
            raise MalformedHeaderError("Pins") from exc
        net = toks[4] if len(toks) >= 5 else ""

        if part_idx < 1 or part_idx > len(parts_raw):
            raise PinPartMismatchError(i)

        owner_k = part_idx - 1
        owner = parts[owner_k]
        pins.append(
            Pin(
                part_refdes=owner.refdes,
                index=len(pin_refs_by_part[owner_k][: pin_refs_by_part[owner_k].index(i) + 1]),
                pos=Point(x=x, y=y),
                net=(net or None),
                probe=(probe if probe != -99 else None),
                layer=owner.layer,
            )
        )

    # Patch parts : pin_refs + bbox
    patched: list[Part] = []
    for k, part in enumerate(parts):
        refs = pin_refs_by_part[k]
        if not refs:
            bbox = part.bbox  # leave zero bbox if part has no pins
        else:
            xs = [pins[j].pos.x for j in refs]
            ys = [pins[j].pos.y for j in refs]
            bbox = (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
        patched.append(part.model_copy(update={"pin_refs": refs, "bbox": bbox}))

    return pins, patched
```

Note : the `index=` calculation above is convoluted ; simplify to 1-based counter within the part :

```python
# replace index= computation with:
local_index = (i - (pin_refs_by_part[owner_k][0] if pin_refs_by_part[owner_k] else i)) + 1
```

Actually, simplest : since `i` iterates pin list sequentially and `pin_refs_by_part[k]` contains the global indexes in order, the local 1-based index of the `j`-th element of `pin_refs_by_part[k]` is `j+1`. Maintain per-part counters instead:

```python
counters = [0] * len(parts_raw)
# ...inside the loop:
counters[owner_k] += 1
local_index = counters[owner_k]
```

Use that in the `Pin(..., index=local_index, ...)` call.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/board/test_brd_parser.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/parser/brd.py tests/board/test_brd_parser.py
git commit -m "feat(brd-parser): parse Pins block, link to parts, compute bbox"
```

---

## Task 8: `.brd` parser — Nails block and dangling-net backfill

**Files:**
- Modify: `api/board/parser/brd.py`
- Modify: `tests/board/test_brd_parser.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_parses_nails_and_backfills_dangling_nets():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    assert len(board.nails) == 1
    nail = board.nails[0]
    assert nail.probe == 1
    assert nail.net == "+3V3"
    assert nail.layer == Layer.TOP


def test_empty_net_is_backfilled_from_nails(tmp_path: Path):
    # pin with empty net + matching probe nail should be resolved
    f = tmp_path / "lenovo.brd"
    f.write_text(
        "str_length: 0\n"
        "var_data: 4 1 1 1\n"
        "Format:\n0 0\n10 0\n10 10\n0 10\n"
        "Parts:\nR1 5 1\n"
        "Pins:\n5 5 42 1 \n"  # empty net_name, probe=42
        "Nails:\n42 5 5 1 +5V0\n"
    )
    board = BRDParser().parse_file(f)
    assert board.pins[0].net == "+5V0"
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Implement Nails block + backfill**

In `parse()`, after the pins/parts patching, add:

```python
nails = _parse_nails(lines, header.num_nails)
pins = _backfill_empty_nets(pins, nails)
```

Add helpers:

```python
def _parse_nails(lines: list[str], n: int) -> list[Nail]:
    if n == 0:
        return []
    try:
        idx = lines.index("Nails:")
    except ValueError:
        raise MalformedHeaderError("Nails")
    out: list[Nail] = []
    for raw in lines[idx + 1 : idx + 1 + n]:
        toks = raw.split()
        if len(toks) < 5:
            raise MalformedHeaderError("Nails")
        try:
            probe = int(toks[0])
            x = int(toks[1])
            y = int(toks[2])
            side = int(toks[3])
        except ValueError as exc:
            raise MalformedHeaderError("Nails") from exc
        net = toks[4]
        layer = Layer.TOP if side == 1 else Layer.BOTTOM
        out.append(Nail(probe=probe, pos=Point(x=x, y=y), layer=layer, net=net))
    if len(out) != n:
        raise MalformedHeaderError("Nails")
    return out


def _backfill_empty_nets(pins: list[Pin], nails: list[Nail]) -> list[Pin]:
    nail_by_probe: dict[int, str] = {n.probe: n.net for n in nails}
    patched: list[Pin] = []
    for pin in pins:
        if pin.net is None and pin.probe is not None and pin.probe in nail_by_probe:
            patched.append(pin.model_copy(update={"net": nail_by_probe[pin.probe]}))
        else:
            patched.append(pin)
    return patched
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/board/test_brd_parser.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/parser/brd.py tests/board/test_brd_parser.py
git commit -m "feat(brd-parser): parse Nails block and backfill empty pin nets"
```

---

## Task 9: `.brd` parser — derive nets + power/ground heuristic

**Files:**
- Modify: `api/board/parser/brd.py`
- Modify: `tests/board/test_brd_parser.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_derives_nets_from_pins_with_power_ground_flags():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    net_names = {n.name for n in board.nets}
    assert net_names == {"+3V3", "GND"}
    vcc = board.net_by_name("+3V3")
    gnd = board.net_by_name("GND")
    assert vcc.is_power is True
    assert vcc.is_ground is False
    assert gnd.is_power is False
    assert gnd.is_ground is True
    # pin_refs must point into board.pins
    for n in board.nets:
        for i in n.pin_refs:
            assert 0 <= i < len(board.pins)
```

- [ ] **Step 2: Run tests**

Expected: FAIL — `board.nets` is empty.

- [ ] **Step 3: Implement net derivation + heuristic**

In `parse()`, after `pins = _backfill_empty_nets(pins, nails)`:

```python
nets = _derive_nets(pins)
```

Add helper:

```python
import re

_POWER_RE = re.compile(r"^(\+?\d+V\d*|VCC|VDD|V_[A-Z0-9_]+)$", re.IGNORECASE)
_GROUND_RE = re.compile(r"^(GND|VSS|AGND|DGND|PGND)$", re.IGNORECASE)


def _derive_nets(pins: list[Pin]) -> list[Net]:
    by_name: dict[str, list[int]] = {}
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        by_name.setdefault(pin.net, []).append(i)
    out: list[Net] = []
    for name, refs in sorted(by_name.items()):
        out.append(
            Net(
                name=name,
                pin_refs=refs,
                is_power=bool(_POWER_RE.match(name)),
                is_ground=bool(_GROUND_RE.match(name)),
            )
        )
    return out
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/board/test_brd_parser.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/parser/brd.py tests/board/test_brd_parser.py
git commit -m "feat(brd-parser): derive nets from pins; power/ground heuristic"
```

---

## Task 10: Validator — refdes/net/pin resolution and suggestions

**Files:**
- Create: `api/board/validator.py`
- Create: `tests/board/test_validator.py`

- [ ] **Step 1: Write the failing test**

Create `tests/board/test_validator.py`:

```python
from pathlib import Path

from api.board.parser.brd import BRDParser
from api.board.validator import (
    is_valid_refdes,
    resolve_net,
    resolve_part,
    resolve_pin,
    suggest_similar,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _board():
    return BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")


def test_is_valid_refdes_true():
    board = _board()
    assert is_valid_refdes(board, "R1") is True
    assert is_valid_refdes(board, "C1") is True


def test_is_valid_refdes_false_is_case_sensitive():
    board = _board()
    assert is_valid_refdes(board, "r1") is False
    assert is_valid_refdes(board, "U999") is False


def test_resolve_part():
    board = _board()
    assert resolve_part(board, "R1").refdes == "R1"
    assert resolve_part(board, "U999") is None


def test_resolve_net():
    board = _board()
    assert resolve_net(board, "+3V3").name == "+3V3"
    assert resolve_net(board, "MISSING") is None


def test_resolve_pin():
    board = _board()
    pin = resolve_pin(board, "R1", 1)
    assert pin is not None
    assert pin.part_refdes == "R1"
    assert pin.index == 1
    assert resolve_pin(board, "R1", 99) is None
    assert resolve_pin(board, "U999", 1) is None


def test_suggest_similar_returns_close_matches():
    board = _board()
    suggestions = suggest_similar(board, "R2", k=3)
    assert "R1" in suggestions
    # empty string → empty list
    assert suggest_similar(board, "", k=3) == []
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/pytest tests/board/test_validator.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the validator**

Create `api/board/validator.py`:

```python
"""Anti-hallucination guardrail — every refdes the agent mentions passes here."""

from __future__ import annotations

from api.board.model import Board, Net, Part, Pin


def is_valid_refdes(board: Board, refdes: str) -> bool:
    return board.part_by_refdes(refdes) is not None


def resolve_part(board: Board, refdes: str) -> Part | None:
    return board.part_by_refdes(refdes)


def resolve_net(board: Board, net_name: str) -> Net | None:
    return board.net_by_name(net_name)


def resolve_pin(board: Board, refdes: str, pin_index: int) -> Pin | None:
    part = board.part_by_refdes(refdes)
    if part is None:
        return None
    for i in part.pin_refs:
        if board.pins[i].index == pin_index:
            return board.pins[i]
    return None


def suggest_similar(board: Board, refdes: str, k: int = 3) -> list[str]:
    """Return up to k refdes names closest to the input by Levenshtein distance."""
    if not refdes:
        return []
    candidates = [p.refdes for p in board.parts]
    scored = sorted(candidates, key=lambda c: _levenshtein(refdes, c))
    return scored[:k]


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/board/test_validator.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/validator.py tests/board/test_validator.py
git commit -m "feat(board): anti-hallucination validator with Levenshtein suggestions"
```

---

## Task 11: Session state with single active board

**Files:**
- Create: `api/session/state.py`
- Create: `tests/session/__init__.py`
- Create: `tests/session/test_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/session/__init__.py` (empty). Create `tests/session/test_state.py`:

```python
from pathlib import Path

from api.board.parser.brd import BRDParser
from api.session.state import SessionState

FIXTURE_DIR = Path(__file__).parent.parent / "board" / "fixtures"


def test_new_session_has_no_board():
    s = SessionState()
    assert s.board is None
    assert s.layer == "top"
    assert s.highlights == set()
    assert s.net_highlight is None


def test_set_board_resets_view():
    s = SessionState()
    s.highlights.add("U1")
    s.net_highlight = "+3V3"

    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    s.set_board(board)

    assert s.board is board
    assert s.highlights == set()
    assert s.net_highlight is None
    assert s.layer == "top"
    assert s.annotations == {}
    assert s.arrows == {}


def test_session_tracks_annotations_and_arrows():
    s = SessionState()
    s.annotations["ann-1"] = {"refdes": "U7", "label": "PMIC"}
    s.arrows["arr-1"] = {"from": [0, 0], "to": [10, 10]}
    assert len(s.annotations) == 1
    assert len(s.arrows) == 1
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement SessionState**

Create `api/session/state.py`:

```python
"""Per-session state for the boardview panel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from api.board.model import Board

Side = Literal["top", "bottom"]


@dataclass
class SessionState:
    board: Board | None = None
    layer: Side = "top"
    highlights: set[str] = field(default_factory=set)
    net_highlight: str | None = None
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    arrows: dict[str, dict[str, Any]] = field(default_factory=dict)
    dim_unrelated: bool = False
    filter_prefix: str | None = None
    layer_visibility: dict[Side, bool] = field(
        default_factory=lambda: {"top": True, "bottom": True}
    )

    def set_board(self, board: Board) -> None:
        """Load a new board and reset all view state."""
        self.board = board
        self.layer = "top"
        self.highlights = set()
        self.net_highlight = None
        self.annotations = {}
        self.arrows = {}
        self.dim_unrelated = False
        self.filter_prefix = None
        self.layer_visibility = {"top": True, "bottom": True}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/session/test_state.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/session/ tests/session/
git commit -m "feat(session): add SessionState for boardview view state"
```

---

## Task 12: Event bus for boardview loads

**Files:**
- Create: `api/board/events.py`
- Create: `tests/board/test_events.py`

**Context:** Internal pub/sub so the (future) knowledge pipeline can subscribe to `board:loaded` events emitted when a board is loaded. Keep the bus tiny and synchronous — we don't need asyncio broadcasting for the hackathon.

- [ ] **Step 1: Write the failing test**

Create `tests/board/test_events.py`:

```python
from api.board.events import BoardEventBus


def test_publish_without_subscribers_is_noop():
    bus = BoardEventBus()
    bus.publish("board:loaded", {"board_id": "x", "is_known": True})


def test_subscribers_receive_events_in_order():
    bus = BoardEventBus()
    received = []

    def handler(payload):
        received.append(payload)

    bus.subscribe("board:loaded", handler)
    bus.publish("board:loaded", {"board_id": "a"})
    bus.publish("board:loaded", {"board_id": "b"})
    assert [p["board_id"] for p in received] == ["a", "b"]


def test_unknown_topic_does_not_raise():
    bus = BoardEventBus()
    bus.publish("unknown:topic", {})  # should not raise
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement the bus**

Create `api/board/events.py`:

```python
"""Tiny synchronous pub/sub for board-level events (e.g. board:loaded)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any


class BoardEventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers[topic].append(handler)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        for h in self._handlers.get(topic, []):
            h(payload)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/board/test_events.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/board/events.py tests/board/test_events.py
git commit -m "feat(board): tiny pub/sub event bus for board-level events"
```

---

## Task 13: WebSocket event schema (Pydantic models)

**Files:**
- Create: `api/tools/ws_events.py`
- Create: `tests/tools/__init__.py`
- Create: `tests/tools/test_ws_events.py`

**Context:** Strongly-typed WS envelopes. Every boardview verb has a model ; tool handlers emit these directly.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/__init__.py`. Create `tests/tools/test_ws_events.py`:

```python
from api.tools.ws_events import (
    BoardLoaded,
    Highlight,
    HighlightNet,
    Focus,
    Flip,
    Annotate,
    ResetView,
    DimUnrelated,
    LayerVisibility,
    Filter,
    DrawArrow,
    Measure,
    ShowPin,
    UploadError,
)


def test_highlight_envelope_round_trip():
    e = Highlight(refdes=["U7"], color="accent")
    dumped = e.model_dump()
    assert dumped["type"] == "boardview.highlight"
    assert dumped["refdes"] == ["U7"]
    assert dumped["color"] == "accent"
    assert dumped["additive"] is False


def test_highlight_net_envelope_shape():
    e = HighlightNet(net="+3V3", pin_refs=[1, 2, 3])
    assert e.model_dump()["type"] == "boardview.highlight_net"


def test_upload_error_envelope():
    e = UploadError(reason="obfuscated", message="refused")
    dumped = e.model_dump()
    assert dumped["reason"] == "obfuscated"
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement the envelopes**

Create `api/tools/ws_events.py`:

```python
"""WebSocket event envelopes for the boardview panel (backend → frontend).

All events have a `type` field of the form "boardview.<verb>".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class _BVEvent(BaseModel):
    """Base class with a fixed `type` field set per subclass."""

    type: str


class BoardLoaded(_BVEvent):
    type: Literal["boardview.board_loaded"] = "boardview.board_loaded"
    board_id: str
    file_hash: str
    parts_count: int
    outline: list[tuple[int, int]]
    parts: list[dict[str, Any]]
    pins: list[dict[str, Any]]
    nets: list[dict[str, Any]]


class Highlight(_BVEvent):
    type: Literal["boardview.highlight"] = "boardview.highlight"
    refdes: list[str]
    color: Literal["accent", "warn", "mute"] = "accent"
    additive: bool = False


class HighlightNet(_BVEvent):
    type: Literal["boardview.highlight_net"] = "boardview.highlight_net"
    net: str
    pin_refs: list[int]


class Focus(_BVEvent):
    type: Literal["boardview.focus"] = "boardview.focus"
    refdes: str
    bbox: tuple[tuple[int, int], tuple[int, int]]
    zoom: float
    auto_flipped: bool = False


class Flip(_BVEvent):
    type: Literal["boardview.flip"] = "boardview.flip"
    new_side: Literal["top", "bottom"]
    preserve_cursor: bool = False


class Annotate(_BVEvent):
    type: Literal["boardview.annotate"] = "boardview.annotate"
    refdes: str
    label: str
    id: str


class ResetView(_BVEvent):
    type: Literal["boardview.reset_view"] = "boardview.reset_view"


class DimUnrelated(_BVEvent):
    type: Literal["boardview.dim_unrelated"] = "boardview.dim_unrelated"


class LayerVisibility(_BVEvent):
    type: Literal["boardview.layer_visibility"] = "boardview.layer_visibility"
    layer: Literal["top", "bottom"]
    visible: bool


class Filter(_BVEvent):
    type: Literal["boardview.filter"] = "boardview.filter"
    prefix: str | None


class DrawArrow(_BVEvent):
    type: Literal["boardview.draw_arrow"] = "boardview.draw_arrow"
    from_: tuple[int, int] = Field(alias="from")
    to: tuple[int, int]
    id: str


class Measure(_BVEvent):
    type: Literal["boardview.measure"] = "boardview.measure"
    from_refdes: str
    to_refdes: str
    distance_mm: float


class ShowPin(_BVEvent):
    type: Literal["boardview.show_pin"] = "boardview.show_pin"
    refdes: str
    pin: int
    pos: tuple[int, int]


class UploadError(_BVEvent):
    type: Literal["boardview.upload_error"] = "boardview.upload_error"
    reason: Literal["obfuscated", "malformed-header", "unsupported-format", "io-error"]
    message: str
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_ws_events.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/ws_events.py tests/tools/
git commit -m "feat(tools): add Pydantic WS event envelopes for boardview verbs"
```

---

## Task 14: Tool handler — highlight_component

**Files:**
- Create: `api/tools/boardview.py`
- Create: `tests/tools/test_boardview_handlers.py`

**Context:** Each handler follows the same pattern : (1) validate with `api.board.validator`, (2) mutate `SessionState`, (3) return either `{"ok": true, "summary": "...", "event": <Pydantic envelope>}` or `{"ok": false, "reason": "...", "suggestions": [...]}`. The FastAPI WS layer (out of scope) is responsible for sending `event` over the wire.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_boardview_handlers.py`:

```python
from pathlib import Path

import pytest

from api.board.parser.brd import BRDParser
from api.session.state import SessionState
from api.tools.boardview import highlight_component

FIXTURE_DIR = Path(__file__).parent.parent / "board" / "fixtures"


@pytest.fixture
def session() -> SessionState:
    s = SessionState()
    s.set_board(BRDParser().parse_file(FIXTURE_DIR / "minimal.brd"))
    return s


def test_highlight_component_happy_path(session):
    result = highlight_component(session, refdes="R1")
    assert result["ok"] is True
    assert result["event"].type == "boardview.highlight"
    assert result["event"].refdes == ["R1"]
    assert "R1" in session.highlights


def test_highlight_component_accepts_list(session):
    result = highlight_component(session, refdes=["R1", "C1"])
    assert result["ok"] is True
    assert set(session.highlights) == {"R1", "C1"}


def test_highlight_component_invalid_refdes_returns_suggestions(session):
    result = highlight_component(session, refdes="R2")
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"
    assert "R1" in result["suggestions"]
    assert "R1" not in session.highlights  # state untouched


def test_highlight_component_additive(session):
    highlight_component(session, refdes="R1")
    highlight_component(session, refdes="C1", additive=True)
    assert session.highlights == {"R1", "C1"}


def test_highlight_component_non_additive_replaces(session):
    highlight_component(session, refdes="R1")
    highlight_component(session, refdes="C1", additive=False)
    assert session.highlights == {"C1"}
```

- [ ] **Step 2: Run tests**

Expected: ImportError on `api.tools.boardview`.

- [ ] **Step 3: Implement the handler**

Create `api/tools/boardview.py`:

```python
"""Tool handlers for the boardview panel — invoked by the agent via tool-use."""

from __future__ import annotations

from typing import Any

from api.board.validator import is_valid_refdes, suggest_similar
from api.session.state import SessionState
from api.tools.ws_events import Highlight


def _no_board(session: SessionState) -> dict[str, Any] | None:
    if session.board is None:
        return {"ok": False, "reason": "no-board-loaded", "suggestions": []}
    return None


def _unknown_refdes(session: SessionState, refdes: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "unknown-refdes",
        "suggestions": suggest_similar(session.board, refdes, k=3),
    }


def highlight_component(
    session: SessionState,
    *,
    refdes: str | list[str],
    color: str = "accent",
    additive: bool = False,
) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err

    targets = [refdes] if isinstance(refdes, str) else list(refdes)
    for r in targets:
        if not is_valid_refdes(session.board, r):
            return _unknown_refdes(session, r)

    if not additive:
        session.highlights = set()
    session.highlights.update(targets)

    event = Highlight(refdes=targets, color=color, additive=additive)
    summary = f"Highlighted {', '.join(targets)}."
    return {"ok": True, "summary": summary, "event": event}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_boardview_handlers.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/boardview.py tests/tools/test_boardview_handlers.py
git commit -m "feat(tools): highlight_component handler with refdes validation"
```

---

## Task 15: Tool handlers — focus_component, reset_view

**Files:**
- Modify: `api/tools/boardview.py`
- Modify: `tests/tools/test_boardview_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from api.tools.boardview import focus_component, reset_view


def test_focus_component_happy_path(session):
    result = focus_component(session, refdes="R1", zoom=2.5)
    assert result["ok"] is True
    ev = result["event"]
    assert ev.type == "boardview.focus"
    assert ev.refdes == "R1"
    assert ev.zoom == 2.5


def test_focus_component_auto_flips_to_other_side(session):
    # C1 is on bottom layer in the fixture. Session starts on "top".
    assert session.layer == "top"
    result = focus_component(session, refdes="C1")
    assert result["ok"] is True
    assert result["event"].auto_flipped is True
    assert session.layer == "bottom"


def test_focus_component_unknown(session):
    result = focus_component(session, refdes="UFOO")
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"


def test_reset_view_clears_everything(session):
    session.highlights.add("R1")
    session.net_highlight = "+3V3"
    session.dim_unrelated = True
    result = reset_view(session)
    assert result["ok"] is True
    assert session.highlights == set()
    assert session.net_highlight is None
    assert session.dim_unrelated is False
    assert result["event"].type == "boardview.reset_view"
```

- [ ] **Step 2: Run tests**

Expected: FAIL — handlers not defined.

- [ ] **Step 3: Implement the handlers (append to `api/tools/boardview.py`)**

Add to imports:
```python
from api.board.validator import resolve_part
from api.tools.ws_events import Focus, ResetView
```

Add handlers:

```python
def focus_component(session: SessionState, *, refdes: str, zoom: float = 2.5) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    part = resolve_part(session.board, refdes)
    if part is None:
        return _unknown_refdes(session, refdes)

    auto_flipped = False
    target_side = "top" if part.layer.value & 1 else "bottom"
    if session.layer != target_side:
        session.layer = target_side
        auto_flipped = True

    session.highlights = {refdes}

    bbox = ((part.bbox[0].x, part.bbox[0].y), (part.bbox[1].x, part.bbox[1].y))
    event = Focus(refdes=refdes, bbox=bbox, zoom=zoom, auto_flipped=auto_flipped)
    summary = f"Focused on {refdes} ({target_side})."
    return {"ok": True, "summary": summary, "event": event}


def reset_view(session: SessionState) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.highlights = set()
    session.net_highlight = None
    session.dim_unrelated = False
    session.annotations = {}
    session.arrows = {}
    session.filter_prefix = None
    return {"ok": True, "summary": "View reset.", "event": ResetView()}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_boardview_handlers.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/boardview.py tests/tools/test_boardview_handlers.py
git commit -m "feat(tools): focus_component with auto-flip + reset_view handler"
```

---

## Task 16: Tool handlers — highlight_net, flip_board

**Files:**
- Modify: `api/tools/boardview.py`
- Modify: `tests/tools/test_boardview_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from api.tools.boardview import flip_board, highlight_net


def test_highlight_net_happy_path(session):
    result = highlight_net(session, net="+3V3")
    assert result["ok"] is True
    ev = result["event"]
    assert ev.type == "boardview.highlight_net"
    assert ev.net == "+3V3"
    assert session.net_highlight == "+3V3"
    assert len(ev.pin_refs) >= 1


def test_highlight_net_unknown(session):
    result = highlight_net(session, net="MISSING")
    assert result["ok"] is False
    assert result["reason"] == "unknown-net"


def test_flip_board_toggles_side(session):
    assert session.layer == "top"
    result = flip_board(session)
    assert result["ok"] is True
    assert result["event"].new_side == "bottom"
    assert session.layer == "bottom"
    flip_board(session)
    assert session.layer == "top"
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Implement the handlers**

Add to imports:
```python
from api.board.validator import resolve_net
from api.tools.ws_events import Flip, HighlightNet
```

Add handlers:

```python
def highlight_net(session: SessionState, *, net: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    n = resolve_net(session.board, net)
    if n is None:
        return {"ok": False, "reason": "unknown-net", "suggestions": []}
    session.net_highlight = net
    event = HighlightNet(net=net, pin_refs=n.pin_refs)
    summary = f"Highlighted net {net} ({len(n.pin_refs)} pins)."
    return {"ok": True, "summary": summary, "event": event}


def flip_board(session: SessionState, *, preserve_cursor: bool = False) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.layer = "bottom" if session.layer == "top" else "top"
    event = Flip(new_side=session.layer, preserve_cursor=preserve_cursor)
    return {"ok": True, "summary": f"Flipped to {session.layer}.", "event": event}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_boardview_handlers.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/boardview.py tests/tools/test_boardview_handlers.py
git commit -m "feat(tools): highlight_net + flip_board handlers"
```

---

## Task 17: Tool handlers — annotate, filter_by_type

**Files:**
- Modify: `api/tools/boardview.py`
- Modify: `tests/tools/test_boardview_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from api.tools.boardview import annotate, filter_by_type


def test_annotate_adds_to_session(session):
    result = annotate(session, refdes="R1", label="Pull-up 10k")
    assert result["ok"] is True
    ann_id = result["event"].id
    assert ann_id in session.annotations
    assert session.annotations[ann_id]["label"] == "Pull-up 10k"


def test_annotate_invalid_refdes(session):
    result = annotate(session, refdes="UFOO", label="...")
    assert result["ok"] is False


def test_filter_by_type_sets_session(session):
    result = filter_by_type(session, prefix="R")
    assert result["ok"] is True
    assert result["event"].prefix == "R"
    assert session.filter_prefix == "R"


def test_filter_by_type_with_empty_prefix_clears(session):
    session.filter_prefix = "U"
    result = filter_by_type(session, prefix="")
    assert result["ok"] is True
    assert session.filter_prefix is None
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Implement the handlers**

Add imports:
```python
import uuid
from api.tools.ws_events import Annotate as AnnotateEvent, Filter
```

Add handlers:

```python
def annotate(session: SessionState, *, refdes: str, label: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    if not is_valid_refdes(session.board, refdes):
        return _unknown_refdes(session, refdes)
    ann_id = f"ann-{uuid.uuid4().hex[:8]}"
    session.annotations[ann_id] = {"refdes": refdes, "label": label}
    event = AnnotateEvent(refdes=refdes, label=label, id=ann_id)
    return {"ok": True, "summary": f"Annotated {refdes}.", "event": event}


def filter_by_type(session: SessionState, *, prefix: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.filter_prefix = prefix if prefix else None
    event = Filter(prefix=session.filter_prefix)
    return {"ok": True, "summary": f"Filter: {prefix or 'none'}.", "event": event}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_boardview_handlers.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/boardview.py tests/tools/test_boardview_handlers.py
git commit -m "feat(tools): annotate + filter_by_type handlers"
```

---

## Task 18: Tool handlers — draw_arrow, measure_distance, show_pin

**Files:**
- Modify: `api/tools/boardview.py`
- Modify: `tests/tools/test_boardview_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from api.tools.boardview import draw_arrow, measure_distance, show_pin


def test_draw_arrow_between_parts(session):
    result = draw_arrow(session, from_refdes="R1", to_refdes="C1")
    assert result["ok"] is True
    arrow_id = result["event"].id
    assert arrow_id in session.arrows


def test_measure_distance_returns_mm(session):
    result = measure_distance(session, refdes_a="R1", refdes_b="C1")
    assert result["ok"] is True
    # Pin coords in fixture: R1 at (100,100)-(100,200) centered 100,150
    # C1 at (400,100)-(400,200) centered 400,150
    # Distance = 300 mils = 7.62 mm
    assert 7.0 < result["event"].distance_mm < 8.0


def test_show_pin_happy_path(session):
    result = show_pin(session, refdes="R1", pin=1)
    assert result["ok"] is True
    ev = result["event"]
    assert ev.refdes == "R1"
    assert ev.pin == 1
    assert ev.pos == (100, 100)


def test_show_pin_unknown_pin(session):
    result = show_pin(session, refdes="R1", pin=99)
    assert result["ok"] is False
    assert result["reason"] == "unknown-pin"
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Implement the handlers**

Add imports:
```python
from api.board.validator import resolve_pin
from api.tools.ws_events import DrawArrow, Measure, ShowPin
```

Add handlers:

```python
def _part_center(part) -> tuple[int, int]:
    (a, b) = part.bbox
    return ((a.x + b.x) // 2, (a.y + b.y) // 2)


def draw_arrow(session: SessionState, *, from_refdes: str, to_refdes: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    a = resolve_part(session.board, from_refdes)
    b = resolve_part(session.board, to_refdes)
    if a is None:
        return _unknown_refdes(session, from_refdes)
    if b is None:
        return _unknown_refdes(session, to_refdes)
    arr_id = f"arr-{uuid.uuid4().hex[:8]}"
    frm = _part_center(a)
    to = _part_center(b)
    session.arrows[arr_id] = {"from": list(frm), "to": list(to)}
    event = DrawArrow(**{"from": frm, "to": to, "id": arr_id})
    return {
        "ok": True,
        "summary": f"Drew arrow from {from_refdes} to {to_refdes}.",
        "event": event,
    }


def measure_distance(session: SessionState, *, refdes_a: str, refdes_b: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    pa = resolve_part(session.board, refdes_a)
    pb = resolve_part(session.board, refdes_b)
    if pa is None:
        return _unknown_refdes(session, refdes_a)
    if pb is None:
        return _unknown_refdes(session, refdes_b)
    (ax, ay) = _part_center(pa)
    (bx, by) = _part_center(pb)
    dx_mils = ax - bx
    dy_mils = ay - by
    # 1 mil = 0.0254 mm
    distance_mm = round(((dx_mils**2 + dy_mils**2) ** 0.5) * 0.0254, 2)
    event = Measure(from_refdes=refdes_a, to_refdes=refdes_b, distance_mm=distance_mm)
    return {
        "ok": True,
        "summary": f"{refdes_a} ↔ {refdes_b}: {distance_mm} mm.",
        "event": event,
    }


def show_pin(session: SessionState, *, refdes: str, pin: int) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    if not is_valid_refdes(session.board, refdes):
        return _unknown_refdes(session, refdes)
    p = resolve_pin(session.board, refdes, pin)
    if p is None:
        return {"ok": False, "reason": "unknown-pin", "suggestions": []}
    event = ShowPin(refdes=refdes, pin=pin, pos=(p.pos.x, p.pos.y))
    return {"ok": True, "summary": f"{refdes}.{pin} at ({p.pos.x}, {p.pos.y}).", "event": event}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_boardview_handlers.py -v`
Expected: 20 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/boardview.py tests/tools/test_boardview_handlers.py
git commit -m "feat(tools): draw_arrow + measure_distance + show_pin handlers"
```

---

## Task 19: Tool handlers — dim_unrelated, layer_visibility

**Files:**
- Modify: `api/tools/boardview.py`
- Modify: `tests/tools/test_boardview_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from api.tools.boardview import dim_unrelated, layer_visibility


def test_dim_unrelated_toggles(session):
    session.highlights.add("R1")
    result = dim_unrelated(session)
    assert result["ok"] is True
    assert session.dim_unrelated is True


def test_layer_visibility_toggles(session):
    result = layer_visibility(session, layer="top", visible=False)
    assert result["ok"] is True
    assert session.layer_visibility["top"] is False
    assert session.layer_visibility["bottom"] is True
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Implement**

Add imports:
```python
from api.tools.ws_events import DimUnrelated, LayerVisibility
```

Add handlers:

```python
def dim_unrelated(session: SessionState) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.dim_unrelated = True
    return {"ok": True, "summary": "Dimmed unrelated components.", "event": DimUnrelated()}


def layer_visibility(session: SessionState, *, layer: str, visible: bool) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    if layer not in ("top", "bottom"):
        return {"ok": False, "reason": "invalid-layer", "suggestions": ["top", "bottom"]}
    session.layer_visibility[layer] = visible  # type: ignore[index]
    event = LayerVisibility(layer=layer, visible=visible)  # type: ignore[arg-type]
    return {"ok": True, "summary": f"Layer {layer} visible={visible}.", "event": event}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_boardview_handlers.py -v`
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/boardview.py tests/tools/test_boardview_handlers.py
git commit -m "feat(tools): dim_unrelated + layer_visibility handlers"
```

---

## Task 20: Anthropic tool schemas for the agent

**Files:**
- Create: `api/tools/schemas.py`
- Create: `tests/tools/test_schemas.py`

**Context:** The agent loop (spec separately) needs JSON Schemas matching the Anthropic tool-use format. We export them here so the loop can pass `tools=[...]` to the SDK.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_schemas.py`:

```python
from api.tools.schemas import BOARDVIEW_TOOL_SCHEMAS


def test_all_twelve_tools_are_exported():
    names = {t["name"] for t in BOARDVIEW_TOOL_SCHEMAS}
    assert names == {
        "highlight_component",
        "focus_component",
        "reset_view",
        "highlight_net",
        "flip_board",
        "annotate",
        "filter_by_type",
        "draw_arrow",
        "measure_distance",
        "show_pin",
        "dim_unrelated",
        "layer_visibility",
    }


def test_schema_shape_is_anthropic_compatible():
    for tool in BOARDVIEW_TOOL_SCHEMAS:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement the schemas**

Create `api/tools/schemas.py`:

```python
"""JSON Schemas exposing the boardview tools to the Anthropic tool-use API."""

from __future__ import annotations

BOARDVIEW_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "highlight_component",
        "description": (
            "Highlight one or more components on the boardview. "
            "Use when the user asks to find, show, or draw attention to a part by refdes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Refdes (e.g. 'U7') or list of refdes.",
                },
                "color": {
                    "type": "string",
                    "enum": ["accent", "warn", "mute"],
                    "default": "accent",
                },
                "additive": {"type": "boolean", "default": False},
            },
            "required": ["refdes"],
        },
    },
    {
        "name": "focus_component",
        "description": "Pan and zoom the boardview to center a single component. Auto-flips layer if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "zoom": {"type": "number", "default": 2.5, "minimum": 0.1, "maximum": 20},
            },
            "required": ["refdes"],
        },
    },
    {
        "name": "reset_view",
        "description": "Clear all highlights, annotations, filters, and reset pan/zoom.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "highlight_net",
        "description": "Highlight every pin belonging to a given net (e.g. '+3V3', 'GND').",
        "input_schema": {
            "type": "object",
            "properties": {"net": {"type": "string"}},
            "required": ["net"],
        },
    },
    {
        "name": "flip_board",
        "description": "Toggle between top and bottom layers.",
        "input_schema": {
            "type": "object",
            "properties": {"preserve_cursor": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "annotate",
        "description": "Add a small floating label near a component.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "label": {"type": "string", "maxLength": 200},
            },
            "required": ["refdes", "label"],
        },
    },
    {
        "name": "filter_by_type",
        "description": "Show only components whose refdes starts with the given prefix (e.g. 'U', 'C'). Empty prefix clears the filter.",
        "input_schema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
            "required": ["prefix"],
        },
    },
    {
        "name": "draw_arrow",
        "description": "Draw an arrow between the centers of two components.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_refdes": {"type": "string"},
                "to_refdes": {"type": "string"},
            },
            "required": ["from_refdes", "to_refdes"],
        },
    },
    {
        "name": "measure_distance",
        "description": "Report the physical distance in millimeters between two component centers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes_a": {"type": "string"},
                "refdes_b": {"type": "string"},
            },
            "required": ["refdes_a", "refdes_b"],
        },
    },
    {
        "name": "show_pin",
        "description": "Zoom to a specific pin (by component refdes + 1-based pin index).",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "pin": {"type": "integer", "minimum": 1},
            },
            "required": ["refdes", "pin"],
        },
    },
    {
        "name": "dim_unrelated",
        "description": "Dim all components not currently highlighted, so the focus stands out.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "layer_visibility",
        "description": "Show or hide a given layer (top or bottom).",
        "input_schema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["top", "bottom"]},
                "visible": {"type": "boolean"},
            },
            "required": ["layer", "visible"],
        },
    },
]
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/tools/test_schemas.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/schemas.py tests/tools/test_schemas.py
git commit -m "feat(tools): add Anthropic tool-use schemas for 12 boardview verbs"
```

---

## Task 21: Boot-time Pi 4 preload — backend pipeline

**Files:**
- Create: `api/board/pipeline.py`
- Create: `board_assets/.gitkeep`
- Create: `tests/board/test_pipeline.py`

**Context:** `pipeline.load_and_register` parses a file, builds a Board, swaps it into the given SessionState, and publishes `board:loaded` on the bus. Used by both boot-preload and the drag-drop handler.

- [ ] **Step 1: Create placeholder gitkeep**

```bash
mkdir -p board_assets
touch board_assets/.gitkeep
```

- [ ] **Step 2: Write the failing test**

Create `tests/board/test_pipeline.py`:

```python
from pathlib import Path

from api.board.events import BoardEventBus
from api.board.pipeline import load_and_register
from api.session.state import SessionState

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_load_and_register_swaps_board_and_publishes():
    s = SessionState()
    bus = BoardEventBus()
    received: list[dict] = []
    bus.subscribe("board:loaded", received.append)

    load_and_register(FIXTURE_DIR / "minimal.brd", session=s, bus=bus, known_hashes=set())

    assert s.board is not None
    assert s.board.board_id == "minimal"
    assert len(received) == 1
    assert received[0]["board_id"] == "minimal"
    assert received[0]["is_known"] is False


def test_load_and_register_marks_known_hash():
    s = SessionState()
    bus = BoardEventBus()
    # pre-parse once to know its hash
    from api.board.parser.brd import BRDParser

    hash_ = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd").file_hash

    received: list[dict] = []
    bus.subscribe("board:loaded", received.append)
    load_and_register(
        FIXTURE_DIR / "minimal.brd", session=s, bus=bus, known_hashes={hash_}
    )
    assert received[0]["is_known"] is True
```

- [ ] **Step 3: Run tests**

Expected: ImportError.

- [ ] **Step 4: Implement the pipeline**

Create `api/board/pipeline.py`:

```python
"""Orchestrates parser → session swap → event publish."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from api.board.events import BoardEventBus
from api.board.parser.base import parser_for
from api.session.state import SessionState


def load_and_register(
    path: Path,
    *,
    session: SessionState,
    bus: BoardEventBus,
    known_hashes: Iterable[str],
) -> None:
    """Parse, swap into session, and publish board:loaded."""
    parser = parser_for(path)
    board = parser.parse_file(path)
    session.set_board(board)

    known_set = set(known_hashes)
    bus.publish(
        "board:loaded",
        {
            "board_id": board.board_id,
            "file_hash": board.file_hash,
            "parts_count": len(board.parts),
            "is_known": board.file_hash in known_set,
        },
    )
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/board/test_pipeline.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add api/board/pipeline.py board_assets/.gitkeep tests/board/test_pipeline.py
git commit -m "feat(board): load_and_register pipeline (parse → session → event bus)"
```

---

## Task 22: FastAPI app skeleton with WebSocket + boot preload

**Files:**
- Create: `api/main.py`
- Modify: `tests/test_health.py` (ensure it passes after app creation)
- Modify: `tests/test_websocket.py`

**Context:** This is the entry point `uvicorn api.main:app` spins up. Serves the frontend static files, exposes `/ws`, and during lifespan preloads the Pi 4 board if the file is present.

- [ ] **Step 1: Read existing test stubs**

Read `tests/test_health.py` and `tests/test_websocket.py` to know what shape they expect. (They were created as placeholders in the hackathon starter.)

- [ ] **Step 2: Write/adjust failing tests as needed**

If the test files are bare stubs, replace them with:

```python
# tests/test_health.py
from fastapi.testclient import TestClient

from api.main import app


def test_health_endpoint_returns_ok():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
```

```python
# tests/test_websocket.py
from fastapi.testclient import TestClient

from api.main import app


def test_websocket_connects_and_echoes_hello():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello"})
            msg = ws.receive_json()
            assert msg.get("type") in ("boardview.board_loaded", "ack")
```

- [ ] **Step 3: Run tests**

Run: `.venv/bin/pytest tests/test_health.py tests/test_websocket.py -v`
Expected: FAIL on missing `api.main:app`.

- [ ] **Step 4: Implement `api/main.py`**

```python
"""FastAPI entry point for microsolder-agent workbench."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.board.events import BoardEventBus
from api.board.pipeline import load_and_register
from api.session.state import SessionState

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "board_assets"


class AppState:
    """Global app state — single-session for the hackathon."""
    def __init__(self) -> None:
        self.session = SessionState()
        self.bus = BoardEventBus()
        self.known_hashes: set[str] = set()
        self.ws_clients: set[WebSocket] = set()


app_state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pi = ASSETS_DIR / "raspberry-pi-4b.brd"
    if pi.exists():
        load_and_register(pi, session=app_state.session, bus=app_state.bus,
                          known_hashes=app_state.known_hashes)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    app_state.ws_clients.add(ws)
    try:
        # On connect, if a board is loaded, push the initial board_loaded event
        if app_state.session.board is not None:
            await ws.send_json({"type": "ack", "connected": True})
        while True:
            msg = await ws.receive_json()
            # message routing is wired in Task 28 (upload) and Task 29 (user events)
            await ws.send_json({"type": "ack", "echo": msg.get("type")})
    except WebSocketDisconnect:
        app_state.ws_clients.discard(ws)
```

Also adjust `/` in `index.html` references: change `<link rel="stylesheet" href="/style.css" />` and `<script src="/app.js"></script>` to `/static/style.css` and `/static/app.js` respectively. Edit `web/index.html`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_health.py tests/test_websocket.py -v`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add api/main.py web/index.html tests/test_health.py tests/test_websocket.py
git commit -m "feat(api): FastAPI app with lifespan preload + /ws endpoint"
```

---

## Task 23: WS router — dispatch boardview.* frontend messages to handlers

**Files:**
- Create: `api/ws_router.py`
- Modify: `api/main.py`
- Create: `tests/test_ws_router.py`

**Context:** Centralize the dispatch from incoming WS messages to tool handlers. Keeps `main.py` thin and testable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ws_router.py`:

```python
from pathlib import Path

from api.board.parser.brd import BRDParser
from api.session.state import SessionState
from api.ws_router import dispatch_message

FIXTURE_DIR = Path(__file__).parent / "board" / "fixtures"


def _session_with_board():
    s = SessionState()
    s.set_board(BRDParser().parse_file(FIXTURE_DIR / "minimal.brd"))
    return s


def test_click_part_returns_ack(_session_with_board=_session_with_board):
    s = _session_with_board()
    result = dispatch_message(s, {"type": "boardview.click_part", "refdes": "R1"})
    assert result["ok"] is True
    assert result["echo"] == "boardview.click_part"


def test_unknown_type_returns_error():
    s = SessionState()
    result = dispatch_message(s, {"type": "unknown.verb"})
    assert result["ok"] is False
    assert result["reason"] == "unknown-type"
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement**

Create `api/ws_router.py`:

```python
"""Dispatch incoming WS messages to the right tool handler."""

from __future__ import annotations

from typing import Any

from api.session.state import SessionState


def dispatch_message(session: SessionState, msg: dict) -> dict[str, Any]:
    t = msg.get("type", "")
    # User-input events — informational (logged, no state change here for MVP)
    if t in ("boardview.click_part", "boardview.click_pin", "boardview.hover"):
        return {"ok": True, "echo": t}
    # Upload — handled in Task 28
    if t == "boardview.upload":
        return {"ok": False, "reason": "upload-not-wired-yet"}
    return {"ok": False, "reason": "unknown-type"}
```

Modify `api/main.py` — replace the body of the `while True:` loop:

```python
from api.ws_router import dispatch_message

# ...
while True:
    msg = await ws.receive_json()
    reply = dispatch_message(app_state.session, msg)
    await ws.send_json({"type": "ack", **reply})
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_ws_router.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add api/ws_router.py api/main.py tests/test_ws_router.py
git commit -m "feat(api): WS router dispatch for boardview messages"
```

---

## Task 24: Drag-drop upload endpoint

**Files:**
- Modify: `api/ws_router.py`
- Modify: `tests/test_ws_router.py`

**Context:** The frontend sends `{type: "boardview.upload", filename, content_b64}`. We decode, write to a temp file, parse via `parser_for`, swap into the session. On failure, return a `boardview.upload_error` envelope.

- [ ] **Step 1: Write the failing test (append)**

```python
import base64
from api.board.events import BoardEventBus
from api.board.parser.brd import BRDParser


MINIMAL_BRD_CONTENT = (FIXTURE_DIR / "minimal.brd").read_bytes()


def test_upload_happy_path_swaps_board():
    s = SessionState()
    bus = BoardEventBus()
    received: list[dict] = []
    bus.subscribe("board:loaded", received.append)

    msg = {
        "type": "boardview.upload",
        "filename": "minimal.brd",
        "content_b64": base64.b64encode(MINIMAL_BRD_CONTENT).decode("ascii"),
    }
    result = dispatch_message(s, msg, bus=bus, known_hashes=set())
    assert result["ok"] is True
    assert s.board is not None
    assert s.board.board_id == "minimal"
    assert len(received) == 1


def test_upload_obfuscated_returns_error():
    s = SessionState()
    obf = b"\x23\xe2\x63\x28" + b"\x00" * 64
    msg = {
        "type": "boardview.upload",
        "filename": "obf.brd",
        "content_b64": base64.b64encode(obf).decode("ascii"),
    }
    result = dispatch_message(s, msg, bus=BoardEventBus(), known_hashes=set())
    assert result["ok"] is False
    assert result["reason"] == "obfuscated"


def test_upload_unsupported_extension():
    s = SessionState()
    msg = {
        "type": "boardview.upload",
        "filename": "nope.xyz",
        "content_b64": base64.b64encode(b"...").decode("ascii"),
    }
    result = dispatch_message(s, msg, bus=BoardEventBus(), known_hashes=set())
    assert result["ok"] is False
    assert result["reason"] == "unsupported-format"
```

- [ ] **Step 2: Run tests**

Expected: FAIL — dispatch_message signature mismatch.

- [ ] **Step 3: Extend `dispatch_message` to accept bus + known_hashes, and handle upload**

Modify `api/ws_router.py`:

```python
"""Dispatch incoming WS messages to the right tool handler."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any

from api.board.events import BoardEventBus
from api.board.parser.base import (
    ObfuscatedFileError,
    UnsupportedFormatError,
    InvalidBoardFile,
)
from api.board.pipeline import load_and_register
from api.session.state import SessionState


def dispatch_message(
    session: SessionState,
    msg: dict,
    *,
    bus: BoardEventBus | None = None,
    known_hashes: set[str] | None = None,
) -> dict[str, Any]:
    t = msg.get("type", "")

    if t in ("boardview.click_part", "boardview.click_pin", "boardview.hover"):
        return {"ok": True, "echo": t}

    if t == "boardview.upload":
        if bus is None:
            return {"ok": False, "reason": "server-misconfigured", "message": "bus missing"}
        return _handle_upload(session, msg, bus=bus, known_hashes=known_hashes or set())

    return {"ok": False, "reason": "unknown-type"}


def _handle_upload(
    session: SessionState,
    msg: dict,
    *,
    bus: BoardEventBus,
    known_hashes: set[str],
) -> dict[str, Any]:
    filename = msg.get("filename") or "unknown.bin"
    b64 = msg.get("content_b64") or ""
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return {"ok": False, "reason": "io-error", "message": "invalid base64"}

    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as f:
        tmp_path = Path(f.name)
        f.write(raw)

    try:
        load_and_register(tmp_path, session=session, bus=bus, known_hashes=known_hashes)
    except UnsupportedFormatError:
        return {"ok": False, "reason": "unsupported-format", "message": f"extension not supported"}
    except ObfuscatedFileError:
        return {"ok": False, "reason": "obfuscated", "message": "refused — OBV-obfuscated file"}
    except InvalidBoardFile as e:
        return {"ok": False, "reason": "malformed-header", "message": str(e)}
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"ok": True, "board_id": session.board.board_id}
```

Update `api/main.py` — pass bus + known_hashes:

```python
reply = dispatch_message(
    app_state.session, msg,
    bus=app_state.bus, known_hashes=app_state.known_hashes,
)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_ws_router.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add api/ws_router.py api/main.py tests/test_ws_router.py
git commit -m "feat(api): boardview.upload endpoint with structured error taxonomy"
```

---

## Task 25: Serialize `Board` into `boardview.board_loaded` event on connect and swap

**Files:**
- Modify: `api/main.py`
- Modify: `api/tools/ws_events.py` (add factory helper)
- Create: `tests/tools/test_board_loaded.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_board_loaded.py`:

```python
from pathlib import Path

from api.board.parser.brd import BRDParser
from api.tools.ws_events import board_loaded_from

FIXTURE_DIR = Path(__file__).parent.parent / "board" / "fixtures"


def test_board_loaded_serialization():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    ev = board_loaded_from(board)
    dumped = ev.model_dump()
    assert dumped["type"] == "boardview.board_loaded"
    assert dumped["parts_count"] == 2
    assert len(dumped["outline"]) == 4
    assert len(dumped["parts"]) == 2
    assert len(dumped["pins"]) == 4
    assert len(dumped["nets"]) == 2
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Add the factory**

Append to `api/tools/ws_events.py`:

```python
def board_loaded_from(board) -> "BoardLoaded":
    """Factory: Board model → BoardLoaded WS envelope."""
    return BoardLoaded(
        board_id=board.board_id,
        file_hash=board.file_hash,
        parts_count=len(board.parts),
        outline=[(p.x, p.y) for p in board.outline],
        parts=[
            {
                "refdes": pt.refdes,
                "layer": pt.layer.value,
                "is_smd": pt.is_smd,
                "bbox": [[pt.bbox[0].x, pt.bbox[0].y], [pt.bbox[1].x, pt.bbox[1].y]],
                "pin_refs": pt.pin_refs,
            }
            for pt in board.parts
        ],
        pins=[
            {
                "part_refdes": pin.part_refdes,
                "index": pin.index,
                "pos": [pin.pos.x, pin.pos.y],
                "net": pin.net,
                "probe": pin.probe,
                "layer": pin.layer.value,
            }
            for pin in board.pins
        ],
        nets=[
            {
                "name": n.name,
                "pin_refs": n.pin_refs,
                "is_power": n.is_power,
                "is_ground": n.is_ground,
            }
            for n in board.nets
        ],
    )
```

- [ ] **Step 4: Push the event on WS connect and after upload**

Modify `api/main.py`. In `ws_endpoint`:

```python
from api.tools.ws_events import board_loaded_from

# ...
await ws.accept()
app_state.ws_clients.add(ws)
try:
    if app_state.session.board is not None:
        ev = board_loaded_from(app_state.session.board)
        await ws.send_json(ev.model_dump())
    while True:
        msg = await ws.receive_json()
        reply = dispatch_message(
            app_state.session, msg,
            bus=app_state.bus, known_hashes=app_state.known_hashes,
        )
        await ws.send_json({"type": "ack", **reply})
        # After a successful upload, broadcast the new board_loaded
        if msg.get("type") == "boardview.upload" and reply.get("ok"):
            ev = board_loaded_from(app_state.session.board)
            for client in list(app_state.ws_clients):
                await client.send_json(ev.model_dump())
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/tools/test_board_loaded.py -v`
Expected: 1 passed.
Run the full suite: `make test` → all green.

- [ ] **Step 6: Commit**

```bash
git add api/main.py api/tools/ws_events.py tests/tools/test_board_loaded.py
git commit -m "feat(api): broadcast boardview.board_loaded on connect and upload"
```

---

## Task 26: Frontend — colors module (palette P2)

**Files:**
- Create: `web/boardview/colors.js`

**Context:** Single source of truth for the P2 semantic rainbow palette used by the renderer.

- [ ] **Step 1: Create the file**

Create `web/boardview/colors.js`:

```javascript
// Boardview Premium Dark — palette P2 (semantic rainbow).
// Constants only ; no logic.

export const COLORS = {
  bg_top: "#142030",
  bg_bot: "#05080e",
  pcb_outline: "rgba(64, 224, 208, 0.55)",
  mounting: "rgba(64, 224, 208, 0.70)",

  part_fill_top: "#1d2b3d",
  part_fill_bot: "#111a26",
  part_border: "#2d4258",
  part_text: "#a9c2dc",

  part_highlight_border: "#40e0d0",
  part_highlight_fill_top: "#1d3a3a",
  part_highlight_fill_bot: "#0c2323",
  part_highlight_text: "#9ff3e8",
  part_highlight_glow: "rgba(64,224,208,0.55)",

  pin_default: "#6b8eb0",
  pin_gnd: "#6b7280",
  pin_power: "#6ee7a7",
  pin_signal: "#7dd3fc",
  pin_testpad: "#c084fc",
  pin_nc: "#f43f5e",
  pin_selected_fill: "#ffffff",
  pin_selected_border: "#40e0d0",
  pin_same_net: "#6ee7a7",

  net_web: "rgba(64, 224, 208, 0.4)",

  annotation: "#fbbf24",
  arrow: "#fbbf24",

  dimmed_opacity: 0.25,
};

export function pinColor(pin, net, highlightedNet) {
  // Priority : selected > same_net > net_highlight > type > default
  if (highlightedNet && net && net.name === highlightedNet) {
    return COLORS.pin_same_net;
  }
  if (net && net.is_ground) return COLORS.pin_gnd;
  if (net && net.is_power) return COLORS.pin_power;
  if (pin.probe) return COLORS.pin_testpad;
  if (!pin.net) return COLORS.pin_nc;
  return COLORS.pin_signal;
}
```

- [ ] **Step 2: Commit**

No test (constants-only JS module).

```bash
git add web/boardview/colors.js
git commit -m "feat(web): add P2 semantic rainbow palette constants + pinColor()"
```

---

## Task 27: Frontend — store with event applier

**Files:**
- Create: `web/boardview/store.js`

**Context:** Single source of truth on the frontend. Receives WS events, applies them to state, notifies subscribers (the renderer).

- [ ] **Step 1: Create the store**

Create `web/boardview/store.js`:

```javascript
// Frontend store — normalized boardview state.
// apply(event) mutates state ; notifies subscribers via a dirty flag.

export function createStore() {
  const state = {
    board: null,            // { board_id, outline, parts, pins, nets }
    layer: "top",
    pan: { x: 0, y: 0 },
    zoom: 1,
    highlights: new Set(),  // refdes currently highlighted
    highlightNet: null,     // name of highlighted net or null
    focusPin: null,         // { refdes, pin, pos } or null
    annotations: {},        // id → { refdes, label }
    arrows: {},             // id → { from:[x,y], to:[x,y] }
    measure: null,          // { from_refdes, to_refdes, distance_mm } or null
    dimUnrelated: false,
    layerVisibility: { top: true, bottom: true },
    filterPrefix: null,
  };

  const subscribers = [];
  const notify = () => subscribers.forEach((fn) => fn(state));

  function apply(ev) {
    switch (ev.type) {
      case "boardview.board_loaded":
        state.board = {
          board_id: ev.board_id,
          file_hash: ev.file_hash,
          outline: ev.outline,
          parts: ev.parts,
          pins: ev.pins,
          nets: ev.nets,
        };
        state.layer = "top";
        state.pan = { x: 0, y: 0 };
        state.zoom = 1;
        state.highlights = new Set();
        state.highlightNet = null;
        state.focusPin = null;
        state.annotations = {};
        state.arrows = {};
        state.measure = null;
        state.dimUnrelated = false;
        state.filterPrefix = null;
        break;
      case "boardview.highlight":
        if (!ev.additive) state.highlights = new Set();
        ev.refdes.forEach((r) => state.highlights.add(r));
        break;
      case "boardview.highlight_net":
        state.highlightNet = ev.net;
        break;
      case "boardview.focus":
        state.highlights = new Set([ev.refdes]);
        if (ev.auto_flipped) state.layer = state.layer === "top" ? "bottom" : "top";
        break;
      case "boardview.flip":
        state.layer = ev.new_side;
        break;
      case "boardview.annotate":
        state.annotations[ev.id] = { refdes: ev.refdes, label: ev.label };
        break;
      case "boardview.reset_view":
        state.highlights = new Set();
        state.highlightNet = null;
        state.focusPin = null;
        state.annotations = {};
        state.arrows = {};
        state.measure = null;
        state.dimUnrelated = false;
        state.filterPrefix = null;
        break;
      case "boardview.dim_unrelated":
        state.dimUnrelated = true;
        break;
      case "boardview.layer_visibility":
        state.layerVisibility[ev.layer] = ev.visible;
        break;
      case "boardview.filter":
        state.filterPrefix = ev.prefix || null;
        break;
      case "boardview.draw_arrow":
        state.arrows[ev.id] = { from: ev.from, to: ev.to };
        break;
      case "boardview.measure":
        state.measure = { from_refdes: ev.from_refdes, to_refdes: ev.to_refdes, distance_mm: ev.distance_mm };
        break;
      case "boardview.show_pin":
        state.focusPin = { refdes: ev.refdes, pin: ev.pin, pos: ev.pos };
        break;
      case "boardview.upload_error":
        console.warn("Upload error:", ev);
        break;
      default:
        // unknown types are ignored at the boardview layer
        break;
    }
    notify();
  }

  function subscribe(fn) {
    subscribers.push(fn);
    return () => {
      const idx = subscribers.indexOf(fn);
      if (idx >= 0) subscribers.splice(idx, 1);
    };
  }

  return { state, apply, subscribe };
}
```

- [ ] **Step 2: Commit**

```bash
git add web/boardview/store.js
git commit -m "feat(web): boardview store with apply(event) dispatcher"
```

---

## Task 28: Frontend — renderer skeleton (Canvas init, outline, parts)

**Files:**
- Create: `web/boardview/renderer.js`

**Context:** Core Canvas 2D renderer. Start with: mount canvas, compute world→screen transform, draw outline + parts. Pan/zoom and pins come in Tasks 29-30.

- [ ] **Step 1: Create the renderer**

Create `web/boardview/renderer.js`:

```javascript
import { COLORS, pinColor } from "./colors.js";

export function createRenderer(canvas, store) {
  const ctx = canvas.getContext("2d");
  let dirty = true;
  const netByName = new Map();

  // Fit canvas to its CSS pixel size
  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr;
    canvas.height = r.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    dirty = true;
  }
  window.addEventListener("resize", resize);
  resize();

  store.subscribe(() => {
    const st = store.state;
    if (st.board) {
      netByName.clear();
      st.board.nets.forEach((n) => netByName.set(n.name, n));
    }
    dirty = true;
  });

  function worldToScreen(x, y) {
    const st = store.state;
    const r = canvas.getBoundingClientRect();
    // fit-to-viewport + user pan/zoom + layer mirror
    const { sx, sy, scale } = computeFit(st, r.width, r.height);
    const mirror = st.layer === "bottom" ? -1 : 1;
    return {
      x: sx + (x * mirror + st.pan.x) * scale * st.zoom,
      y: sy + (y + st.pan.y) * scale * st.zoom,
    };
  }

  function computeFit(st, w, h) {
    if (!st.board || st.board.outline.length === 0) {
      return { sx: w / 2, sy: h / 2, scale: 1 };
    }
    const xs = st.board.outline.map((p) => p[0]);
    const ys = st.board.outline.map((p) => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const bw = maxX - minX || 1;
    const bh = maxY - minY || 1;
    const scale = Math.min(w / bw, h / bh) * 0.92;  // 8% margin
    return {
      sx: w / 2 - ((minX + maxX) / 2) * scale,
      sy: h / 2 - ((minY + maxY) / 2) * scale,
      scale,
    };
  }

  function draw() {
    const st = store.state;
    const r = canvas.getBoundingClientRect();
    const grad = ctx.createRadialGradient(r.width * 0.4, r.height * 0.4, 0, r.width / 2, r.height / 2, Math.max(r.width, r.height) * 0.7);
    grad.addColorStop(0, COLORS.bg_top);
    grad.addColorStop(1, COLORS.bg_bot);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, r.width, r.height);

    if (!st.board) return;

    // PCB outline
    ctx.beginPath();
    st.board.outline.forEach((p, i) => {
      const { x, y } = worldToScreen(p[0], p[1]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.strokeStyle = COLORS.pcb_outline;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Parts
    st.board.parts.forEach((part) => {
      // Layer visibility
      const layerName = part.layer === 1 ? "top" : (part.layer === 2 ? "bottom" : "both");
      if (layerName !== "both" && !st.layerVisibility[layerName]) return;
      if (st.layer !== layerName && layerName !== "both") {
        ctx.globalAlpha = 0.15;  // ghost the other side
      } else {
        ctx.globalAlpha = 1;
      }

      // Filter
      if (st.filterPrefix && !part.refdes.startsWith(st.filterPrefix)) {
        ctx.globalAlpha *= 0.15;
      }

      const highlighted = st.highlights.has(part.refdes);
      const dimmed = st.dimUnrelated && !highlighted;
      if (dimmed) ctx.globalAlpha *= COLORS.dimmed_opacity;

      const [[x1, y1], [x2, y2]] = part.bbox;
      const p1 = worldToScreen(x1, y1);
      const p2 = worldToScreen(x2, y2);
      const w = p2.x - p1.x;
      const h = p2.y - p1.y;

      if (highlighted) {
        ctx.shadowColor = COLORS.part_highlight_glow;
        ctx.shadowBlur = 18;
      } else {
        ctx.shadowBlur = 0;
      }
      ctx.fillStyle = highlighted ? COLORS.part_highlight_fill_top : COLORS.part_fill_top;
      ctx.strokeStyle = highlighted ? COLORS.part_highlight_border : COLORS.part_border;
      ctx.lineWidth = highlighted ? 2 : 1;
      ctx.fillRect(p1.x, p1.y, w, h);
      ctx.strokeRect(p1.x, p1.y, w, h);
      ctx.shadowBlur = 0;

      if (Math.abs(w) > 20 && Math.abs(h) > 12) {
        ctx.fillStyle = highlighted ? COLORS.part_highlight_text : COLORS.part_text;
        ctx.font = "10px 'JetBrains Mono', monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(part.refdes, p1.x + w / 2, p1.y + h / 2);
      }
    });

    ctx.globalAlpha = 1;
  }

  function loop() {
    if (dirty) {
      draw();
      dirty = false;
    }
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);

  return { redraw: () => { dirty = true; }, worldToScreen };
}
```

- [ ] **Step 2: Commit**

```bash
git add web/boardview/renderer.js
git commit -m "feat(web): renderer skeleton — canvas init, outline + parts draw"
```

---

## Task 29: Frontend — pan/zoom (wheel cursor-anchored + drag + keyboard)

**Files:**
- Modify: `web/boardview/renderer.js`

- [ ] **Step 1: Extend renderer with input handlers**

Append to `createRenderer()` in `web/boardview/renderer.js`, **before** `requestAnimationFrame(loop);` :

```javascript
// --- pan / zoom / keyboard input ---

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const st = store.state;
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left;
  const my = e.clientY - r.top;

  const prevZoom = st.zoom;
  const factor = Math.pow(2, -e.deltaY * 0.002);
  const newZoom = Math.min(80, Math.max(0.1, prevZoom * factor));
  if (newZoom === prevZoom) return;

  // Cursor-anchored zoom : keep the world point under the cursor fixed.
  // worldToScreen(wx, wy) = sx + (wx*mirror + pan.x) * scale * zoom (for x)
  // solving for wx at prevZoom, then recomputing pan at newZoom:
  const { sx, sy, scale } = computeFit(st, r.width, r.height);
  const mirror = st.layer === "bottom" ? -1 : 1;
  const wx = ((mx - sx) / (scale * prevZoom) - st.pan.x) * mirror;
  const wy = (my - sy) / (scale * prevZoom) - st.pan.y;
  st.zoom = newZoom;
  st.pan.x = (mx - sx) / (scale * newZoom) - wx * mirror;
  st.pan.y = (my - sy) / (scale * newZoom) - wy;
  dirty = true;
}, { passive: false });

let dragging = false;
let lastX = 0, lastY = 0;
canvas.addEventListener("mousedown", (e) => {
  dragging = true;
  lastX = e.clientX;
  lastY = e.clientY;
});
window.addEventListener("mouseup", () => { dragging = false; });
window.addEventListener("mousemove", (e) => {
  if (!dragging) return;
  const dx = e.clientX - lastX;
  const dy = e.clientY - lastY;
  lastX = e.clientX; lastY = e.clientY;
  const st = store.state;
  const r = canvas.getBoundingClientRect();
  const { scale } = computeFit(st, r.width, r.height);
  const mirror = st.layer === "bottom" ? -1 : 1;
  st.pan.x += (dx / (scale * st.zoom)) * mirror;
  st.pan.y += dy / (scale * st.zoom);
  dirty = true;
});

window.addEventListener("keydown", (e) => {
  if (document.activeElement?.tagName === "INPUT") return;  // don't steal chat input
  const st = store.state;
  const step = 30 / st.zoom;
  switch (e.key.toLowerCase()) {
    case "w": st.pan.y += step; dirty = true; break;
    case "s": st.pan.y -= step; dirty = true; break;
    case "a": st.pan.x += step; dirty = true; break;
    case "d": st.pan.x -= step; dirty = true; break;
    case "r": st.pan = { x: 0, y: 0 }; st.zoom = 1; dirty = true; break;
    case "f":
    case " ":
      st.layer = st.layer === "top" ? "bottom" : "top";
      dirty = true;
      e.preventDefault();
      break;
    case "escape":
      st.highlights = new Set();
      st.highlightNet = null;
      dirty = true;
      break;
  }
});
```

- [ ] **Step 2: Manually verify in the browser**

Run: `make run`
Open `http://localhost:8000`.
Expected: Pi 4 outline visible (once preload is wired in Task 32), wheel zooms under cursor, drag pans, WASD pan, F flips, R resets, Escape clears.

- [ ] **Step 3: Commit**

```bash
git add web/boardview/renderer.js
git commit -m "feat(web): cursor-anchored wheel zoom, drag pan, keyboard shortcuts"
```

---

## Task 30: Frontend — draw pins with semantic colors + net highlight

**Files:**
- Modify: `web/boardview/renderer.js`

- [ ] **Step 1: Draw pins after parts**

In the `draw()` function of `renderer.js`, **after** the parts loop and before `ctx.globalAlpha = 1;` at the end, add:

```javascript
// Pins (skip if zoomed out too far, LOD)
if (st.zoom > 0.4 && st.board.pins) {
  const hn = st.highlightNet;
  st.board.pins.forEach((pin) => {
    const layerName = pin.layer === 1 ? "top" : "bottom";
    if (layerName !== st.layer) return;
    if (!st.layerVisibility[layerName]) return;

    const net = pin.net ? netByName.get(pin.net) : null;
    const isActiveNet = hn && pin.net === hn;

    const { x, y } = worldToScreen(pin.pos[0], pin.pos[1]);
    const r = Math.max(1.5, 2.5 * st.zoom);

    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = pinColor(pin, net, hn);
    if (isActiveNet) {
      ctx.shadowColor = COLORS.pin_same_net;
      ctx.shadowBlur = 8;
    }
    ctx.fill();
    ctx.shadowBlur = 0;
  });
}

// Net-web : dashed lines between every pair of pins on the highlighted net
if (st.highlightNet && st.board.nets) {
  const net = netByName.get(st.highlightNet);
  if (net) {
    const visiblePins = net.pin_refs
      .map((i) => st.board.pins[i])
      .filter((pin) => (pin.layer === 1 ? "top" : "bottom") === st.layer);
    ctx.strokeStyle = COLORS.net_web;
    ctx.lineWidth = 0.8;
    ctx.setLineDash([4, 3]);
    for (let i = 0; i < visiblePins.length; i++) {
      for (let j = i + 1; j < visiblePins.length; j++) {
        const a = worldToScreen(visiblePins[i].pos[0], visiblePins[i].pos[1]);
        const b = worldToScreen(visiblePins[j].pos[0], visiblePins[j].pos[1]);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
    ctx.setLineDash([]);
  }
}
```

- [ ] **Step 2: Manually verify**

Ensure the Pi 4 preload is in place before this test (Task 32). Pins render as colored dots, +3V3 highlight shows dashed web across all +3V3 pins.

- [ ] **Step 3: Commit**

```bash
git add web/boardview/renderer.js
git commit -m "feat(web): draw pins with semantic colors and dashed net-web on highlight"
```

---

## Task 31: Frontend — annotations + arrows + measure overlay

**Files:**
- Modify: `web/boardview/renderer.js`

- [ ] **Step 1: Add overlay rendering**

In `draw()`, **after** the net-web block, add:

```javascript
// Arrows
for (const id in st.arrows) {
  const { from, to } = st.arrows[id];
  const a = worldToScreen(from[0], from[1]);
  const b = worldToScreen(to[0], to[1]);
  ctx.strokeStyle = COLORS.arrow;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.stroke();
  // head
  const angle = Math.atan2(b.y - a.y, b.x - a.x);
  const headLen = 10;
  ctx.beginPath();
  ctx.moveTo(b.x, b.y);
  ctx.lineTo(b.x - headLen * Math.cos(angle - 0.4), b.y - headLen * Math.sin(angle - 0.4));
  ctx.lineTo(b.x - headLen * Math.cos(angle + 0.4), b.y - headLen * Math.sin(angle + 0.4));
  ctx.closePath();
  ctx.fillStyle = COLORS.arrow;
  ctx.fill();
}

// Annotations
for (const id in st.annotations) {
  const ann = st.annotations[id];
  const part = st.board.parts.find((p) => p.refdes === ann.refdes);
  if (!part) continue;
  const cx = (part.bbox[0][0] + part.bbox[1][0]) / 2;
  const cy = (part.bbox[0][1] + part.bbox[1][1]) / 2;
  const p = worldToScreen(cx, cy);
  const boxW = ctx.measureText(ann.label).width + 12;
  const offY = -24;
  ctx.fillStyle = COLORS.annotation;
  ctx.globalAlpha = 0.15;
  ctx.fillRect(p.x - boxW / 2, p.y + offY, boxW, 18);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = COLORS.annotation;
  ctx.strokeRect(p.x - boxW / 2, p.y + offY, boxW, 18);
  ctx.fillStyle = COLORS.annotation;
  ctx.font = "11px 'JetBrains Mono', monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(ann.label, p.x, p.y + offY + 9);
}

// Measure readout
if (st.measure) {
  const { from_refdes, to_refdes, distance_mm } = st.measure;
  const text = `${from_refdes} ↔ ${to_refdes}: ${distance_mm.toFixed(2)} mm`;
  ctx.fillStyle = COLORS.annotation;
  ctx.font = "12px 'JetBrains Mono', monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "top";
  ctx.fillText(text, canvas.getBoundingClientRect().width - 14, 14);
}
```

- [ ] **Step 2: Commit**

```bash
git add web/boardview/renderer.js
git commit -m "feat(web): render annotations, arrows, and measure readout"
```

---

## Task 32: Frontend — dropzone

**Files:**
- Create: `web/boardview/dropzone.js`

- [ ] **Step 1: Create the dropzone**

Create `web/boardview/dropzone.js`:

```javascript
export function createDropzone(element, sendWS) {
  element.addEventListener("dragover", (e) => {
    e.preventDefault();
    element.classList.add("boardview-drop-active");
  });
  element.addEventListener("dragleave", () => {
    element.classList.remove("boardview-drop-active");
  });
  element.addEventListener("drop", async (e) => {
    e.preventDefault();
    element.classList.remove("boardview-drop-active");
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    const buf = await file.arrayBuffer();
    const b64 = btoa(
      new Uint8Array(buf).reduce((s, b) => s + String.fromCharCode(b), "")
    );
    sendWS({ type: "boardview.upload", filename: file.name, content_b64: b64 });
  });
}
```

- [ ] **Step 2: Commit**

```bash
git add web/boardview/dropzone.js
git commit -m "feat(web): drag-drop zone that uploads file over WS"
```

---

## Task 33: Wire boardview into `web/app.js`

**Files:**
- Modify: `web/app.js`
- Modify: `web/index.html` (add `<canvas>` and script type="module")
- Modify: `web/style.css` (canvas layout + drop-active state)

- [ ] **Step 1: Rewrite `web/app.js`**

Replace contents of `web/app.js`:

```javascript
import { createStore } from "/static/boardview/store.js";
import { createRenderer } from "/static/boardview/renderer.js";
import { createDropzone } from "/static/boardview/dropzone.js";

export function workbench() {
  return {
    ws: null,
    connected: false,
    messages: [],
    draft: "",
    board: { label: "no board loaded" },
    schematic: { label: "no schematic loaded" },

    _store: null,
    _renderer: null,

    init() {
      this._store = createStore();
      const canvas = this.$refs.boardCanvas;
      this._renderer = createRenderer(canvas, this._store);
      createDropzone(this.$refs.boardPanel, (msg) => this._send(msg));

      this._store.subscribe((s) => {
        if (s.board) this.board.label = `${s.board.board_id} · ${s.board.parts.length} parts`;
      });

      this.connect();
    },

    connect() {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${scheme}://${window.location.host}/ws`);
      this.ws = ws;
      ws.addEventListener("open", () => { this.connected = true; });
      ws.addEventListener("close", () => { this.connected = false; setTimeout(() => this.connect(), 2000); });
      ws.addEventListener("message", (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch { return; }
        if (typeof payload.type === "string" && payload.type.startsWith("boardview.")) {
          this._store.apply(payload);
        } else if (payload.type === "message") {
          this.pushMessage(payload.role || "assistant", payload.text || "");
        }
      });
    },

    _send(msg) {
      if (!this.connected || !this.ws) return;
      this.ws.send(JSON.stringify(msg));
    },

    send() {
      const text = this.draft.trim();
      if (!text || !this.connected) return;
      this.pushMessage("user", text);
      this._send({ type: "message", text });
      this.draft = "";
    },

    pushMessage(role, text) {
      this.messages.push({ role, text });
      this.$nextTick(() => {
        const log = this.$refs.log;
        if (log) log.scrollTop = log.scrollHeight;
      });
    },
  };
}

window.workbench = workbench;
```

- [ ] **Step 2: Modify `web/index.html`**

Replace the Boardview `<section>` body:

```html
<section class="panel" aria-label="Boardview" x-ref="boardPanel">
  <header class="panel-header">
    <span class="panel-title">Boardview</span>
    <span class="panel-sub" x-text="board.label"></span>
  </header>
  <div class="panel-body">
    <canvas x-ref="boardCanvas" class="boardview-canvas"></canvas>
  </div>
</section>
```

Change the script tag to module:

```html
<script type="module" src="/static/app.js"></script>
```

Remove the old non-module `<script src="/app.js"></script>`.

- [ ] **Step 3: Add canvas CSS to `web/style.css`**

```css
.boardview-canvas {
  width: 100%;
  height: 100%;
  display: block;
  cursor: grab;
}
.boardview-canvas:active { cursor: grabbing; }

.boardview-drop-active {
  outline: 2px dashed #40e0d0;
  outline-offset: -6px;
}
```

- [ ] **Step 4: Manual verification**

Run: `make run`
Open `http://localhost:8000`.
Expected:
- WS connects (green "connected" in chat header).
- Pi 4 outline visible (once Task 34 provides the asset) OR empty canvas (no .brd yet).
- Pan/zoom works.

- [ ] **Step 5: Commit**

```bash
git add web/app.js web/index.html web/style.css
git commit -m "feat(web): wire boardview renderer + store + dropzone into Alpine root"
```

---

## Task 34: Hand-crafted Pi 4 `.brd` (mini viable version)

**Files:**
- Create: `board_assets/raspberry-pi-4b.yaml`
- Create: `tools/brd_compile.py`
- Create: `tools/__init__.py`
- Create: `board_assets/raspberry-pi-4b.brd`

**Context:** The Pi 4 fixture is the demo centerpiece. The YAML source is the authoring format ; `brd_compile.py` emits a valid `.brd`. For MVP we include ~40 key components (SoC, RAM, PMIC, USB hub, connectors, major caps/resistors) — not all 300.

- [ ] **Step 1: Write the YAML source**

Create `board_assets/raspberry-pi-4b.yaml`:

```yaml
board_id: raspberry-pi-4b
outline_mils: [[0, 0], [3346, 0], [3346, 2205], [0, 2205]]   # 85 x 56 mm in mils

parts:
  # Main SoC (BGA) top side
  - refdes: U1
    layer: top
    bbox: [[1200, 800], [1800, 1400]]
    pins:
      # Abbreviated BGA — 10 representative pins on +3V3, GND, signals
      - { x: 1240, y: 840,  net: "+3V3" }
      - { x: 1300, y: 840,  net: GND }
      - { x: 1360, y: 840,  net: "+1V8" }
      - { x: 1420, y: 840,  net: GPIO_0 }
      - { x: 1480, y: 840,  net: GPIO_1 }
      - { x: 1540, y: 840,  net: GND }
      - { x: 1600, y: 840,  net: "+3V3" }
      - { x: 1660, y: 840,  net: GND }
      - { x: 1720, y: 840,  net: DDR_CLK }
      - { x: 1760, y: 840,  net: DDR_DAT }

  - refdes: U2
    layer: top
    bbox: [[1900, 900], [2200, 1300]]   # LPDDR4 package
    pins:
      - { x: 1920, y: 920,  net: DDR_CLK }
      - { x: 1920, y: 980,  net: DDR_DAT }
      - { x: 1920, y: 1040, net: "+1V1" }
      - { x: 1920, y: 1100, net: GND }

  - refdes: U3
    layer: top
    bbox: [[2250, 900], [2500, 1200]]   # LAN7515 USB/Ethernet
    pins:
      - { x: 2270, y: 920, net: "+3V3" }
      - { x: 2270, y: 980, net: GND }
      - { x: 2270, y: 1040, net: USB_DP }

  - refdes: U7
    layer: top
    bbox: [[900, 1050], [1100, 1250]]   # PMIC MxL7704
    pins:
      - { x: 920, y: 1070, probe: 1, net: "+3V3" }
      - { x: 920, y: 1120, probe: 2, net: "+1V8" }
      - { x: 920, y: 1170, probe: 3, net: "+1V1" }
      - { x: 920, y: 1220, probe: 4, net: GND }
      - { x: 1080, y: 1070, net: VBAT }

  # GPIO 40-pin header (20 pins shown)
  - refdes: J8
    layer: top
    bbox: [[500, 200], [2200, 320]]
    pins:
      - { x: 540, y: 230, net: "+3V3" }
      - { x: 620, y: 230, net: GPIO_0 }
      - { x: 700, y: 230, net: "+5V" }
      - { x: 780, y: 230, net: GPIO_1 }
      - { x: 860, y: 230, net: GND }
      - { x: 940, y: 230, net: GPIO_2 }
      - { x: 1020, y: 230, net: GPIO_3 }
      - { x: 1100, y: 230, net: GND }

  # USB / HDMI / Ethernet connectors (top layer)
  - refdes: J1
    layer: top
    bbox: [[2800, 300], [3200, 680]]  # RJ45
    pins:
      - { x: 2820, y: 400, net: ETH_RX }
      - { x: 2820, y: 500, net: ETH_TX }
      - { x: 2820, y: 600, net: GND }

  - refdes: J2
    layer: top
    bbox: [[2800, 720], [3200, 960]]  # USB 3.0 x2
    pins:
      - { x: 2820, y: 740, net: USB_DP }
      - { x: 2820, y: 800, net: USB_DN }
      - { x: 2820, y: 860, net: "+5V" }
      - { x: 2820, y: 920, net: GND }

  - refdes: J3
    layer: top
    bbox: [[2800, 1000], [3200, 1240]]  # USB 2.0 x2
    pins:
      - { x: 2820, y: 1020, net: USB_DP }
      - { x: 2820, y: 1080, net: USB_DN }
      - { x: 2820, y: 1140, net: "+5V" }
      - { x: 2820, y: 1200, net: GND }

  - refdes: J6
    layer: top
    bbox: [[1200, 1950], [1440, 2150]]  # micro-HDMI 0
    pins:
      - { x: 1220, y: 1980, net: HDMI0_DAT }
      - { x: 1220, y: 2040, net: HDMI0_CLK }
      - { x: 1220, y: 2100, net: GND }

  - refdes: J7
    layer: top
    bbox: [[1500, 1950], [1740, 2150]]  # micro-HDMI 1
    pins:
      - { x: 1520, y: 1980, net: HDMI1_DAT }
      - { x: 1520, y: 2040, net: HDMI1_CLK }
      - { x: 1520, y: 2100, net: GND }

  - refdes: J11
    layer: top
    bbox: [[700, 1950], [900, 2150]]  # USB-C power
    pins:
      - { x: 720, y: 1980, net: "+5V" }
      - { x: 720, y: 2100, net: GND }

  # Representative decoupling caps on +3V3 near SoC
  - refdes: C29
    layer: top
    bbox: [[1200, 1430], [1230, 1450]]
    pins:
      - { x: 1205, y: 1440, net: "+3V3" }
      - { x: 1225, y: 1440, net: GND }
  - refdes: C30
    layer: top
    bbox: [[1250, 1430], [1280, 1450]]
    pins:
      - { x: 1255, y: 1440, net: "+3V3" }
      - { x: 1275, y: 1440, net: GND }
  - refdes: C31
    layer: top
    bbox: [[1300, 1430], [1330, 1450]]
    pins:
      - { x: 1305, y: 1440, net: "+3V3" }
      - { x: 1325, y: 1440, net: GND }

  # Bottom-side components (microSD slot, misc caps)
  - refdes: J12
    layer: bottom
    bbox: [[2600, 900], [2950, 1350]]   # microSD
    pins:
      - { x: 2620, y: 920, net: SD_DAT0 }
      - { x: 2620, y: 980, net: SD_CLK }
      - { x: 2620, y: 1040, net: "+3V3" }
      - { x: 2620, y: 1100, net: GND }

nails:
  # Expose PMIC test points for the +3V3 / +1V8 / +1V1 rails
  - { probe: 1, x: 920, y: 1070, layer: top, net: "+3V3" }
  - { probe: 2, x: 920, y: 1120, layer: top, net: "+1V8" }
  - { probe: 3, x: 920, y: 1170, layer: top, net: "+1V1" }
  - { probe: 4, x: 920, y: 1220, layer: top, net: GND }
```

- [ ] **Step 2: Create `tools/` package and the compiler**

```bash
touch tools/__init__.py
```

Create `tools/brd_compile.py`:

```python
"""YAML → .brd Test_Link converter (dev-time authoring tool, not runtime)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def compile_yaml_to_brd(yaml_path: Path, out_path: Path) -> None:
    data = yaml.safe_load(yaml_path.read_text())
    outline = data["outline_mils"]
    parts = data["parts"]
    nails = data.get("nails", [])

    all_pins: list[dict] = []
    for idx, part in enumerate(parts, start=1):
        for pin in part.get("pins", []):
            all_pins.append(
                {
                    "x": pin["x"],
                    "y": pin["y"],
                    "probe": pin.get("probe", -99),
                    "part_idx": idx,  # 1-based
                    "net": pin.get("net", ""),
                }
            )

    lines: list[str] = []
    lines.append("str_length: 0 0")
    lines.append(f"var_data: {len(outline)} {len(parts)} {len(all_pins)} {len(nails)}")

    lines.append("Format:")
    for x, y in outline:
        lines.append(f"{x} {y}")

    lines.append("Parts:")
    cursor = 0
    for part in parts:
        n_pins = len(part.get("pins", []))
        end_of_pins = cursor + n_pins
        cursor = end_of_pins
        layer_is_top = part.get("layer", "top") == "top"
        is_smd = part.get("is_smd", True)
        # Encoding (single-bit scheme — must match parser's _layer_from_bits / _is_smd_from_bits) :
        #   bit 0x1 = presence flag, bit 0x2 = bottom (else top), bit 0x4 = SMD (else TH)
        # So : top+SMD=5 (0b0101), top+TH=1 (0b0001), bottom+SMD=6 (0b0110), bottom+TH=2 (0b0010).
        base = 0x1
        if not layer_is_top: base |= 0x2
        if is_smd:           base |= 0x4
        type_layer = base
        lines.append(f"{part['refdes']} {type_layer} {end_of_pins}")

    lines.append("Pins:")
    for p in all_pins:
        net = p["net"] if p["net"] else ""
        lines.append(f"{p['x']} {p['y']} {p['probe']} {p['part_idx']} {net}")

    lines.append("Nails:")
    for n in nails:
        side = 1 if n.get("layer", "top") == "top" else 2
        lines.append(f"{n['probe']} {n['x']} {n['y']} {side} {n['net']}")

    out_path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Compile a YAML board description to .brd")
    ap.add_argument("input", type=Path, help="Path to source YAML")
    ap.add_argument("-o", "--output", type=Path, help="Output .brd path (default: same name)")
    args = ap.parse_args()
    out = args.output or args.input.with_suffix(".brd")
    compile_yaml_to_brd(args.input, out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Add `pyyaml` to `pyproject.toml` dev deps:

```toml
dev = [
    "pytest ~= 8.4.0",
    "pytest-asyncio ~= 0.26.0",
    "ruff ~= 0.15.0",
    "pyyaml ~= 6.0.0",
]
```

Run: `.venv/bin/pip install -e ".[dev]"` to pick up pyyaml.

- [ ] **Step 3: Compile the Pi 4 YAML**

```bash
.venv/bin/python tools/brd_compile.py board_assets/raspberry-pi-4b.yaml
```

Expected: `board_assets/raspberry-pi-4b.brd` created. Its contents should be 20+ lines of plain text.

- [ ] **Step 4: Sanity test — the compiled .brd parses**

Add a fixture test in `tests/board/test_brd_parser.py`:

```python
def test_pi4_yaml_compiled_brd_parses():
    p = Path(__file__).resolve().parents[2] / "board_assets" / "raspberry-pi-4b.brd"
    if not p.exists():
        pytest.skip("Pi 4 asset not compiled")
    board = BRDParser().parse_file(p)
    assert board.board_id == "raspberry-pi-4b"
    assert board.part_by_refdes("U1") is not None   # SoC present
    assert board.part_by_refdes("U7") is not None   # PMIC present
    vcc = board.net_by_name("+3V3")
    assert vcc is not None and vcc.is_power
```

Run: `.venv/bin/pytest tests/board/test_brd_parser.py -v`
Expected: all parser tests pass, Pi 4 test included.

- [ ] **Step 5: Commit**

```bash
git add board_assets/ tools/ pyproject.toml tests/board/test_brd_parser.py
git commit -m "feat(assets): hand-crafted Pi 4 YAML + brd_compile.py helper"
```

---

## Task 35: E2E manual checklist

**Files:**
- Create: `docs/superpowers/plans/2026-04-21-boardview-e2e-checklist.md`

**Context:** Frontend rendering is not unit-tested. This file is the go/no-go gate before we consider the MVP done.

- [ ] **Step 1: Write the checklist**

Create the file with contents:

```markdown
# Boardview E2E manual checklist

Run before each tag / demo recording. Every box must be checked.

## Setup
- [ ] `make test` all green.
- [ ] `make run` starts uvicorn on :8000.
- [ ] `board_assets/raspberry-pi-4b.brd` exists.

## Boot + preload
- [ ] Open `http://localhost:8000`.
- [ ] Chat header shows "connected" in green within 2s.
- [ ] Boardview panel shows the Pi 4 outline + parts.

## Pan/zoom/keyboard
- [ ] Mouse wheel zooms ; the point under the cursor stays still.
- [ ] Drag pans smoothly.
- [ ] W/A/S/D pan.
- [ ] R resets pan + zoom.
- [ ] F toggles top/bottom.

## Tool calls (simulate with raw WS or via agent when wired)
- [ ] `boardview.highlight` on `U7` adds a glow on the PMIC.
- [ ] `boardview.highlight_net` on `+3V3` paints the rail's pins and draws the net-web.
- [ ] `boardview.focus` on `J12` auto-flips to bottom (J12 is bottom side in the fixture).
- [ ] `boardview.annotate` on `U1` shows "PMIC" near the SoC.
- [ ] `boardview.draw_arrow` between `U7` and `U1` draws a visible arrow.
- [ ] `boardview.measure` shows a mm readout in the top-right HUD.
- [ ] `boardview.reset_view` clears all overlays.

## Drag-drop
- [ ] Drop a valid 2nd `.brd` file → new board renders within 2s.
- [ ] Drop an obfuscated file (bytes `23 e2 63 28 ...`) → WS ack shows `reason: obfuscated` and the current board stays loaded.
- [ ] Drop a `.xyz` file → `reason: unsupported-format`.

## Anti-hallucination
- [ ] Send a message that the agent could answer referencing `U999` (nonexistent) ; verify that the agent never highlights U999 (the refdes is rejected upstream).
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-04-21-boardview-e2e-checklist.md
git commit -m "docs: boardview E2E manual checklist for MVP go/no-go"
```

---

## Stretch — not planned

The following are intentionally NOT scheduled ; schedule them only if all of Tasks 0-35 are complete AND the E2E checklist passes AND there's remaining time before submission :

- `.brd2` parser subclass.
- `.bdv` parser subclass.
- `.fz` (Fritzing) parser subclass.
- Framework Mainboard `.yaml` → `.brd` (similar to Task 34 but for the Framework device).
- Tier 4 verbs : `trace_connection`, `manage_probe_points`, `measure_virtual`, `export_annotated`.
- WebSocket per-message deflate extension (perf optimization).
- Multi-session support.

---

## Self-review notes

**Spec coverage :** every section of `docs/superpowers/specs/2026-04-21-boardview-design.md` maps to at least one task (Tasks 1-10 = parser/model/validator §5-8 ; 11-13 = session + events + WS envelopes §10, §12-14 ; 14-20 = tool handlers §9 ; 21-25 = pipeline + FastAPI wiring §10-14 ; 26-33 = frontend §11-13 ; 34 = assets ; 35 = §15 testing).

**No placeholders :** every step contains actual code or an exact command. Imports and method signatures match across tasks (checked manually).

**Type consistency :** `Part.pin_refs` is a list of pin indexes everywhere ; `Layer` uses `.value` for serialization ; `SessionState.highlights` is a `set[str]` throughout ; `board_loaded_from()` is defined in Task 25 and used by Task 25 only (main.py).

**Known gaps flagged in the spec as non-goals :** T4 verbs, `.brd2`/`.bdv`/`.fz` parsers, compression extension, multi-session. All in the Stretch section above.
