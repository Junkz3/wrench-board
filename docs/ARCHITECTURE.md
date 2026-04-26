# Architecture

Référence complète de l'architecture `wrench-board`. Ce document est la carte mentale à partager entre collaborateurs — il complète `CLAUDE.md` (règles + tour d'horizon) en détaillant les **flux IA**, les **contrats inter-modules** et les **points d'extension**.

À lire à froid avant toute modification structurelle : pipeline, runtime diagnostic, engines déterministes, parsers boardview, registry des tools.

---

## TL;DR

`wrench-board` est un workbench agent-natif pour le diagnostic microsoudure au niveau carte. L'architecture repose sur **quatre workflows IA orthogonaux** qui produisent et consomment un même corpus on-disk (`memory/{slug}/`) :

| # | Workflow | Cadence | Déclencheur |
|---|----------|---------|-------------|
| **A** | Knowledge Factory | offline, par device | `POST /pipeline/generate` |
| **B** | Schematic Ingestion | offline, par device | CLI `api.pipeline.schematic.cli` |
| **C** | Bench Generator | offline, post-A+B | `scripts/generate_bench_from_pack.py` |
| **D** | Diagnostic Runtime | live, par session | `WS /ws/diagnostic/{slug}` |

Plus **deux engines déterministes purs** (`simulator`, `hypothesize`) qui n'appellent jamais de LLM à runtime et qui portent la différenciation technique du produit.

---

## Ordre d'exécution typique pour un nouveau device

```
  ┌─────────────────┐
  │ device_label    │  ex. "iPhone X"  ou  "mnt-reform-motherboard"
  └────────┬────────┘
           │
           ▼
    ┌─────────────┐       POST /pipeline/generate
    │  Workflow A │       Scout → Registry → Writers ×3 → Auditor
    │ (knowledge  │       Produit : registry, knowledge_graph, rules,
    │   factory)  │                 dictionary, audit_verdict
    └──────┬──────┘
           │
           │   (optionnel, si schéma PDF fourni)
           ▼
    ┌─────────────┐       CLI  api.pipeline.schematic.cli
    │  Workflow B │       Render → Vision ×N → Merge → Compile
    │ (schematic  │                → (optionnel) Boot-Analyzer
    │  ingestion) │       Produit : schematic_graph, electrical_graph,
    └──────┬──────┘                 boot_sequence_analyzed, nets_classified
           │
           │   (optionnel, calibrer la fiabilité du simulateur)
           ▼
    ┌─────────────┐       scripts/generate_bench_from_pack.py
    │  Workflow C │       Extractor → V1..V5 + V2b → Scoring
    │   (bench    │       Produit : simulator_reliability.json
    │  generator) │                 (score global + par scénario)
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐       WS /ws/diagnostic/{slug}?tier=…&repair=…
    │  Workflow D │       Conversation live, tier-selectable
    │ (diagnostic │       Consomme tous les artefacts A+B+C
    │   runtime)  │       Persiste : repairs/, field_reports/
    └─────────────┘
```

**Règle d'ordre** : A et B sont indépendants et peuvent tourner en parallèle. C exige A+B complets. D consomme ce qui est disponible et dégrade gracieusement si une brique manque (le pack est lisible sans `electrical_graph.json`, l'agent perd simplement ses tools `mb_schematic_graph` / `mb_hypothesize`).

---

## Workflow A — Knowledge Factory

**Rôle** : produire un pack de connaissances canonique pour un device à partir d'une simple étiquette textuelle, éventuellement enrichie de documents fournis par le technicien.

**Fichier source** : `api/pipeline/orchestrator.py::generate_knowledge_pack(device_label, documents=None)`

### Les 5 phases

| Phase | Module | Modèle | Tool forcé | Sortie |
|-------|--------|--------|------------|--------|
| 1 Scout | `scout.py` | Sonnet | native `web_search` (non-forcé) | `raw_research_dump.md` |
| 2 Registry | `registry.py` | Sonnet | `submit_registry` | `registry.json` |
| 2.5 Mapper | `mapper.py` | Sonnet | `submit_refdes_mapping` | mapping registry → graph refdes (intermédiaire, persistance optionnelle) |
| 3 Writers ×3 | `writers.py` | Opus (Cartographe, Clinicien) + Sonnet (Lexicographe) | `submit_knowledge_graph`, `submit_rules`, `submit_dictionary` | `knowledge_graph.json`, `rules.json`, `dictionary.json` |
| 4 Auditor | `auditor.py` | Opus | `submit_audit_verdict` | `audit_verdict.json` |

### Modules de support de la pipeline A

| Module | Rôle |
|--------|------|
| `prompts.py` | Hub central des system prompts — Scout, Registry, Mapper, Cartographe, Clinicien, Lexicographe, Auditor, phase narrator. Source unique de vérité, modifié quand une persona change. |
| `events.py` | Pubsub asyncio par slug. Orchestrator publie `phase_started` / `phase_progress` / `phase_completed` / `phase_narration` / `coverage_check_*` / `expand_*`. WS `/pipeline/progress/{slug}` les relaie. |
| `phase_narrator.py` | Post-phase Haiku narration FR (2-3 phrases). Lit l'artefact, résume, publie via `events.py`. Découplé : si la narration échoue, la pipeline continue. |
| `expansion.py` | Targeted self-extend : Scout focalisé + Clinicien sur un `focus_symptoms` set. Append au `raw_research_dump.md`, regenère `rules.json` incrémental. Appelé par `POST /pipeline/packs/{slug}/expand` et par la branche **expand** de `POST /pipeline/repairs`. |
| `coverage.py` | Haiku forced-tool — classifie si un symptôme tech est couvert par les `rules.json` existantes. Retourne `{covered, confidence, matched_rule_id, reason}`. Seuil `confidence ≥ 0.7` pour court-circuiter la génération. |
| `intent_classifier.py` | Haiku forced-tool — `POST /pipeline/classify-intent` : free-text de la landing → top-3 device slugs avec confiance. Backbone du form 2-champs de la landing. |
| `subsystem.py` | Pure-function deterministe — tague chaque node du graph (`power` / `charge` / `usb` / `display` / `audio` / `rf` / `io` / `compute`) via règles regex sur refdes + signal names. Pas de LLM. |
| `telemetry/token_stats.py` | Tracking des tokens par phase (input / output / cache_read / cache_creation). Consommé par les events de progress et les pricing dashboards. |

### Particularités non-évidentes

- **Scout résilience** : gère les `pause_turn` du SDK, rejette les dumps « maigres » (< N symptômes / sources / composants), relance avec un scope élargi. Depuis `6377d3b`, Scout peut consommer un **graphe électrique, un boardview parsé et des datasheets PDF** fournis par le tech — les MPN extraits deviennent des query seeds ciblés. Depuis `020c168`, le param `focus_symptom` insère un bloc qui alloue 3-4 web_search queries sur un symptôme tech précis (utilisé par les branches **full** et **expand** de `POST /pipeline/repairs`).
- **Registry → Mapper → Writers** : la `registry.json` est le vocabulaire canonique. Le Mapper produit la passerelle registry → refdes du graph en quote-validating chaque correspondance. Toute violation (refdes émis par un Writer absent de la registry) déclenche `drift.py`, qui compile un rapport de drift passé **en entrée** à l'Auditor comme ground truth déterministe.
- **Writers cache warmup** : les trois writers partagent un préfixe long (raw dump + registry + system prompt) marqué `cache_control: ephemeral`. Writer 1 part en premier, les writers 2 et 3 attendent `cache_warmup_seconds` pour que la cache entry matérialise — d'où un gain tokens réel de −75 % sur les writers 2-3.
- **Auditor loop** : verdict `NEEDS_REVISION` → `_apply_revisions()` relance les writers flaggés (max `pipeline_max_revise_rounds`). Verdict `REJECTED` lève. Un drift check déterministe coupe la boucle après `max_rounds`.
- **Post-pipeline** : `graph_transform.pack_to_graph_payload()` synthétise des nœuds d'action et émet le JSON consommé par `web/js/graph.js` (colonnes Actions → Components → Nets → Symptoms).

### Source de vérité des shapes

`api/pipeline/schemas.py` définit tous les modèles Pydantic du pack. Ils servent *à la fois* de validateurs runtime et de sources JSON Schema pour `input_schema` des tools forcés. **Ne jamais dupliquer une shape** — tout importer de là.

### `POST /pipeline/repairs` — la routing-table à 3 branches

L'entrée principale d'une session client (« nouveau ticket »). À partir d'un `device_label` + `symptom`, l'orchestrator décide laquelle de trois branches déclencher pour minimiser la latence et le coût (commits `860008e`, `b5e4f9f`).

```
                       POST /pipeline/repairs {device_label, symptom}
                                      │
                                      ▼
                      ┌──────────────────────────────┐
                      │ Pack présent sous memory/?   │
                      └──────────────┬───────────────┘
                                     │
                  ┌──────────────────┴──────────────────┐
                  │                                     │
                NON│                                  OUI│
                  │                                     │
                  ▼                                     ▼
          ┌───────────────┐         ┌────────────────────────────────────┐
          │ pipeline_kind │         │ coverage.check_symptom_coverage()  │
          │  = "full"     │         │  Haiku forced-tool sur rules.json  │
          │ Knowledge     │         └────────────────┬───────────────────┘
          │ Factory       │                          │
          │ complète      │      covered=True &      │ covered=False  OU
          │ (focus_symptom│      confidence≥0.7 &    │ confidence<0.7
          │  = symptom)   │      matched_rule_id≠∅   │
          └───────────────┘                          │
                                  ┌──────────────────┴───────────────┐
                                  ▼                                  ▼
                       ┌────────────────────┐             ┌─────────────────────┐
                       │ pipeline_kind      │             │ pipeline_kind       │
                       │   = "none"         │             │   = "expand"        │
                       │ ZÉRO LLM           │             │ expansion.expand_   │
                       │ Renvoie immédiat   │             │   pack(focus_       │
                       │ matched_rule_id +  │             │   symptoms=[…])     │
                       │ coverage_reason    │             │ Scout + Clinicien   │
                       └────────────────────┘             │ ciblés, append      │
                                                          │ rules.json          │
                                                          └─────────────────────┘
```

`RepairResponse` expose `pipeline_kind` (`"full"` | `"expand"` | `"none"`), `matched_rule_id`, `coverage_reason`. Le frontend (`web/js/home.js`) lit ces champs pour décider d'ouvrir la timeline pipeline (full/expand) ou d'aller direct dans la repair (none).

---

## Workflow B — Schematic Ingestion

**Rôle** : compiler un PDF de schéma électrique en graphe interrogeable (`ElectricalGraph`) utilisable par les engines déterministes et le diagnostic.

**Fichier source** : `api/pipeline/schematic/orchestrator.py::ingest_schematic(pdf_path, device_slug, client)`

### Les 5 étapes

```
PDF
 │
 ▼
┌──────────────────────────────────────┐
│ 1. renderer.py                       │  pdftoppm (poppler) → PNG par page
│    + pdfplumber scan detection       │  DPI paramétrable, détection orientation
└──────────────────────────────────────┘
 │
 ▼
┌──────────────────────────────────────┐
│ 2. grounding.py (optionnel)          │  extraction text/layout markers
│                                      │  stabilise la vision
└──────────────────────────────────────┘
 │
 ▼
┌──────────────────────────────────────┐
│ 3. page_vision.py                    │  Claude 4.7 vision
│    forced tool submit_schematic_page │  1 appel / page, cache warmup :
│                                      │  page 1 serial → sleep → N//gather
└──────────────────────────────────────┘
 │   (schematic_pages/page_XXX.json)
 ▼
┌──────────────────────────────────────┐
│ 4. merger.py                         │  déterministe
│                                      │  - dedup refdes cross-page
│                                      │  - stitch nets par label
│                                      │  - synth __local__{page}__{id}
└──────────────────────────────────────┘
 │   (schematic_graph.json)
 ▼
┌──────────────────────────────────────┐
│ 5. compiler.py                       │  déterministe
│                                      │  - classif edges (power/signal)
│                                      │  - rail extraction + voltage
│                                      │  - boot_sequence via Kahn topo
│                                      │  - quality report
└──────────────────────────────────────┘
 │   (electrical_graph.json)
 ▼
┌──────────────────────────────────────┐
│ 6. boot_analyzer.py (optionnel, NEW) │  Opus, ~$0.25, graceful fail
│    forced tool                       │  raffine boot_sequence :
│    submit_analyzed_boot_sequence     │  always-on / sequenced / on-demand
│                                      │  + sequencer_refdes
└──────────────────────────────────────┘
     (boot_sequence_analyzed.json)
```

### Notes architecturales

- **Tous les shapes** sont dans `api/pipeline/schematic/schemas.py` — même principe que le workflow A.
- **Boot analyzer** (ajouté dans `3833b43`) est un pass Opus post-compile qui combine `enable_net`, edges `enables` et designer notes pour identifier le vrai séquencement (LPC/PMIC driving `*_PWR_EN`). Tombe gracieusement : en cas d'échec, le simulator utilise la `boot_sequence` topologique du compiler.
- **Le compiler est déterministe** : pas de LLM après l'étape 3. Les pépites techniques (simulator, hypothesize) en dépendent et exigent cette pureté.

### CLI

`api.pipeline.schematic.cli` est un **outil de debug vision**, pas de pleine ingestion. Il prend `pdf` + `page` (1-based) positionnels et exécute Claude vision sur une seule page, ou re-classifie les passives d'un `electrical_graph.json` existant via `--classify-passives SLUG`. La pleine ingestion PDF → `ElectricalGraph` passe par `ingest_schematic()` invoqué depuis l'orchestrator (typiquement via upload sur `POST /pipeline/packs/{slug}/documents`).

```bash
# debug une page
python -m api.pipeline.schematic.cli board.pdf 3 --model claude-opus-4-7
# re-classifier les passives d'un pack existant
python -m api.pipeline.schematic.cli --classify-passives my-device
```

---

## Workflow C — Bench Generator

**Rôle** : générer automatiquement des scénarios de test (cause → cascade attendue) à partir d'un pack existant, pour calibrer la fiabilité du simulateur sur un device donné.

**Fichier source** : `api/pipeline/bench_generator/orchestrator.py::generate_from_pack(slug)`

### Pipeline

```
memory/{slug}/registry.json + rules.json + knowledge_graph.json
 + electrical_graph.json + raw_research_dump.md
            │
            ▼
  ┌─────────────────────────────┐
  │ extractor.py — Sonnet       │  propose_scenarios (forced tool)
  │   (+ rescue optionnel Opus) │  Input : raw + rules + registry + graph
  └──────────┬──────────────────┘
             │   list[ProposedScenarioDraft]
             ▼
  ┌─────────────────────────────┐
  │ validator.py — déterministe │
  │   V1 sanity                 │
  │   V2 grounding (span quote) │  evidence_span ⊂ source_quote
  │   V2b semantic              │  refdes + rails mentionnés +
  │                             │    topologiquement connectés
  │   V3 topology               │  refdes/rails existent dans graph
  │   V4 pertinence             │  mode/kind cohérents
  │   V5 dedup                  │
  └──────────┬──────────────────┘
             │   survivors + rejections
             ▼
  ┌─────────────────────────────┐
  │ scoring.py — F1-soft        │  scores avec poids FP/FN tunables
  └──────────┬──────────────────┘
             │
             ▼
  ┌─────────────────────────────┐
  │ writer.py                   │
  │   memory/{slug}/            │
  │     simulator_reliability.json    (score global + per-scenario)
  │   benchmark/auto_proposals/     (archives per-run + latest.json)
  └─────────────────────────────┘
```

### Consommation

`simulator_reliability.json` → `api/agent/reliability.py::load_reliability_line()` → injecté dans le system prompt des deux runtimes D. L'agent peut donc déclarer au tech « mon moteur causal est peu fiable sur ce device » si le score est bas. Ne **jamais** sauter cette injection : le prompt path dégrade gracieusement, mais l'agent perd son auto-conscience.

### Frontière lisible / non-lisible par l'agent

| Artefact | Frozen human oracle | Auto-généré |
|----------|---------------------|-------------|
| Chemin | `benchmark/scenarios.jsonl` | `benchmark/auto_proposals/…` |
| Cadence | curation manuelle, ~17 scénarios | régénérable par device |
| Utilisation | scoring définitif, `scripts/eval_simulator.py` | pipeline de calibration reliability |

**Ne jamais** merger le auto-généré dans le frozen oracle — c'est une séparation anti-gaming (voir commit `4d0c9ba`).

---

## Workflow D — Diagnostic Runtime

**Rôle** : conversation live entre le technicien et un agent Claude tier-selectable, avec accès à la boardview, au pack de connaissances et aux engines déterministes via tools.

**Point d'entrée** : `WS /ws/diagnostic/{device_slug}?tier={fast|normal|deep}&repair={id}&conv={id}`

### Deux runtimes, **deux stratégies de mémoire** distinctes

Ce n'est **pas** « un runtime par défaut + un fallback ». Les deux implémentent le même protocole WS côté frontend mais portent une philosophie radicalement différente :

| Dimension | `runtime_managed.py` (~1 400 L) | `runtime_direct.py` (~700 L) |
|-----------|----------------------------------|--------------------------------|
| Pack fourni à l'agent | Monté comme `resources.memory_store` côté session Anthropic (accès `read_only`), lu via memory tools natifs côté serveur | Non-monté — l'agent appelle `mb_*` à la demande, qui relisent les JSON du disque et retournent dans les tool_results |
| Coûts | Pack hors du contexte des turns → gain documenté −61 % sur Haiku | Pack re-payé à chaque `mb_get_component`, amorti par `cache_control: ephemeral` sur system+tools |
| Historique | Persistance serveur Anthropic 30 j (`client.beta.sessions.events.list`) + JSONL local en mirror | JSONL local = source de vérité unique, rechargé en `messages=[…]` à chaque reopen |
| Reprise expirée | Haiku résume la session morte → recovery summary injectée dans la nouvelle | Replay direct de la JSONL |
| Cross-repair memory | `mirror_outcome_to_memory` : findings validés → memory store, accessibles à toute session future du device | Findings écrits sur disque ; relus explicitement via `mb_list_findings` |
| Dépendance SDK | `client.beta.agents`, `client.beta.sessions`, `client.beta.memory_stores` (beta 2026-04-01) | `client.messages.stream` standard |
| Quand s'en servir | Usage répété du même device (cabinet de réparation qui voit 10× le même modèle / mois) | Démo ponctuelle, dev local, outage MA, simplicité d'inspection |

Choisi via l'env var `DIAGNOSTIC_MODE=managed|direct` (par défaut `managed`). Le protocole WS est identique → le frontend ne sait pas lequel tourne.

### Tier selection

Query param `?tier=` au WS-open :

| Tier | Modèle | Usage |
|------|--------|-------|
| `fast` | `claude-haiku-4-5` | triage rapide, classification cheap |
| `normal` | `claude-sonnet-4-6` | conversation standard |
| `deep` | `claude-opus-4-7` | raisonnement causal complexe, hypothesize |

Changer de tier = fermer et rouvrir la WS (nouvelle conversation). Pas de swap in-session.

### Tools exposés à l'agent

Manifest : `api/agent/manifest.py`. Sélection dynamique via `build_tools_manifest(session)` — les BV tools sont strippés quand aucune boardview n'est chargée.

| Famille | Nombre | Handler principal | Dispatched depuis |
|---------|--------|-------------------|-------------------|
| MB knowledge (refdes / rules / findings / expand) | 5 | `api/agent/tools.py` | `runtime_*._dispatch_tool()` |
| MB schematic / hypothesize | 2 (`mb_schematic_graph`, `mb_hypothesize`) | `api/tools/schematic.py`, `api/tools/hypothesize.py` | idem |
| MB measurements (record / list / compare / observations / set / clear) | 6 | `api/tools/measurements.py` (+ `api/agent/measurement_memory.py`) | idem |
| MB validation (`mb_validate_finding`) | 1 | `api/tools/validation.py` (+ `api/agent/validation.py`) | idem |
| BV (boardview control) | 12 | `api/tools/boardview.py` | `api/agent/dispatch_bv.py` |
| Profile (`profile_get`, `profile_check_skills`, `profile_track_skill`) | 3 | `api/profile/tools.py` | `runtime_*._dispatch_tool()` |

Total : **29 tools** déclarés dans `manifest.py` (vérifié par `grep -c '"name":'`). Liste complète des noms — voir `manifest.py`, ne pas dupliquer ici.

**État connu** : les handlers sont dispersés en 5 fichiers sans registry unique. Ajouter un tool oblige à éditer `manifest.py` + un fichier d'implémentation + les deux runtimes. Refactor planifié mais non prioritaire (cf. section *Dette architecturale*).

### Modules de support du runtime D

Au-delà des deux runtimes, plusieurs modules dans `api/agent/` portent du state ou des side-effects propres au diagnostic :

| Module | Rôle |
|--------|------|
| `chat_history.py` | Append-only JSONL par conversation (`memory/{slug}/repairs/{rid}/conversations/{cid}/messages.jsonl`). Source de vérité pour le replay direct + mirror MA. |
| `field_reports.py` | Findings cross-session (per-device, pas per-repair). Mirror vers le memory store MA quand dispo. |
| `measurement_memory.py` | Journal mesures par repair (`measurements.jsonl`). Auto-classifie V/A/W/°C/Ω → `ComponentMode` / `RailMode`. Synthétise des `Observations` pour le simulator. |
| `diagnosis_log.py` | Append-only par repair. Loggue chaque turn d'observation/hypothèse/pruning du tool `mb_hypothesize` — corpus brut consommé par les loops d'évaluation et les skill `microsolder-evolve*`. |
| `validation.py` | `RepairOutcome` persistance (`outcome.json` par repair). Reçoit le clic « Marquer fix » du tech : refdes + mode + rationale. |
| `schematic_boardview_bridge.py` | Enrichit le `SimulationTimeline` (schematic) avec la position 2D de la `Board` (PCB). Émet une `EnrichedTimeline` + jusqu'à 8 `ProbePoint` (route physique de mesures). |
| `reliability.py` | Lit `simulator_reliability.json`, injecte une ligne dans le system prompt des deux runtimes. |
| `memory_seed.py` | Première ouverture WS d'une repair fraîche : injecte le pack + findings dans le contexte (mode managed) ou dans le first turn (mode direct). |
| `memory_stores.py` | Cache per-device des MA memory stores. Beta header `managed-agents-2026-04-01`. NoOp gracieux si la beta est down ou absente. |
| `managed_ids.py` | Loader de `managed_ids.json` (env + 3 agents tier-scopés). Fallback legacy single-agent toléré. |
| `pricing.py` | Estimateur de coût des tokens (Avril 2026 : Haiku $1/$5, Sonnet $3/$15, Opus $5/$25 ; cache read 0.10×, cache creation 1.25×). Consommé par les events de progress. |
| `sanitize.py` | Garde-fou anti-hallucination — voir section dédiée ci-dessous. |

### Garde-fou anti-hallucination

Hard rule #5 de `CLAUDE.md`. Deux couches :

1. **Tool discipline** — les tools renvoient `{found: false, closest_matches: [...]}` pour les refdes inconnus, jamais de donnée fabriquée. Le system prompt force l'agent à piocher dans `closest_matches` ou à demander au tech.
2. **Sanitizer post-hoc** — `api/agent/sanitize.py` scanne chaque texte sortant pour les tokens `\b[A-Z]{1,3}\d{1,4}\b` et wraps en `⟨?U999⟩` tout refdes absent de `session.board.part_by_refdes()`. Les deux runtimes y passent avant `ws.send_json`.

### Persistance

```
memory/{slug}/repairs/{repair_id}/
  conversations/{conv_id}/
    messages.jsonl                 # tous les events Anthropic-shaped
    status.json                    # open | in_progress | closed
    ma_session_{tier}.json         # MA session id (mode managed uniquement)
  index.json                       # liste de convs, tiers, coûts
  findings.json                    # snapshot des field reports attachés
```

Le JSONL est appendé **systématiquement**, même en mode managed — c'est le mirror qui permet de reconstruire l'historique en cas d'expiration de la session MA (30 j TTL).

### Bootstrap Managed Agents (prérequis mode `managed`)

Avant la toute première WS en `DIAGNOSTIC_MODE=managed`, lancer une fois :

```bash
.venv/bin/python scripts/bootstrap_managed_agent.py
```

Le script crée l'environnement MA + 3 agents tier-scopés (Haiku/Sonnet/Opus) et écrit `managed_ids.json` à la racine (gitignored). Idempotent. Sans ce fichier, `runtime_managed.py::load_managed_ids()` lève et la WS renvoie une erreur au frontend — le mode `direct` (fallback) n'a **aucun** prérequis de bootstrap.

---

## Les engines déterministes (les pépites)

Deux modules purs qui ne touchent jamais au réseau et qu'un agent autonome (`microsolder-evolve`) optimise en continu. **Interdiction de refactor cosmétique** sur ces fichiers (cf. section *Invariants*).

### `api/pipeline/schematic/simulator.py` — `SimulationEngine`

Simulateur événementiel qui avance phase par phase sur la `boot_sequence` (ou la version `analyzed` quand dispo), prend en entrée une liste de pannes (`refdes` + mode) + overrides de rails optionnels, et émet un `SimulationTimeline` avec pour chaque phase :
- rails morts / en vie
- composants morts (cascade de dépendances)
- signal states
- la cause bloquante

Exposé à l'agent via `mb_schematic_graph(query="simulate", failures=…, rail_overrides=…)` et à l'UI via `POST /schematic/simulate`.

### `api/pipeline/schematic/hypothesize.py` — diagnostic inverse

Prend une observation partielle (composants/rails dead/alive) et énumère les candidats refdes-kill qui l'expliquent :
- **Single-fault exhaustif** : simule chaque refdes tué individuellement, score en F1-soft-penalty.
- **2-fault pruné** : top-K survivants du single-fault × composants dont la cascade intersecte les résidus inexpliqués.

Retourne le top-N avec diff structuré et narration FR déterministe. Dépend de `SimulationEngine`. Pas d'IO, pas de LLM.

### Invariants property-based — `tests/pipeline/schematic/test_simulator_invariants.py`

10 contrats que `simulator.py` et `hypothesize.py` doivent honorer pour tout device qui présente un `electrical_graph.json` sous `memory/`. Le runner **auto-découvre** les devices (`_discover_devices()` scanne `memory/<slug>/electrical_graph.json`) et applique les 10 invariants à chacun (commits `71c4c23`, `3c5ed3c`, `d205ec3`).

| # | Invariant | Garantit |
|---|-----------|----------|
| INV-1 | Cascade ⊆ graph | Le simulator n'invente jamais de refdes. |
| INV-2 | `failures = []` ⇒ cascade vide | Pas de death spontanée. |
| INV-3 | Toute mort de cascade a une cause physique | Pas de mort « orpheline ». |
| INV-4 | Mort de source ⇒ rail mort | Causalité d'alimentation. |
| INV-5 | Rail mort ⇒ consommateurs morts (sauf alternative live) | Propagation cohérente. |
| INV-6 | Déterminisme | Même input → même timeline, run après run. |
| INV-7 | Rails sans source immunes aux kills internes | Pas de death magique. |
| INV-8 | Recall top-5 ≥ seuil sur paires pertinentes | `hypothesize` retrouve la cause. |
| INV-9 | Cohérence du verdict cascade | Pas de contradiction interne. |
| INV-10 | `hypothesize` sur observation vide ⇒ aucun score positif | Pas de faux signal sans signal. |

C'est le filet de sécurité de la skill `microsolder-evolve` : un commit `evolve:` qui casse un de ces 10 tests est immédiatement reverté.

---

## Contrats de données (on-disk, sous `memory/{slug}/`)

Source de vérité inter-modules. **Toujours regarder ce tableau** avant d'ajouter un producteur ou un consommateur.

| Artefact | Écrit par | Lu par |
|----------|-----------|--------|
| `raw_research_dump.md` | `pipeline/scout.py` (+ append `pipeline/expansion.py`) | `pipeline/registry.py`, `pipeline/writers.py`, `pipeline/bench_generator/extractor.py` |
| `registry.json` | `pipeline/registry.py` | `pipeline/mapper.py`, `pipeline/writers.py`, `pipeline/drift.py`, `agent/tools.py::mb_get_component`, frontend `memory_bank.js` |
| `knowledge_graph.json` | `pipeline/writers.py::Cartographe` | `pipeline/graph_transform.py`, `pipeline/subsystem.py`, frontend `graph.js` |
| `rules.json` | `pipeline/writers.py::Clinicien` (+ `pipeline/expansion.py`) | `pipeline/coverage.py`, `agent/tools.py::mb_get_rules_for_symptoms`, `pipeline/bench_generator` |
| `dictionary.json` | `pipeline/writers.py::Lexicographe` | `agent/tools.py::mb_get_component` |
| `audit_verdict.json` | `pipeline/auditor.py` | frontend `home.js`, `memory_bank.js` |
| `schematic_pages/page_XXX.json` | `pipeline/schematic/page_vision.py` | `pipeline/schematic/merger.py` |
| `schematic_graph.json` | `pipeline/schematic/merger.py` | `pipeline/schematic/compiler.py` |
| `electrical_graph.json` | `pipeline/schematic/compiler.py` | `simulator.py`, `hypothesize.py`, `tools/schematic.py`, `pipeline/bench_generator`, **`tests/.../test_simulator_invariants.py`** (auto-discovery) |
| `boot_sequence_analyzed.json` | `pipeline/schematic/boot_analyzer.py` | `simulator.py` (via `analyzed_boot=…`), `api/pipeline/__init__.py` (merge optionnel) |
| `nets_classified.json` | `pipeline/schematic/net_classifier.py` | `api/pipeline/__init__.py` (merge optionnel) |
| `simulator_reliability.json` | `pipeline/bench_generator/writer.py` | `agent/reliability.py` |
| `field_reports/*.md` | `agent/field_reports.py::record_field_report` | `agent/tools.py::mb_list_findings`, MA memory store mirror |
| `repairs/{rid}/conversations/{cid}/messages.jsonl` | `agent/chat_history.py::append_event` | `runtime_direct.py` (replay), `runtime_managed.py` (JSONL fallback summary) |
| `repairs/{rid}/measurements.jsonl` | `agent/measurement_memory.py::append_measurement` | `tools/measurements.py::mb_*_measurements`, simulator observations |
| `repairs/{rid}/diagnosis_log.jsonl` | `agent/diagnosis_log.py::append_turn` | corpus `microsolder-evolve` (eval offline) |
| `repairs/{rid}/outcome.json` | `agent/validation.py::record_outcome` | UI repair row (✓ « Marquer fix »), exports |
| `managed.json` (per-device) | `agent/memory_stores.py::ensure_store` | `runtime_managed.py` (resource mount) |

**Invariant** : tout nouveau module qui **produit** un JSON sous `memory/{slug}/` doit déclarer sa shape dans `pipeline/schemas.py` ou `pipeline/schematic/schemas.py`. Pas de shape « ad-hoc » en markdown ou en comment.

---

## Endpoints HTTP / WS — surface exhaustive (35 routes)

Source de vérité : `api/pipeline/__init__.py` (27 routes), `api/board/router.py` (1), `api/profile/router.py` (4), `api/main.py` (3).

### Pipeline — packs & lifecycle (`api/pipeline/__init__.py`)
- `POST /pipeline/generate` — knowledge factory synchrone (30–120 s)
- `POST /pipeline/ingest-schematic` — wrapper HTTP du workflow B (PDF schéma → `ElectricalGraph`)
- `GET  /pipeline/packs` — liste des packs + bitmask de présence
- `GET  /pipeline/packs/{slug}` — métadonnées d'un pack
- `GET  /pipeline/packs/{slug}/full` — bundle de tous les JSON (Memory Bank)
- `GET  /pipeline/packs/{slug}/findings` — field reports d'un device
- `GET  /pipeline/packs/{slug}/graph` — payload graphe synthétisé
- `POST /pipeline/packs/{slug}/expand` — `pipeline/expansion.py` (Scout + Clinicien focalisés)
- `POST /pipeline/packs/{slug}/documents` — upload datasheets / schéma / boardview fournis par le tech
- `GET  /pipeline/packs/{slug}/documents` — liste des documents uploadés
- `GET  /pipeline/taxonomy` — arbre brand > model > version (home)

### Pipeline — schematic & engines
- `GET  /pipeline/packs/{slug}/schematic` — `electrical_graph.json` + meta
- `GET  /pipeline/packs/{slug}/schematic/pages` — raw vision pages
- `GET  /pipeline/packs/{slug}/schematic/boot` — boot sequence analyzed
- `GET  /pipeline/packs/{slug}/schematic/passives` — classification passives
- `POST /pipeline/packs/{slug}/schematic/analyze-boot` (202) — lance `boot_analyzer` en arrière-plan
- `POST /pipeline/packs/{slug}/schematic/classify-nets` (202) — lance le classifier nets
- `POST /pipeline/packs/{slug}/schematic/simulate` — drives `SimulationEngine` (même shape que `mb_schematic_graph(query="simulate")`)
- `POST /pipeline/packs/{slug}/schematic/hypothesize` — diagnostic inverse depuis observation

### Pipeline — repairs, conversations, mesures
- `POST /pipeline/repairs` — routing à 3 branches (full / expand / none) — voir section dédiée Workflow A
- `GET  /pipeline/repairs` — liste des repairs (home)
- `GET  /pipeline/repairs/{repair_id}` — métadonnées d'une repair
- `GET  /pipeline/repairs/{repair_id}/conversations` — liste des conversations
- `POST /pipeline/packs/{slug}/repairs/{repair_id}/measurements` — append au journal mesures
- `GET  /pipeline/packs/{slug}/repairs/{repair_id}/measurements` — lecture du journal mesures

### Pipeline — landing & progress
- `POST /pipeline/classify-intent` — Haiku forced-tool, free-text → top-3 device slugs (landing 2-champs)
- `WS   /pipeline/progress/{slug}` — events live (`phase_started`, `phase_progress`, `phase_completed`, `phase_narration`, `coverage_check_*`, `expand_*`)

### Board (`api/board/router.py`)
- `POST /api/board/parse` — upload + parse via `parser_for(path)` → `Board` JSON

### Profile (`api/profile/router.py`)
- `GET /profile` — technician profile (catalog / skills / preferences)
- `PUT /profile/identity` — mise à jour identité
- `PUT /profile/tools` — mise à jour outillage
- `PUT /profile/preferences` — mise à jour préférences

### Main (`api/main.py`)
- `GET /health` — healthcheck
- `WS  /ws/diagnostic/{slug}?tier=&repair=&conv=` — conversation diagnostic live
- `WS  /ws` — legacy echo (smoke test)

---

## Boardview parsers (`api/board/parser/`)

Registry extension-based. Un parser = un fichier qui décore `@register` et déclare `extensions = (".ext",)`. 12 formats au total — la table ci-dessous précise le statut **vérifié sur fichiers réels** (commits `b4c8a1a`, `68b1428`, `03a9380`, `3b7cf60`, `d205ec3`).

### DONE — vérifiés sur fichiers réels

| Parser | Format | Particularité |
|--------|--------|---------------|
| `test_link.py` | OpenBoardView `.brd` v3 ASCII, clean-room | refuse les fichiers obfusqués via `ObfuscatedFileError` |
| `brd2.py` | KiCad boardview `.brd2` | export ASCII KiCad standard |
| `kicad.py` | `.kicad_pcb` natif | helpers dans `_kicad_extract.py` |
| `asc.py` | ASUS TSICT `.asc` (multi-fichier ou combiné) | directory-aware sur `format.asc` / `parts.asc` / `pins.asc` / `nails.asc` |
| `fz.py` | ASUS PCB Repair Tool `.fz` | dispatch par magic byte : zlib (déchiffré) ou XOR (gated par env `WRENCH_BOARD_FZ_KEY`, retourne 422 si absente) |
| `bdv.py` | HONHAN BoardViewer `.bdv` | déchiffrement arithmétique symétrique (key 160..286) puis re-parse Test_Link |
| `cad.py` | GenCAD 1.4 ASCII (Mentor / Allegro) + dispatch umbrella | délègue à `_gencad.py`, `_fz_zlib.py`, `BRD2Parser` ou `Test_Link` selon shape |

### PARTIAL — implémenté mais couverture limitée
- `tvw.py` — Tebo IctView 3.0/4.0. Décode rotation-cipher ASCII (rot-13 / rot-10) ou rejette honnêtement les containers binaires (commit `24b962a`).

### SPECULATIVE — heuristiques, non validés sur corpus réel (commit `3b7cf60`)
- `bv.py` (ATE BoardView 1.5) — détecte ASCII Test_Link shape ; rejette les fichiers binaires non-imprimables (>30 % bytes non-ASCII).
- `gr.py` (BoardView R5.0) — détecte markers `Components:` / `Pins:` / `TestPoints:` ; rejette le binaire.
- `cst.py` (IBM Lenovo Castw v3.32) — détecte INI-style `[Format] [Components] [Pins] [Nails]` ; rejette le binaire.
- `f2b.py` (Unisoft ProntoPLACE Place5) — détecte markers ASCII Test_Link ; ignore `Annotations:`.

Ces 4 parsers extraient correctement quand ils tombent sur un dialecte ASCII compatible Test_Link, mais aucun fichier réel propriétaire n'a encore été parsé end-to-end. Le label `SPECULATIVE` est explicite dans la roadmap pour ne pas mentir au technicien.

### Helpers partagés
- `_ascii_boardview.py` — `parse_test_link_shape(text, dialect)` factorise les dialectes Test_Link (commit `d1381bb`).
- `_fz_zlib.py` — décompression zlib + format pipe-delimited (Quanta / ASRock / ASUS Prime / Gigabyte).
- `_gencad.py` — parser ASCII GenCAD 1.4 (`$HEADER`, `$SHAPES`, `$COMPONENTS`, `$SIGNALS`).
- `_kicad_extract.py` — extraction modules / nets depuis l'arbre s-expr KiCad.
- `_stub.py` — shape générique pour les parsers en attente d'un corpus.

### Tests d'intégration sur vrais boards
- `tests/board/test_parser_real_hardware.py` — fixtures MNT Reform (493 parts, 2 104 pins) — vrai matériel open-hardware.
- `tests/board/test_parser_consistency.py` — invariants cross-parser sur tous les fichiers détectés.
- `tests/board/test_parser_realistic_scale.py` — sweep scale-up.
- `tests/board/test_real_files_runner.py` — drop-in runner pour fichiers locaux ad-hoc.

Ajouter un format = un nouveau fichier sous `api/board/parser/`, aucun changement dans `base.py`.

---

## Dette architecturale connue

Ne pas les résoudre « par hygiène » — chaque item a un arbitrage ROI que le code actuel ne justifie pas encore.

| Zone | Description | Pourquoi on tolère |
|------|-------------|--------------------|
| **Tool registry dispersé** | 29 tools déclarés dans `manifest.py`, handlers en 5 fichiers, dispatch dupliqué dans les 2 runtimes | Refactor `tool_registry.py` planifié, ROI faible tant que le manifest est stable |
| **Boot sequence dual-path** | `compiler.boot_sequence` (topologique) vs `boot_analyzer.analyzed_boot_sequence` (Opus) | Analyzer optionnel, graceful fail documenté ; le simulator accepte les deux |
| **WS events sans schema partagé** | Backend Pydantic (`api/tools/ws_events.py`), frontend lit `event.type` en string matching | Faible surface de changement, schema TypeScript généré coûte cher pour un bénéfice marginal |
| **`memory/` contrats JSON non-versionnés** | Pas de migration framework quand une shape évolue | `pipeline/schemas.py` est contract-first ; les fichiers sont régénérables |
| **Modules vides** (`api/vision/`, `api/telemetry/`) | `__init__.py` de 2 lignes, réservés | Intentionnellement stubs — espaces réservés à une extension future |

---

## Invariants

### À NE JAMAIS faire

1. **Refactor cosmétique sur `simulator.py` ou `hypothesize.py`.** Le loop `microsolder-evolve` optimise ces deux fichiers et a besoin de mesurer des deltas propres. Functional changes OK, style churn non.
2. **Écrire sur `benchmark/scenarios.jsonl`.** Oracle humain, read-only pour toute l'IA.
3. **Merger l'auto-généré (`benchmark/auto_proposals/`) dans le frozen oracle.**
4. **Duplication de shape** : toute nouvelle shape JSON doit vivre dans un fichier `schemas.py`, jamais redéfinie ailleurs.
5. **Skip `simulator_reliability.json`.** L'agent perd son auto-conscience de fiabilité.
6. **Migrer le pipeline sur Managed Agents.** Le split stateless/stateful est intentionnel.
7. **Casser un des 10 invariants** de `tests/pipeline/schematic/test_simulator_invariants.py`. C'est le filet de sécurité de `microsolder-evolve` ; un commit qui les casse est immédiatement reverté par la skill.
8. **Promouvoir un parser SPECULATIVE en DONE sans run sur fichier réel.** Le label SPECULATIVE est honnête vis-à-vis du tech ; le passer à DONE exige un fichier propriétaire vérifié end-to-end.

### À TOUJOURS faire

1. **Tools retournent `{found: false, reason: …, closest_matches: […]}`** — jamais de donnée fabriquée.
2. **Chaque reply agent passe par `sanitize_agent_text()`** avant d'être envoyé au frontend.
3. **Streaming token-par-token** : jamais de batch full-response sur le WS diagnostic ni sur pipeline progress.
4. **`git commit -- path1 path2`** avec paths explicites — `evolve` tourne en parallèle, `git add .` bundlerait son travail sous un message trompeur (incident réel : `e053002`, corrigé dans `71dd23a`).

---

## Extension points

### « Je veux ajouter une phase au workflow A »
1. Nouveau module sous `api/pipeline/` avec son forced tool.
2. Shape Pydantic ajoutée à `pipeline/schemas.py`.
3. Appel ajouté dans `orchestrator.generate_knowledge_pack()`.
4. Artefact écrit sous `memory/{slug}/`.
5. Update `pipeline/drift.py` si la phase ajoute du vocabulaire canonique.
6. Update `auditor.py` si le verdict doit couvrir la nouvelle phase.

### « Je veux ajouter un tool à l'agent »
1. Handler Python quelque part sous `api/agent/` ou `api/tools/`.
2. Entrée dans `api/agent/manifest.py` avec `input_schema` JSON Schema.
3. Branch de dispatch dans **les deux** runtimes (`_dispatch_tool` de managed, `_dispatch_mb_tool` / `_dispatch_profile_tool` de direct).
4. (Si BV) update `api/agent/dispatch_bv.py` + `api/tools/boardview.py`.
5. Test unitaire isolé du dispatch.

### « Je veux ajouter un parser boardview »
1. Nouveau fichier sous `api/board/parser/`.
2. Classe décorée `@register`, attribut `extensions = (".xxx",)`.
3. Parse vers le modèle `Board` commun (`api/board/model.py`).
4. Test fixture minimal.

### « Je veux ajouter une section UI »
1. Append dans `SECTIONS` (`web/js/router.js`) + entrée `SECTION_META`.
2. Rail button dans `web/index.html` avec `data-section="…"`.
3. Soit un vrai DOM block avec id, soit `<section class="stub" data-section-stub="…">` temporaire.
4. Handler dans `web/js/main.js` si la section a une logique de montage.

---

## Références croisées

- Règles produit + hard rules : [CLAUDE.md](../CLAUDE.md)
- Specs actuelles : [docs/superpowers/specs/](superpowers/specs/)
- Plans en cours : [docs/superpowers/plans/](superpowers/plans/)
- Scénarios oracle : [benchmark/README.md](../benchmark/README.md)

Ce document reflète l'état du repo au `2026-04-26`. Maintenir avec les changements structurels ; les ajustements purement tactiques (nouveau tool, nouveau parser, nouvelle section) vivent dans leur spec dédiée sous `docs/superpowers/`.
