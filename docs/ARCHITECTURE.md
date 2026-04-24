# Architecture

Référence complète de l'architecture `microsolder-agent`. Ce document est la carte mentale à partager entre collaborateurs — il complète `CLAUDE.md` (règles + tour d'horizon) en détaillant les **flux IA**, les **contrats inter-modules** et les **points d'extension**.

À lire à froid avant toute modification structurelle : pipeline, runtime diagnostic, engines déterministes, parsers boardview, registry des tools.

---

## TL;DR

`microsolder-agent` est un workbench agent-natif pour le diagnostic microsoudure au niveau carte. L'architecture repose sur **quatre workflows IA orthogonaux** qui produisent et consomment un même corpus on-disk (`memory/{slug}/`) :

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

### Les 4 phases

| Phase | Module | Modèle | Tool forcé | Sortie |
|-------|--------|--------|------------|--------|
| 1 Scout | `scout.py` | Sonnet | native `web_search` (non-forcé) | `raw_research_dump.md` |
| 2 Registry | `registry.py` | Sonnet | `submit_registry` | `registry.json` |
| 3 Writers ×3 | `writers.py` | Opus ×3 | `submit_knowledge_graph`, `submit_rules`, `submit_dictionary` | `knowledge_graph.json`, `rules.json`, `dictionary.json` |
| 4 Auditor | `auditor.py` | Opus | `submit_audit_verdict` | `audit_verdict.json` |

### Particularités non-évidentes

- **Scout résilience** : gère les `pause_turn` du SDK, rejette les dumps « maigres » (< N symptômes / sources / composants), relance avec un scope élargi. Depuis `6377d3b`, Scout peut consommer un **graphe électrique, un boardview parsé et des datasheets PDF** fournis par le tech — les MPN extraits deviennent des query seeds ciblés.
- **Registry → Writers** : la `registry.json` est le vocabulaire canonique. Toute violation (refdes émis par un Writer absent de la registry) déclenche `drift.py`, qui compile un rapport de drift passé **en entrée** à l'Auditor comme ground truth déterministe.
- **Writers cache warmup** : les trois writers partagent un préfixe long (raw dump + registry + system prompt) marqué `cache_control: ephemeral`. Writer 1 part en premier, les writers 2 et 3 attendent `cache_warmup_seconds` pour que la cache entry matérialise — d'où un gain tokens réel de −75 % sur les writers 2-3.
- **Auditor loop** : verdict `NEEDS_REVISION` → `_apply_revisions()` relance les writers flaggés (max `pipeline_max_revise_rounds`). Verdict `REJECTED` lève. Un drift check déterministe coupe la boucle après `max_rounds`.
- **Post-pipeline** : `graph_transform.pack_to_graph_payload()` synthétise des nœuds d'action et émet le JSON consommé par `web/js/graph.js` (colonnes Actions → Components → Nets → Symptoms).

### Source de vérité des shapes

`api/pipeline/schemas.py` définit tous les modèles Pydantic du pack. Ils servent *à la fois* de validateurs runtime et de sources JSON Schema pour `input_schema` des tools forcés. **Ne jamais dupliquer une shape** — tout importer de là.

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
│    + pdfplumber scan detection       │  200 DPI, détection orientation
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

```bash
python -m api.pipeline.schematic.cli --pdf=board.pdf --slug=my-device
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
| MB (memory bank + board aggregation) | 5 | `api/agent/tools.py` | `runtime_*._dispatch_tool()` |
| MB (schematic) | 2 (`mb_schematic_graph`, `mb_hypothesize`) | `api/tools/schematic.py`, `api/tools/hypothesize.py` | idem |
| MB (measurements + validation) | 7 | `api/tools/measurements.py`, `api/tools/validation.py` | idem |
| BV (boardview control) | 12 | `api/tools/boardview.py` | `api/agent/dispatch_bv.py` |
| Profile | 3 | `api/profile/tools.py` | `runtime_*._dispatch_tool()` |

**État connu** : les handlers sont dispersés en 5 fichiers sans registry unique. Ajouter un tool oblige à éditer `manifest.py` + un fichier d'implémentation + les deux runtimes. Refactor planifié mais non prioritaire (cf. section *Dette architecturale*).

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

---

## Contrats de données (on-disk, sous `memory/{slug}/`)

Source de vérité inter-modules. **Toujours regarder ce tableau** avant d'ajouter un producteur ou un consommateur.

| Artefact | Écrit par | Lu par |
|----------|-----------|--------|
| `raw_research_dump.md` | `pipeline/scout.py` | `pipeline/registry.py`, `pipeline/writers.py`, `pipeline/bench_generator/extractor.py` |
| `registry.json` | `pipeline/registry.py` | `pipeline/writers.py`, `pipeline/drift.py`, `agent/tools.py::mb_get_component`, frontend `memory_bank.js` |
| `knowledge_graph.json` | `pipeline/writers.py::Cartographe` | `pipeline/graph_transform.py`, frontend `graph.js` |
| `rules.json` | `pipeline/writers.py::Clinicien` | `agent/tools.py::mb_get_rules_for_symptoms`, `pipeline/bench_generator` |
| `dictionary.json` | `pipeline/writers.py::Lexicographe` | `agent/tools.py::mb_get_component` |
| `audit_verdict.json` | `pipeline/auditor.py` | frontend `home.js`, `memory_bank.js` |
| `schematic_pages/page_XXX.json` | `pipeline/schematic/page_vision.py` | `pipeline/schematic/merger.py` |
| `schematic_graph.json` | `pipeline/schematic/merger.py` | `pipeline/schematic/compiler.py` |
| `electrical_graph.json` | `pipeline/schematic/compiler.py` | `simulator.py`, `hypothesize.py`, `tools/schematic.py`, `pipeline/bench_generator` |
| `boot_sequence_analyzed.json` | `pipeline/schematic/boot_analyzer.py` | `simulator.py` (via `analyzed_boot=…`), `api/pipeline/__init__.py` (merge optionnel) |
| `nets_classified.json` | `pipeline/schematic/net_classifier.py` | `api/pipeline/__init__.py` (merge optionnel) |
| `simulator_reliability.json` | `pipeline/bench_generator/writer.py` | `agent/reliability.py` |
| `field_reports/*.md` | `agent/field_reports.py::record_field_report` | `agent/tools.py::mb_list_findings`, MA memory store mirror |
| `repairs/{rid}/conversations/{cid}/messages.jsonl` | `agent/chat_history.py::append_event` | `runtime_direct.py` (replay), `runtime_managed.py` (JSONL fallback summary) |

**Invariant** : tout nouveau module qui **produit** un JSON sous `memory/{slug}/` doit déclarer sa shape dans `pipeline/schemas.py` ou `pipeline/schematic/schemas.py`. Pas de shape « ad-hoc » en markdown ou en comment.

---

## Endpoints HTTP / WS

### Pipeline (`api/pipeline/__init__.py`)
- `POST /pipeline/generate` — knowledge factory synchrone (30–120 s)
- `POST /pipeline/repairs` — crée une repair + fire-and-forget pack gen si device nouveau
- `WS /pipeline/progress/{slug}` — events live (phase_started, phase_progress, phase_completed)
- `GET /pipeline/packs` — liste des packs + bitmask de présence
- `GET /pipeline/packs/{slug}` — métadonnées d'un pack
- `GET /pipeline/packs/{slug}/full` — bundle de tous les JSON (Memory Bank)
- `GET /pipeline/taxonomy` — arbre brand > model > version (home)

### Board (`api/board/router.py`)
- `POST /api/board/parse` — upload + parse via `parser_for(path)` → `Board` JSON

### Schematic
- `POST /schematic/simulate` — drives `SimulationEngine` (même shape que `mb_schematic_graph(query="simulate")`)

### Diagnostic
- `WS /ws/diagnostic/{slug}?tier=&repair=&conv=` — conversation live
- `WS /ws` — legacy echo (smoke test)

---

## Boardview parsers (`api/board/parser/`)

Registry extension-based. Un parser = un fichier qui décore `@register` et déclare `extensions = (".ext",)`.

### Implémentés
- `test_link.py` — OpenBoardView `.brd` v3 clean-room, refuse les fichiers obfusqués (`ObfuscatedFileError`)
- `brd2.py` — KiCad boardview `.brd2`
- `kicad.py` — `.kicad_pcb` natif (helpers dans `_kicad_extract.py`)

### Stubs (chaque fichier déclare ses extensions et raise `NotImplementedError`)
`bv.py`, `cad.py`, `gr.py`, `cst.py`, `tvw.py`, `asc.py`, `fz.py`, `f2b.py`, `bdv.py`. Shape générique dans `_stub.py`.

Ajouter un format = un nouveau fichier sous `api/board/parser/`, aucun changement dans `base.py`.

---

## Dette architecturale connue

Ne pas les résoudre « par hygiène » — chaque item a un arbitrage ROI que le code actuel ne justifie pas encore.

| Zone | Description | Pourquoi on tolère |
|------|-------------|--------------------|
| **Tool registry dispersé** | 26 tools déclarés dans `manifest.py`, handlers en 5 fichiers, dispatch dupliqué dans les 2 runtimes | Refactor `tool_registry.py` planifié, ROI faible tant que le manifest est stable |
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

Ce document reflète l'état du repo au `2026-04-25`. Maintenir avec les changements structurels ; les ajustements purement tactiques (nouveau tool, nouveau parser, nouvelle section) vivent dans leur spec dédiée sous `docs/superpowers/`.
