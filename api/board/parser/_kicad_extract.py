#!/usr/bin/env python3
"""Extract KiCad .kicad_pcb data as JSON for consumption by the microsolder-agent
Python parser. Runs under system Python 3 with pcbnew (installed with KiCad).

Usage: python3 _kicad_extract.py <path-to-.kicad_pcb>
Output on stdout: JSON dict with keys: outline, parts, pins, nets, nails.
"""

from __future__ import annotations

import json
import sys

try:
    import pcbnew
except ImportError as e:  # pragma: no cover - system dep
    sys.stderr.write(f"pcbnew not available: {e}\n")
    sys.exit(2)


NM_PER_MIL = 25400  # 1 mil = 25400 nm

PAD_SHAPE_NAMES = {
    pcbnew.PAD_SHAPE_CIRCLE: "circle",
    pcbnew.PAD_SHAPE_RECT: "rect",
    pcbnew.PAD_SHAPE_OVAL: "oval",
    pcbnew.PAD_SHAPE_TRAPEZOID: "trapezoid",
    pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect",
}
# Chamfered / custom shapes fall back to "custom"


def nm_to_mils(nm: int) -> int:
    return int(round(nm / NM_PER_MIL))


def main(path: str) -> None:
    pcb = pcbnew.LoadBoard(path)

    # Outline: use GetBoardPolygonOutlines, take first polygon
    outlines = pcbnew.SHAPE_POLY_SET()
    pcb.GetBoardPolygonOutlines(outlines)
    outline_pts: list[dict] = []
    if outlines.OutlineCount() > 0:
        o = outlines.Outline(0)
        for i in range(o.PointCount()):
            p = o.GetPoint(i)
            outline_pts.append({"x": nm_to_mils(p.x), "y": nm_to_mils(p.y)})

    # Nets: id 0 is "no net" by pcbnew convention
    net_info = pcb.GetNetInfo()
    nets: list[dict] = []
    for n in range(1, net_info.GetNetCount()):
        ni = net_info.GetNetItem(n)
        nets.append({"code": ni.GetNetCode(), "name": ni.GetNetname()})

    # Footprints (parts) and pads (pins)
    parts: list[dict] = []
    pins: list[dict] = []
    pin_index_counter = 0

    for fp in pcb.GetFootprints():
        refdes = fp.GetReference()
        value = fp.GetValue()
        footprint_name = str(fp.GetFPID().GetLibItemName())
        lib_nickname = str(fp.GetFPID().GetLibNickname())
        footprint_ref = f"{lib_nickname}:{footprint_name}" if lib_nickname else footprint_name
        rotation_deg = fp.GetOrientationDegrees()
        flipped = fp.IsFlipped()
        side = 2 if flipped else 1  # match BRD2 convention (1=top, 2=bottom)
        # Attributes: SMD flag lives in GetAttributes() bitmask
        attrs = fp.GetAttributes()
        is_smd = bool(attrs & pcbnew.FP_SMD)

        # Pads-only bounding box (union over pad.GetBoundingBox() in board coords)
        pads = list(fp.Pads())
        if pads:
            x0 = y0 = float("inf")
            x1 = y1 = float("-inf")
            for pad in pads:
                bb = pad.GetBoundingBox()
                if bb.GetLeft() < x0:
                    x0 = bb.GetLeft()
                if bb.GetTop() < y0:
                    y0 = bb.GetTop()
                if bb.GetRight() > x1:
                    x1 = bb.GetRight()
                if bb.GetBottom() > y1:
                    y1 = bb.GetBottom()
        else:
            # Fallback: overall bbox
            bb = fp.GetBoundingBox()
            x0, y0, x1, y1 = bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()

        part_first_pin = pin_index_counter
        part_entry = {
            "refdes": refdes,
            "value": value or None,
            "footprint": footprint_ref,
            "rotation_deg": rotation_deg,
            "side": side,
            "is_smd": is_smd,
            "bbox": [
                {"x": nm_to_mils(x0), "y": nm_to_mils(y0)},
                {"x": nm_to_mils(x1), "y": nm_to_mils(y1)},
            ],
            "first_pin": part_first_pin,
        }
        parts.append(part_entry)

        # Per-pin data — note: each pad has its own orientation independent
        # of the parent footprint (on multi-row packages like QFP / BGA the
        # pads on the sides are rotated 90° relative to the top/bottom pads,
        # regardless of the footprint's overall placement rotation).
        for pad in pads:
            pos = pad.GetPosition()
            size = pad.GetSize()
            shape_id = pad.GetShape()
            shape = PAD_SHAPE_NAMES.get(shape_id, "custom")
            net_code = pad.GetNetCode()
            pin_side_flipped = pad.IsFlipped()
            pad_rot = pad.GetOrientationDegrees() if hasattr(pad, "GetOrientationDegrees") else 0.0
            pins.append({
                "x": nm_to_mils(pos.x),
                "y": nm_to_mils(pos.y),
                "net_code": net_code,
                "side": 2 if pin_side_flipped else 1,
                "pad_shape": shape,
                "pad_size": [nm_to_mils(size.x), nm_to_mils(size.y)],
                "pad_rotation_deg": pad_rot,
                "pin_number": pad.GetNumber(),
            })
            pin_index_counter += 1

    # Nails: pcbnew exposes test points as footprints with PTH/SMD attrs;
    # for MVP we emit an empty list — upstream BRD2 parser handles nails from
    # the .brd NAILS: block, but KiCad doesn't have a dedicated "nails" concept
    # in the same sense. Leave empty; downstream is fine (b.nails = []).
    nails: list[dict] = []

    out = {
        "outline": outline_pts,
        "parts": parts,
        "pins": pins,
        "nets": nets,
        "nails": nails,
    }
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: _kicad_extract.py <kicad_pcb>\n")
        sys.exit(2)
    main(sys.argv[1])
