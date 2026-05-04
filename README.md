<p align="center">
  <img src="docs/assets/wrench-mascot.svg" alt="Wrench Board mascot" width="160" />
</p>

# Wrench Board

> Agent-native diagnostic workbench for board-level electronics repair,
> powered by Claude Opus 4.7. **Right-to-repair, built in the open, by the
> people who actually do the repairs.**

🥈 **2nd place** at Anthropic's *Build with Opus 4.7* hackathon — April 2026.

**📺 Demo video (3 min):** https://youtu.be/OZ2D_p82z6w

![Wrench Board — boardview + diagnostic agent on an MNT Reform motherboard](docs/assets/screenshot-workbench.png)

## What it is

Tens of millions of tonnes of electronics end up as e-waste every year. A
large share of that is recoverable at the board level — a dead capacitor, a
blown diode, a bad PMIC — but only a microsoldering technician can find and
fix it. We are the **last mile** of repair before the landfill, and there
are not many of us.

Wrench Board is a senior microsoldering teammate built for that last mile.
For the seasoned tech, it's a second pair of eyes that never gets tired.
For the apprentice, it's a senior teammate who explains the boot sequence
the tenth time, in their language, with their tools, without judgment. It ingests a schematic PDF and a
boardview, builds a per-device knowledge pack in two minutes, and runs an
Opus 4.7 diagnostic agent that pilots the board visually — highlights
pins, traces nets, simulates failures — while the technician keeps the
iron in their hand.

The bet is **precision over magic**. The agent is not allowed to invent a
reference designator. Every refdes it utters originates from a tool lookup,
and a server-side sanitizer wraps any token it cannot verify *before* the
text reaches the screen. The deterministic engines underneath produce
verifiable causal chains, not vibes.

## Why it exists

I've been a microsoldering technician for three years. For most of that
time, I sent screenshots to Claude one at a time, manually, and pasted the
answer into a paper notebook. I built the workbench I needed.

## How it works

Four orthogonal workflows feed a single on-disk corpus per device under
`memory/{slug}/`:

- **Knowledge Factory** — four Claude personas (Scout, Registry, Writers,
  Auditor) build a verified repair pack from a device label in ~2 minutes.
  The three Writers (Cartographe / Clinicien / Lexicographe) run in
  parallel and share a cache-warmed prefix to amortize the long shared
  input across writers.
- **Schematic Ingestion** — Opus 4.7 vision compiles a PDF schematic, page
  by page, into a queryable `ElectricalGraph`: nets classified, boot
  sequence inferred, quality report attached.
- **Diagnostic Agent** — an Anthropic Managed Agent per device, with a
  four-store layered memory (`global-patterns`, `global-playbooks`,
  `device-{slug}`, `repair-{repair_id}`), pilots the boardview through 12
  `bv_*` tools and queries the pack, schematic graph, measurements,
  validations and technician profile through ~24 more — 36 custom tools
  declared in `api/agent/manifest.py`. The agent never fabricates a
  refdes : tool discipline plus a post-hoc sanitizer.
- **microsolder-evolve** — four overnight search loops, one
  per surface : the deterministic simulator + hypothesize engines
  (`sim`), the schematic compiler (`pipeline`), the schematic vision
  pass (`pipeline-vision`), and the diagnostic agent itself (`agent`).
  Each loop proposes patches against an oracle benchmark and either
  keeps them (`evolve:`-prefixed commit) or reverts. The loops have
  been running and shipping improvements while I work on other things.

![Wrench Board — repair dashboard with knowledge artefacts and diagnostic threads](docs/assets/screenshot-dashboard.png)

### Files + Vision — the agent can ask to see

A microsoldering diagnosis lives or dies on what the probe is touching
*right now*, and a chat box can't carry that. The technician plugs a USB
microscope or webcam into the workbench and the agent requests a frame on
demand through the `cam_capture` tool, reads the image, and feeds it back
into its reasoning. The technician can also drop a macro shot or a close-up
of a suspect chip into the chat at any time. Captures and uploads are
persisted under the repair so a session can be replayed end-to-end —
words, decisions, and the actual photographs the agent looked at.

This closes the loop the screenshot-pasting workflow never could: the
agent stops *guessing* what the board looks like and starts *seeing* it,
on the technician's cue, on the technician's optics.

## Under the hood

- **Backend** — Python 3.11+ / FastAPI / native WebSocket / Pydantic v2 /
  pdfplumber. No build step, no bundler.
- **Frontend** — vanilla HTML + CSS + JS, OKLCH design tokens, D3 v7 for
  the boardview and knowledge graph. Inline SVG icons. No framework.
- **Models** — Claude Opus 4.7 (heavy pipeline writers, schematic vision,
  `deep` diagnostic tier), Claude Sonnet 4.6 (Scout, Registry, Mapper,
  Lexicographe, `normal` tier), Claude Haiku 4.5 (intent classifier, phase
  narrator, coverage gate, `fast` tier).
- **Memory** — per-device Anthropic Managed Agents memory stores. The
  agent self-orients across sessions by reading its own scribe notebook
  (`state.md`, `decisions/`, `measurements/`, `open_questions.md`)
  instead of relying on an LLM-generated resume.
- **Boardview** — 13 clean-room parsers in `api/board/parser/`, dispatched
  by extension: KiCad `.kicad_pcb`, OpenBoardView Test_Link `.brd`,
  KiCad-boardview BRD2, plus `.asc` `.bdv` `.bv` `.bvr` `.cad` `.cst`
  `.f2b` `.fz` `.gr` `.pcb` `.tvw`. Adding a format = one new file.
- **Tests** — 1 589 fast tests (~30 s) plus a `@slow` accuracy-gate suite,
  including 10 deterministic invariants on the simulator + hypothesize
  engines and frozen-oracle gates.
- **Tooling** — `make doctor` runs 8 local health checks (env, packs,
  parsers, camera) for atelier deployment. `make eval-all` orchestrates
  the four eval surfaces (simulator, pipeline, vision, agent) with
  cross-skill regression detection. `make tools-inventory` writes a
  local agent-manifest index for offline review.
- **Anti-hallucination** — defense in depth, two layers. (1) Tools return
  `{found: false, closest_matches: [...]}` for unknown refdes; the system
  prompt instructs the agent to pick from suggestions or ask the user.
  (2) `api/agent/sanitize.py` scans every outbound text for refdes-shaped
  tokens (`\b[A-Z]{1,3}\d{1,4}\b`) and wraps any unverified match as
  `⟨?U999⟩` before it reaches the technician.

Two pure-sync deterministic engines (`simulator.py`, `hypothesize.py`) sit
at the core of the diagnostic stack. The simulator advances phase-by-phase
over a boot sequence and emits a timeline of dead rails, dead components,
and the cause of blocking per phase. The hypothesizer takes a partial
observation and enumerates 1- and 2-fault refdes-kill candidates that
explain it, ranked by F1 against the observation. Neither calls an LLM at
runtime.

The diagnostic agent has two interchangeable runtimes — **managed** via
Anthropic Managed Agents, **direct** via the Messages API. Managed is the
default and the production path; direct serves as a fallback when the MA
beta is unavailable and as an on-disk inspection harness during
development. The WebSocket protocol is identical so the frontend doesn't
know which one is running.

## Roadmap — Community Evolution Loop

Wrench Board runs locally. Each technician's instance can improve its
deterministic simulator against their own field cases. When the evolve
loop discovers a rule that holds up, it surfaces a candidate pull request
to the upstream repo. Right-to-repair, built in the open, by the people
who actually do the repairs.

## Quickstart

```bash
git clone https://github.com/Junkz3/wrench-board
cd wrench-board
make install          # create .venv and install deps (incl. [dev])
cp .env.example .env  # then fill in ANTHROPIC_API_KEY
make run              # uvicorn --reload on http://localhost:8000
```

On the first `make run` in Managed Agents mode (default), the start
script prints a one-screen warning describing what it is about to create
on your Anthropic account (1 environment + 4 tier-scoped agents — idle,
no cost until used) and waits 5 seconds for Ctrl+C before bootstrapping.
The IDs land in `managed_ids.json` (gitignored) and subsequent runs go
straight to uvicorn.

Fallback to direct mode if the Managed Agents beta is unavailable on
your account — no bootstrap, plain `messages.create` tool loop :

```bash
make demo-fallback
# or: DIAGNOSTIC_MODE=direct make run
```

## License & credits

Source-available under a proprietary license — see [`LICENSE`](LICENSE).
Free for personal evaluation, study, and local use. **Independent
electronics repair professionals may also use it as an internal tool
when servicing their own clients** (commercial remuneration OK), with
no separate licence needed. Redistribution, hosted SaaS deployment,
sublicensing, and any use for training competing AI / ML models still
require written permission (contact: alexis@repairmind.co.uk).
Dependencies are MIT / Apache 2.0 / BSD only. The MNT Reform motherboard
used as the canonical test target is CERN-OHL-S-2.0. Built solo at
Repair Valley, an independent electronics repair workshop.

## Contributing

Wrench Board is open to contributors who care about right-to-repair.
Field reports, new boardview parsers, simulator rules — open an issue or
a PR.
