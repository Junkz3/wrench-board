"""Capture a real diagnostic-agent WS session into a replayable frame log.

Dev tool (not shipped to the browser). Drives /ws/diagnostic/{slug} like the
browser would: sends client.capabilities, asks a question, records every frame
with a relative timestamp, and — for a measurement protocol — accepts the
proposal and submits a result per step until it completes. The output JSON is
replayed client-side in the onboarding demo so the agent is shown "in action"
for free (no live LLM call at demo time).

Usage:
    .venv/bin/python -m scripts.capture_demo_session \
        --slug mnt-reform-motherboard --repair 8023b32d6fa7 --tier normal \
        --question "..." --out web/demos/mnt-reform-diag.json \
        [--drive-protocol] [--fault-first-step]

Records both directions; the replayer only plays dir=="recv" frames (the
client→server frames are kept for reference/debugging).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import websockets


async def capture(
    *,
    url: str,
    question: str,
    out_path: Path,
    drive_protocol: bool,
    fault_first_step: bool,
    idle_timeout: float,
) -> None:
    frames: list[dict] = []
    t0: float | None = None

    def rec(direction: str, obj: dict) -> None:
        nonlocal t0
        now = time.monotonic()
        if t0 is None:
            t0 = now
        frames.append({"ms": round((now - t0) * 1000), "dir": direction, "frame": obj})

    # Protocol drive state.
    active_protocol: dict | None = None
    answered_steps: set[str] = set()
    protocol_done = False
    first_numeric_answered = False

    async with websockets.connect(url, max_size=None, open_timeout=15) as ws:
        async def send(obj: dict) -> None:
            await ws.send(json.dumps(obj))
            rec("send", obj)
            print(f"  → {obj.get('type')}")

        # Browser sends capabilities immediately on open.
        await send({"type": "client.capabilities", "camera_available": False, "selected_device_id": None})

        question_sent = False

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                print("  (idle timeout — session quiet, ending capture)")
                break

            obj = json.loads(raw)
            rec("recv", obj)
            t = obj.get("type", "")
            # Compact log line.
            extra = obj.get("name") or obj.get("title") or obj.get("role") or obj.get("stop_reason") or ""
            print(f"  ← {t} {extra}".rstrip())

            # Send the question once the session is ready.
            if t == "session_ready" and not question_sent:
                await send({"type": "message", "text": question})
                question_sent = True
                continue

            if t == "protocol_pending_confirmation" and drive_protocol:
                await send({
                    "type": "client.protocol_confirmation",
                    "tool_use_id": obj.get("tool_use_id"),
                    "decision": "accept",
                })
                continue

            if t == "protocol_proposed":
                active_protocol = obj
                continue

            if t in ("protocol_updated",):
                # Refresh tracked steps + detect completion.
                if obj.get("steps"):
                    if active_protocol is None:
                        active_protocol = {}
                    active_protocol["steps"] = obj["steps"]
                active_protocol = active_protocol or {}
                active_protocol["current_step_id"] = obj.get("current_step_id")
                if obj.get("status") == "completed" or obj.get("current_step_id") is None:
                    protocol_done = True
                continue

            if t == "protocol_completed":
                protocol_done = True
                continue

            if t == "turn_complete":
                # If a protocol step is awaiting a result, submit one and let the
                # agent react (next turn). Otherwise the session is idle.
                if drive_protocol and active_protocol and not protocol_done:
                    step_id = active_protocol.get("current_step_id")
                    steps = active_protocol.get("steps") or []
                    step = next((s for s in steps if s.get("id") == step_id), None)
                    if step and step_id and step_id not in answered_steps:
                        answered_steps.add(step_id)
                        await asyncio.sleep(0.4)  # human-ish pause (kept short)
                        await send(_step_result_for(step, fault_first_step, first_numeric_answered))
                        if step.get("type") == "numeric":
                            first_numeric_answered = True
                        continue
                # No pending protocol work → the turn that just ended is the last
                # one we care about. Give the bus a beat, then stop.
                if protocol_done or not drive_protocol:
                    await asyncio.sleep(0.5)
                    print("  (turn complete, nothing pending — ending capture)")
                    break

            if t in ("error", "session_terminated", "stream_error"):
                print(f"  !! terminal frame: {t} — ending capture")
                break

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"frames": frames}, ensure_ascii=False, indent=1), encoding="utf-8")
    n_recv = sum(1 for f in frames if f["dir"] == "recv")
    print(f"\nWrote {out_path} — {len(frames)} frames ({n_recv} server→client).")


# Keywords that mark a step measuring the suspected-dead rail (the MNT Reform's
# +1V2 from U13/MP2458 — the board's documented recurring failure). We answer
# those ~0 V so the protocol CONFIRMS the culprit, giving a coherent demo story.
_SUSPECT_KW = ("1v2", "1.2v", "+1v2", "u13", "c37", "buck", "mp2458")


def _step_result_for(step: dict, fault_first_step: bool, first_numeric_answered: bool) -> dict:
    """Plausible measurement for a step, crafted so the protocol tells a clean
    story: the rail tied to the known culprit reads ~0 V (dead), everything else
    reads healthy/nominal."""
    base = {"type": "protocol_step_result", "protocol_id": step.get("protocol_id"), "step_id": step.get("id")}
    kind = step.get("type")
    text = " ".join(str(step.get(k) or "") for k in ("target", "test_point", "instruction", "rationale")).lower()
    suspect = any(kw in text for kw in _SUSPECT_KW)
    if kind == "numeric":
        nominal = step.get("nominal")
        if suspect or (fault_first_step and not first_numeric_answered):
            value = 0.02  # dead rail — matches this board's repair history
        elif isinstance(nominal, (int, float)):
            value = round(float(nominal), 3)
        else:
            value = 3.3
        return {**base, "value": value, "unit": step.get("unit")}
    if kind == "boolean":
        # A suspect "is the rail present?" check fails; everything else passes.
        return {**base, "value": not suspect, "unit": None}
    if kind == "observation":
        return {**base, "value": "RAS à l'inspection visuelle.", "unit": None}
    return {**base, "value": True, "unit": None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost:8000")
    ap.add_argument("--slug", default="mnt-reform-motherboard")
    ap.add_argument("--repair", default="8023b32d6fa7")
    ap.add_argument("--tier", default="normal", choices=["fast", "normal", "deep"])
    ap.add_argument("--conv", default="new")
    ap.add_argument("--question", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--drive-protocol", action="store_true")
    ap.add_argument("--fault-first-step", action="store_true")
    ap.add_argument("--idle-timeout", type=float, default=90.0)
    args = ap.parse_args()

    from urllib.parse import quote
    url = (
        f"ws://{args.host}/ws/diagnostic/{quote(args.slug)}"
        f"?tier={args.tier}&repair={quote(args.repair)}&conv={quote(args.conv)}"
    )
    print(f"Connecting {url}\nQuestion: {args.question}\n")
    asyncio.run(capture(
        url=url,
        question=args.question,
        out_path=args.out,
        drive_protocol=args.drive_protocol,
        fault_first_step=args.fault_first_step,
        idle_timeout=args.idle_timeout,
    ))


if __name__ == "__main__":
    main()
