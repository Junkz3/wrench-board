---
name: microsolder-evolve-monitor
description: Lance, surveille, et reporte sur la boucle nocturne `microsolder-evolve`. À utiliser quand Alexis dit "monitor evolve", "lance evolve", "status evolve", "où on en est sur evolve", "stop evolve", ou demande un avis sur les commits récents du runner. Mode one-shot via `/monitor-evolve` (résumé immédiat) ou continu via `/loop /monitor-evolve` (auto-paced via ScheduleWakeup, surface uniquement les events notables).
---

# Microsolder Evolve — Monitor

## Mission

Tu es l'observateur d'Alexis sur la boucle `scripts/evolve-runner.sh`. Tu **ne touches pas le code** que le runner produit (c'est le job de l'agent evolve, skill `microsolder-evolve`). Ton job ici :

1. **Cycle de vie du runner** : check up, propose-launch si down, kill propre sur demande.
2. **Lecture d'état** : `evolve/state.json`, `evolve/results.tsv` (via cursor incrémental), `/tmp/microsolder-evolve.log`.
3. **Classification des nouvelles lignes** depuis le dernier check : ✅ vrai fix / ⚠️ convention défendable / ❌ gaming-suspect, en t'inspirant des patterns documentés dans `evolve/reviews/2026-04-24-1750-batch1.md` et `benchmark/weaknesses.md`.
4. **Cadence adaptive** via `ScheduleWakeup` : 600s à 1800s selon l'activité.
5. **Stop propre** sur demande d'Alexis.

## Modes d'invocation

| Invocation | Mode | Comportement |
|---|---|---|
| `/monitor-evolve` (one-shot) | A | Toujours produire un summary complet, même si rien de neuf depuis le dernier cursor. Pas de ScheduleWakeup en sortie. |
| `/loop /monitor-evolve` (continu) | B | Si nouvelles lignes notables → message court à Alexis. Sinon silencieux. ScheduleWakeup pour le prochain tick. |

Le skill détecte le mode via la présence d'un sentinel `<<autonomous-loop-dynamic>>` dans le prompt fired (mode B) vs prompt utilisateur direct (mode A). En cas de doute, traite comme A.

## Étapes du tick (chaque invocation)

### Step 1 — Setup checks

```bash
test -f scripts/evolve-runner.sh || { echo "ERROR: scripts/evolve-runner.sh missing — wrong cwd?"; exit 1; }
test -f evolve/state.json       || { echo "ERROR: evolve/state.json missing — run scripts/evolve-bootstrap.sh first"; exit 1; }
test -f evolve/results.tsv      || { echo "ERROR: evolve/results.tsv missing — run scripts/evolve-bootstrap.sh first"; exit 1; }
```

Si une vérif rate → message clair à Alexis, exit 1, **pas** de ScheduleWakeup.

### Step 2 — Lifecycle du runner

```bash
RUNNER_PID=$(pgrep -f "scripts/evolve-runner.sh" | head -1)
LOCKFILE="/tmp/microsolder-evolve.lock"
LOGFILE="/tmp/microsolder-evolve.log"
LAST_RUN_AT=$(.venv/bin/python -c "import json; print(json.load(open('evolve/state.json')).get('last_run_at','?'))")
```

Décision :

- **Runner up** (`$RUNNER_PID` non-vide) → continue step 3.
- **Runner down ET Alexis n'a rien dit** → mode A: propose-launch (« Le runner est mort depuis $LAST_RUN_AT. Tu veux que je le relance avec `nohup ./scripts/evolve-runner.sh > /tmp/microsolder-evolve.log 2>&1 &` ? »). Mode B: silencieux + reschedule 1800s.
- **Runner down ET Alexis a dit "lance"** → lance-le :
  ```bash
  nohup ./scripts/evolve-runner.sh > /tmp/microsolder-evolve.log 2>&1 &
  disown
  sleep 2
  pgrep -f "scripts/evolve-runner.sh" | head -1  # confirme PID
  ```
- **Alexis a dit "stop evolve" / "tue le runner"** :
  ```bash
  pkill -f "scripts/evolve-runner.sh"
  sleep 1
  rm -f /tmp/microsolder-evolve.lock
  ```
  Confirme à Alexis. Pas de reschedule.

### Step 3 — Lecture d'état + cursor

Le cursor `evolve/.monitor_cursor` est un JSON compact :
```json
{"last_results_lineno": 42, "last_seen_state_run": 18, "last_check_ts": "2026-04-25T10:30:00Z"}
```

(Auto-créé au premier tick, auto-couvert par `evolve/*` dans `.gitignore`.)

```bash
.venv/bin/python <<'PY'
import json, os
from pathlib import Path
cursor_path = Path("evolve/.monitor_cursor")
cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {"last_results_lineno": 0, "last_seen_state_run": 0, "last_check_ts": None}
state = json.load(open("evolve/state.json"))
results_lines = Path("evolve/results.tsv").read_text().splitlines()
new_rows = results_lines[cursor["last_results_lineno"]:] if cursor["last_results_lineno"] < len(results_lines) else []
print(json.dumps({
    "cursor": cursor,
    "state": state,
    "new_rows": new_rows,
    "total_results_lines": len(results_lines),
}))
PY
```

Tail du log pour contexte (run actuel ou crash récent) :
```bash
tail -n 30 /tmp/microsolder-evolve.log 2>/dev/null
```

### Step 4 — Classification des nouvelles lignes

Pour chaque ligne dans `new_rows` (skip header si `last_results_lineno == 0`), lire le 6e champ TSV (`status`) :

| Status | Action | Avis à produire ? |
|---|---|---|
| `keep` | Lire `git show <sha> -- api/pipeline/schematic/`, classifier ✅ / ⚠️ / ❌ via les patterns ci-dessous, 1 ligne d'avis | **OUI** (en mode A et B) |
| `discard` | Compter consécutifs depuis dernier non-discard | OUI si **3+ consécutifs** (alerte mode exploration) |
| `crash` | Extract stderr depuis description | OUI direct |
| `propose-evaluator-fix` | Pointer vers `evolve/proposals/` (dernier fichier) | OUI direct |
| `out-of-scope` | Note brève dans summary mode A, silencieux mode B | A: oui, B: non |
| `skip-no-idea` | Compter | OUI si **3+ consécutifs** (l'agent stagne) |
| `review-checkpoint` | L'auto-review a déjà écrit un MD dans `evolve/reviews/` | OUI : pointer vers le fichier |
| `baseline` | Bootstrap row, ignore | non |

#### Patterns ✅ / ⚠️ / ❌ pour classer un `keep`

Inspire-toi de `evolve/reviews/2026-04-24-1750-batch1.md` (la review humaine référence).

**❌ Gaming-suspect** — flagger immédiatement :
- Le diff marque un composant `dead` dans `_apply_failures_at_init` **sans cause upstream** (pas de `power_in` sur un rail mort, pas de source IC dead, juste pour casser un tie cluster).
- La justification du commit message contient « set-level », « oracle », « fingerprint », « tie cluster » sans **aussi** mentionner un mécanisme physique réel.
- Pattern « if mode produces no observable effect → mark component dead anyway » — c'est exactement le pattern `f33d2da` / `7b821cf` / `e09dd47` qui a été reverted.
- Aucun garde par `comp.role` ou `comp.kind` quand le pattern devrait en avoir.

**⚠️ Convention défendable** — flagger pour relecture :
- Mécanisme physique plausible mais **non testé en bench** sur ce mode (cascade_recall inchangé, gain entièrement sur self_mrr).
- Filtrage par `comp.role` partiel (couvre series mais pas pull_up/feedback alors que les deux pourraient s'appliquer).
- No-op sur la sample window actuelle mais défensif pour topologies futures (typique de `512eec1`, `a88e8b8`).

**✅ Vrai fix** :
- Cascade physique réelle (`cascade_recall` monte ou tient à 1.0 via un canal légitime comme `_forced_dead_rails`).
- Justification ancrée sur un mode de défaillance documenté du device (ex : load switch stuck-on, ferrite filter open, transitive rail death).
- Garde par `comp.role` ou `comp.kind` clean, pas de surkill.

L'avis tient en **1 ligne** : `<sha7>  ✅ vrai fix — <pourquoi en 8 mots>` ou `<sha7>  ❌ gaming-suspect — self-dead sans cause upstream, recommande revert manuel`.

### Step 5 — Synthèse + sortie

**Mode A (one-shot)** — produire systématiquement :

```
Runner: <up depuis $UPTIME | DOWN depuis $LAST_RUN_AT>
Score:  <baseline_score> (commit <baseline_commit>)
Depuis dernier check (<n> lignes neuves):
  <classification ligne par ligne>
État global: <total_runs> runs, last_5: [<keep>, <keep>, <discard>, …]
Mon avis: <2-3 phrases — trajectoire saine / dérive gaming / runner bloqué / score plateau>
```

**Mode B (auto-loop)** — silencieux SAUF si au moins UNE des conditions :
- ≥1 nouveau `keep` (toujours : surface l'avis ligne par ligne)
- ≥3 `discard` consécutifs depuis le dernier non-discard
- ≥1 `crash`
- ≥1 `propose-evaluator-fix`
- Runner mort inopinément (`pgrep` vide ET `last_run_at > 5 min`)
- Score milestone franchi (premier passage au-dessus de 0.85, 0.90, 0.95)

Si **aucune** condition → silencieux total (pas de message à Alexis).

### Step 6 — Update cursor + ScheduleWakeup

```bash
.venv/bin/python <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
cursor = {
    "last_results_lineno": <total_results_lines>,
    "last_seen_state_run": <state.total_runs>,
    "last_check_ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
}
Path("evolve/.monitor_cursor").write_text(json.dumps(cursor, indent=2))
PY
```

**ScheduleWakeup en mode B uniquement** (mode A = one-shot, sortie sans reschedule) :

| Activité dernier intervalle | Délai prochain tick |
|---|---|
| ≥2 keeps | 600 s (10 min) |
| 1 keep ou 1 crash | 1200 s (20 min — défaut) |
| 0 nouvelle ligne ET runner up | 1500 s (25 min) |
| Runner down depuis >1h | 1800 s (30 min) |

Appel : `ScheduleWakeup(delaySeconds=…, prompt="<<autonomous-loop-dynamic>>", reason="monitor evolve — <résumé tick>")`.

## Commandes textuelles d'Alexis à reconnaître

| Phrase d'Alexis (FR/EN) | Action |
|---|---|
| "lance evolve" / "start evolve" / "démarre le runner" | Step 2 → lance le runner si down |
| "stop evolve" / "tue le runner" / "arrête evolve" | Step 2 → kill, no reschedule |
| "status evolve" / "où on en est" / "résumé evolve" | Bascule en mode A pour ce tick (summary complet) |
| "stop monitor" / "arrête de surveiller" | Pas de reschedule, exit propre |
| "que penses-tu de \<sha>" | Lire `git show <sha>`, produire avis ✅/⚠️/❌ ad-hoc |

## Garde-fous

| Situation | Comportement |
|---|---|
| `evolve/state.json` corrompu (JSON parse fail) | Alert Alexis, exit, pas de reschedule |
| Runner crash 3 fois consécutives (3 lignes `crash` adjacentes dans results.tsv) | Alert Alexis avec extracts stderr, propose stop |
| Cursor désaligné (last_results_lineno > nb lignes actuelles, e.g. après reset --hard humain) | Reset cursor à 0, log un avertissement, continuer |
| Score régresse alors que dernière ligne est `keep` (incohérence theoretical, signe que baseline_score n'a pas été mis à jour) | Alert direct : « ⚠️ état incohérent — baseline_score=X mais dernier keep score=Y » |
| `propose-evaluator-fix` détecté | Toujours surface, montre le path du fichier proposal pour lecture immédiate |
| Stale runner (lockfile présent mais PID mort) | Nettoie : `rm -f /tmp/microsolder-evolve.lock`, alert Alexis |

## Ce que tu **ne fais pas**

- **Tu ne modifies aucun fichier sous `api/`, `tests/`, `benchmark/`.** Le monitor est read-only sur le code.
- **Tu n'écris pas dans `evolve/results.tsv`, `evolve/state.json`, `evolve/proposals/`, `evolve/reviews/`.** Ces fichiers sont la propriété du runner et de l'agent evolve.
- **Tu ne `git revert` jamais.** Si tu détectes un commit gaming, tu **recommandes** à Alexis de le revert. La main reste la sienne.
- **Tu ne push jamais** (règle générale du repo).
- **Tu n'ouvres pas une session diagnostique LLM.** Le monitor ne consomme pas de tokens API au-delà de ses propres turns.

## Format d'avis (référence rapide)

Inspiré de `evolve/reviews/2026-04-24-1750-batch1.md` mais en **1 ligne par commit** :

```
512eec1  ✅ vrai fix — sync last-state défensif, no-op mais propage cascade physique
a540987  ✅ vrai fix — unique-supply-path topologique, 4 fingerprints uniques sans self-dead
1fe2258  ✅ vrai fix — series passive_r open sur enable_net, role guard clean
a88e8b8  ⚠️ convention défendable — union enable_net no-op sur sampling window actuelle
e09dd47  ❌ gaming-suspect — regulating_low marque IC dead sans cause physique → recommande revert
```

Pour une revue plus détaillée (>3 lignes), pointer vers `evolve/reviews/<date>-auto.md` si l'auto-review a tourné, sinon proposer à Alexis de lancer une revue manuelle plus poussée via le subagent `code-reviewer`.

## Reset cognitif

Si tu te perds (cursor incohérent, état ambigu, plusieurs alertes en cascade) :

1. Re-lis intégralement `evolve/state.json`, les 30 dernières lignes de `evolve/results.tsv`, et la dernière review dans `evolve/reviews/`.
2. Reset `evolve/.monitor_cursor` à `{"last_results_lineno": 0, "last_seen_state_run": 0, "last_check_ts": null}`.
3. Alert Alexis du reset, continue avec un summary mode A complet.
