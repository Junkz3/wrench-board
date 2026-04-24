# Microsolder Evolve — Overnight self-improvement loop

A bash + Claude Code loop that mutates `simulator.py` / `hypothesize.py`
overnight, measures via `eval_simulator`, and keeps or discards via git.

## Prerequisites

- The schematic-simulator-axes-2-3 spec must be implemented first:
  `scripts/eval_simulator.py`, `api/pipeline/schematic/evaluator.py`,
  `benchmark/scenarios.jsonl` (>= 10 sourced scenarios), `benchmark/sources/`.
- Working tree clean.
- Claude Code CLI installed and authenticated (`claude --version`).

## First night setup

```bash
make evolve-bootstrap   # one-shot: creates branch, measures baseline, inits state
make evolve-status      # sanity check
make evolve-run-bg      # launch overnight loop in background
```

Stop in the morning:

```bash
make evolve-stop
```

## What happens overnight

The runner spawns a fresh `claude -p` session every 60 seconds. Each session
loads the skill at `.claude/skills/microsolder-evolve/SKILL.md`, executes
**one** evolve iteration (analyse → propose → edit → measure → keep/discard
→ log), and quits. Sessions are independent — state lives in `evolve/`.

Expected throughput: ~30-60 sessions per night depending on per-session latency.

## Reading the results in the morning

```bash
make evolve-status              # quick summary
column -t -s $'\t' evolve/results.tsv | tail -30   # last 30 experiments, aligned
git log --oneline evolve/$(date +%Y-%m-%d)         # commits kept this night
git diff main..evolve/$(date +%Y-%m-%d) -- api/pipeline/schematic/    # net change
```

To compare baseline vs final score:

```bash
head -2 evolve/results.tsv | tail -1 | cut -f3   # baseline score
tail -1 evolve/results.tsv | cut -f3             # last score (may be discard)
# Or look at state.json baseline_score for the current best
python3 -c "import json; print(json.load(open('evolve/state.json'))['baseline_score'])"
```

## Merging the night's improvements

Review the diff, then:

```bash
git checkout main
git merge --squash evolve/$(date +%Y-%m-%d)
git commit -m "feat(simulator): overnight evolve improvements (score X→Y)"
```

Or cherry-pick individual commits if you only want some.

## Files and conventions

| Path | Role | Versioned? |
|---|---|---|
| `.claude/skills/microsolder-evolve/SKILL.md` | The agent system prompt | Yes |
| `scripts/evolve-runner.sh` | Bash loop | Yes |
| `scripts/evolve-bootstrap.sh` | One-shot init | Yes |
| `evolve/state.json` | Baseline + run counter | No (gitignored, per-machine) |
| `evolve/results.tsv` | Append-only experiment log | No |
| `evolve/reports/*.md` | Per-session mini-reports | No |
| `/tmp/microsolder-evolve.log` | Runner stdout/stderr | No (transient) |
| `/tmp/microsolder-evolve.lock` | Lockfile (PID of running session) | No |

## Troubleshooting

- **Runner exits immediately**: run `./scripts/evolve-runner.sh` in foreground
  to see the pre-flight error. Common causes: skill missing, `evolve/` not
  bootstrapped, `claude` CLI not on PATH.
- **All sessions return `crash`**: `eval_simulator.py` is broken upstream of
  the agent. Check `tail /tmp/microsolder-evolve.log`, fix the eval, re-bootstrap.
- **All sessions return `discard`**: the agent is unable to find improvements.
  This is normal at saturation. After 3 consecutive discards the agent enters
  exploration mode automatically. If it persists for the whole night, the
  current local maximum is probably reached — consider a manual architectural
  change before the next night.
- **`out-of-scope` entries in TSV**: the agent tried to edit a read-only file
  (typically because a real improvement requires touching `evaluator.py` or
  `schemas.py`). Review these manually — they're hints from the agent about
  what would unlock further progress.

## See also

- Spec: `docs/superpowers/specs/2026-04-24-microsolder-evolve-skill-design.md`
- Plan: `docs/superpowers/plans/2026-04-24-microsolder-evolve-skill.md`
- Eval infra spec: `docs/superpowers/specs/2026-04-24-schematic-simulator-axes-2-3-design.md`
