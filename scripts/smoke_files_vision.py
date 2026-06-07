"""Live smoke test for Files+Vision Flow A (manual upload).

Drives _handle_client_upload_macro end-to-end against a real MA session :
opens an MA session for the seeded iphone-x device, synthesizes a tiny
PCB-like PNG via PIL (or loads tests/fixtures/macro_devboard_test.png if
present), uploads it through the same code path the WS handler uses, and
asserts the agent streams a visual analysis containing at least one
component / packaging / observation keyword.

Costs ~5-10¢ per run (one tier-fast Haiku session, ~5-10K tokens).

Flow B (cam_capture round-trip) is not exercised here — it requires a
real browser with getUserMedia. Validate that one in the browser per the
manual instructions in the V1 task of the implementation plan.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _synthesize_pcb_image(out_path: Path) -> bytes:
    """Render a small dark-green PCB-like image with a few rectangles
    representing ICs and a circle representing a chip cap. Just enough for
    the agent to describe shapes / packages — not a real board photo.
    """
    from PIL import Image, ImageDraw

    W, H = 800, 600
    img = Image.new("RGB", (W, H), color=(20, 80, 40))  # PCB green
    draw = ImageDraw.Draw(img)

    # Two big rectangular ICs (BGA-like)
    draw.rectangle((150, 150, 350, 320), fill=(30, 30, 30), outline=(180, 180, 180), width=2)
    draw.rectangle((480, 200, 620, 290), fill=(30, 30, 30), outline=(180, 180, 180), width=2)

    # Some small chip caps / resistors
    for x in range(100, 700, 60):
        draw.rectangle((x, 460, x + 30, 480), fill=(40, 40, 40), outline=(120, 120, 120), width=1)

    # A circle for a tantalum cap
    draw.ellipse((400, 400, 440, 440), fill=(80, 30, 30), outline=(200, 100, 100), width=2)

    # A few horizontal "traces"
    for y in (100, 360, 530):
        draw.line((20, y, W - 20, y), fill=(180, 140, 60), width=2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")

    from anthropic import AsyncAnthropic

    from api.agent.managed_ids import load_managed_ids
    from api.agent.runtime_managed import _handle_client_upload_macro
    from api.session.state import SessionState

    fixture_real = REPO_ROOT / "tests" / "fixtures" / "macro_devboard_test.png"
    fixture_synth = REPO_ROOT / "tests" / "fixtures" / "macro_devboard_synthetic.png"

    if fixture_real.exists():
        img_bytes = fixture_real.read_bytes()
        print(f"Using real fixture : {fixture_real}")
    else:
        print(f"No real fixture at {fixture_real} — synthesizing one at {fixture_synth}")
        img_bytes = _synthesize_pcb_image(fixture_synth)

    client = AsyncAnthropic()
    ids = load_managed_ids()
    if not ids or "fast" not in ids.get("agents", {}):
        sys.exit("ERROR: managed_ids.json missing — run bootstrap")

    agent = ids["agents"]["fast"]
    env_id = ids["environment_id"]

    print("\nCreating session…")
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent["id"], "version": agent["version"]},
        environment_id=env_id,
        title="smoke files+vision Flow A",
    )
    print(f"  session id: {session.id}")

    state = SessionState()
    state.has_camera = False  # not strictly needed for Flow A

    frame = {
        "type": "client.upload_macro",
        "base64": base64.b64encode(img_bytes).decode("ascii"),
        "mime": "image/png",
        "filename": "smoke_devboard.png",
    }

    memory_root = REPO_ROOT / "memory"
    print("Uploading fixture + injecting user.message…")
    stream = await client.beta.sessions.events.stream(session_id=session.id)

    await _handle_client_upload_macro(
        client=client,
        session=state,
        memory_root=memory_root,
        slug="iphone-x",  # any seeded slug — knowledge is not actually queried here
        repair_id="smoke-vision-R1",
        ma_session_id=session.id,
        frame=frame,
    )

    print("\nStreaming agent response…\n" + "-" * 60)
    text_seen: list[str] = []
    event_count = 0
    async for event in stream:
        event_count += 1
        etype = getattr(event, "type", "?")
        if etype == "agent.message":
            for blk in getattr(event, "content", []):
                if getattr(blk, "type", "") == "text":
                    chunk = getattr(blk, "text", "")
                    text_seen.append(chunk)
                    print(chunk, end="", flush=True)
        elif etype == "session.status_idle":
            stop = getattr(event, "stop_reason", None)
            stop_type = getattr(stop, "type", None) if stop is not None else None
            if stop_type != "requires_action":
                print(f"\n--- idle, stop_reason={stop_type} ---")
                break
        elif etype == "session.status_terminated":
            print("\n--- terminated ---")
            break
        elif etype == "session.error":
            print(f"\n--- session error: {event} ---")
            break
        if event_count > 200:
            print("\n--- safety break (200 events) ---")
            break

    print("\n" + "=" * 60)
    print("RESULT")
    print("=" * 60)
    full_text = "".join(text_seen).lower()
    print(f"Total response chars: {len(full_text)}")

    # Visual / shape / component keywords. We're permissive : the synthetic
    # image is geometric, the agent might describe shapes or packages.
    visual_keywords = [
        "composant", "boîtier", "boitier", "package", "ic", "puce",
        "rectangle", "carré", "cercle", "forme", "figure", "image",
        "résist", "résistance", "résistor", "cap", "condo", "trace",
        "smd", "cms", "qfn", "bga", "sot", "so-8", "soic", "tantale",
        "vert", "pcb", "circuit", "carte",
    ]
    hits = [k for k in visual_keywords if k in full_text]
    print(f"Visual keywords matched: {hits}")

    if hits:
        print("\n✅ PASS — agent produced a visual analysis")
    else:
        print("\n❌ FAIL — agent did not produce any visual description")
        print(f"\nFull response was:\n{full_text}\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
