# Auto-generator de scenarios bench depuis le knowledge factory — design spec

**Date :** 2026-04-24
**Scope :** pipeline qui transforme un pack device (`memory/{slug}/raw_research_dump.md` + `rules.json` + `registry.json` + `electrical_graph.json`) en un **bench benchable** par `api.pipeline.schematic.evaluator`, avec garde-fous de provenance (grounding evidence + topology) qui rendent la validation humaine inutile. Score de fiabilité par device exposé au runtime diagnostic via `memory_seed` (MA) et un helper `reliability.py` (direct + MA).
**Hors scope :** tout ce qu'`evolve/` touche — `api/pipeline/schematic/simulator.py`, `api/pipeline/schematic/hypothesize.py`, `api/pipeline/schematic/evaluator.py`, `benchmark/scenarios.jsonl` et le dossier `evolve/` entier. Le générateur écrit **uniquement** dans `benchmark/auto_proposals/` + `memory/{slug}/simulator_reliability.json` + l'extension fine des deux runtimes agent et de `memory_seed.py`. Le bench frozen consommé par le runner nocturne reste intact.

---

## 1. Contexte

Le simulateur électrique (`api/pipeline/schematic/simulator.py`) est évalué par un scalaire `score = 0.6 × self_mrr + 0.4 × cascade_recall` (voir `api/pipeline/schematic/evaluator.py`). Le `cascade_recall` consomme `benchmark/scenarios.jsonl` — **17 scenarios MNT Reform** aujourd'hui, tous produits à la main par Alexis + Sonnet en lisant le schematic KiCad. Le `self_mrr` ne consomme que le graph, pas le bench.

Deux conséquences pratiques :

1. **Extension multi-device impossible** à la main : chaque nouveau device demande un humain qui lit son schematic, identifie des failure modes, trouve une source externe, rédige un scenario avec quote verbatim. Coût humain prohibitif au-delà de 3-5 devices.
2. **Le Scout deepsearch fait déjà 80 % du travail** : pour chaque pack, `raw_research_dump.md` contient des failure modes sourcés par URL + verbatim (via `web_search`) et `rules.json` les structure en `symptom → likely_causes → sources`. Ce qui manque : le **pont entre ces failure modes fonctionnels et la topologie refdes** que le simulateur attend en entrée (`cause.refdes = "C19"`, `expected_dead_rails = ["+3V3"]`).

Le générateur comble exactement ce pont. Il consomme un pack complet + son `electrical_graph.json` et produit des scenarios benchables sans invention : chaque champ est prouvé par un fragment littéral d'une quote sourcée, et chaque refdes / rail doit exister dans la topologie compilée.

Bénéfice second : l'agent diagnostic peut afficher au tech une mesure d'**honnêteté épistémique**. « Sur ce device la fiabilité mesurée du simulateur est 0.78, 17 scenarios vérifiés — prends les top-3 hypothèses avec prudence. » Ça ferme la boucle entre ce qu'on sait du device et ce qu'on montre à l'humain.

---

## 2. Règles dures préservées

Les règles du `CLAUDE.md` sont toutes respectées — rappel des points sensibles que ce design doit honorer :

- **Apache 2.0, pas de copie de code externe, dependencies permissives** — rien de nouveau ici, tout le code sera écrit à partir de zéro.
- **Provenance obligatoire sur chaque scenario** — `source_url + source_quote + source_archive` restent des champs requis. Ils sont **recopiés verbatim** depuis les URLs référencées dans `rules.json.rules[].sources` et `raw_research_dump.md` (sections « Source : … ») ; le générateur ne forge **jamais** une URL.
- **No hallucinated component IDs** — chaque `cause.refdes`, chaque entrée de `expected_dead_rails` et `expected_dead_components` doit être validée contre `electrical_graph.json`. Un scenario dont un refdes ou un rail n'est pas dans la topologie tombe dans `{slug}-{date}.rejected.jsonl` avec motif explicite.
- **`memory/{slug}/` est gitignored runtime** — le fichier `simulator_reliability.json` que le générateur y écrit suit la même convention (jamais commité, régénéré à la demande).

### 2.1 Contrat avec le runner evolve nocturne

Le runner `evolve/` mute en continu `simulator.py` et `hypothesize.py` la nuit. Ce générateur **ne doit jamais** écrire dans ces fichiers, ni dans `evaluator.py`, ni dans `benchmark/scenarios.jsonl` (le bench frozen qu'evolve lit). S'il le faisait, on créerait une **boucle fermée** où le simulateur se calibre contre des scenarios auto-générés qui reflètent exactement ce que le simulateur prédit — c'est l'anti-pattern Goodhart précis que le TODO d'origine appelle à éviter.

Règle d'or : **`benchmark/auto_proposals/` ↔ `benchmark/scenarios.jsonl` sont disjoints**. La promotion de l'un vers l'autre (si elle arrive un jour) est un geste **manuel** hors de ce scope, non-automatisable par ce générateur.

Sous le capot, pour matérialiser cette garantie, le travail se fait dans un **worktree git isolé** (`/home/alex/Documents/hackathon-wrench-board-bench-gen/`) basé sur `main`. L'evolve runner continue de tourner dans son working tree d'origine sans interférence.

---

## 3. Architecture globale

```
                     INPUT PACK (read-only)
                     ─────────────────────
  memory/{slug}/raw_research_dump.md       Scout narrative + URLs
  memory/{slug}/rules.json                 Clinicien symptom → cause → sources
  memory/{slug}/registry.json              canonical refdes / rail vocabulary
  memory/{slug}/electrical_graph.json      ⚠ REQUIRED — topology + rails
                              │
                              ▼
   scripts/generate_bench_from_pack.py      CLI wrapper (argparse + run)
                              │
                              ▼
   api/pipeline/bench_generator/            testable module
   ├── schemas.py      Pydantic ProposedScenario + Rejection + RunManifest
   │                   + ReliabilityCard — strict, extra='forbid'
   ├── prompts.py      system + user prompts assembled per pack
   ├── extractor.py    single-call Sonnet via call_with_forced_tool
   │                   + optional Opus rescue pass (--escalate-rejects)
   ├── validator.py    pure functions — grounding ∩ topology ∩ refdes resolve
   ├── scoring.py      thin wrapper over evaluator.compute_score (read-only)
   └── writer.py       atomic JSONL writes + latest.json merge + cleanup
                              │
                              ▼
                   OUTPUT (this run — deterministic paths)
                   ──────────────────────────────────────
  benchmark/auto_proposals/{slug}-YYYY-MM-DD.jsonl           accepted
  benchmark/auto_proposals/{slug}-YYYY-MM-DD.rejected.jsonl  rejects
  benchmark/auto_proposals/{slug}-YYYY-MM-DD.score.json      Scorecard
  benchmark/auto_proposals/{slug}-YYYY-MM-DD.manifest.json   run metadata
  benchmark/auto_proposals/_latest.json                      multi-slug agg
  memory/{slug}/simulator_reliability.json                   runtime-ready
                              │
                              ▼
                   RUNTIME CONSUMPTION
                   ───────────────────
  api/agent/reliability.py          load_reliability_line(slug) helper
  api/agent/runtime_managed.py      ←── inject line in system prompt
  api/agent/runtime_direct.py       ←── inject line in system prompt
  api/agent/memory_seed.py          ←── add reliability.json to _SEED_FILES
```

Le module `api/pipeline/bench_generator/` vit **à côté** du pipeline existant, il ne le mute pas. Il importe `call_with_forced_tool` de `api/pipeline/tool_call.py` (mécanique de forced tool use déjà en place), et `compute_score` de `api/pipeline/schematic/evaluator.py` en **lecture seule** (on n'ajoute pas de wrapper sur evaluator, on l'appelle).

---

## 4. Flux de données détaillé

### 4.1 Entrées consolidées en `PackBundle`

`PackBundle` (Pydantic, `schemas.py`) charge atomiquement les 4 fichiers depuis `memory/{slug}/` et lève avant tout appel LLM si :

- `electrical_graph.json` manque → `BenchGeneratorPreconditionError("no electrical_graph.json — run schematic ingestion first")`. C'est la précondition dure : sans topologie compilée, le simulateur ne peut pas tourner, donc mesurer quoi que ce soit n'a pas de sens.
- `raw_research_dump.md` manque ou fait < 500 caractères → `BenchGeneratorPreconditionError("Scout dump empty or absent")`.
- `rules.json` ou `registry.json` manquent → warning loggué, on continue (le dump seul suffit formellement).

### 4.2 Construction du contexte LLM

L'input LLM est petit et structuré pour éviter l'explosion de tokens (electrical_graph.json de MNT Reform fait **703 KB** — inutilisable brut). On envoie :

- **Narrative bloc** = `raw_research_dump.md` *in extenso* (15 KB typiques, fittable directement).
- **Rules bloc** = `rules.json` intégralement — c'est structuré donc compact (13 KB MNT Reform).
- **Graph summary bloc** = projection compacte de `electrical_graph.json` :
  - Liste `{refdes, kind, role, description}` pour chaque composant (typ. ~3-5 KB).
  - Liste `{id, nominal_voltage, source_refdes}` pour chaque rail (typ. ~1 KB).
  - **Pas les edges, pas les pins.** L'extracteur n'a pas besoin de tracer les cascades, juste de proposer refdes + mode + rails concernés ; le simulateur tranche par la suite.
- **Registry bloc** = `registry.json` pour donner les alias (ex : « LPC controller » → *hint that it is some refdes*, mais le refdes précis vient du graph summary).

Ce contexte tient en ~25 KB pour MNT Reform. Appel uniquement sonnet-4-6 en mode `tool_choice={"type":"tool", "name":"propose_scenarios"}`. Le tool est défini dans `prompts.py` et son `input_schema` vient de la Pydantic `ProposalsPayload.model_json_schema()` — zero duplication, c'est exactement le pattern `call_with_forced_tool` existant.

### 4.3 Schema de sortie LLM — `ProposalsPayload`

```python
class EvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal[
        "cause.refdes", "cause.mode", "cause.value_ohms", "cause.voltage_pct",
        "expected_dead_rails", "expected_dead_components",
    ]
    source_quote_substring: str  # doit être ⊂ source_quote, strictement literal
    reasoning: str                # court, en anglais, pourquoi ce span justifie le field

class ProposedScenarioDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_id: str                 # stable hash(quote[0:80]) sans device prefix
    cause: ProposedCause          # {refdes, mode, value_ohms?, voltage_pct?}
    expected_dead_rails: list[str] = []
    expected_dead_components: list[str] = []
    source_url: str
    source_quote: str             # ≥ 50 chars, verbatim
    confidence: float             # 0..1, indicatif (pas un filtre)
    evidence: list[EvidenceSpan]  # ≥ 1 par field non-vide
    reasoning_summary: str        # ≤ 200 mots, pourquoi ce scenario est distinct

class ProposalsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: list[ProposedScenarioDraft] = Field(..., min_length=0, max_length=50)
```

Le `evidence` array est **le cœur** du garde-fou : il force le LLM à ancrer chaque champ structuré dans une sous-chaîne littérale du `source_quote`. Ce qui empêche de « remplir `expected_dead_rails` avec ce qui semble logique » — il faut une justification littérale. Absence de fragment littéral = rejet déterministe côté validator.

### 4.4 Pipeline de validation (`validator.py`)

Pour chaque `ProposedScenarioDraft` :

**V1. Sanity checks statiques**
- `source_quote` ≥ 50 chars
- `source_url` matche un regex URL raisonnable
- `cause.mode ∈ {"dead", "shorted", "open", "leaky_short", "regulating_low"}` (les 5 modes simulables)
- `cause.value_ohms` requis ssi `mode == "leaky_short"`
- `cause.voltage_pct` requis ssi `mode == "regulating_low"`

**V2. Grounding check**
- Pour chaque `evidence[i]`, `source_quote_substring` doit être **littéralement** une substring de `source_quote` (comparaison case-sensitive, pas de normalisation). `in` Python strict.
- Chaque field listé dans `evidence[].field` doit correspondre à un field réellement rempli dans le scenario (ex : `expected_dead_rails` ne peut avoir d'evidence que si la liste est non vide).
- Chaque field rempli (`cause.refdes`, les entrées non-vides de `expected_*`) doit avoir **au moins une** evidence.

**V3. Topology check** (contre `electrical_graph.json`)
- `cause.refdes ∈ graph.components.keys()` — sinon rejet `refdes_not_in_graph`.
- Chaque entrée de `expected_dead_rails` doit exister dans `graph.power_rails.keys()` — sinon rejet `rail_name_not_in_graph`.
- Chaque entrée de `expected_dead_components` doit exister dans `graph.components.keys()` — sinon rejet `component_not_in_graph`.

**V4. Pertinence mode / kind**
- On **ré-applique inline** les 3 règles de pertinence miroir de `evaluator._is_pertinent` (doc-link dans le docstring) plutôt que d'importer une fonction privée. Les 3 règles sont courtes et stables, le mirror est plus robuste qu'un import privé qui casserait silencieusement au prochain rename :
  - `ic + regulating_low` → exige que le refdes source au moins un rail dans `graph.power_rails`.
  - `passive_c + leaky_short` → exige que le refdes soit dans `rail.decoupling` d'un des rails.
  - `passive_r + open` → exige que `role ∈ {series, damping, inrush_limiter}`.
- Échec = rejet `mode_not_pertinent`.

**V5. Unicité locale**
- Deux scenarios du même run avec la même `(cause.refdes, cause.mode, expected_dead_rails_sorted, expected_dead_components_sorted)` → on garde le premier, le second va dans rejected avec motif `duplicate_in_run`.

Un scenario qui passe V1→V5 est promu en `ProposedScenario` final, enrichi avec les champs systèmes :

```python
class ProposedScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str                          # "{slug}-{local_id}" après dé-collision
    device_slug: str
    cause: Cause
    expected_dead_rails: list[str]
    expected_dead_components: list[str]
    source_url: str
    source_quote: str
    source_archive: str              # benchmark/auto_proposals/sources/{id}.txt
    confidence: float
    generated_by: str                # "bench-gen-sonnet-4-6" ou "bench-gen-opus-rescue"
    generated_at: str                # ISO UTC
    validated_by_human: bool = False # toujours False par ce pipeline
    evidence: list[EvidenceSpan]     # traçabilité pour audit
```

Le flag `validated_by_human` reste **`False`** ; la robustesse ne vient **pas** d'un humain dans la boucle mais de l'intersection grounding × topology. Le flag existe pour rester compatible avec `scenarios.jsonl` existant (qui en hériterait si un jour une promotion manuelle était faite).

### 4.5 Escalation optionnelle vers Opus (`--escalate-rejects`)

Sans ce flag : les scenarios rejetés en V2/V3 sont simplement écrits dans `rejected.jsonl` avec motif, et c'est fini. Comportement par défaut pour contrôler le budget.

Avec `--escalate-rejects` : après la passe Sonnet, les rejects motivés par `evidence_span_not_literal` ou `refdes_not_in_graph` **uniquement** (pas les rejets de pertinence ni de doublon) sont re-proposés à Opus avec un contexte ciblé : « voici la quote source, voici le draft rejeté, voici les refdes/rails valides du graph, corrige au plus près ». Un scenario corrigé par Opus porte `generated_by = "bench-gen-opus-rescue"` et doit re-traverser V1→V5 (zéro raccourci).

Ce fallback est codé prod-ready mais désactivé par défaut. Un run exploratoire Sonnet-seul permettra de mesurer la distribution des rejets avant de décider si l'escalation en vaut le coût.

### 4.6 Écriture atomique des outputs

`writer.py` écrit via un pattern `write-to-temp + atomic-rename` par fichier, pour qu'un Ctrl-C en cours de run ne laisse pas un `.jsonl` partiel dans `auto_proposals/`. Les six fichiers produits par run (voir section 3) sont groupés :

1. `{slug}-{date}.manifest.json` écrit en premier (run metadata : model, n_accepted, n_rejected, input_mtimes).
2. `{slug}-{date}.jsonl` + `.rejected.jsonl` écrits en parallèle.
3. `{slug}-{date}.score.json` = `evaluator.compute_score(graph, accepted_scenarios).model_dump()`.
4. `memory/{slug}/simulator_reliability.json` = projection compacte du Scorecard (voir 4.7).
5. `benchmark/auto_proposals/_latest.json` est lu, muté sous verrou advisory (`fcntl.flock`), réécrit atomiquement — pour gérer un run concurrent éventuel.

Les sources verbatim de chaque scenario accepté sont aussi archivées sous `benchmark/auto_proposals/sources/{scenario_id}.txt` (simple texte brut, un fichier par scenario) pour honorer `source_archive` même si l'URL meurt.

### 4.7 `simulator_reliability.json` — shape runtime

```json
{
  "device_slug": "mnt-reform-motherboard",
  "score": 0.78,
  "self_mrr": 0.82,
  "cascade_recall": 0.72,
  "n_scenarios": 17,
  "generated_at": "2026-04-24T21:00:00Z",
  "source_run_date": "2026-04-24",
  "notes": [
    "Based on auto-generated scenarios, not human-validated.",
    "Per-scenario breakdown available at benchmark/auto_proposals/mnt-reform-motherboard-2026-04-24.score.json"
  ]
}
```

Cette shape est **petite volontairement** : elle est destinée à être lue dans un system prompt. Le détail scenario-par-scenario vit dans `benchmark/auto_proposals/`, accessible à l'humain qui audite, pas injecté par défaut dans le context de l'agent.

---

## 5. Intégration runtime

### 5.1 Helper commun `api/agent/reliability.py`

```python
def load_reliability_line(device_slug: str) -> str | None:
    """Return a one-liner for the system prompt, or None if no pack has been
    benched yet. Reads memory/{slug}/simulator_reliability.json if present."""
```

Retourne une chaîne du style :

> `Simulator reliability for mnt-reform-motherboard: score=0.78 (self_mrr=0.82, cascade_recall=0.72, n=17 scenarios, as of 2026-04-24). Treat top ranked hypotheses with proportional caution.`

Si le fichier n'existe pas → `None` (pas d'injection). Si corrompu → log warning + `None` (le comportement ne doit jamais casser).

### 5.2 Runtime MA (`runtime_managed.py`) + runtime direct (`runtime_direct.py`)

Au moment de construire le system prompt initial, les deux runtimes ajoutent une section « Simulator reliability » conditionnée à `load_reliability_line(slug)`. Trois lignes de code dans chaque runtime. Rien d'autre ne change.

### 5.3 Memory seed (`memory_seed.py`)

On étend `_SEED_FILES` avec :

```python
("simulator_reliability.json", "/knowledge/simulator_reliability.json"),
```

Conséquence : le fichier est upload dans le memory store MA dès le prochain `seed_memory_store_from_pack()`. Un agent en session peut alors `read /mnt/memory/{slug}/knowledge/simulator_reliability.json` pour voir le détail s'il en a besoin. Zéro tool custom ajouté. Zéro autre changement dans `manifest.py` ni `tools.py`.

---

## 6. Interface CLI — `scripts/generate_bench_from_pack.py`

```
usage: generate_bench_from_pack.py [-h] --slug SLUG
                                   [--model MODEL] [--escalate-rejects]
                                   [--output-dir OUTPUT_DIR]
                                   [--dry-run] [--verbose]

Generate benchable scenarios from a device's knowledge pack.

Required:
  --slug SLUG             Device slug (matches memory/{slug}/ directory).

Optional:
  --model MODEL           Sonnet model id. Default: settings.anthropic_model_sonnet
                          (depuis .env, fallback "claude-sonnet-4-6" si absent).
  --escalate-rejects      Re-propose rejected scenarios via Opus (claude-opus-4-7).
                          Costs tokens. Off by default.
  --output-dir OUTPUT_DIR Proposals destination. Default: benchmark/auto_proposals/
  --dry-run               Run extraction + validation, skip file writes. Prints
                          a summary table to stdout. Does NOT hit memory/ either.
  --verbose               DEBUG-level logs + per-scenario dump.

Exit codes:
  0 - success, at least 1 scenario accepted.
  1 - success but 0 scenarios accepted (valid outcome for sparse packs).
  2 - precondition failed (missing electrical_graph.json, empty Scout dump, ...).
  3 - LLM call failed after retries.
```

`--dry-run` est essentiel pour la validation manuelle avant mise en prod (Alexis exécute une passe, lit la sortie, valide, re-run sans `--dry-run` pour persister).

---

## 7. Concurrence et safety

### 7.1 Isolation du worktree

Le développement se fait dans un worktree git distinct (`/home/alex/Documents/hackathon-wrench-board-bench-gen/`) sur la branche `feature/bench-auto-generator` basée sur `main`. L'evolve runner continue de tourner dans le working tree principal (`/home/alex/Documents/hackathon-microsolder/`) sur `evolve/2026-04-24` sans interférence. La branche `feature/bench-auto-generator` ne fera **jamais** de fast-forward ou merge avec `evolve/*`.

### 7.2 Hard-gate evolve

Aucun fichier dans les chemins suivants n'est muté par ce design, sous aucun prétexte, même pour un refactor cosmétique :

- `api/pipeline/schematic/simulator.py`
- `api/pipeline/schematic/hypothesize.py`
- `api/pipeline/schematic/evaluator.py` (lecture seule — on l'importe mais on ne le modifie pas)
- `benchmark/scenarios.jsonl` et `benchmark/sources/`
- `evolve/*`
- `api/pipeline/schematic/boot_analyzer.py` (WIP evolve)
- `tests/pipeline/schematic/test_boot_analyzer.py` (WIP evolve)

Si un besoin émerge d'adapter un de ces fichiers, **stop, retour brainstorm** — ce ne serait plus ce spec.

### 7.3 Pas de promotion automatique

Aucun mécanisme dans ce design ne copie un scenario de `auto_proposals/*.jsonl` vers `benchmark/scenarios.jsonl`. Le runner evolve n'ira jamais chercher ses scenarios ailleurs que dans le bench frozen historique. La séparation est topologique (chemins disjoints) et contractuelle (spec + review). Un futur `scripts/approve_proposals.py` pour cette promotion sera un work item séparé, avec son propre spec.

---

## 8. Stratégie de tests

Tests unitaires dans `tests/pipeline/bench_generator/` (nouveau dossier) :

- **`test_schemas.py`** — round-trip JSON ↔ Pydantic pour `ProposedScenarioDraft`, `ProposedScenario`, `ProposalsPayload`, `Rejection`, `RunManifest`, `ReliabilityCard`. Vérifie `extra='forbid'` (payloads avec champ inconnu → ValidationError), vérifie les invariants : `mode=leaky_short` force `value_ohms`, `evidence` non-vide pour chaque field rempli, etc.

- **`test_validator.py`** — fonctions pures, sans mock LLM. Fixtures : un `ElectricalGraph` minimal de 6 composants + 3 rails écrit inline. Cas testés :
  - V1 `mode` inconnu → rejet `unknown_mode`.
  - V2 `source_quote_substring` présent → accept.
  - V2 `source_quote_substring` absent (même à une lettre près) → rejet `evidence_span_not_literal`.
  - V2 evidence sur `expected_dead_rails` avec liste vide → rejet `evidence_field_empty`.
  - V2 field non-vide sans evidence → rejet `evidence_missing`.
  - V3 `refdes=XZ999` non dans graph → rejet `refdes_not_in_graph`.
  - V3 rail `+42V` inexistant → rejet `rail_name_not_in_graph`.
  - V4 `regulating_low` sur IC non-source → rejet `mode_not_pertinent`.
  - V5 doublon exact → seul le premier passe.

- **`test_extractor.py`** — mock `AsyncAnthropic` avec un client stub (pattern des tests existants `tests/pipeline/test_writers.py`). Vérifie :
  - Prompt assembly (bonne concaténation des 4 blocs input).
  - `call_with_forced_tool` est appelé avec le bon `tool_choice`.
  - Réponse LLM = `ProposalsPayload` → parseée cleanly.
  - Échec tool call (JSON invalide) → retry x3 puis `BenchGeneratorLLMError`.
  - `--escalate-rejects` : les rejets éligibles sont re-soumis à Opus, les autres passent en rejected.

- **`test_writer.py`** — mock filesystem via `tmp_path`. Vérifie :
  - 6 fichiers de sortie aux chemins attendus.
  - `_latest.json` mergé correctement quand il contient déjà une autre slug.
  - Archives `sources/{id}.txt` écrites.
  - `memory/{slug}/simulator_reliability.json` écrit avec shape runtime.
  - Atomicité : simuler un `OSError` au milieu → working dir reste propre (pas de fichier partiel committé).

- **`test_reliability_helper.py`** — `api/agent/reliability.py`. Charge un `simulator_reliability.json` fixture, vérifie le format de la ligne, vérifie `None` quand le fichier manque, vérifie `None` + log warning quand corrompu.

- **`test_integration_end_to_end.py`** — le test d'intégration clé. Un fixture pack minimal (electrical_graph inline avec 5 composants dont 1 source de rail, raw_research_dump avec 3 failure modes dont 1 au refdes mentionnable + 1 au refdes absent), mock client Sonnet qui retourne 3 drafts connus (1 accept, 1 rejet evidence, 1 rejet topology), `generate_from_pack(slug)` appelé → assertions précises sur les 6 fichiers output + summary dict.

Au moment du vrai run (post-tests), Alexis exécute :

```bash
python scripts/generate_bench_from_pack.py --slug mnt-reform-motherboard --dry-run
```

Lit la sortie, valide qu'elle ressemble à ce qu'il attend, puis re-run sans `--dry-run`. Le run produit donc **le premier vrai** bench auto-généré du repo. La comparaison avec les 17 scenarios manuels existants sera intéressante (non pas pour superposer mais pour vérifier qu'on n'en « rate » pas de majeurs).

---

## 9. Sécurité + cas limites

- **Pack complètement vide** (Scout dump < 500 chars, pas de rules.json) → `BenchGeneratorPreconditionError` avant tout appel LLM. Exit code 2.
- **Graph avec 0 rail** ou **0 composant** → précondition checkée par `PackBundle.__post_init__`. Exit code 2.
- **LLM retourne 0 scenarios** → c'est un outcome valide (pack trop léger, Scout a peu de matière). On écrit tous les fichiers output, la `score.json` contient un Scorecard avec `n_scenarios=0`, `cascade_recall=0.0`, `self_mrr` calculé seul. Exit code 1 (pas une erreur).
- **Tous les scenarios rejetés** → idem, `jsonl` vide, rejected rempli, score sans cascade_recall. Exit code 1.
- **Pack fraîchement changé** (input mtime avance pendant la run) → le manifest log les mtimes au démarrage ; en cas de discordance au writeback on warn mais on écrit quand même. La traçabilité est préservée via le manifest.
- **Deux runs simultanés sur la même slug** → `fcntl.flock` exclusif sur `_latest.json` pendant le merge. Les autres fichiers sont datés donc leurs noms sont uniques per-day ; si vraiment deux runs tombent le même jour, le second écrase avec un warning.

---

## 10. Futur work — déclaré mais hors scope

- **`scripts/approve_proposals.py`** — CLI manuel qui promeut un subset de `auto_proposals/*.jsonl` vers `benchmark/scenarios.jsonl` avec `validated_by_human=True`. Protection : exige un `--confirm-slug` + un review diff stdout avant écriture. Non-implémenté ici — aucune auto-promotion possible par design.
- **`mb_simulator_reliability(slug)` tool** — si un jour l'agent a besoin d'appeler le score explicitement via tool call (plutôt que de le recevoir en system prompt). YAGNI tant que `load_reliability_line()` dans le system prompt suffit.
- **Opus-only mode** — un `--model-all opus` qui force Opus sur la passe principale, non pas juste en fallback. Utile si un pack a des failure modes très ambigus. Pas un must.
- **Benchs par subsystème** — un futur `--subsystem power` pour ne générer des scenarios que sur une partie de la topologie. Pertinent quand les packs feront 1000+ composants.
- **CI gate** — un hook `make bench-all-devices` qui re-run le générateur sur tous les packs de `memory/` et commit les updates de `simulator_reliability.json`. Hors scope car ça croise la ligne evolve.

---

## 11. Succès = ?

Le spec est implémenté avec succès quand :

1. `make test` passe sans toucher aux tabous evolve (contrat topologique respecté).
2. `python scripts/generate_bench_from_pack.py --slug mnt-reform-motherboard --dry-run` produit une sortie textuelle lisible avec ≥ 5 scenarios acceptés et ≥ 1 rejet motivé.
3. Le même sans `--dry-run` produit les 6 fichiers output attendus, sans corruption.
4. Ouvrir une session `/ws/diagnostic/mnt-reform-motherboard?tier=deep` montre dans les logs que la ligne « Simulator reliability … » apparaît dans le system prompt.
5. Relancer `make test-all` pendant que l'evolve runner tourne en parallèle → zéro interférence (tests passent, evolve continue de committer ses scores sans échec).

Les 5 points ensemble signent un livrable prod-ready qui remplit l'intention du TODO initial sans casser l'outil en cours.
