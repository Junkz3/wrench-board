---
pattern_id: thermal-cascades
applies_to_classes: [smartphone-logic, single-board-computer]
typical_refdes_classes: [PMIC, SoC, charger-IC]
---

# Thermal cascade failures

## Signature
- Multiple components heating in sequence after power-on.
- Rail voltages start nominal then sag as temperature climbs.
- Reset behavior: device boots, runs N seconds, then shuts down.

## Cascade archetypes
1. **Charger IC fail → battery FET overheats**: charger pushes current
   through a partially-shorted FET. The FET runs hot before the charger
   does. Probe the FET first, even though the charger is the upstream cause.
2. **Buck regulator coil saturation → adjacent IC heats**: a bad inductor
   stops bucking and pushes raw input voltage downstream. The downstream
   IC's internal protection clamps and dissipates the difference.
3. **PMIC LDO short → host SoC throttles**: SoC sees its supply collapse
   and reduces clock, but the PMIC keeps dissipating. SoC stays cool, PMIC
   gets hot.

## Diagnostic order
- Thermal cam the entire board within 5 seconds of power-up.
- Identify the FIRST hot spot, not the hottest at steady state.
- Probe upstream of the first hot spot — the symptom is downstream of the cause.

## Anti-pattern
- Replacing the hottest component without tracing the rail upstream is
  the #1 mistake. The hot one is often the victim, not the culprit.
