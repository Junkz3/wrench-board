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
cat benchmark/weaknesses.md  # priority-ranked list of known gaps (READ-ONLY)
```

Tu dois en sortir avec :
- `baseline_score` (la cible à battre)
- `last_5_statuses` (pour décider si exploration mode)
- Liste des hypothèses récemment testées (pour ne pas répéter)
- Le `per_scenario` du dernier eval réussi (pour identifier les scénarios qui ratent)
- Les items P1 de `benchmark/weaknesses.md` — priorités explicites avec pointeurs fichier/fonction. **Préfère un item P1 non-résolu à une exploration ad-hoc** tant qu'il en reste. Quand une mutation `keep` résout un item P1, mentionne-le en description dans `results.tsv` (ex: `resolves P1: passive_fb open rail death`) — l'humain déplacera l'item en RESOLVED.

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
