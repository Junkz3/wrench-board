"""Schematic ingestion pipeline — PDF schematic → ElectricalGraph.

Per-page Claude vision extracts a `SchematicPageGraph` from each rendered page.
A deterministic merger stitches pages by net label and cross-page reference,
derives power rails and a boot sequence, and writes the final `ElectricalGraph`
to `memory/{device_slug}/electrical_graph.json`.
"""
