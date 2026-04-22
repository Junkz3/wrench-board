"""Unit tests for the Scout dump threshold guard.

`assess_dump` counts load-bearing entities in the Markdown produced by the Scout
(symptom blocks, distinct components, distinct source URLs) so the orchestrator
can reject a bankrupt dump before spending on Phases 2-4.
"""

from __future__ import annotations

from api.pipeline.scout import assess_dump

SAMPLE_DUMP = """\
# Research Dump — Acme SBC

## Device overview
Single-board computer using the SoC X.

## Known failure modes

- **Symptom:** 3V3 rail dead
  - **Likely cause:** PMIC failure
  - **Components mentioned:** U7, C29
  - **Diagnostic hint:** measure at TP18
  - **Source:** https://repair.wiki/acme/3v3-dead

- **Symptom:** USB enumeration fails
  - **Likely cause:** redriver fried
  - **Components mentioned:** U14
  - **Diagnostic hint:** check D+/D- swing
  - **Source:** https://ifixit.com/acme/usb

- **Symptom:** No display output
  - **Likely cause:** DSI level shifter
  - **Components mentioned:** U21
  - **Diagnostic hint:** probe MIPI clock
  - **Source:** https://community.mnt.re/t/no-hdmi

## Components mentioned by the community
- **U7** — aliases: main PMIC. Role: primary power controller.
- **C29** — aliases: . Role: 3V3 decoupling.
- **U14** — aliases: USB redriver. Role: USB re-timer.
- **U21** — aliases: DSI shifter. Role: level translation.

## Signals / power rails / nets mentioned
- **3V3_RAIL** — aliases: 3.3V. Nominal voltage: 3.3 V.

## Sources
- https://repair.wiki/acme/3v3-dead — 3V3 dead guide
- https://ifixit.com/acme/usb — USB fix
- https://community.mnt.re/t/no-hdmi — HDMI troubleshooting
"""


def test_assess_counts_symptoms_components_sources():
    a = assess_dump(SAMPLE_DUMP, min_symptoms=3, min_components=3, min_sources=3)
    assert a.symptoms == 3
    assert a.components == 4
    assert a.sources == 3
    assert a.viable is True


def test_assess_below_threshold_not_viable():
    a = assess_dump(SAMPLE_DUMP, min_symptoms=5, min_components=3, min_sources=3)
    assert a.viable is False
    assert a.symptoms == 3  # counted truthfully even when below threshold


def test_assess_empty_dump():
    a = assess_dump("", min_symptoms=1, min_components=1, min_sources=1)
    assert a.symptoms == 0
    assert a.components == 0
    assert a.sources == 0
    assert a.viable is False


def test_assess_duplicate_urls_counted_once():
    dump = """\
## Known failure modes
- **Symptom:** X
  - **Source:** https://example.com/a

## Components mentioned by the community
- **U1** — stuff

## Sources
- https://example.com/a — same link twice
- https://example.com/b — second
"""
    a = assess_dump(dump, min_symptoms=1, min_components=1, min_sources=1)
    assert a.sources == 2  # two unique URLs, not three


def test_assess_url_trailing_punctuation_stripped():
    dump = """\
## Components mentioned by the community
- **U1** — stuff

See https://example.com/x, and https://example.com/x. for details.
"""
    a = assess_dump(dump, min_symptoms=0, min_components=1, min_sources=1)
    # Both occurrences resolve to the same URL after stripping ',' and '.'
    assert a.sources == 1
