# SPDX-License-Identifier: Apache-2.0
"""Anti-hallucination guardrail — every refdes the agent mentions passes here.

Pure functions over an already-parsed `Board`. No I/O, no mutation. If a
lookup fails the caller is expected to return a structured null/unknown
response, not fabricate data (per CLAUDE.md hard rule #5).
"""

from __future__ import annotations

from api.board.model import Board, Net, Part, Pin


def is_valid_refdes(board: Board, refdes: str) -> bool:
    """Return True iff `refdes` matches a part on the board (case-sensitive)."""
    return board.part_by_refdes(refdes) is not None


def resolve_part(board: Board, refdes: str) -> Part | None:
    """Return the Part with `refdes`, or None."""
    return board.part_by_refdes(refdes)


def resolve_net(board: Board, net_name: str) -> Net | None:
    """Return the Net named `net_name`, or None."""
    return board.net_by_name(net_name)


def resolve_pin(board: Board, refdes: str, pin_index: int) -> Pin | None:
    """Return the Pin at `(refdes, pin_index)` (1-based within the part), or None."""
    part = board.part_by_refdes(refdes)
    if part is None:
        return None
    for i in part.pin_refs:
        pin = board.pins[i]
        if pin.index == pin_index:
            return pin
    return None


def suggest_similar(board: Board, refdes: str, k: int = 3) -> list[str]:
    """Return up to `k` refdes names closest to `refdes` by Levenshtein distance.

    Order is ascending distance ; ties broken by alphabetical order on the
    refdes string so calls are deterministic across runs.

    The input is stripped of leading/trailing whitespace before comparison.
    An empty or whitespace-only string returns an empty list — there is no
    sensible "close match" to whitespace. This also means a padded query
    like `" R1 "` correctly matches `R1` at distance 0 after strip.
    """
    refdes = refdes.strip()
    if not refdes:
        return []
    candidates = [p.refdes for p in board.parts]
    scored = sorted(candidates, key=lambda c: (_levenshtein(refdes, c), c))
    return scored[:k]


def _levenshtein(a: str, b: str) -> int:
    """Classic iterative Wagner–Fischer DP.

    Space-optimized to two rows. Runs in O(len(a) * len(b)) time and
    O(min(len(a), len(b))) space.
    """
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
