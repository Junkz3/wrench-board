# Microsolder Evolve Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implémenter le skill Claude Code `microsolder-evolve` + son runner bash + bootstrap, pour permettre à un agent Opus d'améliorer en boucle nocturne autonome le pipeline `simulator.py` + `hypothesize.py`, avec keep/discard via score scalaire et discipline git stricte.

**Architecture:** Markdown skill agent-side (`.claude/skills/microsolder-evolve/SKILL.md`) consommé par un runner bash (`scripts/evolve-runner.sh`) qui appelle `claude -p --system-prompt-file SKILL.md` en sessions fraîches. État persistant dans `evolve/state.json` + `evolve/results.tsv`. La métrique vient de `scripts/eval_simulator.py` (défini par la spec axes 2/3) sous forme JSON one-line.

**Tech Stack:** Bash, Python 3.11+, Make, git, Claude Code CLI (`claude -p`).

**Spec source:** `docs/superpowers/specs/2026-04-24-microsolder-evolve-skill-design.md` (commit `d3cad1e`).

**Pré-requis non gérés par ce plan** (de la spec axes 2/3) : `scripts/eval_simulator.py`, `api/pipeline/schematic/evaluator.py`, `benchmark/scenarios.jsonl`, `benchmark/sources/`. Le bootstrap (Task 2) **vérifie** leur présence et abort proprement si absents — il ne les crée pas.

---

## File Structure

| Fichier | Action | Responsabilité |
|---|---|---|
| `.claude/skills/microsolder-evolve/SKILL.md` | CREATE | Skill markdown agent-side : système prompt complet pour la boucle d'optimisation |
| `scripts/evolve-runner.sh` | CREATE | Boucle bash infinie : lockfile + invocation `claude -p` + sleep + log |
| `scripts/evolve-bootstrap.sh` | CREATE | Init unique : vérifie pré-requis, crée branche, mesure baseline, écrit `evolve/state.json` |
| `evolve/.gitkeep` | CREATE | Marker pour git du dossier (le contenu est gitignored sauf .gitkeep) |
| `evolve/reports/.gitkeep` | CREATE | Idem pour le sous-dossier reports |
| `.gitignore` | MODIFY | Ajouter `evolve/*` avec exception `!evolve/.gitkeep`, `!evolve/reports/.gitkeep` |
| `Makefile` | MODIFY | Ajouter cibles `evolve-bootstrap`, `evolve-run`, `evolve-stop`, `evolve-status` |
| `docs/EVOLVE.md` | CREATE | Doc utilisateur courte : comment lancer la 1ʳᵉ nuit, comment interpréter les résultats |

---

## Task 1: Directory scaffold + .gitignore

**Files:**
- Create: `evolve/.gitkeep`
- Create: `evolve/reports/.gitkeep`
- Modify: `.gitignore`

- [ ] **Step 1: Vérifier l'état git de départ**

```bash
cd /home/alex/Documents/hackathon-microsolder
git status --short
git branch --show-current
```

Expected: working tree clean ou seulement les specs untracked déjà connues. Branche : `main`.

- [ ] **Step 2: Créer les dossiers et marqueurs**

```bash
mkdir -p evolve/reports
touch evolve/.gitkeep evolve/reports/.gitkeep
```

Expected: création silencieuse. `ls -la evolve/` doit montrer `.gitkeep` et `reports/`.

- [ ] **Step 3: Lire le .gitignore actuel pour identifier où ajouter**

```bash
cat .gitignore
```

Expected: voir le contenu pour ajouter au bon endroit (en bas, dans une section dédiée).

- [ ] **Step 4: Ajouter les règles d'exclusion evolve**

Ajouter à la fin de `.gitignore` :

```
# Evolve loop state — locally-managed per-machine, do not version
evolve/*
!evolve/.gitkeep
!evolve/reports
evolve/reports/*
!evolve/reports/.gitkeep
```

Vérifier que les règles fonctionnent :

```bash
git check-ignore -v evolve/results.tsv evolve/state.json evolve/reports/2026-04-24.md
```

Expected: chaque fichier est marqué ignoré par la règle. `evolve/.gitkeep` et `evolve/reports/.gitkeep` ne sont PAS ignorés (vérifier avec `git status --short` qui doit les montrer comme `??`).

- [ ] **Step 5: Commit**

```bash
git add .gitignore evolve/.gitkeep evolve/reports/.gitkeep
git commit -m "$(cat <<'EOF'
feat(evolve): scaffold evolve/ directory + gitignore rules

Evolve loop state (results.tsv, state.json, reports/) lives outside git.
Only .gitkeep markers are versioned to preserve the directory layout.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 1 commit créé, working tree clean.

---

## Task 2: Bootstrap script

**Files:**
- Create: `scripts/evolve-bootstrap.sh`

Le bootstrap est lancé une fois par l'humain avant la 1ʳᵉ nuit. Il : (a) vérifie que tous les pré-requis externes existent, (b) crée la branche `evolve/<date>` si pas déjà sur une, (c) lance le baseline measure, (d) écrit `evolve/state.json` initial + `evolve/results.tsv` avec header.

- [ ] **Step 1: Créer le squelette du script**

```bash
touch scripts/evolve-bootstrap.sh
chmod +x scripts/evolve-bootstrap.sh
```

- [ ] **Step 2: Écrire le contenu complet**

`scripts/evolve-bootstrap.sh`:

```bash
#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Microsolder Evolve — Bootstrap (one-shot, run before the first night)
#
# Usage: scripts/evolve-bootstrap.sh
#
# Verifies all prerequisites (eval infra from spec axes 2/3, clean git tree),
# creates the evolve/<date> branch, measures baseline, writes evolve/state.json
# and evolve/results.tsv. Idempotent: safe to re-run.

set -euo pipefail
cd "$(dirname "$0")/.."

DATE=$(date +%Y-%m-%d)
EVOLVE_DIR="evolve"
RESULTS_TSV="$EVOLVE_DIR/results.tsv"
STATE_JSON="$EVOLVE_DIR/state.json"
BASELINE_LOG="/tmp/microsolder-evolve-baseline.json"

echo "==> Microsolder Evolve bootstrap ($DATE)"

# --- 1. Pre-requisite checks ---
echo "    Checking prerequisites..."

[ -d .git ] || { echo "ERROR: not a git repository"; exit 1; }

for f in scripts/eval_simulator.py api/pipeline/schematic/evaluator.py benchmark/scenarios.jsonl; do
  if [ ! -f "$f" ]; then
    echo "ERROR: missing prerequisite '$f'"
    echo "       This file must be implemented per the spec at"
    echo "       docs/superpowers/specs/2026-04-24-schematic-simulator-axes-2-3-design.md"
    exit 1
  fi
done

[ -d benchmark/sources ] || { echo "ERROR: missing benchmark/sources/ directory"; exit 1; }

SCENARIO_COUNT=$(wc -l < benchmark/scenarios.jsonl)
if [ "$SCENARIO_COUNT" -lt 10 ]; then
  echo "ERROR: benchmark/scenarios.jsonl has $SCENARIO_COUNT scenarios, need >= 10"
  exit 1
fi
echo "    Prerequisites OK ($SCENARIO_COUNT scenarios)"

# --- 2. Clean git tree check ---
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: tracked working tree dirty. Commit or stash before bootstrapping."
  exit 1
fi

# --- 3. Create or switch to evolve/<date> branch ---
CURRENT_BRANCH=$(git branch --show-current)
TARGET_BRANCH="evolve/$DATE"

if [[ "$CURRENT_BRANCH" =~ ^evolve/ ]]; then
  echo "    Already on evolve branch: $CURRENT_BRANCH"
  TARGET_BRANCH="$CURRENT_BRANCH"
else
  if git show-ref --verify --quiet "refs/heads/$TARGET_BRANCH"; then
    git checkout "$TARGET_BRANCH"
  else
    git checkout -b "$TARGET_BRANCH"
  fi
  echo "    Switched to branch: $TARGET_BRANCH"
fi

# --- 4. Init evolve/ ---
mkdir -p "$EVOLVE_DIR/reports"

if [ ! -f "$RESULTS_TSV" ]; then
  printf "timestamp\tcommit\tscore\tself_mrr\tcascade_recall\tstatus\tdescription\n" > "$RESULTS_TSV"
  echo "    Created $RESULTS_TSV (empty, header only)"
fi

# --- 5. Baseline measure ---
echo "    Measuring baseline (this may take a few minutes)..."
if ! timeout 600 python -m scripts.eval_simulator > "$BASELINE_LOG" 2>&1; then
  echo "ERROR: baseline eval_simulator failed. Output:"
  cat "$BASELINE_LOG"
  exit 1
fi

# --- 6. Validate JSON output and extract score ---
BASELINE_SCORE=$(python3 -c "
import json, sys
try:
    d = json.load(open('$BASELINE_LOG'))
    assert 'score' in d and 'self_mrr' in d and 'cascade_recall' in d, 'missing fields'
    print(d['score'])
except Exception as e:
    print(f'INVALID JSON: {e}', file=sys.stderr)
    sys.exit(1)
")

if [ -z "$BASELINE_SCORE" ]; then
  echo "ERROR: could not parse baseline score from $BASELINE_LOG"
  exit 1
fi

BASELINE_COMMIT=$(git rev-parse --short HEAD)

# --- 7. Write state.json ---
python3 -c "
import json
state = {
    'baseline_score': float('$BASELINE_SCORE'),
    'baseline_commit': '$BASELINE_COMMIT',
    'total_runs': 0,
    'last_run_at': None,
    'last_status': None,
    'last_5_statuses': [],
    'branch': '$TARGET_BRANCH',
}
with open('$STATE_JSON', 'w') as f:
    json.dump(state, f, indent=2)
print('    state.json written')
"

# --- 8. Append baseline as first entry in results.tsv ---
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SELF_MRR=$(python3 -c "import json; print(json.load(open('$BASELINE_LOG'))['self_mrr'])")
CASCADE=$(python3 -c "import json; print(json.load(open('$BASELINE_LOG'))['cascade_recall'])")
printf "%s\t%s\t%.6f\t%.6f\t%.6f\t%s\t%s\n" \
  "$TIMESTAMP" "$BASELINE_COMMIT" "$BASELINE_SCORE" "$SELF_MRR" "$CASCADE" \
  "baseline" "initial baseline measurement" >> "$RESULTS_TSV"

echo ""
echo "==> Bootstrap complete"
echo "    Branch:         $TARGET_BRANCH"
echo "    Baseline score: $BASELINE_SCORE (commit $BASELINE_COMMIT)"
echo "    Next:           ./scripts/evolve-runner.sh  (or: nohup ./scripts/evolve-runner.sh 2>&1 &)"
```

- [ ] **Step 3: Lint avec shellcheck**

```bash
shellcheck scripts/evolve-bootstrap.sh
```

Expected: no errors. Warnings acceptables s'ils sont commentés. Si shellcheck pas installé: `sudo apt install shellcheck`.

- [ ] **Step 4: Smoke test (dry mode — pré-requis manquants attendus)**

Sur la branche `main` (sans pré-requis), lancer le bootstrap doit échouer proprement :

```bash
./scripts/evolve-bootstrap.sh
echo "Exit code: $?"
```

Expected: exit 1, message clair "ERROR: missing prerequisite 'scripts/eval_simulator.py'", **aucun fichier créé** dans `evolve/` au-delà des `.gitkeep`, **pas de branche créée**.

Vérifier :

```bash
ls evolve/
git branch | grep evolve
```

Expected: que `.gitkeep` et `reports/`, pas de branche `evolve/<date>`.

- [ ] **Step 5: Commit**

```bash
git add scripts/evolve-bootstrap.sh
git commit -m "$(cat <<'EOF'
feat(evolve): bootstrap script for first-night setup

Verifies prerequisites (eval_simulator.py + benchmark/scenarios.jsonl
from spec axes 2/3), creates evolve/<date> branch, measures baseline,
initializes evolve/state.json and evolve/results.tsv. Idempotent.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Runner script

**Files:**
- Create: `scripts/evolve-runner.sh`

- [ ] **Step 1: Créer le squelette**

```bash
touch scripts/evolve-runner.sh
chmod +x scripts/evolve-runner.sh
```

- [ ] **Step 2: Écrire le contenu complet**

`scripts/evolve-runner.sh`:

```bash
#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Microsolder Evolve — Runner (infinite loop, agent-driven)
#
# Usage:
#   Foreground (smoke test):  ./scripts/evolve-runner.sh
#   Background (overnight):   nohup ./scripts/evolve-runner.sh 2>&1 &
#
# Override interval (default 60s between sessions):
#   EVOLVE_INTERVAL=120 ./scripts/evolve-runner.sh

set -uo pipefail
cd "$(dirname "$0")/.."

LOCKFILE="/tmp/microsolder-evolve.lock"
LOGFILE="/tmp/microsolder-evolve.log"
INTERVAL="${EVOLVE_INTERVAL:-60}"
SKILL_FILE=".claude/skills/microsolder-evolve/SKILL.md"

# --- Pre-flight ---
if [ ! -f "$SKILL_FILE" ]; then
  echo "ERROR: missing $SKILL_FILE — install the skill first." | tee -a "$LOGFILE"
  exit 1
fi

if [ ! -f "evolve/state.json" ] || [ ! -f "evolve/results.tsv" ]; then
  echo "ERROR: evolve/ not initialized. Run scripts/evolve-bootstrap.sh first." | tee -a "$LOGFILE"
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: 'claude' CLI not found in PATH." | tee -a "$LOGFILE"
  exit 1
fi

# --- Cleanup on exit ---
trap "rm -f $LOCKFILE; echo '[EVOLVE] runner stopped at $(date)' >> $LOGFILE; exit 0" EXIT INT TERM

echo "[EVOLVE] runner started at $(date)" >> "$LOGFILE"
echo "[EVOLVE] interval: ${INTERVAL}s, log: $LOGFILE" >> "$LOGFILE"

# --- Main loop ---
while true; do
  if [ -f "$LOCKFILE" ]; then
    # Another session is running (shouldn't happen since claude -p is synchronous,
    # but guards against accidental double-start).
    echo "[EVOLVE] lockfile present, skipping (PID in lock: $(cat $LOCKFILE))" >> "$LOGFILE"
    sleep "$INTERVAL"
    continue
  fi

  echo $$ > "$LOCKFILE"
  echo "" >> "$LOGFILE"
  echo "=== EVOLVE SESSION $(date) ===" >> "$LOGFILE"

  # Invoke a fresh Claude session with the skill as system prompt.
  # --max-turns 100: hard cap so a stuck session can't burn unlimited tokens.
  # --dangerously-skip-permissions: required for autonomous git/file ops.
  echo "Execute one evolve session." | claude -p \
    --dangerously-skip-permissions \
    --max-turns 100 \
    --system-prompt-file "$SKILL_FILE" \
    >> "$LOGFILE" 2>&1 || true

  echo "=== EVOLVE EXIT $(date) (exit=$?) ===" >> "$LOGFILE"
  rm -f "$LOCKFILE"

  sleep "$INTERVAL"
done
```

- [ ] **Step 3: Lint**

```bash
shellcheck scripts/evolve-runner.sh
```

Expected: no errors.

- [ ] **Step 4: Smoke test pre-flight (skill manquant attendu)**

Le runner doit refuser de démarrer si le skill n'existe pas encore :

```bash
./scripts/evolve-runner.sh
echo "Exit code: $?"
```

Expected: exit 1, message "ERROR: missing .claude/skills/microsolder-evolve/SKILL.md".

- [ ] **Step 5: Commit**

```bash
git add scripts/evolve-runner.sh
git commit -m "$(cat <<'EOF'
feat(evolve): bash runner for overnight Claude evolve loop

Infinite loop: lockfile, fresh `claude -p` session per iteration,
reads skill from .claude/skills/microsolder-evolve/SKILL.md.
Pre-flight checks for skill presence, evolve/ init, claude CLI on PATH.
Logs to /tmp/microsolder-evolve.log.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: SKILL.md (the heart)

**Files:**
- Create: `.claude/skills/microsolder-evolve/SKILL.md`

- [ ] **Step 1: Créer le dossier skill**

```bash
mkdir -p .claude/skills/microsolder-evolve
```

- [ ] **Step 2: Écrire le SKILL.md complet**

`.claude/skills/microsolder-evolve/SKILL.md`:

````markdown
---
name: microsolder-evolve
description: Boucle d'amélioration nocturne autonome du simulateur diagnostic microsolder. Pattern autoresearch — modifies simulator.py ou hypothesize.py, mesure via eval_simulator, garde ou jette via git. NEVER STOP, autonomie totale.
---

# Microsolder Evolve

## Mission

Tu es un agent Opus autonome qui améliore le pipeline de diagnostic électronique microsolder (`api/pipeline/schematic/simulator.py` et `api/pipeline/schematic/hypothesize.py`).

**Objectif scalaire :** maximiser `score = 0.6 × self_MRR + 0.4 × cascade_recall`. Plus haut = meilleur. Cette métrique vient de `scripts/eval_simulator.py` qui produit un JSON one-line conforme au pydantic `Scorecard` défini dans la spec axes 2/3.

**Tu ne t'arrêtes JAMAIS.** Le runner bash te relance toutes les 60 secondes en lançant une session fraîche. Ton job par session est : exécuter UNE itération propre (analyse → 1 hypothèse → édit → mesure → keep/discard → log) puis quitter. La nuit fait des centaines de sessions indépendantes.

## Surface d'édition

**TU PEUX éditer (et seulement ces fichiers) :**

- `api/pipeline/schematic/simulator.py`
- `api/pipeline/schematic/hypothesize.py`

**TU NE DOIS PAS toucher (READ-ONLY ABSOLU) :**

- `api/pipeline/schematic/schemas.py`
- `api/pipeline/schematic/evaluator.py`
- `scripts/eval_simulator.py`
- `benchmark/scenarios.jsonl` ni aucun fichier sous `benchmark/sources/`
- `config/settings.json`, `.env`
- Tout fichier sous `tests/`
- Tout autre fichier du repo non listé comme éditable

Si une amélioration nécessite de toucher un fichier read-only : **tu n'élargis pas la surface**. Tu logs `out-of-scope` dans `evolve/results.tsv` et tu quittes la session. L'humain reverra au matin.

## Setup (vérifications obligatoires au début de CHAQUE session)

Avant toute analyse ou édition, vérifier l'environnement :

```bash
# 1. On est sur une branche evolve/*
CURRENT_BRANCH=$(git branch --show-current)
if [[ ! "$CURRENT_BRANCH" =~ ^evolve/ ]]; then
  echo "ERROR: not on an evolve branch (current: $CURRENT_BRANCH). Run scripts/evolve-bootstrap.sh first."
  exit 1
fi

# 2. Pré-requis infra eval (peuvent disparaître entre sessions si l'humain refactor)
test -f scripts/eval_simulator.py || { echo "ERROR: eval_simulator.py disappeared"; exit 1; }
test -f benchmark/scenarios.jsonl || { echo "ERROR: scenarios.jsonl disappeared"; exit 1; }

# 3. State files
test -f evolve/state.json || { echo "ERROR: evolve/state.json missing — re-run bootstrap"; exit 1; }
test -f evolve/results.tsv || { echo "ERROR: evolve/results.tsv missing — re-run bootstrap"; exit 1; }

# 4. Working tree clean (tracked only — untracked OK)
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: tracked working tree dirty. An interrupted previous session left changes. Aborting safely."
  exit 1
fi
```

Si l'une échoue → afficher le message + quitter avec exit 1. Le runner réessaiera dans 60s.

## La boucle (9 étapes par session)

### Step 1 — Read state

```bash
cat evolve/state.json
tail -20 evolve/results.tsv
LATEST_REPORT=$(ls -t evolve/reports/ 2>/dev/null | head -1)
[ -n "$LATEST_REPORT" ] && cat "evolve/reports/$LATEST_REPORT"
```

Tu dois en sortir avec :
- `baseline_score` (la cible à battre)
- `last_5_statuses` (pour décider si exploration mode)
- Liste des hypothèses récemment testées (pour ne pas répéter)
- Le `per_scenario` du dernier eval réussi (pour identifier les scénarios qui ratent)

### Step 2 — Analyse

Identifier l'axe d'amélioration le plus actionnable :

- **Si `last_5_statuses` contient ≥ 3 `discard` consécutifs** → mode exploration : tu lis en profondeur `simulator.py`, `hypothesize.py`, `schemas.py` ET tu regardes le `per_scenario` détaillé pour comprendre où ça rate vraiment. Pas de hâte. Si tu n'as pas une hypothèse solide à la fin, log `status=skip-no-idea` et quitte (rare, mais préférable à lancer une mauvaise hypothèse).

- **Sinon** : à partir du `per_scenario` du dernier eval réussi, identifie soit :
  - 1 scénario du benchmark qui rate (cascade_recall faible pour ce scénario), comprends pourquoi en lisant le code,
  - OU 1 famille de pannes (refdes/mode) avec `self_mrr_contribution` faible, comprends pourquoi `hypothesize` ne retrouve pas la cause.

### Step 3 — Dispatch optionnel (multi-agent audit)

Si tu n'arrives pas à formuler une hypothèse claire, OU si tu sens qu'un audit multi-angle débloquerait, tu PEUX (pas obligatoire) invoquer le skill `superpowers:dispatching-parallel-agents` pour lancer 2-4 audit-agents en parallèle, chacun avec un angle différent. Exemples d'angles :

- "Trouve un mode de panne manquant pour les `passive_C` dans `simulator.py`"
- "Identifie pourquoi le scénario `<scenario_id>` rate dans `cascade_recall`"
- "Propose une amélioration de l'algorithme de scoring dans `hypothesize.py`"
- "Cherche des cascades downstream non propagées dans `_PASSIVE_CASCADE_TABLE`"

Synthèse des findings → tu retournes au step 4 avec UNE hypothèse fusionnée. Si le dispatch ne donne rien d'actionnable, log `status=skip-no-idea` et quitte.

### Step 4 — Propose UNE hypothèse

Formule en 1-2 phrases. Pas plus. Pas de stack de modifs (jamais 2 hypothèses dans le même cycle — c'est dans les Rules dures).

Exemple format :
> "Hypothèse : ajouter mode `intermittent_short` à `passive_C` dans simulator.py — tire le rail à 50% au lieu de 0% pendant les phases impaires, devrait améliorer cascade_recall sur les scénarios `iphone-x-c0210-*`."

### Step 5 — Pré-édit guard

Re-vérifier working tree clean (déjà fait au setup, mais double-check après les commandes du step 1) :

```bash
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ABORT: tracked working tree became dirty during analysis"
  exit 1
fi
```

### Step 6 — Édit

Modifier UNIQUEMENT `api/pipeline/schematic/simulator.py` et/ou `api/pipeline/schematic/hypothesize.py`. Si l'hypothèse demande de toucher autre chose (schemas, evaluator, fixtures, etc.) → ne pas éditer, écrire dans `evolve/results.tsv` :

```
<timestamp>	<baseline_commit>	0.000000	0.000000	0.000000	out-of-scope	<hypothèse> — needs <other_file> edit
```

Puis quitter.

### Step 7 — Mesure

```bash
timeout 600 python -m scripts.eval_simulator > /tmp/score.json 2> /tmp/score.err
EXIT_CODE=$?
```

Cas possibles :
- `EXIT_CODE == 0` ET `/tmp/score.json` est un JSON valide avec champ `score` → continuer step 8.
- `EXIT_CODE != 0` (crash bench, timeout, exception) OU JSON invalide → traiter comme **crash** (step 8 cas crash).

### Step 8 — Décide

```python
import json, subprocess
from datetime import datetime, timezone

state = json.load(open('evolve/state.json'))
baseline_score = state['baseline_score']
baseline_commit = state['baseline_commit']

try:
    result = json.load(open('/tmp/score.json'))
    new_score = result['score']
    new_self_mrr = result['self_mrr']
    new_cascade = result['cascade_recall']
    crashed = False
except Exception:
    crashed = True

timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
description = "<ta description courte sans tab ni newline>"
```

#### Cas KEEP (`new_score >= baseline_score` et pas de crash)

```bash
delta=$(python3 -c "print(f'{$new_score - $baseline_score:+.4f}')")
git add api/pipeline/schematic/
git commit -m "evolve: <description> (score: $new_score, $delta)"
NEW_COMMIT=$(git rev-parse --short HEAD)

# Update baseline in state.json
python3 -c "
import json
state = json.load(open('evolve/state.json'))
state['baseline_score'] = $new_score
state['baseline_commit'] = '$NEW_COMMIT'
json.dump(state, open('evolve/state.json', 'w'), indent=2)
"

# Append results.tsv
printf '%s\t%s\t%.6f\t%.6f\t%.6f\t%s\t%s\n' \
  "$timestamp" "$NEW_COMMIT" "$new_score" "$new_self_mrr" "$new_cascade" "keep" "$description" \
  >> evolve/results.tsv
```

#### Cas DISCARD (`new_score < baseline_score`, pas de crash)

```bash
git reset --hard HEAD  # annule l'édit non-committée

printf '%s\t%s\t%.6f\t%.6f\t%.6f\t%s\t%s\n' \
  "$timestamp" "$baseline_commit" "$new_score" "$new_self_mrr" "$new_cascade" "discard" "$description" \
  >> evolve/results.tsv
```

#### Cas CRASH (bench failed)

```bash
git reset --hard HEAD  # annule l'édit non-committée

ERR_EXCERPT=$(head -c 180 /tmp/score.err | tr '\n\t' '  ')
printf '%s\t%s\t%.6f\t%.6f\t%.6f\t%s\t%s\n' \
  "$timestamp" "$baseline_commit" "0.000000" "0.000000" "0.000000" "crash" "$description — $ERR_EXCERPT" \
  >> evolve/results.tsv
```

### Step 9 — Mini-report + state update + quit

```bash
REPORT_FILE="evolve/reports/$(date -u +%Y-%m-%d-%H%M).md"
cat > "$REPORT_FILE" <<EOF
# Evolve session $(date -u +%Y-%m-%dT%H:%M:%SZ)

**Hypothesis:** $description
**Score:** $baseline_score → $new_score (delta $delta)
**Status:** $status
EOF
```

Mettre à jour `evolve/state.json` :

```python
import json
from datetime import datetime, timezone

state = json.load(open('evolve/state.json'))
state['total_runs'] += 1
state['last_run_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
state['last_status'] = status  # "keep" | "discard" | "crash" | "out-of-scope" | "skip-no-idea"
state['last_5_statuses'] = (state['last_5_statuses'] + [status])[-5:]
json.dump(state, open('evolve/state.json', 'w'), indent=2)
```

Quitter la session (l'agent termine son tour). Le runner relance dans 60s.

## Schema `evolve/results.tsv`

Tab-separated, header obligatoire en 1ʳᵉ ligne :

```
timestamp	commit	score	self_mrr	cascade_recall	status	description
```

- `timestamp` : ISO 8601 UTC, ex `2026-04-25T03:14:22Z`
- `commit` : SHA court 7 chars. Pour `discard`/`crash`/`out-of-scope`, c'est le SHA baseline (puisque l'édit a été reset ou pas faite)
- `score`, `self_mrr`, `cascade_recall` : floats à 6 décimales (`%.6f`). Pour `crash`/`out-of-scope` → `0.000000`
- `status` ∈ `keep` | `discard` | `crash` | `out-of-scope` | `skip-no-idea` | `baseline`
- `description` : texte court (< 200 chars, no tab, no newline). Pour crash → inclure extrait stderr.

## Rules dures

1. **NEVER STOP.** Pas de "should I continue?". Tu fais ta session et tu quittes. Le runner gère le restart.
2. **One change at a time.** Jamais 2 hypothèses dans le même cycle. Si tu veux tester 2 idées, c'est 2 sessions.
3. **Always commit pré-édit guard.** Si tracked dirty au start ou pré-édit, abort proprement.
4. **Pas de `--no-verify`, pas de `git push`, pas de `git tag`, pas de `git rebase`.** La branche reste locale jusqu'à validation humaine au matin.
5. **Test set sacré.** Tu ne touches JAMAIS `benchmark/scenarios.jsonl` ni `benchmark/sources/`.
6. **Pas de `pytest.skip`, pas de tests désactivés.** Si un test casse à cause de ta modif, c'est un signal de régression — discard.
7. **Surface d'édition stricte.** `simulator.py` + `hypothesize.py`. Tout autre fichier touché → status `out-of-scope` + quit.

## Garde-fous

| Situation | Comportement |
|---|---|
| Bench > 10 min | `timeout 600` kill, status=`crash`, reset hard |
| 5 discards consécutifs | Mode exploration au prochain step 2 |
| Working tree dirty au start | Abort propre, pas de destruction, exit 1 |
| Crash bench (exit != 0 ou JSON invalide) | `git reset --hard HEAD`, status=`crash`, extrait stderr en description |
| Édit hors surface autorisée | Status=`out-of-scope`, quit, pas d'édit |
| Pas d'idée actionnable | Status=`skip-no-idea`, quit, pas d'édit |

## Reset cognitif (mode exploration)

Quand `last_5_statuses` contient ≥ 3 `discard` consécutifs :

1. Lire intégralement `api/pipeline/schematic/simulator.py` et `api/pipeline/schematic/hypothesize.py` (pas juste skim — vraie lecture).
2. Lire `api/pipeline/schematic/schemas.py` pour comprendre les types.
3. Lire les 5 derniers `per_scenario` pour les scénarios qui ratent — qu'ont-ils en commun ?
4. Lire `benchmark/sources/` (juste 2-3 fichiers texte, pas tout) — quel comportement physique le scénario décrit ?
5. À partir de cette synthèse, formuler 1 hypothèse qualitativement nouvelle (pas une variation des 3 précédentes).

Si après ça tu n'as toujours pas d'idée → status=`skip-no-idea`. C'est OK. La nuit fera plein d'autres sessions.
````

- [ ] **Step 3: Vérifier le frontmatter YAML**

```bash
head -5 .claude/skills/microsolder-evolve/SKILL.md
```

Expected: lignes `---`, `name: microsolder-evolve`, `description: ...`, `---`. Le frontmatter doit être valide YAML.

```bash
python3 -c "
import yaml
content = open('.claude/skills/microsolder-evolve/SKILL.md').read()
parts = content.split('---', 2)
assert len(parts) >= 3, 'frontmatter delimiters missing'
fm = yaml.safe_load(parts[1])
assert 'name' in fm and 'description' in fm, 'missing required fields'
print(f'OK: name={fm[\"name\"]}, description={fm[\"description\"][:80]}...')
"
```

Expected: `OK: name=microsolder-evolve, description=Boucle...`

- [ ] **Step 4: Vérifier le runner accepte maintenant le skill**

```bash
./scripts/evolve-runner.sh
echo "Exit code: $?"
```

Expected: cette fois la pre-flight `[ ! -f "$SKILL_FILE" ]` passe. Mais il bloque sur la pre-flight `evolve/state.json` manquant (pas encore bootstrappé). Message : "ERROR: evolve/ not initialized. Run scripts/evolve-bootstrap.sh first."

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/microsolder-evolve/SKILL.md
git commit -m "$(cat <<'EOF'
feat(evolve): SKILL.md for microsolder-evolve agent loop

System prompt for autonomous Opus loop. Defines mission (maximize
0.6·self_MRR + 0.4·cascade_recall), edit surface (simulator.py +
hypothesize.py only), 9-step loop (read state → analyse → optional
multi-agent audit → propose → guard → edit → measure → decide →
report), TSV schema, hard rules and guardrails.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Makefile targets

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Lire le Makefile actuel**

```bash
cat Makefile
```

Expected: identifier les cibles existantes (`install`, `run`, `test`, `lint`, `format`, `clean`) et le style (tab indent, `.PHONY`, etc.) pour rester cohérent.

- [ ] **Step 2: Ajouter les cibles evolve à la fin du Makefile**

À la fin de `Makefile`, ajouter :

```makefile

# --- Evolve (overnight self-improvement loop) ---

.PHONY: evolve-bootstrap evolve-run evolve-stop evolve-status

evolve-bootstrap:
	@./scripts/evolve-bootstrap.sh

evolve-run:
	@./scripts/evolve-runner.sh

evolve-run-bg:
	@nohup ./scripts/evolve-runner.sh >> /tmp/microsolder-evolve.log 2>&1 &
	@echo "Evolve runner started in background. Tail: tail -f /tmp/microsolder-evolve.log"
	@echo "Stop:  make evolve-stop"

evolve-stop:
	@if [ -f /tmp/microsolder-evolve.lock ]; then \
		PID=$$(cat /tmp/microsolder-evolve.lock); \
		echo "Killing runner PID $$PID"; \
		kill $$PID 2>/dev/null || true; \
		rm -f /tmp/microsolder-evolve.lock; \
	fi
	@pkill -f evolve-runner.sh 2>/dev/null || true
	@echo "Evolve runner stopped."

evolve-status:
	@echo "=== State ==="
	@cat evolve/state.json 2>/dev/null || echo "(not initialized)"
	@echo ""
	@echo "=== Last 10 results ==="
	@tail -10 evolve/results.tsv 2>/dev/null || echo "(no results yet)"
	@echo ""
	@echo "=== Lock ==="
	@if [ -f /tmp/microsolder-evolve.lock ]; then echo "Locked by PID $$(cat /tmp/microsolder-evolve.lock)"; else echo "No lock"; fi
	@echo ""
	@echo "=== Last 20 log lines ==="
	@tail -20 /tmp/microsolder-evolve.log 2>/dev/null || echo "(no log)"
```

- [ ] **Step 3: Tester chaque cible**

```bash
make evolve-status  # avant bootstrap, doit dire "(not initialized)" et "(no results yet)"
```

Expected: pas de crash, messages explicites pour l'absence des fichiers.

```bash
make evolve-stop  # rien à arrêter, doit juste afficher "Evolve runner stopped."
```

Expected: pas de crash.

- [ ] **Step 4: Commit**

```bash
git add Makefile
git commit -m "$(cat <<'EOF'
feat(evolve): Makefile targets for evolve lifecycle

evolve-bootstrap: one-shot init (calls scripts/evolve-bootstrap.sh)
evolve-run:       foreground runner (smoke test)
evolve-run-bg:    background runner via nohup
evolve-stop:      kill runner via lockfile + pkill fallback
evolve-status:    show state.json, last 10 results, lock state, log tail

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Validation manuelle (smoke + scenarios spec §9)

**Files:** none (manual ops + observation)

Cette task n'est exécutable qu'**après** que la spec axes 2/3 soit implémentée (`scripts/eval_simulator.py` + `benchmark/scenarios.jsonl` doivent exister). Avant ça, sauter en notant le blocage.

- [ ] **Step 1: Vérifier que les pré-requis axes 2/3 sont en place**

```bash
ls scripts/eval_simulator.py api/pipeline/schematic/evaluator.py benchmark/scenarios.jsonl benchmark/sources/ 2>&1
wc -l benchmark/scenarios.jsonl
```

Expected: tous présents, `scenarios.jsonl` ≥ 10 lignes.

**Si manquant** : marquer Task 6 comme bloquée par axes 2/3, ne pas continuer. Cette task se débloquera après l'autre plan.

- [ ] **Step 2: Smoke test bootstrap**

```bash
make evolve-bootstrap
```

Expected output (succinct) :
```
==> Microsolder Evolve bootstrap (2026-04-XX)
    Checking prerequisites...
    Prerequisites OK (NN scenarios)
    Switched to branch: evolve/2026-04-XX
    Created evolve/results.tsv (empty, header only)
    Measuring baseline (this may take a few minutes)...
    state.json written

==> Bootstrap complete
    Branch:         evolve/2026-04-XX
    Baseline score: 0.XXXXXX (commit XXXXXXX)
    Next:           ./scripts/evolve-runner.sh
```

Vérifier :
```bash
cat evolve/state.json   # baseline_score doit matcher
cat evolve/results.tsv  # header + 1 ligne baseline
git branch --show-current  # evolve/<date>
```

- [ ] **Step 3: Smoke test single-run du runner (foreground)**

```bash
EVOLVE_INTERVAL=10 ./scripts/evolve-runner.sh
```

Laisser tourner 3-5 itérations puis Ctrl+C. Pendant ce temps, dans un autre terminal :

```bash
tail -f /tmp/microsolder-evolve.log
```

Vérifier visuellement à chaque session :
- `=== EVOLVE SESSION ... ===` apparaît dans le log
- L'agent lit l'état, propose une hypothèse, édite, mesure, décide
- `evolve/results.tsv` reçoit une nouvelle ligne par session avec le bon format
- Pour `keep` : nouveau commit `evolve: ...` apparaît dans `git log evolve/<date>`
- Pour `discard` : pas de nouveau commit, `git status` clean après session
- `evolve/reports/<timestamp>.md` est écrit
- L'agent reste dans la surface autorisée (vérifier avec `git diff evolve/<date>~5..evolve/<date> -- :^api/pipeline/schematic/simulator.py :^api/pipeline/schematic/hypothesize.py` doit être vide)

- [ ] **Step 4: Test crash bench (injection volontaire)**

Pendant que le runner tourne (ou en single-shot), modifier temporairement `scripts/eval_simulator.py` pour qu'il crash :

```bash
# Sauvegarde
cp scripts/eval_simulator.py /tmp/eval_simulator.py.bak

# Injection crash (en début de fichier, après les imports)
python3 -c "
content = open('scripts/eval_simulator.py').read()
content = content.replace('def main', 'raise RuntimeError(\"smoke test crash\")\\n\\ndef main', 1)
open('scripts/eval_simulator.py', 'w').write(content)
"

# Lancer 1 session
EVOLVE_INTERVAL=10 timeout 120 ./scripts/evolve-runner.sh

# Restore
cp /tmp/eval_simulator.py.bak scripts/eval_simulator.py
```

Expected dans `evolve/results.tsv` : 1 ligne avec `status=crash`, `score=0.000000`, description contenant "smoke test crash". `git status` doit être clean après (reset hard a fonctionné).

- [ ] **Step 5: Test dirty tree**

```bash
# Laisser une modif non-committée
echo "# touched" >> api/pipeline/schematic/simulator.py

# Lancer 1 session
EVOLVE_INTERVAL=10 timeout 60 ./scripts/evolve-runner.sh
```

Expected : l'agent abort proprement, le log contient "ABORT: tracked working tree dirty", **aucune destruction** de la modif laissée. Vérifier :

```bash
git diff api/pipeline/schematic/simulator.py  # doit toujours montrer le "# touched"
```

Restore :
```bash
git checkout -- api/pipeline/schematic/simulator.py
```

- [ ] **Step 6: Test out-of-scope**

Difficile à forcer artificiellement (dépend de ce que l'agent décide). Vérifier indirectement après quelques sessions : si le log contient au moins une entrée `out-of-scope` au cours d'une nuit, c'est que le garde-fou fonctionne. Si jamais aucune session n'a produit d'`out-of-scope`, on peut considérer le test comme passant par défaut (l'agent reste dans la surface).

- [ ] **Step 7: Documenter les résultats du smoke test**

Créer `evolve/reports/SMOKE-TEST-RESULTS.md` avec un résumé court (5-10 lignes) :

```markdown
# Smoke test results — YYYY-MM-DD

- Bootstrap: OK (baseline X.XXXXXX)
- Runner foreground 5 sessions: N keep / M discard / K crash
- Crash injection: detected, status=crash, no destruction
- Dirty tree: aborted cleanly, no destruction
- Surface: agent stayed within simulator.py + hypothesize.py
- Verdict: ready for overnight | blocked by <issue>
```

Pas committé (gitignored par evolve/*).

---

## Task 7: User documentation

**Files:**
- Create: `docs/EVOLVE.md`

- [ ] **Step 1: Écrire la doc usage**

`docs/EVOLVE.md`:

```markdown
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
```

- [ ] **Step 2: Vérifier le rendu markdown**

```bash
head -30 docs/EVOLVE.md
```

Expected: rendu propre, pas de markdown cassé.

- [ ] **Step 3: Commit**

```bash
git add docs/EVOLVE.md
git commit -m "$(cat <<'EOF'
docs(evolve): user-facing usage guide

Bootstrap, run, stop, status, reading results, morning merge workflow,
file conventions, troubleshooting. Points to spec and plan for design
context.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Checklist

Avant de marquer ce plan comme prêt, vérifier :

- [ ] **Couverture spec :**
  - §1 Goal — couvert par Tasks 1-7 collectivement
  - §3 Non-goals — respectés (pas de généricité, pas d'eval infra créée ici, pas de validation humaine)
  - §4.1 Skill SKILL.md — Task 4
  - §4.2 Runner — Task 3
  - §4.3 Contrat eval — consommé par Task 4 step 7, vérifié au bootstrap Task 2 step 6
  - §5 Boucle 9 étapes — Task 4 step 2 contient toute la boucle dans le SKILL.md
  - §6 Schéma `results.tsv` — Task 4 schema, Task 2 init, Task 4 append
  - §7 Pré-requis — Task 2 vérifie tout
  - §8 Garde-fous — Task 4 (rules + guardrails dans le skill)
  - §9 Tests/validation — Task 6 couvre smoke, crash, dirty tree
  - §10 Risques — pas de tâche dédiée mais mitigations en place via Tasks 4 + 6

- [ ] **Pas de placeholders** : rechercher dans le plan `TBD`, `TODO`, `implement later`, `fill in`, `appropriate error handling`. Aucun trouvé.

- [ ] **Cohérence types/noms** :
  - `evolve/state.json` champs : `baseline_score`, `baseline_commit`, `total_runs`, `last_run_at`, `last_status`, `last_5_statuses`, `branch` — utilisés cohéremment dans Tasks 2, 4.
  - `evolve/results.tsv` colonnes : `timestamp commit score self_mrr cascade_recall status description` — alignées entre Tasks 2 (init), 4 (append).
  - Status enum : `keep` | `discard` | `crash` | `out-of-scope` | `skip-no-idea` | `baseline` — Task 4 schema + Task 4 boucle + Task 2 baseline.
  - Skill path : `.claude/skills/microsolder-evolve/SKILL.md` — utilisé identique en Tasks 3, 4, 5.
  - Lockfile path : `/tmp/microsolder-evolve.lock` — utilisé en Tasks 3, 5.

---

## Execution dependencies

- **Task 1** : indépendant, peut commencer immédiatement.
- **Tasks 2, 3, 4, 5** : indépendants entre eux, peuvent être faits en parallèle après Task 1. Dépendent uniquement de la structure dir de Task 1.
- **Task 6** (validation manuelle) : **bloquée** jusqu'à ce que la spec axes 2/3 soit implémentée. Tâche à faire en dernier, manuellement.
- **Task 7** (doc) : peut être fait à n'importe quel moment après Task 1, mais utile de l'avoir avant Task 6 pour que le smoke test puisse référencer la doc.

Ordre suggéré : Task 1 → Tasks 2+3+4+5 (en parallèle si subagent-driven) → Task 7 → Task 6 (quand axes 2/3 prêt).
