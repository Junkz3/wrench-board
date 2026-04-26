---
pattern_id: anti-patterns-bench
applies_to_classes: [all]
---

# Bench anti-patterns (mistakes to avoid)

## Power-on sequencing
- **Don't apply full nominal voltage to a suspected shorted rail.** Use a
  bench supply with current limit set to ~100-300 mA. The short heats up
  faster than you can localize it at full current.
- **Don't bypass the battery on a phone for diagnostic.** The battery
  acts as a giant decoupling cap; without it, fast transients on PMIC
  outputs can latch protection and mimic a different fault.

## Rework hygiene
- **Don't reflow without removing nearby polymer caps.** Polymer caps die
  silently above 250 °C and you won't notice until the rail collapses
  next power-on.
- **Don't pull a BGA without preheating the bottom of the board.** Sudden
  thermal gradient cracks pads and traces under the IC.

## Measurement discipline
- **Don't probe high-impedance nodes with a 1 MΩ scope.** Switch to 10 MΩ
  or use an active probe — you'll shift the bias point and chase a
  phantom signal.
- **Don't trust diode-mode below 0.4 V.** Below that, modern multimeters
  enter their own protection and read garbage. Use a 4-wire ohms range
  for sub-ohm shorts.
