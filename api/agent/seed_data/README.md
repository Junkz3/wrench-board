# Seed data — Managed Agents global stores

Two singleton MA memory stores are created lazily by the agent runtime
(see `api/agent/memory_stores.py::ensure_global_store`) and attached
read-only to every diagnostic session:

- **`wrench-board-global-patterns`** — cross-device failure archetypes
  (PMU shorts, thermal cascades, BGA lift, bench anti-patterns).
  Seeded from `global_patterns/*.md`.
- **`wrench-board-global-playbooks`** — JSON protocol templates
  conformant to `bv_propose_protocol(steps=[...])`.
  Seeded from `global_playbooks/*.json`.

To push changes upstream after editing any seed file:

```bash
.venv/bin/python scripts/seed_global_memory_stores.py
```

The script is idempotent — store ids are cached in
`memory/_managed/global.json` and the API is upsert-by-path so
re-running just replaces content.
