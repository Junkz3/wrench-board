# Built with Opus 4.7 — Hackathon context

> This file exists only while the original build window is open.
> It can be deleted or moved to `docs/archive/` after submission
> without affecting the product.

## Submission

- Event : Anthropic × Cerebral Valley, "Built with Opus 4.7"
- Window : 2026-04-21 → 2026-04-26 (submission 20:00 EST)
- All code written from scratch during this window
  (see `CLAUDE.md` hard rule #1)

## Reference board for the demo

The MNT Reform motherboard is the canonical target for the
submission demo :

- License : CERN-OHL-S-2.0 (fully open hardware)
- Source : KiCad
- Why : non-trivial topology (PMIC + multi-rail), satisfies the
  open-hardware-only rule in `CLAUDE.md` (#4), high-quality
  parseable sources

Note : the product is board-format-agnostic by design — MNT Reform
is the demo board, not a hard dependency.

## Prize-track context

Cerebral Valley announced a $5 000 "best use of Managed Agents"
track on top of the main hackathon prize. This is CONTEXT, not
scope : do not warp architectural choices to chase it. We use
Managed Agents where they genuinely fit — the diagnostic
conversation path (persistent agent + memory store per device +
session event stream + custom tool use, cf. spec §2.3 Flow A).
The pipeline path stays on `messages.create` direct because it's
batch and doesn't benefit from session primitives. Never mention
prizes in commit messages, plans, or code — keep the work
technically-motivated.

## Implementation scope for the window

See `docs/superpowers/plans/2026-04-22-v1-hackathon-shipping-plan.md`
— Phases A → D through 2026-04-26. That plan is the source of truth
for what ships during the submission window and what is explicitly
deferred.

## Post-submission

After 2026-04-26 :

- This file can be deleted, or moved to
  `docs/archive/HACKATHON-2026.md`
- The shipping plan is already dated ; it ages out naturally
- `CLAUDE.md` stays as-is : it makes no reference to this file
