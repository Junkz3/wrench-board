# benchmark/

Frozen oracle for `api.pipeline.schematic.evaluator`. One JSON object per
line in `scenarios.jsonl`, one verbatim quote per file in `sources/`.

## Provenance contract

Every scenario MUST carry:

- `source_url` — public URL the quote was extracted from.
- `source_quote` — verbatim text (50+ chars).
- `source_archive` — relative path to a local snapshot in `sources/`.

Scenarios missing any of these three are rejected at load time. This
forces the bench to be *« structuring real human knowledge »*, not
*« generating plausible-sounding intuition »*. URL rot is mitigated by
the local archive — the score never depends on a live URL.

## Refresh cadence

The bench is **frozen** during ordinary work. Refresh only when:
- A new device family is added to the workshop (one scenario per family).
- An existing scenario is invalidated by upstream knowledge (rare).

Never refresh just to "match what the simulator now does" — that's the
gaming failure mode the spec calls out.
