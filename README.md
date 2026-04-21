# microsolder-agent

> Board-level diagnostics on a $30 microscope — the $10,000 repair bench, rewritten by Claude.

## What it does

`microsolder-agent` is an agent-native diagnostics workbench for board-level
electronics repair. A microsoldering technician asks questions in natural
language — "where is the PMIC?", "why isn't the 3V3 rail coming up?" — and
Claude Opus 4.7 drives a three-panel UI in response, highlighting components,
pulling up schematic nets, and narrating the reasoning in a persistent repair
journal.

## How it works

The UI is split into three synchronized panels:

- **Boardview** — the physical board layout (image + component footprints).
- **Schematic** — the electrical logic (PDF-rendered schematic with nets).
- **Agent chat + journal** — conversation with Claude and a running repair log.

The agent is not a chatbot glued on top of buttons. It *is* the interface:
every pan/zoom/highlight/annotation happens through tool calls emitted by
Claude in response to the technician's questions. The user never clicks a
"find component" button — they ask, and the agent drives the view.

## Stack

- **Backend:** Python 3.11+, FastAPI, WebSocket, Pydantic, pdfplumber
- **Agent:** Anthropic Python SDK with `claude-opus-4-7` (reasoning) and
  `claude-haiku-4-5` (fast validation/formatting)
- **Frontend:** Vanilla HTML/CSS/JS, Tailwind CSS (CDN), Alpine.js (CDN),
  PDF.js (CDN) — no build step
- **Target board:** Raspberry Pi 4 Model B (open hardware, official public schematics)

## Quick start

```bash
make install          # create venv and install deps
cp .env.example .env  # then fill in ANTHROPIC_API_KEY
make run              # uvicorn --reload on http://localhost:8000
```

Run the tests with `make test`.

## Project status

In development — **Built with Opus 4.7 Hackathon 2026** (Anthropic × Cerebral Valley,
April 21–26 2026). All code written from scratch during the hackathon week.

## License

Apache 2.0 — see [LICENSE](LICENSE).
