# wrench-board

> A senior microsoldering technician, available to every repair shop.

## What it does

`wrench-board` is an agent-native diagnostics workbench for board-level
electronics repair. A technician asks questions in natural language —
"where is the PMIC?", "why isn't the 3V3 rail coming up?" — and Claude
highlights components, traces nets on the boardview, and narrates the
reasoning in a persistent repair journal.

The agent is not a chatbot glued on top of buttons. It *is* the interface:
pan, zoom, highlight, and net tracing happen through tool calls emitted by
Claude in response to the technician's questions.

## Stack

- **Backend:** Python 3.11+, FastAPI, WebSocket, Pydantic, pdfplumber
- **Agent:** Anthropic Python SDK with `claude-opus-4-7` (reasoning) and
  `claude-haiku-4-5` (validation, formatting)
- **Frontend:** Vanilla HTML, CSS, JS — no build step, no framework
- **Boards:** open-hardware only. Ships with KiCad `.kicad_pcb` support;
  parser architecture designed to extend to OpenBoardView formats.

## Quick start

```bash
make install          # create venv and install deps
cp .env.example .env  # then fill in ANTHROPIC_API_KEY
make run              # uvicorn --reload on http://localhost:8000
```

Run the tests with `make test`.

## Project status

In active development.

## License

Apache 2.0 — see [LICENSE](LICENSE).
