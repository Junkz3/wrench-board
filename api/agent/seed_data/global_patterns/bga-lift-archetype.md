---
pattern_id: bga-lift
applies_to_classes: [smartphone-logic, laptop-mainboard, single-board-computer]
typical_refdes_classes: [SoC, PMIC, GPU, baseband]
---

# BGA solder ball lift

## Signature
- Intermittent boot failures, sometimes correlated with cold/hot.
- Touching or pressing the IC stabilizes the device transiently.
- Specific bus signals (high-speed: PCIe, DDR, MIPI) marginal at best.

## Common scenarios
- Drop event with cosmetic dent near the BGA — re-ball or replace.
- Reflow over-temperature on an adjacent IC — solder voids open under stress.
- Underfill aging on consumer parts (older flagships >3 years) — re-ball
  is often a stop-gap; full IC replacement is the durable fix.

## Diagnostic order
1. Press-test: gently press the IC with a finger while powered. If symptom
   resolves, BGA is suspected.
2. Cold spray each suspect IC; symptom returning when warmed back is
   confirmation.
3. Temperature soak the board to 50 °C for 5 minutes; symptom appearing
   only when warm is BGA expansion-related.

## Anti-pattern
- Re-balling without addressing the underlying root cause (solder mask
  damage, pad lift, drop-induced trace fracture under the BGA) gives a
  short-lived fix. Always inspect after pull.
