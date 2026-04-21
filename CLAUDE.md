# CLAUDE.md — microsolder-agent

Context file for Claude Code sessions on this repository.

## Project overview

`microsolder-agent` is an agent-native diagnostics workbench for board-level
electronics repair. Claude Opus 4.7 drives a three-panel UI (boardview,
schematic, chat+journal) through tool calls, in response to a microsoldering
technician's natural-language questions. The target demo board is the
Raspberry Pi 4 Model B (open hardware, public official schematics). Built for
the Anthropic × Cerebral Valley "Built with Opus 4.7" hackathon, April 21–26 2026.

## Hard rules — NEVER violate

1. **All code written from scratch during the hackathon week.** Never copy
   from any external codebase.
2. **Apache 2.0** is the license for all code in this repo.
3. **Permissive dependencies only** (MIT, Apache 2.0, BSD). Never pull in
   GPL, AGPL, or LGPL packages.
4. **Open hardware only.** No proprietary schematics or boardviews — no
   Apple, Samsung, ZXW, WUXINJI content. Target is the Raspberry Pi 4.
5. **No hallucinated component IDs.** Every refdes (e.g. `U7`, `C29`) the
   agent mentions must be validated against parsed board data *before* being
   shown to the user. Tools that cannot answer return structured
   null/unknown — never fake data.

## Stack

- **Backend:** Python 3.11+, FastAPI, uvicorn, Pydantic, WebSocket (native),
  pdfplumber, pytest
- **Agent:** Anthropic Python SDK — `claude-opus-4-7` for reasoning,
  `claude-haiku-4-5` for fast validation/formatting
- **Frontend:** Vanilla HTML + CSS + JS (no build step), Tailwind CSS via
  CDN, Alpine.js via CDN, PDF.js via CDN

## Layout

```
api/           FastAPI backend
  agent/       Claude orchestration, tool loop
  board/       Boardview parsing, component lookup
  session/     Per-session state, journal
  vision/      Image/PDF rendering helpers
  tools/       Tool-use handlers exposed to the agent
  telemetry/   Structured logs, metrics
web/           Vanilla frontend served from FastAPI
tests/         Pytest suite
```

## Development principles

- **Clean separation.** Top-level boundaries are `api/`, `web/`, `tests/`.
  Do not cross them without reason.
- **No God class.** Keep modules focused on one responsibility. If a file
  creeps past ~300 lines, ask whether it should split.
- **Tools return structured null/unknown, never fake data.** If a lookup
  fails, return `{"found": false, "reason": "..."}`. The agent will choose
  how to recover.
- **Anti-hallucination guardrail.** Before the agent's reply renders in the
  UI, validate every refdes against the parsed board. Drop or flag any that
  don't resolve.
- **Streaming over polling.** Agent output flows to the client through the
  WebSocket, token by token / event by event. Never batch a full response
  before sending.
- **Commit hygiene — one commit = one logical concern.** Descriptive English
  messages, conventional-commits style (`feat(scope):`, `fix(scope):`,
  `refactor(scope):`, `chore(scope):`, `docs(scope):`, `test(scope):`). Each
  commit passes tests and is independently reviewable by a hackathon judge
  reading the history.
  - Never bundle a cleanup (`git rm`, rename, dead-code removal) inside a
    feature or fix commit — it goes in its own `chore(scope):` or
    `refactor(scope):` commit even when the diff feels small.
  - Never bundle changes from two different domains (e.g. `web/` + `api/`
    pivots) into the same commit, even if they land in the same working
    session. Stage narrowly, commit narrowly.
  - If you catch yourself writing a commit message with two distinct body
    paragraphs describing unrelated things, **stop and split**.
  - Never rewrite history (`reset --soft`, `rebase -i`, `commit --amend`)
    once another agent has committed on top of yours — leave the sub-optimal
    commit and split better next time.
  - **Never `git push` without explicit authorization from Alexis** —
    committing locally is encouraged, pushing to `origin` is not. Always ask
    first (« tu veux que je push ? ») even if the commits look clean. This
    applies to `push`, `push --force`, `push --set-upstream`, and any
    equivalent. No exceptions, even for a trivial `docs:` commit.
- **Verify before declaring done.** Run `make test` before saying a change
  is complete. UI changes require a manual check in the browser.

## Models

- `ANTHROPIC_MODEL_MAIN` → `claude-opus-4-7` (agent reasoning, tool planning)
- `ANTHROPIC_MODEL_FAST` → `claude-haiku-4-5` (validation, formatting,
  cheap classification)

Both are loaded from `.env` via `api/config.py`.

## Hackathon prize-track context (background, not a rule)

Cerebral Valley announced a **$5 000 "best use of Managed Agents"** track on
top of the main hackathon prize. This is CONTEXT, not scope : do not warp
architectural choices to chase it. We use Managed Agents where they genuinely
fit — the **diagnostic conversation** path (persistent agent + memory store
per device + session event stream + custom tool use, cf. spec §2.3 Flow A). The
**pipeline** path stays on `messages.create` direct because it's batch and
doesn't benefit from session primitives. See
`docs/superpowers/plans/2026-04-22-v1-hackathon-shipping-plan.md` for the full
split rationale. Never mention prizes in commit messages, plans, or code —
keep the work technically-motivated.
