# wrench-board — Design Spec v1

> **⚠️ ARCHIVE — brainstorming historique (2026-04-21). Ne pas implémenter depuis ce doc.**
>
> Le produit a pivoté depuis cette spec. Pour la logique actuelle, voir :
> - **Architecture backend** : `2026-04-22-backend-v2-knowledge-factory.md`
> - **Cible démo** : MNT Reform motherboard (cf. `CLAUDE.md` — plus de triptyque Pi 4 / Framework / iPhone)
> - **Code source** : `api/pipeline/`, `api/board/parser/`, `api/tools/`
>
> **Encore valide dans ce doc** : §4 (layout disque des knowledge packs), §10.5 (UI Memory Bank — 3 onglets Timeline / Knowledge / Stats), §10.6 (UI Agent — 4 onglets Config / Historique / Traces / Coûts), §10.7 (Profile), §10.8 (Aide).
>
> **Périmé dans ce doc** : §1.3 (triptyque de devices), §3 (schema Postgres — on est disque-only), toute mention de `api/memory_bank/` (renommé `api/pipeline/`), toute mention de Raspberry Pi 4, les 7 tools `mb_*` (devenus 12 tool handlers boardview dans `api/tools/`), "Document-Centric" (remplacé par "Knowledge-Centric" via web_search).

---

> Agent-native board-level diagnostics workbench piloté par Claude Opus 4.7.

| Champ | Valeur |
|---|---|
| Date de rédaction | 2026-04-21 |
| Status | Draft — section par section avec gate de validation |
| Auteur | Alexis (+ Claude Opus 4.7 en pair-design) |
| Contexte | *Built with Opus 4.7* Hackathon (Anthropic × Cerebral Valley, 2026-04-21 → 2026-04-26) |
| Spec location | `docs/superpowers/specs/2026-04-21-wrench-board-v1-design.md` |

---

## 1. Vision & scope

### 1.1 Positionnement

`wrench-board` transforme un technicien microsoudure en opérateur d'un copilote de diagnostic. L'utilisateur pose ses questions en langage naturel (« pourquoi le rail 3V3 ne monte pas ? », « où est le PMIC ? »), et un agent Claude Opus 4.7 **pilote visuellement** l'interface — il highlight des composants sur le boardview, ouvre les bonnes pages du schematic, cite ses sources, et consigne tout dans un journal de réparation.

Le projet est construit autour d'une **couche de connaissance technique par device** — la Memory Bank — qui est simultanément :

- **consultée** par l'agent diagnostic via 7 tools typés `mb_*` (contrat anti-hallucination : refdes invalide → `{error, closest_matches}`, jamais d'invention) ;
- **générée autonomement** par un pipeline multi-agents (Coordinator + Registry Builder + 3 Writers parallèles + Auditor + Reviser + Facts Extractor) ;
- **bootstrappée par deep research web** (Anthropic `web_search_20250305`) quand aucun schematic n'est fourni ;
- **enrichie en continu** par un cycle apprenant : chaque cas résolu consolide les patterns `evidence.json`, et au-delà d'un seuil (3 cas même cause, 60 % confirmation) un sub-agent `rule_synthesizer` promeut une règle `[LEARNED]` avec confidence initiale 0.55.

Le résultat : l'outil **s'améliore à l'usage** — plus il résout de cas sur un device donné, plus son guidage devient précis, et plus un symptôme récurrent est diagnostiqué vite (`« I've seen this 3 times on Pi 4, likely C29 again. »`).

### 1.1b Livrable hackathon

Trois artefacts sont dus **dimanche 26 avril 20:00 EST** via la plateforme Cerebral Valley :

1. Une **vidéo démo de 3 minutes** — enregistrée, éditée, voix off anglaise générée par **ElevenLabs** (pas de jugement en live, donc storyboard précis possible, plans montés, timelapses accélérés là où les durées réelles dépassent le budget temps de la vidéo).
2. Le **repo GitHub public complet**, sous licence **Apache 2.0**, prêt à être cloné et lancé.
3. Un **résumé écrit de 100–200 mots** décrivant le projet, les features, et la posture « agent-native ».

Conséquence directe sur les décisions de stack et d'architecture : la solution doit être **démontrable sans dépendre d'une board physique filmée en temps réel**. Les plans atelier (microscope, fer, board) peuvent être statiques ou B-roll ; le cœur de la vidéo est l'interface pilotée par l'agent, capturable en screen-recording à tout moment.

### 1.2 Les cinq règles dures du projet

Ces règles sont **non-négociables** et guident toute décision d'architecture, de dépendance, de contenu. Elles figurent déjà dans `CLAUDE.md` et doivent être auditées avant chaque commit.

| # | Règle | Conséquence concrète |
|---|---|---|
| 1 | **Tout le code from scratch** pendant la semaine du hackathon | Zéro import de repos externes. Composants UI, prompts, schemas, pipelines : tout vient de ce repo uniquement. |
| 2 | **Licence Apache 2.0** | Fichier `LICENSE` à la racine, copyright 2026 Alexis. Chaque fichier source démarre par un header Apache court. |
| 3 | **Dépendances permissives uniquement** (MIT / Apache 2.0 / BSD / PostgreSQL License) | Jamais GPL / AGPL / LGPL / SSPL. MongoDB exclu d'office. Chaque ajout à `pyproject.toml` vérifié. |
| 4 | **Hardware open uniquement** | Aucun schematic propriétaire Apple / Samsung / ZXW / WUXINJI. L'iPhone X de la démo est knowledge-only via sources publiques, **zéro asset proprio** dans le repo. |
| 5 | **Anti-hallucination stricte sur les refdes** | Tout refdes mentionné par l'agent (`U7`, `C29`, etc.) est validé via `mb_get_component` contre le registre du device avant rendu à l'utilisateur. Tool qui ne trouve pas → `{error, closest_matches}`, jamais d'invention. |

### 1.3 Triptyque de devices pour la démo

La démo enchaîne trois devices choisis pour raconter une **progression narrative** du pipeline de knowledge.

| Device | Rôle narratif | Source primaire | Confidence cap | Couverture démo |
|---|---|---|---|---|
| **Raspberry Pi 4 Model B** | Device primaire, diagnostic complet avec pilotage UI de bout en bout | Schematic officiel public de `raspberrypi.com` | **0.85** | 100 % des fonctionnalités : tous les `mb_*` résolvent, cycle learned rules, journal, cases. |
| **Framework Laptop 13** (mainboard) | Preuve de scalabilité : même pipeline sur hardware complexe multi-rails | Schematics officiels Framework sur GitHub (licence **CC-BY**) | **0.85** | Génération du knowledge pack **filmée puis montée en timelapse accéléré** (6–10 s dans la vidéo finale), avec split-screen des 5 sub-agents actifs en parallèle. La génération réelle (30–60 s) tourne en arrière-plan ; les étapes-clés sont capturées et montées en post-production. Tournage planifié **J+4 matin** (cf. section 12). |
| **iPhone X** | Démo du **deep research fallback** quand aucun schematic n'est fourni | Uniquement sources publiques : iFixit, teardowns Rossmann publics, datasheets composants publics (PMIC, Tristar U2, etc.), Wikipedia A11 Bionic | **0.65** | Knowledge pack construit autonomement depuis le web. Badges UI explicites `inferred from public sources`. **Zéro asset propriétaire** dans le repo. |

**Enchaînement de la démo** : Pi 4 (pipeline rodé + diagnostic complet) → Framework (même pipeline scale sur hardware complexe) → iPhone X (pipeline se débrouille sans schematic).

### 1.4 Hors-scope explicite (v1 hackathon)

Ce qui n'est **pas** livré pendant la semaine, mais laissé possible par l'architecture :

- Éditeur utilisateur de schematics ou de boardviews
- UX portrait-only / mobile pur (iPad landscape = cible unique)
- Marketplace ou partage cross-utilisateurs des knowledge packs
- Authentification multi-utilisateurs (profil = single-user local)
- Intégration hardware tiers (oscilloscopes, multimètres connectés, caméras thermiques)
- Features de formation / certification officielles
- Support de schematics propriétaires (Apple, Samsung, ZXW, WUXINJI)
- **Persistance distribuée ou sync cloud** — le repo est local-first sur la machine du développeur, Postgres en `docker-compose` local uniquement
- **Authentification externe ou rate limiting pour usage public** — le repo public est un proof-of-concept open source, pas un service hébergé en production

Tout ce qui n'est pas explicitement listé dans les sections 2–13 de ce spec est **hors-scope par défaut**.

---

## 2. Architecture globale

### 2.1 Diagramme en couches

```
┌──────────────────────────────────────────────────────────────┐
│  Frontend — iPad Safari (landscape)                          │
│  Vanilla HTML/CSS/JS · Tailwind · Alpine.js · PDF.js         │
│  sidebar nav · section full-page · panel LLM (push)          │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTP + WebSocket (LAN)
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Backend FastAPI (uvicorn, asyncio, single worker en dev)    │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ orchestration/   ManagedAgentOrchestrator (impl unique) │ │
│  │ memory_bank/     packs on-disk + 7 mb_* tools + pipeline│ │
│  │ agent/           diagnostic agent session runtime       │ │
│  │ tools/           UI control tools + web_search wrapper  │ │
│  │ board / schematic / vision                              │ │
│  │ profile / telemetry / db / managed / ws                 │ │
│  └────────────────────────────────────────────────────────┘ │
└────┬────────────────┬──────────────────┬─────────────────────┘
     │                │                  │
     ▼                ▼                  ▼
┌──────────┐  ┌────────────────┐  ┌────────────────────┐
│ Postgres │  │ Anthropic API  │  │ memory/ (disque)   │
│ 16       │  │ Managed Agents │  │ knowledge packs    │
│ docker   │  │ + Memory       │  │ markdown + JSON    │
│ local    │  │ Stores         │  │ par device         │
│          │  │ + sessions     │  │                    │
│ meta +   │  │ + web_search_  │  │ versionnable git   │
│ events + │  │ 20250305       │  │                    │
│ cache    │  │                │  │                    │
└──────────┘  └────────────────┘  └────────────────────┘
```

Trois tiers au service du backend :
- **Postgres 16** en `docker-compose` local — source de vérité pour les métadonnées persistantes et requêtables
- **Anthropic Managed Agents API** (beta) — hébergement des agents, memory stores, sessions, tool `web_search_20250305`
- **Dossier `memory/`** sur disque — où vivent les knowledge packs en markdown + JSON (versionnables via git, format canonique lu directement par les tools `mb_*`)

### 2.2 Responsabilités par couche

**Frontend** (`web/`) — vanilla HTML/CSS/JS, zéro build step. Tailwind + Alpine.js + PDF.js via CDN. Tient l'état UI courant (device actif, conversation ouverte) en mémoire browser + rehydrate depuis l'API à chaque reload. Ne fait **aucun appel** direct à Anthropic — tout passe par le backend.

**Backend FastAPI** (`api/`) — un seul processus uvicorn en dev, stateless à l'échelle des requêtes HTTP, stateful pour les WebSocket ouverts. Cinq rôles :
1. **Relais** entre browser WebSocket et Anthropic session event stream
2. **Tool dispatcher** — intercepte `agent.custom_tool_use` et route vers le handler (mb_\*, UI control, web_search)
3. **Orchestrateur** du pipeline de génération de knowledge packs (`SubAgentOrchestrator`)
4. **Gardien anti-hallucination** — validation systématique des refdes sortants de l'agent
5. **Persistance** — écriture événements, cases, coûts, profile vers Postgres

**Postgres** — stocke tout ce qui doit survivre aux redémarrages et être requêtable :
- Métadonnées devices et knowledge packs (pointeurs disque, confidence, origin)
- Sessions (id, device, `agent_id`, `memory_store_id`, `usage_stats`)
- Événements agent (mirror de la timeline pour l'UI)
- Cases résolus (fiches structurées)
- Cost tracking (ligne par appel API)
- Cache `web_search` (clé = `SHA256(query_text)`, TTL 7 jours)
- Profil utilisateur (stats dérivées + overrides editable)

**`memory/`** (disque) — knowledge packs en format texte canonique : `registry.json`, `architecture.md`, `rules.md`, `dictionary.md`, `evidence.json`, `sources.json`, `cases/*.md`. Les tools `mb_*` lisent **directement** ces fichiers ; Postgres ne stocke que des pointeurs. Ce choix permet le versioning git natif et le debug manuel (lecture humaine).

**Anthropic Managed Agents** — **un agent managé par rôle** (1 diagnostic + 7 sub-agents du pipeline), **un memory store par device**, des sessions créées à la demande. Les memory stores remplacent toute logique de persistance de conversation côté backend : Anthropic consulte automatiquement + sauvegarde les learnings à la fin de chaque session.

### 2.3 Les trois flux principaux

**Flux A — Session de diagnostic** (utilisateur ↔ agent)

1. L'utilisateur ouvre un device depuis Home → le browser ouvre `/ws/session/{device_id}`
2. Backend crée une `managed_session` Anthropic attachée à l'`agent_id` et au `memory_store_id` du device → `session_id` inscrit en Postgres
3. L'utilisateur tape → backend envoie `user.message` à Anthropic via `sessions.events.send()`
4. Anthropic consulte automatiquement le memory store, l'agent décide → émet `agent.message` (texte streamé) et/ou `agent.custom_tool_use`
5. Backend intercepte chaque `custom_tool_use` :
   - `mb_*` → dispatch vers `api/memory_bank/tools.py`, lecture disque, réponse structurée
   - Tools **boardview** (les 12 de Boardviewer spec §9) → dispatch vers `api/tools/boardview.py` → **L1 validator `is_valid_refdes`** → **L2 validation `mb_get_component`** → émission d'un message WS suivant **exactement** le protocole `boardview.*` défini dans Boardviewer spec §10 → réponse Anthropic `user.custom_tool_result`. wrench-board **ne définit pas** de nouveaux messages `boardview.*` (cf. §2.7).
   - Tools **schematic** (à venir, spec séparée) → messages `schematic.*` équivalents
6. Événements mirrorés dans Postgres `events` au fil de l'eau, pour alimenter la Timeline de l'UI
7. Fin de session → Anthropic sauvegarde learnings dans le memory store automatiquement (zéro prompt custom à écrire)

**Flux B — Génération de knowledge pack depuis schematic**

1. User clique « Generate knowledge pack for [device] » dans Memory Bank, avec PDF uploadé
2. Backend crée `generation_job` en DB → le browser ouvre `/ws/generation/{job_id}`
3. `ManagedAgentOrchestrator` séquence :
   - **Phase 1** — `run_registry_builder(device, sources=[pdf])` → session sur agent managé `registry_builder` → tool `submit_registry` forcé (`tool_choice={type:"tool", name:"submit_registry"}`) → JSON structuré retourné
   - **Phase 2** — `run_writers_parallel(registry, sources)` → 3 sessions concurrentes via `asyncio.gather` sur `writer_architecture`, `writer_rules`, `writer_dictionary` → 3 sorties `.md` + `.json`, contrainte système : utiliser uniquement les `canonical_name` du registre
   - **Phase 3** — `run_auditor(arch, rules, dict)` → session sur agent `auditor` → tool `submit_audit_verdict` forcé → `{overall_status, consistency_score, files_to_rewrite, drift_report, revision_brief}`
   - **Phase 4** (conditionnelle, max 1 round) — si `files_to_rewrite` non vide : `run_reviser(brief, file_name, content)` par fichier flaggé → retour Phase 3
   - **Phase 5** (optionnelle, Haiku 4.5) — `run_facts_extractor(files)` → `facts.native.json` pour benchmark
4. Backend écrit les fichiers finaux dans `memory/{vendor}/{model}/`
5. Enregistre les métadonnées pack dans Postgres `knowledge_packs` (path, confidence, origin, coût total)
6. Émet des événements de progression au browser tout au long (fin de chaque phase, écriture fichier, update audit)

**Flux C — Deep research fallback** (aucun schematic fourni)

Identique au Flux B, avec trois différences :
- Le `registry_builder` reçoit le tool `web_search_20250305` en plus → effectue 10–20 recherches pour bootstrapper le registre depuis iFixit, Rossmann, datasheets publics, Wikipedia
- Les writers reçoivent le même tool pour enrichir architecture / rules / dictionary
- Chaque fichier généré est tagué `origin: "deep_research"`, `confidence_cap: 0.65`, et `sources.json` consolide **toutes** les URLs citées par `web_search`
- Résultats `web_search` cachés dans Postgres `web_search_cache` (clé `SHA256(query_text)`, TTL 7 jours) → permet de re-rouler la démo sans repayer les recherches

### 2.4 Topologie réseau et déploiement dev

- Machine dev : Pop!_OS (cf. `~/.claude/CLAUDE.md` pour le hardware)
- Serveur dev : `uvicorn api.main:app --reload --host 0.0.0.0 --port 8000`
- Postgres : `docker compose up -d postgres` (image officielle `postgres:16`, volume monté pour persister entre runs)
- Navigateur iPad : connexion LAN directe sur `http://192.168.1.48:8000`
- Companion visuel de design (sessions brainstorming uniquement) : `192.168.1.48:56127`

Aucune synchro cloud, aucun déploiement production. Repo local-first, pushable sur GitHub public quand la démo est prête.

### 2.5 Arborescence backend (révision du scaffolding initial)

```
api/
├── main.py                  # FastAPI bootstrap, montages WS + HTTP + static
├── config.py                # Settings Pydantic (env)
├── logging_setup.py         # logging stdout
│
├── db/                      # pool asyncpg, schema.sql, repositories/
├── managed/                 # Anthropic Managed Agents SDK wrapper (agents, memory_stores, sessions)
├── orchestration/           # SubAgentOrchestrator Protocol + ManagedAgentOrchestrator impl
├── memory_bank/             # knowledge packs on-disk + 7 mb_* tools + pipeline generation
├── agent/                   # diagnostic agent session runtime (prompting, decision loop)
├── tools/
│   ├── boardview.py         # 12 handlers Tier 1-2-3 (cf. Boardviewer spec §9) — L1+L2 validation
│   ├── schematic.py         # handlers schematic.* (spec séparée à venir)
│   └── web_search.py        # wrapper web_search_20250305 + cache Postgres
├── board/                   # domaine Boardview — structuré selon Boardviewer spec
│   ├── model.py             #   Board, Part, Pin, Net, Nail, Layer (Boardviewer spec §5)
│   ├── validator.py         #   is_valid_refdes, suggest_similar (Boardviewer spec §7)
│   └── parser/
│       ├── base.py          #   parser abstrait
│       ├── brd.py           #   OpenBoardView .brd
│       ├── brd2.py
│       ├── bdv.py
│       └── fz.py
├── schematic/               # PDF parse + net extraction + rendering pour affichage
├── profile/                 # stats dérivées + overrides editable
├── telemetry/               # event logging + cost tracking (pricing.py, usage.py)
└── ws/                      # endpoints /ws/session et /ws/generation
```

**Conservés** du scaffolding initial : `agent`, `board`, `tools`, `telemetry`.
**Ajoutés** : `db`, `managed`, `orchestration`, `memory_bank`, `schematic`, `profile`, `ws`.
**Fusionné / supprimé** :
- `vision/` → fusionné dans `schematic/` (rendering PDF pour le viewer schematic). Les rendus du domaine Boardview sont gérés côté frontend par `web/boardviewer/` conformément à la Boardviewer spec.
- `session/` → supprimé, état session porté soit par WebSocket (éphémère) soit par Postgres (persistant).

**Note sur `api/board/`** — ce dossier implémente la partie backend du domaine Boardview selon les indications structurelles de la Boardviewer spec (§5, §7, et §2–§4 pour le parser). wrench-board ne **redéfinit pas** ces composants dans le présent spec — la Boardviewer spec est la source de vérité (cf. §2.7).

### 2.6 Arborescence frontend

Le frontend (`web/`) évolue significativement (Home + sidebar + 6 sections full-page + panel LLM push), mais le détail du layout, des fichiers JS/CSS et des composants Alpine est traité **section 9** (UI layout général) et **section 10** (détail par section). Section 2 se limite à acter que `web/` devient plus qu'un `index.html` + deux fichiers : il y aura plusieurs vues (Home et workbench) et plusieurs modules JS (un par section).

**Point critique** : le composant **boardviewer** (parsing de fichiers PCB + rendering canvas + API de contrôle) est développé **en parallèle par un agent Claude Code séparé** (« Agent Boardviewer »). Il vit sous `web/boardviewer/` et expose son API comme spécifié en **§2.7**. wrench-board **consomme** cette API, ne spécifie **pas** son implémentation.

### 2.7 Composants externes et contrats d'interface

#### 2.7.1 Séparation des responsabilités

Le projet est développé par **deux agents Claude Code en parallèle**. Le **domaine Boardview** — parsing de fichiers PCB, modèle de données board, validation anti-hallucination des refdes, tools de contrôle UI, protocole WebSocket, renderer canvas, event bus interne — est **spécifié par un document dédié produit par l'Agent Boardviewer** (« **Boardviewer spec** »). Ce document est la **source de vérité** pour tout ce qui relève du Boardview ; wrench-board s'y conforme, **ne le redéfinit pas**.

Référence canonique figée à J+2 dans `docs/integration/boardviewer-contract.md` (co-édité par les 2 agents, versioning sémantique).

| Composant | Source de vérité | Emplacement code |
|---|---|---|
| Parser multi-format (`.brd`, `.brd2`, `.bdv`, `.fz`, extensions futures) | Boardviewer spec §2–§4 | `api/board/parser/` |
| Data model `Board`, `Part`, `Pin`, `Net`, `Nail`, `Layer` | Boardviewer spec §5 | `api/board/model.py` |
| Validator anti-hallucination refdes (`is_valid_refdes`, `suggest_similar`, ...) | Boardviewer spec §7 | `api/board/validator.py` |
| **12 tools (Tier 1-2-3)** exposés à l'agent | Boardviewer spec §9 | `api/tools/boardview.py` |
| Protocole WebSocket `boardview.*` | Boardviewer spec §10 | consommé par `api/ws/` |
| Renderer Canvas 2D | Boardviewer spec §11 | `web/boardviewer/` |
| Event bus interne `board:loaded` | Boardviewer spec §14 | frontend + backend |

**Ce que wrench-board spécifie** (dans *ce* document) :

- Comment le **diagnostic agent** Claude Opus 4.7 **utilise** les 12 tools boardview (section 7 — prompting système, stratégie de décision)
- Comment l'event `board:loaded` **déclenche** le pipeline de génération de knowledge pack (section 5)
- Comment les refdes du **knowledge pack** (`registry.json`) **s'alignent** avec les refdes validés par `api/board/validator.py`
- Le **contrat de responsabilité partagée** pour la règle dure #5 (§2.7.3 ci-dessous)

**Ce que wrench-board ne spécifie PAS** (responsabilité Boardviewer spec) :

- Format interne des fichiers `.brd`, `.brd2`, `.bdv`, `.fz`, et tous formats ajoutés ultérieurement
- Algorithme de rendering canvas 2D
- Data model des entités board
- Shape des 14 events `boardview.*` (list en §2.7.4)
- Signatures complètes des 12 tools de contrôle UI
- Événements souris bas niveau (drag, wheel, tap)
- Parsing library, dépendances JS du boardviewer

#### 2.7.2 Les 12 tools boardview — liste fermée, signatures dans Boardviewer spec §9

Exposés à l'agent Claude Opus 4.7 **tels que définis** par la Boardviewer spec §9. Leur liste est fermée ; wrench-board **n'en invente ni n'en renomme aucun**.

Exemples cités au fil de ce spec (la liste exhaustive des 12 est dans Boardviewer spec §9, reprise verbatim dans `docs/integration/boardviewer-contract.md`) :
- `load_board(...)`
- `focus_component(...)` — centre la vue sur un composant (remplace toute velléité de « `pan_to_component` »)
- `highlight_component(...)`

**Règle** : si un besoin émerge côté wrench-board qui n'est pas couvert par les 12 tools existants, c'est une **demande d'extension à coordonner via Boardviewer spec**, pas une invention locale. Aucun tool boardview fantôme ne sera ajouté dans `api/tools/boardview.py` sans mise à jour préalable de la Boardviewer spec.

#### 2.7.3 Règle dure #5 — double enforcement

La validation anti-hallucination est appliquée **aux deux niveaux** du contrat, indépendants et complémentaires :

| Niveau | Où | Quand | Réponse si invalide |
|---|---|---|---|
| **L1 — Boardviewer validator** | `api/board/validator.py::is_valid_refdes` | Avant émission du message WS `boardview.*` par chaque handler de `api/tools/boardview.py` | `{ok: false, suggestions: [...]}` → agent reçoit immédiatement |
| **L2 — Memory Bank `mb_*`** | `api/memory_bank/tools.py::mb_get_component` | Appelé par l'agent diagnostic quand il veut confirmer un refdes avant de raisonner / répondre | `{error: "not_found", closest_matches: [...]}` |

- L1 protège les **actions UI** (jamais de highlight sur un refdes fantôme).
- L2 protège les **réponses textuelles** (jamais d'affirmation sur un refdes inexistant dans le knowledge pack).

L'agent Claude Opus 4.7 reçoit donc des signaux d'erreur structurés **des deux côtés**, rapidement, et peut corriger sa trajectoire sans jamais mentir à l'utilisateur.

#### 2.7.4 Protocole WebSocket `boardview.*` — 14 messages

Les messages WS émis par `api/tools/boardview.py` et reçus par `web/boardviewer/` suivent **exactement** le protocole défini dans **Boardviewer spec §10** :

```
boardview.board_loaded       boardview.highlight
boardview.focus              boardview.flip
boardview.annotate           boardview.reset_view
boardview.dim_unrelated      boardview.layer_visibility
boardview.filter             boardview.draw_arrow
boardview.measure            boardview.show_pin
boardview.highlight_net      boardview.upload_error
```

wrench-board ne définit **aucun** message WS supplémentaire pour le domaine Boardview. Le domaine Schematic (spec séparée à venir) aura ses propres messages `schematic.*`.

#### 2.7.5 Contrats transverses entre les deux specs

| Contrat | Côté wrench-board | Côté Boardviewer |
|---|---|---|
| **Alignement refdes** | `registry.json` (§4.2) est source des `canonical_name` ; sanity script au boot : `set(registry.components.where(naming_level='exact_ref'))` doit être inclus dans les refdes parsés par le viewer | `validator.is_valid_refdes`, `suggest_similar` (spec §7) |
| **Event `board:loaded`** | consommé par le pipeline de génération (§5.1, trois cas is_known) | émis après `load_board` réussi (spec §14) |
| **device_id match** | `devices.id` Postgres (§3.2.1) | `BoardMetadata.device_id` dans le payload de `boardview.board_loaded` (spec §10) |

#### 2.7.6 Coordination et intégration — checkpoint J+2

- **Document de contrat** : `docs/integration/boardviewer-contract.md` figé **à J+2**, versionné sémantique, inclut : version du protocole WS, liste des 12 tools avec signatures verbatim, références croisées section par section des deux specs.
- **Test d'intégration cross-spec J+2** :
  - **Golden path** — agent émet `custom_tool_use highlight_component("U7")` → validator L1 OK → event WS `boardview.highlight` émis → frontend rend → agent reçoit confirmation
  - **Failure path** — agent émet `custom_tool_use highlight_component("U999")` → validator L1 renvoie `{ok: false, suggestions: ["U9", "U99"]}` → agent reçoit l'erreur structurée et **ne ment pas** dans sa réponse textuelle
- **Si divergence détectée** lors du test : **Boardviewer spec = source de vérité**, wrench-board s'aligne.

#### 2.7.7 Mocks pour développement parallèle

- **Mock backend** : `api/tools/boardview.py` dispose d'un mode `mock=True` qui log les dispatch au lieu d'émettre les events WS. Permet de tester le flow `agent → validation → dispatch` sans frontend.
- **Mock frontend** : `web/boardviewer/boardviewer.mock.js` (fourni par Agent Boardviewer) rend un placeholder simple à la réception des events `boardview.*`. Active via `window.USE_BOARDVIEWER_MOCK = true`.
- Tests unitaires wrench-board : n'importent ni le vrai boardviewer ni le mock, stubent uniquement le pipeline de dispatch côté Python.

---

## 3. Données (schema Postgres)

### 3.1 Conventions générales

| Convention | Choix | Justification |
|---|---|---|
| Identifiants | `UUID` (`gen_random_uuid()`) sauf events/cost/web_search_cache | Crypto-stable, composables, pas de collision cross-DB. `BIGSERIAL` pour les tables à très haut volume d'insert (events, cost_tracking) où l'ordre d'insertion est utile et l'id n'est jamais exposé. |
| Timestamps | `TIMESTAMPTZ` partout | Une seule vérité UTC, rendu local côté UI. Jamais de `TIMESTAMP WITHOUT TIME ZONE`. |
| Booléens | `BOOLEAN` strict, pas d'entier 0/1 | Lisibilité des requêtes. |
| Enums | `TEXT` + `CHECK` inline | Plus souple qu'un `ENUM` Postgres (pas de migration pour ajouter une valeur). |
| JSONB vs colonnes | Colonnes pour les champs requêtés souvent ou indexés ; `JSONB` pour les shapes variables ou l'inspection ponctuelle | Cf. §3.4 pour la grille de décision par table. |
| Migrations | `schema.sql` unique versionné en repo, appliqué au boot via `CREATE TABLE IF NOT EXISTS` ; évolutions mid-semaine en manuel (ALTER ou drop-and-recreate en dev) | Alembic = overkill pour une semaine. La DB est redémarrable à volonté en local. |
| Extensions Postgres | Aucune requise (`gen_random_uuid()` natif depuis PG13, on est sur PG16) | Zéro dépendance `pgcrypto` / `uuid-ossp`. |

### 3.2 Tables

Le schéma compte **10 tables** regroupées en 4 domaines :

- **Catalogue** : `devices`, `knowledge_packs`
- **Anthropic plumbing** : `managed_agents`, `managed_memory_stores`
- **Activité** : `sessions`, `subagent_calls`, `events`, `cases`
- **Transverse** : `cost_tracking`, `web_search_cache`, `profile`

#### 3.2.1 `devices` — catalogue des appareils supportés

```sql
CREATE TABLE devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor          TEXT NOT NULL,                       -- 'raspberry-pi', 'framework', 'apple'
    model           TEXT NOT NULL,                       -- 'pi-4-model-b', 'laptop-13-mainboard', 'iphone-x'
    display_name    TEXT NOT NULL,                       -- 'Raspberry Pi 4 Model B'
    primary_source  TEXT NOT NULL CHECK (primary_source IN ('schematic', 'deep_research')),
    confidence_cap  NUMERIC(3,2) NOT NULL,               -- 0.85 pour schematic, 0.65 pour deep_research
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vendor, model)
);
```

Seed initial (3 lignes) : Pi 4 Model B (schematic, 0.85), Framework Laptop 13 (schematic, 0.85), iPhone X (deep_research, 0.65).

#### 3.2.2 `knowledge_packs` — métadonnées des packs sur disque

```sql
CREATE TABLE knowledge_packs (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id                   UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    disk_path                   TEXT NOT NULL,           -- 'memory/raspberry-pi/pi-4-model-b'
    origin                      TEXT NOT NULL CHECK (origin IN ('schematic', 'deep_research')),
    quality                     JSONB NOT NULL DEFAULT '{}',  -- voir §3.4
    last_generation_session_id  UUID,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (device_id)
);
```

Shape JSONB de `quality` :
```json
{
  "intrinsic_effectiveness": 0.82,
  "consistency_score": 0.91,
  "evidence_readiness": 0.74,
  "overall_quality": 0.82,
  "audit_verdict": "APPROVED",
  "rewrites_needed": [],
  "computed_at": "2026-04-23T14:02:11Z"
}
```

Les fichiers du pack (`registry.json`, `architecture.md`, etc.) vivent sur le disque à `disk_path`. La DB ne stocke **aucun contenu de pack**, uniquement le pointeur + la métadonnée qualité. Conséquence : un `git diff` sur `memory/` montre directement les changements de knowledge, aucun dump SQL requis.

#### 3.2.3 `managed_agents` — un par rôle, créé une fois sur Anthropic

```sql
CREATE TABLE managed_agents (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role                 TEXT NOT NULL UNIQUE,           -- 'diagnostic', 'coordinator',
                                                         -- 'registry_builder',
                                                         -- 'writer_architecture',
                                                         -- 'writer_rules', 'writer_dictionary',
                                                         -- 'auditor', 'reviser',
                                                         -- 'facts_extractor',
                                                         -- 'rule_synthesizer'
    anthropic_agent_id   TEXT NOT NULL,
    version              INT NOT NULL,
    model                TEXT NOT NULL,                  -- 'claude-opus-4-7', 'claude-haiku-4-5', ...
    system_prompt_hash   TEXT NOT NULL,                  -- SHA256 du system prompt, détecte les drifts
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Bootstrap : script `scripts/bootstrap_agents.py` crée les 10 agents la première fois (idempotent — check par rôle avant création).

#### 3.2.4 `managed_memory_stores` — un par device

```sql
CREATE TABLE managed_memory_stores (
    id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id                    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    anthropic_memory_store_id    TEXT NOT NULL UNIQUE,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (device_id)
);
```

Créé à l'ajout du device. Attaché à chaque session diagnostic de ce device.

#### 3.2.5 `sessions` — diagnostic & generation_job unifiés

```sql
CREATE TABLE sessions (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type                   TEXT NOT NULL CHECK (type IN ('diagnostic', 'generation_job')),
    device_id              UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    anthropic_session_id   TEXT,                         -- NULL pour generation_job (voir subagent_calls)
    status                 TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    metadata               JSONB NOT NULL DEFAULT '{}',  -- phase courante, verdict audit, ...
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at               TIMESTAMPTZ
);
CREATE INDEX idx_sessions_device_started ON sessions (device_id, started_at DESC);
CREATE INDEX idx_sessions_type_status ON sessions (type, status);
```

Pour une session `diagnostic` : `anthropic_session_id` pointe la session Anthropic unique. Pour un `generation_job` : le champ est NULL et les sub-sessions Anthropic sont dans `subagent_calls`.

Shape JSONB de `metadata` pour `generation_job` :
```json
{
  "current_phase": "2_writers_parallel",
  "phases": {
    "1_registry":        {"status": "completed", "duration_s": 18.4},
    "2_writers_parallel": {"status": "running"},
    "3_audit":           {"status": "pending"},
    "4_revise":          {"status": "pending", "rounds_used": 0, "max_rounds": 1},
    "5_facts":           {"status": "pending"}
  }
}
```

#### 3.2.6 `subagent_calls` — une ligne par appel de sub-agent

```sql
CREATE TABLE subagent_calls (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id             UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role                   TEXT NOT NULL,                -- matches managed_agents.role
    phase                  TEXT NOT NULL,                -- '1_registry', '2_writers_parallel', etc.
    anthropic_session_id   TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    input_summary          JSONB,                        -- ex: {"files_to_rewrite": ["rules.md"]}
    output_summary         JSONB,                        -- ex: {"audit": "APPROVED", "consistency_score": 0.91}
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at               TIMESTAMPTZ
);
CREATE INDEX idx_subagent_calls_session ON subagent_calls (session_id);
```

#### 3.2.7 `events` — mirror timeline pour l'UI

```sql
CREATE TABLE events (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    subagent_call_id    UUID REFERENCES subagent_calls(id) ON DELETE CASCADE,
    seq                 INT NOT NULL,                    -- ordre stable à l'intérieur d'une session
    event_type          TEXT NOT NULL,
    payload             JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_session_seq ON events (session_id, seq);
```

Types d'events loggés : `user.message`, `agent.message`, `agent.custom_tool_use`, `tool_result`, `agent.thinking`, `phase.started`, `phase.completed`, `ui.command_pushed` (highlight/pan/etc.), `error`.

Payload variable — shape dépend du type. Justifie `JSONB` sans ambiguïté.

#### 3.2.8 `cases` — fiches diagnostic résolues ou abandonnées

```sql
CREATE TABLE cases (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    session_id    UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    symptoms      JSONB NOT NULL,     -- [{"symptom": "3V3 rail dead", "evidence": "..."}]
    resolution    JSONB,              -- {"cause_refdes": "C29", "action": "replace", "wasted_time_min": 12}
    status        TEXT NOT NULL CHECK (status IN ('resolved', 'dead_end', 'abandoned')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ
);
CREATE INDEX idx_cases_device_status ON cases (device_id, status);
```

Les cases sont **aussi** écrits en markdown dans `memory/{vendor}/{model}/cases/case-NNN-*.md` pour versioning git et consultation humaine. La DB sert surtout au pattern matching (requêtes d'agrégation pour le cycle learned rules).

#### 3.2.9 `cost_tracking` — une ligne par appel API Anthropic

```sql
CREATE TABLE cost_tracking (
    id                              BIGSERIAL PRIMARY KEY,
    session_id                      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    subagent_call_id                UUID REFERENCES subagent_calls(id) ON DELETE CASCADE,
    agent_role                      TEXT NOT NULL,
    model                           TEXT NOT NULL,
    input_tokens                    INT NOT NULL DEFAULT 0,
    cache_creation_input_tokens     INT NOT NULL DEFAULT 0,
    cache_creation_ttl              TEXT NOT NULL DEFAULT '5m' CHECK (cache_creation_ttl IN ('5m', '1h')),
    cache_read_input_tokens         INT NOT NULL DEFAULT 0,
    output_tokens                   INT NOT NULL DEFAULT 0,
    web_search_calls                INT NOT NULL DEFAULT 0,
    computed_cost_usd               NUMERIC(10,6) NOT NULL,
    pricing_snapshot                JSONB NOT NULL,      -- prix du modèle au moment du calcul
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cost_session ON cost_tracking (session_id);
CREATE INDEX idx_cost_role_model ON cost_tracking (agent_role, model);
```

`pricing_snapshot` est un snapshot des tarifs au moment du calcul (par si Anthropic change ses prix pendant le projet — les coûts passés restent auditables). Shape :
```json
{
  "input_per_mtok":  15.00,
  "output_per_mtok": 75.00,
  "cache_write_5m":  18.75,
  "cache_write_1h":  30.00,
  "cache_read":       1.50,
  "web_search_per_call": 0.01
}
```

#### 3.2.10 `web_search_cache` — résultats `web_search_20250305` cachés 7 jours

```sql
CREATE TABLE web_search_cache (
    query_hash    CHAR(64) PRIMARY KEY,                  -- SHA256 hex de query_text
    query_text    TEXT NOT NULL,
    results       JSONB NOT NULL,                        -- résultats bruts Anthropic (sources + extracts)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_web_search_expires ON web_search_cache (expires_at);
```

Purge quotidienne via `DELETE FROM web_search_cache WHERE expires_at < now()` (job simple au boot FastAPI).

#### 3.2.11 `profile` — fiche utilisateur (singleton en v1)

```sql
CREATE TABLE profile (
    id                UUID PRIMARY KEY,                  -- enforced singleton (voir CHECK ci-dessous)
    display_name      TEXT NOT NULL DEFAULT 'Technician',
    declared_level    TEXT,                              -- override manuel : 'beginner', 'intermediate', 'advanced', 'expert'
    declared_skills   JSONB NOT NULL DEFAULT '[]',       -- ex: [{"name": "BGA rework", "years": 5}]
    derived_cache     JSONB NOT NULL DEFAULT '{}',       -- snapshot des stats calculées, rafraîchi à la demande
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id = '00000000-0000-0000-0000-000000000001')
);
```

Le `CHECK (id = ...)` force un singleton — un seul profil en v1 single-user. Une future v2 multi-user remplacera ce CHECK par un vrai `user_id`.

Shape JSONB `derived_cache` :
```json
{
  "estimated_level": "intermediate",
  "cases_resolved": 12,
  "cases_dead_end": 3,
  "avg_resolution_time_min": 18,
  "specializations": [
    {"area": "Raspberry Pi power issues", "strength": 0.78},
    {"area": "BGA rework", "strength": 0.42}
  ],
  "last_recomputed": "2026-04-24T10:12:33Z"
}
```

### 3.3 Diagramme de relations (texte)

```
                    ┌─────────────┐
                    │  profile    │  (singleton)
                    └─────────────┘

  ┌──────────┐      ┌─────────────────────┐
  │ devices  │◄─────│ managed_memory_     │
  │          │      │   stores            │
  │          │      └─────────────────────┘
  │          │
  │          │      ┌─────────────────────┐
  │          │◄─────│ knowledge_packs     │
  │          │      │ (quality JSONB)     │
  │          │      └─────────────────────┘
  │          │
  │          │      ┌─────────────┐    ┌──────────────────┐
  │          │◄─────│  sessions   │◄───│ subagent_calls   │
  │          │      │ (metadata   │    │                  │
  │          │      │   JSONB)    │    └────────┬─────────┘
  │          │      └──┬──┬───────┘             │
  │          │         │  │                     │
  │          │◄────────┤  │                     │
  │          │    ┌────▼──▼──┐           ┌──────▼───────┐
  │          │    │  cases   │           │    events    │
  │          │    │ (symptoms│           │  (payload    │
  └──────────┘    │  JSONB)  │           │    JSONB)    │
                  └──────────┘           └──────┬───────┘
                                                │
                                         ┌──────▼─────────┐
                                         │ cost_tracking  │
                                         │ (pricing_      │
                                         │    snapshot    │
                                         │    JSONB)      │
                                         └────────────────┘

  ┌───────────────────────┐   ┌─────────────────────┐
  │ managed_agents         │   │ web_search_cache    │
  │ (one per role)         │   │ (standalone,        │
  └───────────────────────┘   │  TTL 7j)            │
                              └─────────────────────┘
```

**Clés étrangères cascades** : tout pend de `devices` — supprimer un device supprime ses sessions, knowledge_pack, memory_store, cases. Les events et cost_tracking cascadent via sessions. `managed_agents` et `web_search_cache` sont standalone.

### 3.4 Grille JSONB vs colonnes

| Table | Colonnes normales | JSONB | Justification |
|---|---|---|---|
| `devices` | tout en colonnes | — | Champs stables, requêtés par vendor/model |
| `knowledge_packs` | `device_id`, `disk_path`, `origin` | `quality` | Les métriques de qualité peuvent s'enrichir (new metric = pas de migration) |
| `sessions` | `type`, `status`, timestamps | `metadata` | Phase state varie par type de session, shape évolue |
| `events` | `session_id`, `event_type`, `seq` | `payload` | Shape dépend strictement de l'event_type (11+ types) |
| `cases` | `device_id`, `status`, timestamps | `symptoms`, `resolution` | Structures nested, pas requêtées en SQL agrégé — c'est le pipeline Python qui lit |
| `cost_tracking` | token counts, `model`, `cost_usd` | `pricing_snapshot` | Snapshot complet, non-indexé, pur audit |
| `profile` | `display_name`, `declared_level` | `declared_skills`, `derived_cache` | Listes et structures calculées — JSONB flex |
| `web_search_cache` | `query_hash`, timestamps | `results` | Blob opaque Anthropic |

Règle générale : **colonne** si champ requêté, filtré, indexé, ou soumis à contrainte. **JSONB** si shape variable ou tableau imbriqué.

### 3.5 Migrations et bootstrap

- **`api/db/schema.sql`** — un seul fichier, versionné, contient tous les `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` + seed minimal (la ligne `profile` singleton uniquement, pas les devices qui sont seedés par un script dédié).
- **Bootstrap au démarrage FastAPI** — appel unique à `schema.sql` au `lifespan` startup, puis vérification d'intégrité (présence des 10 tables). Log explicite si schema appliqué pour la 1ère fois vs already applied.
- **`scripts/bootstrap_devices.py`** — seed les 3 devices (Pi 4, Framework 13, iPhone X), idempotent (INSERT ... ON CONFLICT DO NOTHING par `(vendor, model)`).
- **`scripts/bootstrap_agents.py`** — crée les 10 managed_agents Anthropic (via API beta), stocke `anthropic_agent_id` et `version` en DB. Idempotent par `role` (si déjà présent en DB, skip).
- **Évolution mid-semaine** — si un champ doit changer (ajout colonne, rename), c'est fait en manuel (ALTER TABLE dans `psql`) puis ajouté au `schema.sql` pour les futurs fresh starts. Pas d'Alembic. Si la DB est en mauvais état, `docker compose down -v` + `up -d` et on rejoue seed + bootstrap.

### 3.6 Volumétrie attendue (hackathon, 3 devices)

| Table | Ordre de grandeur fin de hackathon |
|---|---|
| `devices` | 3 |
| `managed_agents` | 10 |
| `managed_memory_stores` | 3 |
| `knowledge_packs` | 3 |
| `sessions` | ~50 (tests + démo + quelques runs réels) |
| `subagent_calls` | ~200 |
| `events` | ~10 000 |
| `cases` | ~20 |
| `cost_tracking` | ~500 |
| `web_search_cache` | ~100 |
| `profile` | 1 |

Aucune optimisation de volumétrie nécessaire à cette échelle — les index simples déclarés suffisent amplement.

---

## 4. Knowledge packs — format disque

### 4.1 Layout

Chaque device a un dossier sous `memory/{vendor}/{model}/`. Les fichiers dans ce dossier sont la **source canonique** de la connaissance pour ce device — lus directement par les tools `mb_*`, versionnés par git, éditables par un humain en cas de besoin.

```
memory/
├── raspberry-pi/
│   └── pi-4-model-b/
│       ├── registry.json              # glossaire canonique (refs, signals, aliases)
│       ├── architecture.md            # exposé human-readable du power tree et blocs
│       ├── architecture.json          # même contenu, structuré machine
│       ├── dictionary.md              # exposé human-readable refdes → rôle, nets, TP
│       ├── dictionary.json            # structuré machine
│       ├── rules.md                   # rules diagnostiques, narratif
│       ├── rules.json                 # rules structurées (core + learned)
│       ├── evidence.json              # patterns agrégés depuis cases résolus
│       ├── sources.json               # traçabilité (datasheet p., URL, case_id)
│       ├── facts.native.json          # facts extraits par Phase 5 (benchmark)
│       ├── sources/
│       │   └── schematic.pdf          # PDF original (si primary_source=schematic)
│       └── cases/
│           ├── case-001-c29-failure.md
│           ├── case-001-c29-failure.json
│           ├── case-002-...
│           └── ...
├── framework/
│   └── laptop-13-mainboard/
│       └── ...
└── apple/
    └── iphone-x/
        ├── registry.json              (origin: "deep_research", confidence_cap: 0.65)
        ├── sources/                   (VIDE — aucun PDF propriétaire)
        └── ...
```

**Règle dure** : le dossier `sources/` d'iPhone X reste vide. Aucun schematic Apple n'entre dans le repo. Le `sources.json` pour iPhone X liste uniquement des **URLs publiques** citées par le pipeline deep research.

### 4.2 `registry.json` — le glossaire canonique

Fondation de tout le pack. Tous les autres fichiers référencent uniquement des `canonical_name` définis ici — contrainte système explicite envoyée aux 3 writers en Phase 2.

```json
{
  "schema_version": "1.0",
  "device": {
    "vendor": "raspberry-pi",
    "model": "pi-4-model-b",
    "display_name": "Raspberry Pi 4 Model B"
  },
  "generated_at": "2026-04-23T14:02:11Z",
  "origin": "schematic",
  "confidence_cap": 0.85,
  "components": [
    {
      "canonical_name": "U7",
      "naming_level": "exact_ref",
      "aliases": ["PMIC", "power IC", "main PMIC"],
      "kind": "pmic",
      "description": "Main power management IC, manages 5V→3V3 and 5V→1V8 rails."
    },
    {
      "canonical_name": "C29",
      "naming_level": "exact_ref",
      "aliases": ["C29 decoupling cap", "3V3 rail cap near U7"],
      "kind": "capacitor",
      "description": "Decoupling capacitor on 3V3 rail adjacent to U7."
    }
  ],
  "signals": [
    {
      "canonical_name": "3V3_RAIL",
      "naming_level": "exact_name",
      "aliases": ["3.3V", "VDD_3V3", "main 3V3"],
      "kind": "power_rail",
      "nominal_voltage": 3.3
    }
  ]
}
```

**`naming_level`** est un enum strict :
- `exact_ref` — ID littéral imprimé sur le PCB ou dans le schematic (ex. `U7`, `C29`)
- `exact_name` — nom de signal officiel dans le schematic (ex. `3V3_RAIL`, `USB_DP1`)
- `logical_alias` — nom fonctionnel sans ID canonique (ex. `main power rail`) ; réservé au deep research quand aucun ID officiel n'est trouvé

Un `canonical_name` est **unique** dans son type (components / signals). Les `aliases` permettent la résolution tolérante côté `mb_get_component`.

### 4.3 `architecture.md` et `architecture.json`

Description du hardware : blocs fonctionnels, power tree, interactions entre rails.

**`architecture.md`** — narratif human-readable, ~500–2000 mots. Structure imposée :
```markdown
# Architecture — Raspberry Pi 4 Model B

## 1. Power tree
- 5V in via USB-C → ...
- 5V → U7 (PMIC) → 3V3_RAIL, 1V8_RAIL
- 3V3_RAIL consumers: BCM2711 I/O, USB controller, ...

## 2. Functional blocks
### 2.1 SoC
- BCM2711, quad A72 @ 1.5 GHz
- Power: VDD_CORE from U7, decoupled by C29, C30, C31

## 3. Rails et mesures attendues
| Rail | Nominal | Tolérance | Test point |
|---|---|---|---|
| 3V3_RAIL | 3.3V | ±5% | TP18 |
```

**`architecture.json`** — mêmes informations, structurées :
```json
{
  "schema_version": "1.0",
  "power_tree": {
    "inputs": [{"source": "USB-C", "voltage": 5.0}],
    "rails": [
      {
        "canonical_name": "3V3_RAIL",
        "from": "U7",
        "nominal_voltage": 3.3,
        "tolerance_pct": 5,
        "test_point": "TP18",
        "consumers": ["BCM2711", "USB_controller"]
      }
    ]
  },
  "blocks": [
    {"name": "SoC", "components": ["BCM2711"], "nets": ["VDD_CORE"]}
  ]
}
```

### 4.4 `dictionary.md` et `dictionary.json`

Catalogue refdes → rôle, nets connectés, test points proches. Le complément de `registry.json` (le registre nomme ; le dictionnaire détaille).

**`dictionary.json`** :
```json
{
  "schema_version": "1.0",
  "entries": [
    {
      "canonical_name": "U7",
      "role": "Main PMIC, converts 5V to 3V3 and 1V8 rails.",
      "nets_connected": ["5V_IN", "3V3_RAIL", "1V8_RAIL", "GND"],
      "pins": [
        {"pin": 1, "signal": "VIN_5V"},
        {"pin": 12, "signal": "VOUT_3V3"}
      ],
      "test_points_nearby": ["TP17", "TP18"],
      "package": "QFN-24",
      "location_hint": "near USB-C connector, bottom side"
    }
  ]
}
```

### 4.5 `rules.md` et `rules.json`

Règles diagnostiques. Deux origines coexistent : **`core`** (issues de schematic/datasheet/deep research lors de la génération) et **`learned`** (synthétisées par le sub-agent `rule_synthesizer` après détection de pattern via cycle apprenant — cf. section 8).

**`rules.json`** :
```json
{
  "schema_version": "1.0",
  "rules": [
    {
      "id": "rule-pi4-001",
      "origin": "core",
      "symptoms": ["3V3 rail dead", "device doesn't boot"],
      "likely_causes": [
        {"refdes": "C29", "probability": 0.35, "mechanism": "short-to-ground"},
        {"refdes": "U7", "probability": 0.25, "mechanism": "dead PMIC"}
      ],
      "diagnostic_steps": [
        {"action": "measure 3V3_RAIL at TP18", "expected": 3.3, "unit": "V"},
        {"action": "if 0V, measure resistance TP18 to GND", "pass_threshold": "> 100 ohm"}
      ],
      "confidence": 0.82,
      "needs_validation": false,
      "sources": ["datasheet:BCM2711:p23", "schematic:sheet3"]
    },
    {
      "id": "rule-pi4-learned-001",
      "origin": "learned",
      "symptoms": ["3V3 rail dead after liquid damage"],
      "likely_causes": [{"refdes": "C29", "probability": 0.78, "mechanism": "short-to-ground"}],
      "confidence": 0.55,
      "needs_validation": true,
      "sources": ["case:001", "case:007", "case:012"]
    }
  ]
}
```

**Règle d'évolution de confidence** (cf. section 8 pour détail) :
- `learned` rule créée à `0.55` quand 3+ cases confirment ≥60 %
- `+0.05` à chaque cas résolu via cette rule
- `-0.10` à chaque dead end
- `< 0.30` → rule `disabled: true`, exclue des réponses `mb_get_rules_for_symptoms`

### 4.6 `evidence.json` — patterns agrégés

Statistiques consolidées depuis les cases résolus. Ce n'est pas une rule, c'est un **signal** pour le rule_synthesizer.

```json
{
  "schema_version": "1.0",
  "patterns": [
    {
      "pattern_id": "p-pi4-001",
      "symptoms": ["3V3 rail dead"],
      "observed_causes": [
        {"refdes": "C29", "count": 5, "resolution_rate": 0.80},
        {"refdes": "U7", "count": 2, "resolution_rate": 0.50}
      ],
      "total_cases": 7,
      "promoted_to_rule_id": "rule-pi4-learned-001",
      "last_update": "2026-04-25T09:11:23Z"
    }
  ]
}
```

Mis à jour par code pur (pas d'agent) après chaque case résolu — cf. section 8.

### 4.7 `sources.json` — traçabilité

Chaque fait dans le pack peut citer une source. `sources.json` est l'index des sources utilisables par l'agent.

```json
{
  "schema_version": "1.0",
  "sources": [
    {
      "id": "schematic:sheet3",
      "kind": "schematic_pdf",
      "disk_path": "sources/schematic.pdf",
      "page": 3,
      "citation": "Sheet 3 — Power distribution"
    },
    {
      "id": "datasheet:BCM2711:p23",
      "kind": "datasheet_url",
      "url": "https://datasheets.raspberrypi.com/bcm2711/bcm2711-peripherals.pdf",
      "page": 23,
      "citation": "BCM2711 datasheet — Power sequencing"
    },
    {
      "id": "web:ifixit:iphonex-teardown",
      "kind": "web_public",
      "url": "https://www.ifixit.com/Teardown/iPhone+X+Teardown/98975",
      "citation": "iFixit iPhone X teardown",
      "confidence_weight": 0.7
    }
  ]
}
```

Pour les packs `origin: deep_research`, **chaque** source listée dans `sources.json` est une URL publique ; le `confidence_weight` pondère la fiabilité perçue par source (iFixit = 0.7, forum non-modéré = 0.4, Wikipedia = 0.8, datasheet composant = 0.95).

### 4.8 `facts.native.json` — sortie de Phase 5 (optionnelle)

Produit par le `facts_extractor` (Haiku 4.5) — un flat-list de facts atomiques pour benchmark et débogage. Pas consommé par l'agent diagnostic directement.

```json
{
  "schema_version": "1.0",
  "facts": [
    {"subject": "U7", "predicate": "converts", "object": "5V→3V3", "source_ids": ["schematic:sheet3"]},
    {"subject": "3V3_RAIL", "predicate": "test_point", "object": "TP18", "source_ids": ["architecture.md"]},
    {"subject": "C29", "predicate": "decouples", "object": "3V3_RAIL", "source_ids": ["dictionary.md"]}
  ]
}
```

Utilité : métrique de couverture du pack (facts/composants ratio), comparaison cross-devices, validation que l'info importante a bien été extraite. Non-bloquant si absent — Phase 5 est marquée optionnelle dans le plan de livraison (section 12).

### 4.9 `cases/` — un fichier `.md` + un `.json` par case

**`case-001-c29-failure.md`** (narratif, écrit par l'utilisateur ou l'agent lors de la clôture d'une session diagnostic résolue) :
```markdown
# Case 001 — 3V3 rail dead, C29 short to ground

**Device** : Raspberry Pi 4 Model B
**Date** : 2026-04-23
**Resolution time** : 14 min
**Status** : resolved

## Symptômes
- Device ne boot pas
- LED rouge stable
- 3V3_RAIL à 0V mesuré à TP18

## Diagnostic
1. Mesure TP18 → 0V
2. Résistance TP18 → GND : 4.2 Ω (short)
3. Thermique sur C29 à la mise sous tension 5V limitée
4. Remplacement C29 → 3V3_RAIL = 3.31V, boot OK

## Cause finale
C29 en court-circuit (claquage après surtension USB-C non conforme).

## Notes
- Confirmer test de surtension sur les Pi 4 venant de clients similaires.
```

**`case-001-c29-failure.json`** (mêmes infos, structuré) :
```json
{
  "schema_version": "1.0",
  "id": "case-001",
  "device_id": "raspberry-pi/pi-4-model-b",
  "session_id": "uuid-...",
  "title": "3V3 rail dead, C29 short to ground",
  "symptoms": ["3V3 rail dead", "device doesn't boot", "LED rouge stable"],
  "resolution": {
    "cause_refdes": "C29",
    "cause_mechanism": "short-to-ground",
    "action": "replace",
    "outcome": "device boots",
    "wasted_time_min": 2
  },
  "duration_min": 14,
  "status": "resolved",
  "created_at": "2026-04-23T15:22:00Z"
}
```

La **double écriture** (`.md` + `.json`) est synchronisée — le `.md` est la source narrative que l'utilisateur peut éditer à la main, le `.json` est le miroir structuré régénéré à partir du markdown (via un parseur simple) à chaque edit, ou écrit directement par l'agent lors d'une clôture automatique. Le DB `cases` table miroir également ces données pour requêter en SQL agrégé (cf. section 3.2.8).

### 4.10 Modèle de confidence unifié

Un unique score `confidence ∈ [0, 1]` circule dans tout le pack. Sa valeur est bornée et calculée selon ces règles :

| Niveau | Règle |
|---|---|
| **Plafond pack** | `confidence_cap` déclaré dans `registry.json` selon `origin` : 0.85 (schematic), 0.65 (deep_research) |
| **Fait tiré du schematic PDF** | confidence ≤ `confidence_cap`, typiquement 0.80–0.85 |
| **Fait tiré d'une datasheet publique** | confidence ≤ 0.85 × `source.confidence_weight` |
| **Fait deep research** | confidence ≤ `confidence_cap` × `source.confidence_weight` (ex: iFixit 0.65 × 0.7 = 0.455) |
| **Rule `core`** | initiale = moyenne pondérée des facts la soutenant |
| **Rule `learned`** | initiale = 0.55, évolue ±0.05 / ±0.10 selon feedback loop |
| **Auto-cap à 0.30** | toute rule `learned` descendue en-dessous est désactivée (`disabled: true`) |

L'UI affiche la confidence sous forme de **badge** dans Memory Bank > Knowledge, plus un `origin` textuel (`verified from schematic` / `inferred from public sources` / `learned from your cases`).

### 4.11 Contrainte de nommage canonical (anti-drift)

Les 3 writers (Phase 2) reçoivent `registry.json` en entrée via cache prompt statique, avec la consigne système :

> *Vous DEVEZ utiliser uniquement les `canonical_name` présents dans le registre fourni. Toute référence à un composant ou signal inconnu du registre est une erreur. Si vous identifiez un composant manquant, signalez-le via le tool `report_missing_component(name, evidence)` — ne l'inventez pas dans votre sortie.*

Le pipeline tracke les appels à `report_missing_component` : s'il y en a ≥5 après une Phase 2 complète, l'auditor (Phase 3) peut déclencher un retour en Phase 1 partiel pour enrichir le registre avant de relancer les writers. Bornée à 1 retour (idem que le limit revise en Phase 4).

---

## 5. Pipeline de génération de knowledge pack

### 5.1 Déclencheurs du pipeline — via l'event `board:loaded`

Le pipeline est déclenché **en réaction** à l'event interne `board:loaded` émis par le système Boardviewer (cf. Boardviewer spec §14) après un `load_board` réussi. Le payload de l'event contient au minimum `{device_id, is_known}` où `is_known` indique si un `knowledge_packs` row existe en DB pour ce `device_id`.

Trois cas possibles, gérés par un handler `api/memory_bank/bootstrap.py::on_board_loaded(payload)` :

```
board:loaded { device_id, is_known }
    │
    ├── is_known == true
    │   └─► memory_bank.load_pack(device_id) → pack ready for diagnostic agent
    │       Aucun pipeline lancé.
    │
    ├── is_known == false  AND  user_requested_generation == true
    │   └─► Pipeline démarre (Phase 1 → 2 → 3 → 4 → 5)
    │       WS /ws/generation/{job_id} ouvert pour streamer la progress
    │
    └── is_known == false  AND  user_requested_generation == false
        └─► UI Memory Bank affiche un call-to-action :
            "Generate knowledge pack for [device.display_name]"
            Clic → pipeline démarre (cas #2)
```

Le flag `user_requested_generation` est un paramètre explicite passé par le frontend quand l'utilisateur clique le CTA depuis Memory Bank, ou préalablement au `load_board` (ex. dans le flow d'import de device qui pré-coche « generate knowledge pack on load »).

**Aucun pipeline n'est déclenché automatiquement sans consentement** — un chargement de board inconnue qui n'entraînerait pas de pipeline ne coûte pas de tokens API. Cohérent avec la règle de coût maîtrisé (budget 500 $ du hackathon).

**Cas « generation déjà en cours » — anti-doublon explicite** : si l'utilisateur déclenche une nouvelle génération pour un device alors qu'un `generation_job` est déjà en statut `running` pour ce même `device_id` :

1. Le backend **rejette** le second déclenchement (HTTP 409 Conflict côté REST, ou refus côté WS)
2. Il retourne le payload `{error: "generation_in_progress", job_id: "<uuid existant>", ws_url: "/ws/generation/<uuid>"}`
3. Le frontend redirige l'utilisateur vers le job en cours au lieu d'en créer un nouveau

Un index `idx_sessions_type_status_device` sur `sessions(type, status, device_id)` permet au backend de vérifier rapidement l'existence d'un job `running` avant d'en créer un. Cette garde évite les race conditions et les doublons coûteux en tokens (2 pipelines concurrents sur le même device = ~3 $ cramés pour rien).

### 5.2 Vue d'ensemble du flow — 5 phases orchestrées par le Coordinator

```
┌─────────────────────────────────────────────────────────────┐
│ Coordinator (Opus 4.7, managed agent)                        │
│   └─ orchestre via custom tool dispatch_subagent(role, ...)  │
└──────────────────────────┬──────────────────────────────────┘
                           │
              Phase 1 ─────▼─────
           Registry Builder (Opus)
           inputs : PDF schematic │ web_search  (selon mode)
           output : registry.json (submit_registry forcé)
                           │
              Phase 2 ─────▼─────   (asyncio.gather)
     ┌───────────────┬───────────────┬───────────────┐
     │ writer_arch.  │ writer_rules  │ writer_dict.  │
     │ (Opus, cached │ (Opus, cached │ (Opus, cached │
     │  registry)    │  registry)    │  registry)    │
     │ forcés :      │ forcés :      │ forcés :      │
     │ submit_arch.  │ submit_rules  │ submit_dict.  │
     │ + report_     │ + report_     │ + report_     │
     │ missing_*     │ missing_*     │ missing_*     │
     └───────────────┴───────────────┴───────────────┘
                           │
              Phase 3 ─────▼─────
               Auditor (Opus)
               output : audit_verdict.json
                          │
                   files_to_rewrite ≠ [] ?
                   │                    │
             oui (rounds_used < 1)    non / rounds épuisés
                   │                    │
              Phase 4 ──▼──              │
              Reviser (Opus)             │
              rewrite fichiers flaggés   │
              ↳ retour Phase 3 (1 fois)  │
                   │                    │
                   └────────┬───────────┘
                            │
              Phase 5 ──────▼──── (optionnelle)
           Facts Extractor (Haiku 4.5)
           output : facts.native.json
                            │
                     Finalisation
                  - atomic swap dossier
                  - compute quality[]
                  - UPDATE knowledge_packs
                  - event phase.completed
                  - WS pipeline.complete
```

**Durées indicatives** : Phase 1 ~15–25 s, Phase 2 ~15–25 s (parallélisée), Phase 3 ~5–10 s, Phase 4 ~10–20 s par fichier rewritten, Phase 5 ~5 s. **Total typique 45–90 s**, bien compatible avec le timelapse démo (compression × 10 pour Framework en J+4).

### 5.3 Coordinator

| Champ | Valeur |
|---|---|
| `role` | `coordinator` |
| Modèle | `claude-opus-4-7` |
| Tools exposés | `dispatch_subagent(role, input)`, `report_phase_progress(phase, status, details)`, `finalize_knowledge_pack(quality_metrics)` |
| `tool_choice` par défaut | `auto` — le coordinator décide quand dispatcher |

**Fonctionnement du custom tool `dispatch_subagent`** (intercepté par le backend, pas exécuté côté Anthropic) :

```
Coordinator émet :
  agent.custom_tool_use {
    name: "dispatch_subagent",
    input: { role: "registry_builder", input: { device_id, sources } }
  }
       │
       ▼
Backend (api/orchestration/managed.py::handle_dispatch) :
  1. Log phase.started dans events
  2. Crée une nouvelle session Anthropic sur l'agent ciblé (role == "registry_builder")
  3. Stream les events de cette session, intercepte son résultat structuré (submit_registry)
  4. Retourne au Coordinator un user.custom_tool_result avec le payload structuré
  5. Log phase.completed dans events + update metadata session
       │
       ▼
Coordinator reçoit le résultat, décide la phase suivante (ou finalise)
```

**System prompt résumé** (prompt complet dans `api/agent/system_prompts/coordinator.md`) :

> Tu orchestres un pipeline de génération de knowledge pack pour un device électronique. Enchaîne les phases : 1 (Registry) → 2 (Writers en parallèle) → 3 (Audit) → 4 (Revise, max 1 round) → 5 (Facts, optionnel).
>
> Pour chaque phase, invoque `dispatch_subagent` avec le `role` approprié et les inputs requis. Entre chaque phase, évalue le résultat : si Phase 3 renvoie `files_to_rewrite` non vide ET `rounds_used < 1`, lance Phase 4 ; sinon passe à Phase 5 ou finalise.
>
> Tu n'écris **jamais** toi-même de contenu du pack — tu orchestres, tu ne rédiges pas. Si une phase échoue, logge via `report_phase_progress(status="failed")` et termine via `finalize_knowledge_pack` avec `quality.status = "PARTIAL"`.

**Coût indicatif** : ~5–10 k tokens input accumulés (décisions multi-étapes) + ~1–2 k tokens output → **~0.15–0.25 $ par run**.

### 5.4 Phase 1 — Registry Builder

| Champ | Valeur |
|---|---|
| `role` | `registry_builder` |
| Modèle | `claude-opus-4-7` |
| Tools exposés | `submit_registry(registry_json)` **forcé** (`tool_choice={type:"tool", name:"submit_registry"}`) ; en mode deep research : + `web_search_20250305` |
| Inputs | Mode schematic : PDF fourni en `content: [{type: "document", source: {type: "url", url: ".../schematic.pdf"}}]`. Mode deep research : `device_id`, `description`, bootstrap queries |
| Output | `registry.json` conforme §4.2 |

**Contrainte du tool forcé** : pas de texte libre émis. Le seul output valide est un `submit_registry` avec JSON structuré. Toute tentative de réponse textuelle → relance.

**System prompt résumé** :

> Extrait un glossaire canonique depuis les sources fournies. Chaque entrée a un `canonical_name` unique (exact_ref > exact_name > logical_alias), des `aliases` tolérés, un `kind`, et une description concise. Pour les signaux, inclus `nominal_voltage` quand pertinent.
>
> Soumets le résultat via `submit_registry` — aucun texte libre. Si une information est incertaine, plutôt omettre que spéculer.

**Coûts** :
- Mode schematic (Pi 4, Framework) : input ~30–100 k tokens (PDF complet) + output ~3–5 k → **~0.15–0.30 $**
- Mode deep research (iPhone X) : ~10–30 appels `web_search` + input ~10–30 k + output ~3–5 k → **~0.20–0.40 $**

### 5.5 Phase 2 — Writers parallèles

**Trois sub-agents lancés simultanément via `asyncio.gather`** sur 3 sessions Anthropic distinctes. Partagent la même entrée (`registry.json` + sources), écrivent 3 fichiers différents.

| Writer | `role` | Tool forcé | Output |
|---|---|---|---|
| Architecture | `writer_architecture` | `submit_architecture(md, json)` | `architecture.md` + `architecture.json` (§4.3) |
| Rules core | `writer_rules` | `submit_rules(md, json)` | `rules.md` + `rules.json` avec `origin: "core"` uniquement (§4.5) |
| Dictionary | `writer_dictionary` | `submit_dictionary(md, json)` | `dictionary.md` + `dictionary.json` (§4.4) |

Chaque writer reçoit en plus `report_missing_component(name, evidence)` pour signaler un composant mentionné dans les sources mais absent du registry. En mode deep research, tous les 3 reçoivent également `web_search_20250305`.

**Cache prompt statique** (TTL 1 h) :
- `registry.json` complet
- Le PDF du schematic (mode schematic)
- Ces prompts sont partagés entre les 3 writers → **cache hit attendu ~60–70 %** dès le 2ᵉ writer lancé

**Contrainte canonique** (system prompt partagé, cf. §4.11) :

> Tu DOIS utiliser uniquement les `canonical_name` présents dans le registre fourni. Toute référence à un composant ou signal inconnu du registre est une erreur. Si tu identifies un composant manquant dans le registre, signale-le via `report_missing_component(name, evidence)` — ne l'invente JAMAIS dans ta sortie.

**Tracking des missing components** : le backend compte les appels `report_missing_component` à travers les 3 writers. Si le total atteint le **seuil configurable `PIPELINE_MISSING_COMPONENTS_THRESHOLD`** (défaut `5`) → l'Auditor en Phase 3 peut déclencher un retour en Phase 1 partiel (enrichissement du registry, max 1 retour). En-dessous du seuil, les signals sont simplement loggés dans les events pour audit manuel post-run.

**Configuration** — exposée dans `api/config.py` :
```python
PIPELINE_MISSING_COMPONENTS_THRESHOLD: int = 5
```

Permet de tuner en J+3 sans rebuild si les runs réels montrent que 5 est trop bas (trop de retours Phase 1 coûteux) ou trop haut (drift de vocabulaire non détecté).

**Coûts par writer** :
- schematic : ~30–100 k input (avec cache 60 % : ~12–40 k facturés en full price) + ~2–4 k output → **~0.10–0.25 $ par writer**
- deep research : + ~5–15 web_search calls → **+0.05–0.15 $**

**Total Phase 2** : **~0.30–0.75 $ schematic**, **~0.45–1.20 $ deep research**, **durée ~15–25 s** grâce à la parallélisation (le writer le plus lent dicte le total, pas la somme).

### 5.6 Phase 3 — Auditor

| Champ | Valeur |
|---|---|
| `role` | `auditor` |
| Modèle | `claude-opus-4-7` |
| Tool forcé | `submit_audit_verdict(verdict_json)` |
| Inputs | `registry.json`, `architecture.md+json`, `rules.md+json`, `dictionary.md+json`, liste des `report_missing_component` reçus en Phase 2 |

**System prompt résumé** :

> Audite la cohérence interne du knowledge pack généré. Détecte :
> 1. Les drifts de vocabulaire (composants mentionnés dans les writers mais absents du registry)
> 2. Les incohérences cross-files (rail dans architecture absent de dictionary, rule référençant un refdes inconnu)
> 3. Les erreurs factuelles manifestes (test point TP99 mentionné pour un board qui n'en a que 20)
>
> Produis un verdict structuré via `submit_audit_verdict` avec : `overall_status`, `consistency_score ∈ [0,1]`, les 4 métriques qualité, `files_to_rewrite` (sous-ensemble de `["architecture", "rules", "dictionary"]`), `drift_report` (liste), `revision_brief` (texte actionnable).

**Output** — `audit_verdict.json` :

```json
{
  "overall_status": "APPROVED",
  "consistency_score": 0.91,
  "intrinsic_effectiveness": 0.82,
  "evidence_readiness": 0.74,
  "overall_quality": 0.82,
  "files_to_rewrite": ["rules"],
  "drift_report": [
    {"file": "rules.md", "mentions": ["U99"], "reason": "U99 absent du registry"}
  ],
  "revision_brief": "Dans rules.md, remplacer les mentions de U99 par U9 (closest-match) ou retirer la rule si la mention est erronée."
}
```

**Statuts possibles** : `APPROVED` (pipeline continue vers Phase 5), `NEEDS_REVISION` (déclenche Phase 4 si budget de rounds restant), `REJECTED` (failure, pipeline stoppe avec `quality.status = "REJECTED"` et pas d'écriture disque).

**Coût** : ~20–30 k input + ~1–2 k output → **~0.10–0.20 $**.

### 5.7 Phase 4 — Reviser (conditionnelle, max 1 round)

| Champ | Valeur |
|---|---|
| `role` | `reviser` |
| Modèle | `claude-opus-4-7` |
| Tool forcé | `submit_revision(file_name, md, json)` |
| Inputs | `revision_brief`, `file_name` ciblé, `current_content` du fichier, `registry.json` (cached) |

**Une invocation par fichier à réécrire**. Si `files_to_rewrite = ["rules", "dictionary"]`, 2 invocations séquentielles (pas parallèles, pour éviter conflits de réécriture concurrente).

**System prompt résumé** :

> Réécris le fichier ciblé selon le revision brief, en respectant strictement les `canonical_name` du registry. Conserve la structure markdown / JSON originale. Ne réécris **que** le fichier demandé — ne touche pas aux autres fichiers du pack.

Après Phase 4 → retour Phase 3 pour un re-audit (1 seul retour par défaut, `rounds_used` devient 1). Si ce re-audit retourne encore `NEEDS_REVISION`, le pipeline **accepte les fichiers en l'état** et marque le pack `quality.audit_verdict = "APPROVED_WITH_RESIDUAL_ISSUES"` — pas de spirale infinie.

**Configuration** — exposée dans `api/config.py` :
```python
PIPELINE_MAX_REVISE_ROUNDS: int = int(os.getenv("PIPELINE_MAX_REVISE_ROUNDS", "1"))
```

> ⚠️ **Warning** — toute valeur `> 2` est réservée au **debug**. En démo et en pipeline typique, rester à **1 ou 2 maximum** pour borner la durée totale à < 90 s et éviter les boucles `revise → audit → revise` coûteuses en tokens (chaque round ≈ 0.25–0.45 $).

**Coût** : ~30–60 k input (content + registry cached à ~70 %) + ~3–5 k output par fichier → **~0.15–0.25 $ par fichier réécrit**, généralement 1 fichier.

### 5.8 Phase 5 — Native Facts Extractor (optionnelle, Haiku 4.5)

| Champ | Valeur |
|---|---|
| `role` | `facts_extractor` |
| Modèle | `claude-haiku-4-5` |
| Tool forcé | `submit_facts(facts_array)` |
| Inputs | `architecture.md+json`, `rules.md+json`, `dictionary.md+json`, `registry.json` |

**System prompt résumé** :

> Extrait une liste flat de facts atomiques `{subject, predicate, object, source_ids[]}` depuis les fichiers fournis. Chaque fact est auto-portant, court, vérifiable. Pas d'interprétation, pas de synthèse — extraction littérale.

**Output** : `facts.native.json` (§4.8).

**Coût** : ~30–50 k input + ~5–10 k output → **~0.01–0.02 $** (Haiku = ~15× moins cher qu'Opus 4.7 au token).

**Optionnelle** : première candidate au drop si le pipeline dépasse 120 s cumulés ou si budget tokens sous pression. Marquée `skipped` dans `events`.

**Fallback quand Phase 5 est skippée** : `facts.native.json` est **absent** du pack. La métrique `quality.intrinsic_effectiveness` est alors calculée en fallback heuristique **directement depuis `architecture.json`, `rules.json`, `dictionary.json`** :

- Comptage d'entrées structurées (nombre de rails, de composants, de rules)
- Ratio `rules / components` (densité de connaissance actionnable)
- Couverture `dictionary.entries / registry.components.where(naming_level='exact_ref')` (proportion du registre effectivement documenté)
- Présence de sources citées par rule (signal de traçabilité)

Ces métriques donnent une approximation raisonnable de la richesse du pack sans passer par l'extracteur LLM. Le pack reste **pleinement valide pour le diagnostic agent** — seul le benchmarking fine-grained est indisponible.

Metadata du pack marquée explicitement :
```json
{"phase_5_skipped": true, "intrinsic_effectiveness_source": "heuristic_fallback"}
```

Permet à l'onglet Stats (Memory Bank §10) d'afficher un badge discret « metrics approximated — run Phase 5 to refine » si l'utilisateur veut une mesure plus précise plus tard.

### 5.9 Coûts estimés par run complet

| Mode | Coût typique Phases 1–5 | Durée typique | Commentaire |
|---|---|---|---|
| **Schematic** (Pi 4, Framework) | **~0.90–1.80 $** | 45–90 s | Phase 2 parallélisée = 15–25 s. Cache hit ~60 % économise ~25 %. |
| **Deep research** (iPhone X) | **~1.30–2.80 $** | 60–120 s | Surcoût `web_search` (~15–45 calls × 0.01 $) + latence réseau |

Avec un budget hackathon supposé de **500 $ de crédit API**, on tient ~250 runs schematic ou ~180 runs deep research dans l'hypothèse théorique où **tout** le budget partirait dans la génération.

**Répartition typique réelle du budget** (anticipée) :

| Poste | % budget | Note |
|---|---|---|
| Pipeline de génération knowledge pack | **~20 %** | ~50 re-runs de dev inclus, dont une majorité bénéficient du cache agressif (coût réel ≈ 30 % du nominal après réchauffe cache) |
| Sessions **diagnostic agent** (tests quotidiens + démo finale) | **~70 %** | Le diagnostic agent consomme le plus : sessions longues, multi-tour, tool calls répétés |
| Autres (bootstrap managed agents, web_search cache warmup, tests d'intégration) | **~10 %** | |

Les chiffres « ~250 runs schematic / ~180 runs deep research » de plus haut sont donc le **max théorique** ; en pratique le budget alloué au pipeline en représente ~20 %, soit confortablement 40–50 runs complets pendant la semaine, largement suffisant.

Le détail par sub-agent (tokens, cache hit ratio, coût) est loggué dans `cost_tracking` à chaque appel (§3.2.9) et agrégé dans l'onglet Coûts de la section Agent (§11).

### 5.10 Stratégie de prompt caching — 3 couches

| Couche | TTL | Contenu typique | Hit ratio attendu |
|---|---|---|---|
| **Statique** | 1 h | System prompts par rôle, JSON schemas des tools forcés, `registry.json` quand partagé cross-phase | ~80 % après 1ᵉʳ appel |
| **Dynamique session** | 5 min | PDF schematic du device, résultats `web_search` accumulés, `architecture.md` générée (pour Auditor/Reviser) | ~60–70 % |
| **À la demande** | 5 min | Contenu ponctuel volumineux (ex. `rules.md` courant dans un prompt de Phase 4) | ~50 % |

**Ordre d'insertion dans le contexte** (du plus statique au plus dynamique) : system prompt → tool schemas → `registry.json` → sources (PDF/web) → messages dynamiques. Cet ordre maximise la réutilisation : tout ce qui est avant un cache hit reste caché pour les appels suivants.

**Économie attendue** : **~20–30 %** sur le coût brut par run. Chaque appel Anthropic renvoie `cache_creation_input_tokens` et `cache_read_input_tokens` dans `usage`, qui sont loggués dans `cost_tracking` pour audit.

### 5.11 Gestion d'erreurs, retries, idempotence

| Cas | Comportement |
|---|---|
| Timeout par phase | 120 s soft / 300 s hard. Soft → log WARN, continue. Hard → phase marquée `failed`, pipeline stoppe. |
| Retry par sous-agent | 1 retry automatique sur erreur 5xx ou rate limit (respect `Retry-After`). Au-delà : failure propagée. |
| Writer failure (1 sur 3) | Pipeline continue avec les 2 fichiers disponibles. Pack marqué `quality.status = "PARTIAL"`, l'Auditor adapte son verdict en conséquence. |
| Auditor retourne `REJECTED` | Pipeline stoppe, rien écrit sur disque, `knowledge_packs` row `status = "FAILED"`. Re-run manuel requis. |
| Crash backend en plein pipeline | Le dossier temp `memory/{vendor}/{model}.new/` reste en place — purgé au prochain boot si aucun pipeline actif n'en tient référence. |
| Écriture atomique finale | Chaque phase écrit dans `memory/{vendor}/{model}.new/` ; succès final = `mv .new/ .` (rename atomique POSIX). Si pack existant → `mv existing/ existing.bak.<ts>/` avant swap pour rollback possible. |

**Events émis** à chaque transition pour la Timeline UI : `phase.started`, `phase.completed`, `phase.failed`, `subagent.dispatched`, `subagent.completed`, `pack.finalized`, `pack.rejected`.

---

## 6. Orchestration

### 6.1 Intention : une interface propre, une seule implémentation

Le pipeline de la section 5 délègue à un **orchestrateur de sous-agents** la mécanique bas niveau : création des sessions Anthropic, forçage des tools, attente des résultats, tracking des coûts, parallélisation. Cette mécanique est isolée derrière un **`Protocol` Python** (`typing.Protocol`) pour trois raisons :

1. **Lisibilité du pipeline** — le code de `api/memory_bank/pipeline.py` lit comme du pseudo-code (`await orch.run_registry_builder(...)`), sans se polluer avec les détails de l'API Anthropic.
2. **Testabilité** — un `MockOrchestrator` conforme au Protocol permet de tester le pipeline sans Anthropic (CI, dev offline, tests unitaires).
3. **Extensibilité future** — si demain on veut basculer sur un orchestrateur local (code Python qui appelle directement `messages.create` sans passer par Managed Agents) pour des raisons de fiabilité, coût, ou portabilité, c'est un swap de classe, pas une refonte.

**Décision hackathon** : **une seule implémentation codée** — `ManagedAgentOrchestrator`. Pas de `LocalOrchestrator` dans le repo. L'interface existe pour documenter le contrat, permettre les tests avec mock, et laisser la porte ouverte ; **elle ne double pas le travail d'implémentation** (§6.8).

### 6.2 `SubAgentOrchestrator` — Protocol Python

```python
from typing import Protocol

from api.memory_bank.packs import (
    Registry, Architecture, Rules, Dictionary,
    AuditVerdict, FactsList, LearnedRule,
)


class SubAgentOrchestrator(Protocol):
    """Contrat d'orchestration des sub-agents du pipeline de génération
    et du cycle apprenant. L'implémentation concrète est
    ManagedAgentOrchestrator (cf. §6.3).

    Toute implémentation DOIT :
    - être sûre pour l'appel concurrent (cf. run_writers_parallel)
    - tracker chaque appel dans la table subagent_calls (cf. §3.2.6)
    - émettre les events phase.* et subagent.* (cf. §5.11)
    - respecter les tools forcés définis en §5 (submit_registry, submit_architecture, ...)
    """

    async def run_registry_builder(
        self,
        *,
        session_id: str,
        device_id: str,
        sources: "PipelineSources",
        mode: "PipelineMode",           # 'schematic' | 'deep_research'
    ) -> Registry: ...

    async def run_writers_parallel(
        self,
        *,
        session_id: str,
        registry: Registry,
        sources: "PipelineSources",
        mode: "PipelineMode",
    ) -> tuple[Architecture, Rules, Dictionary]: ...

    async def run_auditor(
        self,
        *,
        session_id: str,
        registry: Registry,
        architecture: Architecture,
        rules: Rules,
        dictionary: Dictionary,
        missing_components_reports: list[dict],
    ) -> AuditVerdict: ...

    async def run_reviser(
        self,
        *,
        session_id: str,
        file_name: str,                 # 'architecture' | 'rules' | 'dictionary'
        revision_brief: str,
        current_content: tuple[str, dict],   # (markdown, json)
        registry: Registry,
    ) -> tuple[str, dict]: ...

    async def run_facts_extractor(
        self,
        *,
        session_id: str,
        architecture: Architecture,
        rules: Rules,
        dictionary: Dictionary,
        registry: Registry,
    ) -> FactsList: ...

    async def run_rule_synthesizer(
        self,
        *,
        device_id: str,
        cases: list[dict],              # cases JSON réussis
        evidence_pattern: dict,         # entrée de evidence.json
    ) -> LearnedRule: ...
```

**Notes de contrat** :

- Toutes les méthodes sont `async` — elles awaitent du I/O réseau (Anthropic).
- `session_id` fait référence à la **session wrench-board** (ligne `sessions` du `generation_job`), **pas** à la session Anthropic. Les sessions Anthropic sont internes à l'orchestrator.
- Les paramètres sont **kwargs-only** (`*,`) pour forcer la lisibilité des appels côté pipeline.
- Les types de retour sont les dataclasses structurées définies dans `api/memory_bank/packs.py` (à écrire — alignées sur les schemas §4).

### 6.3 `ManagedAgentOrchestrator` — implémentation unique

```python
from anthropic import AsyncAnthropic

class ManagedAgentOrchestrator(SubAgentOrchestrator):
    """Orchestre les sub-agents via Anthropic Managed Agents (beta).

    Un instance par process FastAPI (singleton du lifespan).
    Cache les IDs des managed_agents en mémoire au démarrage.
    """

    def __init__(self, client: AsyncAnthropic, agent_ids: dict[str, "ManagedAgentRef"]):
        self._client = client
        self._agents = agent_ids        # {role: {anthropic_agent_id, version}}

    async def run_registry_builder(self, *, session_id, device_id, sources, mode):
        agent = self._agents["registry_builder"]
        call_row = await self._begin_subagent_call(
            session_id=session_id, role="registry_builder", phase="1_registry"
        )
        try:
            result = await self._run_session_forced_tool(
                agent=agent,
                tool_name="submit_registry",
                sources=sources,
                mode=mode,
                call_id=call_row.id,
            )
            registry = Registry.model_validate(result)
            await self._complete_subagent_call(call_row, output_summary={"component_count": len(registry.components)})
            return registry
        except Exception as exc:
            await self._fail_subagent_call(call_row, reason=str(exc))
            raise
    # ... idem pour les autres méthodes
```

**Caractéristiques** :

- **Instanciée une fois** par `api.main.lifespan` — le dict `agent_ids` est chargé depuis Postgres `managed_agents` au boot (§3.2.3). Si une ligne manque (nouveau rôle ajouté), le boot échoue avec message clair.
- **Stateless entre appels** — pas de state inter-sessions dans l'instance. Safe pour l'appel concurrent depuis `asyncio.gather`.
- **Chaque méthode suit le même squelette** : `_begin_subagent_call` → logique métier (création session Anthropic, streaming, forcing tool) → `_complete_subagent_call` OU `_fail_subagent_call`. La table `subagent_calls` est mise à jour à chaque transition pour que l'UI Traces voie l'état en temps réel.

**Trois niveaux de retry distincts** — aucun ne masque les autres :

| Niveau | Déclencheur | Responsable |
|---|---|---|
| 1. Retry HTTP SDK | 5xx, rate limit (Retry-After), network errors transitoires | `anthropic` SDK (retries intégrés) |
| 2. **Retry applicatif tool output** (nouveau — cf. ci-dessous) | Tool forcé a répondu 200 OK mais l'output ne valide pas le schema Pydantic attendu | `_call_with_tool_validation()` dans l'orchestrator |
| 3. Retry pipeline (phase) | Échec terminal d'un sub-agent après retries 1 et 2 épuisés → partial pack handling | Logique de §5.11 |

**Méthode protégée `_call_with_tool_validation`** :

```python
async def _call_with_tool_validation(
    self,
    *,
    role: str,
    tool_name: str,
    expected_schema: type[BaseModel],       # ex. RegistrySubmission
    agent: ManagedAgentRef,
    session_inputs: dict,
    max_attempts: int = 2,
    call_row: SubagentCallRow,
) -> BaseModel:
    """Lance une session Anthropic sur `agent` avec tool forcé `tool_name`,
    attend un output qui valide `expected_schema`. Si l'output renvoyé
    par le tool forcé ne parse pas, retry avec un message système
    additionnel explicitant l'erreur. Max `max_attempts` tentatives.
    """
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        system_suffix = ""
        if attempt > 1:
            system_suffix = (
                f"\n\nPREVIOUS ATTEMPT FAILED: your tool output did not match "
                f"the expected schema. Error: {last_error}. "
                f"Retry with correct structure as per the {tool_name} tool definition."
            )
            await events.emit("subagent.retry", {
                "role": role, "attempt": attempt, "last_error": last_error,
                "subagent_call_id": call_row.id,
            })

        raw_output = await self._run_forced_tool_session(
            agent=agent, tool_name=tool_name,
            inputs=session_inputs, system_suffix=system_suffix,
        )
        try:
            return expected_schema.model_validate(raw_output)
        except ValidationError as exc:
            last_error = str(exc)

    raise PipelinePhaseError(
        f"{role} failed to produce a valid {tool_name} output after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )
```

**Raison d'être** : en API beta, l'écart « shape attendue vs shape reçue » survient plus souvent qu'avec les modèles GA. Un retry ciblé avec feedback explicite corrige ~80 % des cas sans escalader en failure de phase. Chaque retry est loggué en `events` sous le type `subagent.retry` avec l'erreur reçue — métrique de fiabilité consultable dans l'onglet Traces.

### 6.4 Pattern `dispatch_subagent` — intercepté par le backend, pas exécuté côté Anthropic

Le Coordinator managed agent (§5.3) utilise un **custom tool** `dispatch_subagent(role, input)`. Ce tool n'est **pas** exécuté côté Anthropic — il émet un event `agent.custom_tool_use` que notre backend intercepte.

**Flow détaillé** :

```
Coordinator (Anthropic session "coordinator") émet :
    agent.custom_tool_use {
      id: "evt_xxx",
      name: "dispatch_subagent",
      input: {
        role: "registry_builder",
        input: {device_id: "...", sources: {...}, mode: "schematic"}
      }
    }
           │
           ▼
Backend intercepte dans api/managed/session_stream.py::handle_event :
  1. Match: event.type == "agent.custom_tool_use" AND event.name == "dispatch_subagent"
  2. Parse input["role"] et input["input"]
  3. Appel de la méthode correspondante sur l'orchestrator :
       match input["role"]:
           case "registry_builder":    result = await orch.run_registry_builder(**input["input"])
           case "writer_architecture": result = await orch.run_writer_architecture(**input["input"])
           ...
  4. Emit phase.dispatched et phase.completed dans events
  5. Renvoyer à la session Coordinator :
       user.custom_tool_result {
         custom_tool_use_id: "evt_xxx",
         content: [{type: "text", text: json.dumps(result.to_dict())}]
       }
           │
           ▼
Coordinator reçoit le résultat structuré et décide la phase suivante
```

**Garantie d'isolation** : chaque invocation de sub-agent crée une **session Anthropic distincte** (pas un `messages.create` direct). Avantages :
- Chaque sub-agent bénéficie de son propre prompt caching (TTL 1h par session)
- Les tools forcés (`submit_registry`, etc.) sont scopés par session, pas de collision
- Le Coordinator voit juste le résultat structuré, pas le détail des tool calls internes du sub-agent (propre séparation des niveaux d'abstraction)

### 6.5 Parallélisation des writers via `asyncio.gather`

```python
async def run_writers_parallel(self, *, session_id, registry, sources, mode):
    tasks = [
        self._run_writer(role="writer_architecture", ...),
        self._run_writer(role="writer_rules", ...),
        self._run_writer(role="writer_dictionary", ...),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    architecture, rules, dictionary = results
    if any(isinstance(r, Exception) for r in results):
        # Partial pack handling — cf. §5.11
        ...
    return (architecture, rules, dictionary)
```

**Points clés** :

- `return_exceptions=True` — une failure d'un writer ne cancelle pas les autres. Les 3 tournent en isolation.
- **3 sessions Anthropic simultanées** — Anthropic supporte ça nativement, rate limits partagés au niveau compte.
- Le client `AsyncAnthropic` est **thread-safe et task-safe** (le SDK gère la concurrence HTTP).
- Le `registry` est passé par référence aux 3 writers → chacun l'inclut dans son prompt statique → **cache hit Anthropic** quand le 2ᵉ et 3ᵉ writer reçoivent le même registre que le 1ᵉʳ (1ʳᵉ invocation écrit le cache, les 2 suivantes le lisent).

Durée observable : le writer le plus lent dicte le `gather` (pas la somme). En pratique, architecture et dictionary tournent en 15–20 s, rules peut pousser à 20–25 s.

### 6.6 Lifecycle des managed agents et sessions

**Bootstrap (une fois, idempotent)** — `scripts/bootstrap_agents.py` :

```
for role in REQUIRED_ROLES:  # les 10 rôles §3.2.3
    if managed_agents.select(role=role) is None:
        agent = client.beta.agents.create(
            name=f"wrench-board-{role}",
            model=MODEL_FOR_ROLE[role],       # Opus sauf facts_extractor (Haiku)
            tools=TOOLS_FOR_ROLE[role],
        )
        managed_agents.insert(role=role, anthropic_agent_id=agent.id, version=agent.version, ...)
```

Script rejouable en sécurité ; un rôle déjà créé est skip. Si le system prompt change (détecté via `system_prompt_hash` en DB), création d'une **nouvelle version** du managed agent (Anthropic gère le versioning), ancienne archivée.

**Sessions — créées à la demande, courte durée** :

- Une session Anthropic par invocation de sub-agent, vivante le temps du tool call forcé (~10–60 s).
- Pas de cleanup explicite requis — Anthropic archive les sessions inactives automatiquement.
- Le Coordinator est spécial : sa session dure **toute la durée du pipeline** (~60–90 s) car il fait plusieurs tool calls successifs.

**Memory stores — créés à l'ajout du device, persistants** :

- `managed_memory_stores` row + création Anthropic au premier chargement d'un device (cf. §2.3 flow A).
- Attachée à toutes les futures sessions diagnostic de ce device.
- Jamais supprimée sauf si le device est supprimé de `devices`.

**Cleanup explicite à la suppression d'un device** — handler FastAPI best-effort :

```python
async def delete_device(device_id: UUID) -> None:
    memory_store_id = await db.get_memory_store_id(device_id)
    if memory_store_id:
        try:
            await anthropic_client.beta.memory_stores.delete(memory_store_id)
        except anthropic.APIError as exc:
            logger.warning(
                "Failed to delete Anthropic memory store %s for device %s: %s. "
                "Proceeding with local deletion anyway.",
                memory_store_id, device_id, exc,
            )
    await db.delete_device(device_id)  # cascade Postgres sur managed_memory_stores, sessions, etc.
```

Best-effort côté Anthropic : si l'appel `memory_stores.delete` échoue, on log et on **continue** la suppression DB. Évite qu'un orphelin Anthropic bloque les futures actions locales. Pas de trigger DB, pas de GC périodique — le cleanup est explicit et synchrone au moment de la suppression utilisateur.

### 6.7 Observabilité — `subagent_calls` + events en temps réel

Chaque appel d'orchestrateur alimente deux surfaces d'observabilité **simultanément** :

**A. La table `subagent_calls`** (§3.2.6) — pour l'onglet Traces UI (§10) :

```
INSERT au début :  {session_id, role, phase, status: 'running', started_at}
UPDATE à la fin :  {status: 'completed'|'failed', ended_at, input_summary, output_summary}
```

Consultable via l'onglet Traces en SQL agrégé : durée moyenne par rôle, taux de failure, séquences typiques.

**B. Les events** (§3.2.7) — pour le stream WebSocket temps réel :

```
phase.started       { phase, role, subagent_call_id }
subagent.dispatched { role, anthropic_session_id }
subagent.completed  { role, duration_s, output_summary }
phase.completed     { phase, duration_s }
```

Le frontend connecté à `/ws/generation/{job_id}` reçoit ces events au fil de l'eau et anime la progression (barre de phase, pastilles sub-agents actifs, compteur temps écoulé). Essentiel pour la **capture timelapse Framework J+4** (split-screen des 5 sub-agents actifs en Phase 2 animé en direct).

### 6.8 Note sur `LocalOrchestrator` — non implémenté pour le hackathon

Le Protocol `SubAgentOrchestrator` admettrait une implémentation alternative `LocalOrchestrator` qui bypasserait Managed Agents et appellerait directement `messages.create` pour chaque sub-agent. **Cette implémentation n'est pas codée dans ce projet.**

Raisons :
1. **Critère de prix « Best use of Managed Agents »** — le projet vise ce prix ($5 k) et doit utiliser Managed Agents comme chemin principal.
2. **YAGNI** — pas d'évidence que Managed Agents sera instable pendant la semaine. Coder un fallback « au cas où » = 2–3 h jetées si pas utilisé.
3. **Bascule d'urgence si bug bloquant** — en J+2 si `client.beta.agents.create` est inutilisable, le swap vers un LocalOrchestrator minimal se fait en 2–3 h grâce à l'interface propre, pas besoin de l'avoir en avance. Noté explicitement dans la section 13 (Risques & mitigations).
4. **Engagement narratif** — la démo vidéo mentionne Managed Agents comme choix architectural assumé. Avoir un fallback pré-codé diluerait ce message.

**Env var `ORCHESTRATION_MODE`** — `api/config.py` expose la variable mais n'accepte que `"managed"` en v1 :

```python
ORCHESTRATION_MODE: Literal["managed"] = "managed"  # "local" réservé pour une évolution future
```

Si une valeur autre que `"managed"` est lue au boot, exception claire : « LocalOrchestrator not implemented in this version — see section 6.8 of the design spec ».

---

## 7. Diagnostic agent & tools `mb_*`

### 7.1 Le rôle du diagnostic agent

Le **diagnostic agent** est le managed agent Anthropic de rôle `diagnostic` que l'utilisateur manipule dans l'UI (panel LLM droit, section PCB, section Schematic). Il est :

- **Face visible** du système vis-à-vis de l'utilisateur — chaque message, chaque highlight board, chaque citation de rule passe par lui
- **Consommateur** des deux familles de tools :
  - Les **12 tools boardview** (définis par Boardviewer spec §9, cf. §2.7) pour piloter la vue physique
  - Les **7 tools `mb_*`** (définis ci-dessous) pour interroger la Memory Bank
  - Plus un lot de **tools schematic** (`open_schematic_page`, `highlight_schematic_net`, ...) à spécifier séparément
- **Distinct des sub-agents du pipeline** (§5) — leur rôle est la **génération** de connaissance offline ; le rôle du diagnostic agent est la **conversation temps réel** qui exploite cette connaissance.

Un managed agent **unique** de rôle `diagnostic` existe, partagé entre tous les devices. Il reçoit un **memory store différent par session** (celui du device ouvert), ce qui le rend contextuel sans dupliquer 3 agents identiques côté Anthropic.

### 7.2 Les 7 tools `mb_*` — signatures et retours

Chaque tool est exposé côté Anthropic avec un `input_schema` JSON Schema. Côté backend, les retours sont des modèles Pydantic qui contraignent strictement la shape.

Convention commune : **tous** les tools prennent un paramètre implicite `device_id` injecté par le backend avant dispatch (l'agent n'a pas à le passer ; il est déterminé par la session en cours). Seuls les paramètres métier figurent dans l'`input_schema` exposé à l'agent.

#### 7.2.0 Décision architecturale — tools natifs vs MCP

Les 7 tools `mb_*` sont définis **en natif** dans le backend Python (JSON Schema + handler dans `api/memory_bank/tools.py`), exposés à l'agent diagnostic via le paramètre `tools=[...]` de l'appel API Anthropic (ou via `agents.create(tools=...)` pour les managed agents). Ils ne sont **pas** exposés via un serveur MCP (Model Context Protocol).

**Rationale** :

- **Coupling élevé avec le backend** — chaque tool touche Postgres, le dossier `memory/` on-disk, le validator boardview. MCP ajouterait une couche de sérialisation JSON-RPC sans apporter de valeur à ce coupling interne.
- **Consommateur unique** — le diagnostic agent interne. MCP est conçu pour la réutilisabilité cross-clients (Claude Desktop, Cursor, autres éditeurs), ce qui n'est pas le cas ici.
- **Latence critique** — le contrat anti-hallucination exige un feedback en **moins de 50 ms** (lookup registry cached). MCP via stdio ou HTTP ajoute ~20–50 ms par appel, ce qui double le latency budget.
- **Pipeline interne** — les sub-agents du §5 tournent dans le même process backend, pas besoin de protocole externe.

**MCP reste pertinent en roadmap post-hackathon** pour exposer le boardviewer comme serveur standalone réutilisable par des clients tiers (Claude Desktop, Cursor, autres LLM hosts) — hors scope v1.

#### 7.2.1 `mb_get_component(refdes)`

Récupère un composant depuis le registry + dictionary du device courant.

```json
{
  "name": "mb_get_component",
  "description": "Retrieve a component by refdes from the knowledge pack of the current device. Returns full info (role, pins, nets, test_points_nearby) if found, or {error, closest_matches} if not.",
  "input_schema": {
    "type": "object",
    "properties": {"refdes": {"type": "string", "description": "Reference designator, e.g. U7, C29, R123."}},
    "required": ["refdes"]
  }
}
```

**Retour** — discriminé sur `found` :

```python
class ComponentFound(BaseModel):
    found: Literal[True] = True
    canonical_name: str
    aliases: list[str]
    kind: str
    role: str
    nets_connected: list[str]
    pins: list[ComponentPin]
    test_points_nearby: list[str]
    package: str | None
    location_hint: str | None
    confidence: float

class ComponentNotFound(BaseModel):
    found: Literal[False] = False
    error: Literal["not_found"] = "not_found"
    queried_refdes: str
    closest_matches: list[str]       # top 5 refdes proches (§7.3)
    hint: str                         # ex. "Did you mean U17? — same prefix letter"

ComponentLookupResult = ComponentFound | ComponentNotFound
```

#### 7.2.2 `mb_get_net(net_name)`

Récupère un signal / net depuis le registry + architecture.

```json
{
  "name": "mb_get_net",
  "description": "Retrieve a net / signal by name (exact or alias). Returns voltage, kind, test_point, consumers, source components. Error + closest_matches if not found.",
  "input_schema": {
    "type": "object",
    "properties": {"net_name": {"type": "string", "description": "Net name, e.g. 3V3_RAIL, VDD_CORE, USB_DP1."}},
    "required": ["net_name"]
  }
}
```

**Retour** :

```python
class NetFound(BaseModel):
    found: Literal[True] = True
    canonical_name: str
    aliases: list[str]
    kind: str                         # 'power_rail', 'signal', 'reference'
    nominal_voltage: float | None
    tolerance_pct: float | None
    test_point: str | None
    source_components: list[str]      # refdes qui pilotent ce net (ex. PMIC pour 3V3)
    consumer_components: list[str]    # refdes qui consomment ce net

class NetNotFound(BaseModel):
    found: Literal[False] = False
    error: Literal["not_found"] = "not_found"
    queried_net_name: str
    closest_matches: list[str]
```

#### 7.2.3 `mb_get_test_point(tp_id)`

Récupère un test point nommé (ex. `TP18`).

```json
{
  "name": "mb_get_test_point",
  "description": "Retrieve a test point by id (TPxx). Returns the net it exposes and expected voltage. Error + closest_matches if not found.",
  "input_schema": {
    "type": "object",
    "properties": {"tp_id": {"type": "string", "description": "Test point id, e.g. TP18."}},
    "required": ["tp_id"]
  }
}
```

**Retour** :

```python
class TestPointFound(BaseModel):
    found: Literal[True] = True
    tp_id: str
    net_canonical_name: str
    expected_voltage: float | None
    tolerance_pct: float | None
    nearby_components: list[str]

class TestPointNotFound(BaseModel):
    found: Literal[False] = False
    error: Literal["not_found"] = "not_found"
    queried_tp_id: str
    closest_matches: list[str]
```

#### 7.2.4 `mb_get_rules_for_symptoms(symptoms, max_results=5)`

Requête les rules du device matchant une liste de symptômes.

```json
{
  "name": "mb_get_rules_for_symptoms",
  "description": "Retrieve diagnostic rules that match the given symptoms. Rules come from rules.json (core + learned). Returns up to max_results rules ranked by relevance × confidence. Each rule includes diagnostic_steps and likely_causes.",
  "input_schema": {
    "type": "object",
    "properties": {
      "symptoms":   {"type": "array", "items": {"type": "string"}, "minItems": 1},
      "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
    },
    "required": ["symptoms"]
  }
}
```

**Retour** :

```python
class RuleMatch(BaseModel):
    rule_id: str
    origin: Literal["core", "learned"]
    symptoms_matched: list[str]
    likely_causes: list[Cause]
    diagnostic_steps: list[DiagnosticStep]
    confidence: float
    needs_validation: bool
    sources: list[str]

class RulesQueryResult(BaseModel):
    device_id: str
    query_symptoms: list[str]
    matches: list[RuleMatch]         # empty list if no match (pas d'erreur, juste vide)
    total_available_rules: int       # info meta : taille du corpus
```

Note : ce tool **ne retourne pas** `{error, closest_matches}` — il retourne un résultat vide si aucun match. L'absence de rules n'est pas une erreur ; l'agent doit gérer le cas (poser des questions de clarification, proposer une exploration).

#### 7.2.5 `mb_get_rework(identifier)`

Récupère les procédures de rework (remplacement, reflow, reballing) pour un composant ou une opération nommée.

```json
{
  "name": "mb_get_rework",
  "description": "Retrieve rework procedures for a component (by refdes) or a named operation (by rework_id). Returns tooling needed, time estimate, risk level, steps. Error + closest_matches if not found.",
  "input_schema": {
    "type": "object",
    "properties": {"identifier": {"type": "string", "description": "Either a refdes (U7, C29) or a rework procedure id (rw-bga-reball-bcm2711)."}},
    "required": ["identifier"]
  }
}
```

**Retour** :

```python
class ReworkFound(BaseModel):
    found: Literal[True] = True
    rework_id: str
    target_refdes: str | None
    tooling_needed: list[str]        # ex. ['hot air', '0402 tweezers', 'flux']
    time_estimate_min: int
    risk_level: Literal["low", "medium", "high"]
    steps: list[str]
    sources: list[str]

class ReworkNotFound(BaseModel):
    found: Literal[False] = False
    error: Literal["not_found"] = "not_found"
    queried_identifier: str
    closest_matches: list[str]
```

#### 7.2.6 `mb_find_similar_cases(symptoms, top_k=3)`

Requête les cases résolus historiques matchant des symptômes (similarité sur les listes de symptômes + filtre device).

```json
{
  "name": "mb_find_similar_cases",
  "description": "Find similar resolved cases in the current device history. Ranked by symptom overlap × recency. Returns top_k cases with resolution summary.",
  "input_schema": {
    "type": "object",
    "properties": {
      "symptoms": {"type": "array", "items": {"type": "string"}, "minItems": 1},
      "top_k":    {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}
    },
    "required": ["symptoms"]
  }
}
```

**Retour** :

```python
class SimilarCase(BaseModel):
    case_id: str
    title: str
    symptoms: list[str]
    overlap_score: float              # [0,1], fraction des symptômes matchés
    resolution_summary: str           # 1-2 phrases : cause + action
    cause_refdes: str | None
    resolved_at: datetime

class SimilarCasesResult(BaseModel):
    device_id: str
    query_symptoms: list[str]
    cases: list[SimilarCase]         # empty list if no match
    total_cases_in_history: int
```

Même logique que `mb_get_rules_for_symptoms` — liste vide ≠ erreur.

#### 7.2.7 `mb_save_case(case)`

Persiste un case résolu (ou abandonné) en DB + disque. Appelé par l'agent à la **fin** d'une session diagnostic, quand la résolution est établie (ou l'abandon acté).

```json
{
  "name": "mb_save_case",
  "description": "Save a resolved (or abandoned) diagnostic case. Writes to Postgres `cases` table and to memory/{vendor}/{model}/cases/case-NNN-*.{md,json}. Triggers the learning cycle (pattern aggregation).",
  "input_schema": {
    "type": "object",
    "properties": {
      "title":    {"type": "string", "maxLength": 120},
      "symptoms": {"type": "array", "items": {"type": "string"}, "minItems": 1},
      "resolution": {
        "type": "object",
        "properties": {
          "cause_refdes":     {"type": "string"},
          "cause_mechanism":  {"type": "string"},
          "action":           {"type": "string"},
          "outcome":          {"type": "string"},
          "wasted_time_min":  {"type": "integer"}
        }
      },
      "status":   {"type": "string", "enum": ["resolved", "dead_end", "abandoned"]}
    },
    "required": ["title", "symptoms", "status"]
  }
}
```

**Retour** :

```python
class CaseSaved(BaseModel):
    ok: Literal[True] = True
    case_id: str                      # id généré côté backend
    disk_path: str                    # chemin du .md écrit
    learning_cycle_triggered: bool    # cf. §8
```

Si le refdes de `cause_refdes` n'existe pas dans le registry (validation `mb_get_component`), le case est **refusé** avec `{ok: False, error: "invalid_cause_refdes", closest_matches: [...]}`. L'agent doit corriger avant retry.

### 7.3 Contrat anti-hallucination — `{error, closest_matches}` jamais d'invention

**Règle** : tout tool `mb_*` qui ne trouve pas la donnée demandée retourne une **shape d'erreur structurée**, jamais une réponse textuelle vague, jamais des valeurs inventées. La shape type :

```json
{
  "found": false,
  "error": "not_found",
  "queried_refdes": "U999",
  "closest_matches": ["U99", "U9", "U19"],
  "hint": "No refdes 'U999' in registry. Closest by prefix: U99."
}
```

**Algorithme de `closest_matches`** (côté backend, dans `api/memory_bank/closest_match.py`) :

1. **Filtre par prefix letter** (`U*`, `C*`, `R*`, …) — restreint le corpus aux refdes du même type
2. **Distance de Levenshtein** sur la partie numérique (`U7 ↔ U17 = 1`, `U7 ↔ U70 = 1`, `U7 ↔ C29 = N/A car filtré`)
3. **Score composite** : `score = 1 / (1 + levenshtein) + 0.1 × same_kind` (bonus si kind concorde)
4. **Top 5** retournés, triés score descendant
5. Si aucun match raisonnable (score < 0.25 pour tous) : `closest_matches: []` avec `hint` suggérant d'élargir la recherche ou de vérifier le device

**Conséquence pour l'agent** : il reçoit un feedback actionable en moins de 50 ms (lookup en mémoire sur le registry cached), peut rapidement corriger son hypothèse et relancer. Pas de spirale d'invention pour « combler » un trou.

### 7.4 System prompt — injection en 3 couches

Le prompt système du diagnostic agent est structuré en **3 couches de cache distinctes**, alignées sur la stratégie §5.10 mais avec des TTL et des contenus propres à la conversation.

#### Couche 1 — STATIQUE (cache 1 h)

Contenu identique à chaque session sur un même device. Ordre d'insertion :

1. **Persona** — « Tu es un assistant de diagnostic microsoudure, calme, méthodique. Tu pilotes visuellement un board électronique et guides l'utilisateur pas à pas. Tu ne devines jamais ; tu vérifies. »
2. **Règles dures (rappel)** — les 5 règles du §1.2, avec focus sur la règle #5.
3. **Contrat des tools** — synthèse : « Pour mentionner un refdes, tu DOIS d'abord valider via `mb_get_component`. Pour highlighter, tu utilises les 12 tools boardview — chacun est doublement validé en backend. Si un tool te retourne `{error, closest_matches}`, corrige ou demande clarification, jamais invente. **Note : un tableau `matches: []` ou `cases: []` vide dans la réponse d'un tool de recherche n'est PAS une erreur et ne demande PAS de retry. Continue avec les infos disponibles ou demande à l'utilisateur plus de symptômes.** »
4. **`architecture.md` du device courant** — inclus intégralement. Donne à l'agent le plan d'ensemble (power tree, blocs, rails) avant toute conversation.

Hit cache attendu : **~95 %** après le 1ᵉʳ message utilisateur de la session (TTL 1 h renouvelé à chaque hit).

#### Couche 2 — DYNAMIQUE session (cache 5 min)

Contenu calculé au début de la session, rafraîchi si le contexte change significativement :

1. **Liste compacte des refdes** — flatlist `["U7", "U8", "C29", ...]` (quelques centaines d'entrées pour Pi 4, ~1 k pour Framework) — permet à l'agent d'« avoir en tête » les refdes disponibles sans tool call
2. **Liste des nets/signaux** canonical names
3. **Liste des test points** avec voltage nominal
4. **3–5 rules top-ranked** basées sur les symptômes du premier message utilisateur — pré-fetch géré par le backend :
   1. Backend reçoit le 1ᵉʳ `user.message` de la session
   2. Extraction rapide des symptômes via **Haiku 4.5** (prompt court « list the symptoms in this sentence, JSON array ») OU via un matcher regex / keyword list si la sentence match un pattern simple (optimisation coût — Haiku n'est appelé qu'en fallback du matcher regex)
   3. Si ≥ 1 symptôme extrait → `mb_get_rules_for_symptoms(symptoms, max_results=5)` → les rules sont injectées dans la couche 2
   4. Si 0 symptôme (ex. « Hello », « can you help me? ») → couche 2 sans rules pré-fetchées ; l'agent fera le tool call lui-même après que l'utilisateur aura précisé
5. **Résumé du memory store** — automatiquement fourni par Anthropic Managed Agents (cf. §2.2), donc pas à re-injecter nous-mêmes

Hit cache attendu : **~60–70 %** au fil de la session.

#### Couche 3 — À LA DEMANDE (non pré-injectée)

Disponible via les tool calls `mb_*`. L'agent appelle quand il a besoin :

- Détails complets d'un composant (`mb_get_component` → pins, package, location_hint)
- Net détaillé (`mb_get_net` → consumers, source)
- Rules d'une symptomatique précise (`mb_get_rules_for_symptoms`)
- Cases similaires (`mb_find_similar_cases`)
- Rework (`mb_get_rework`)

**Raison du split** : injecter tout le registry + dictionary + rules en cache 1 h ferait exploser le prompt statique (>100 k tokens pour Framework). On garde juste l'essentiel en statique (architecture narrative + listes flat en dynamique), et l'agent enrichit son contexte via tool calls quand il creuse.

### 7.5 State machine — 4 phases avec seuils de confidence

L'agent navigue dans une state machine **implicite** (portée par le prompt système + son propre raisonnement, pas par du code applicatif rigide). Les transitions de phase sont signalées explicitement via un tool `set_diagnostic_phase` (cf. §7.5.3) — pas par parsing texte.

#### 7.5.1 Les 4 phases et leurs seuils

| Phase | Seuil de confidence | Actions autorisées | Transition vers phase suivante |
|---|---|---|---|
| **1. OBSERVATION** | — (tout permis) | Poser des questions ouvertes, demander mesures, highlight/focus sur composants candidats, `mb_get_rules_for_symptoms` en shotgun | Au moins 2 symptômes clairs collectés ET ≥1 rule match avec `confidence ≥ hypothesis_propose` |
| **2. HYPOTHESIS** | ≥ `hypothesis_propose` pour proposer, ≥ `hypothesis_rank_high` pour présenter en tête de liste | Proposer des hypothèses, afficher les `likely_causes` des rules, ranker par probabilité, solliciter confirmation utilisateur | Une hypothèse atteint `≥ act_measure` après 1–2 mesures confirmantes |
| **3. ACT** | ≥ `act_measure` pour demander une mesure spécifique | Demander mesure ciblée, valider/invalider l'hypothèse, éventuellement `focus_component` + pan | Confidence de l'hypothèse atteint `≥ resolution_recommend` après les mesures |
| **4. RESOLUTION** | ≥ `resolution_recommend` pour recommander un rework | Présenter le rework via `mb_get_rework`, expliquer les risques, attendre confirmation, enregistrer via `mb_save_case` | Case résolu ou abandonné → session terminée |

**Exception de sécurité** : l'agent peut **toujours** recommander « consulter un pro » ou « remplacer la carte plutôt que rework » si aucune hypothèse n'atteint `resolution_recommend` après plusieurs mesures. Il n'y a pas de forcing — mieux vaut une session abandonnée qu'un rework risqué sur hypothèse faible.

#### 7.5.2 Seuils en configuration — `DiagnosticThresholds`

Exposés dans `api/config.py` pour permettre A/B testing en J+3/J+4 sans ré-éditer le prompt :

```python
from pydantic import BaseModel

class DiagnosticThresholds(BaseModel):
    hypothesis_propose: float    = 0.50
    hypothesis_rank_high: float  = 0.70
    act_measure: float           = 0.65
    resolution_recommend: float  = 0.75

DIAGNOSTIC_THRESHOLDS: DiagnosticThresholds = DiagnosticThresholds()
```

Le prompt système contient un **template** qui reçoit ces valeurs au runtime via `.format()` :

```
...
You escalate through 4 phases with these confidence thresholds:
- HYPOTHESIS: propose at ≥ {hypothesis_propose}, rank-high at ≥ {hypothesis_rank_high}
- ACT: specific measurement at ≥ {act_measure}
- RESOLUTION: rework recommendation at ≥ {resolution_recommend}
...
```

#### 7.5.3 Tool `set_diagnostic_phase` — signalement explicite, pas de regex sur la narration

Pour éviter un parsing fragile de la réponse texte, les changements de phase passent par un **tool UI de contrôle** catégorisé avec les 12 tools boardview (même pattern de dispatch, intercepté côté backend) :

```json
{
  "name": "set_diagnostic_phase",
  "description": "Signal a transition between the 4 diagnostic phases. Call this whenever your confidence clears a threshold or you decide to switch phase. Always pair with a short textual reason.",
  "input_schema": {
    "type": "object",
    "properties": {
      "phase":  {"type": "string", "enum": ["OBSERVATION", "HYPOTHESIS", "ACT", "RESOLUTION"]},
      "reason": {"type": "string", "description": "Short justification for the transition (1 sentence)."}
    },
    "required": ["phase", "reason"]
  }
}
```

Flow d'un appel :

```
Agent émet custom_tool_use set_diagnostic_phase(phase="HYPOTHESIS", reason="User confirmed 3V3 dead + no boot, 2 rules matched ≥ 0.50.")
    ↓
Backend intercepte (api/tools/diagnostic_ui.py)
    ↓
UPDATE sessions.metadata SET current_phase = 'HYPOTHESIS'
Emit WS event diagnostic.phase_changed { phase, reason }
    ↓
UI panel LLM met à jour le badge d'en-tête
    ↓
Retour à l'agent : user.custom_tool_result {"ok": true}
```

Ce tool appartient à la **famille « UI control pour le panel diagnostic »**, distincte des `mb_*` (data) et des 12 tools boardview (vue physique). Il est propre, testable, et évite toute reconnaissance heuristique sur le texte de l'agent.

### 7.6 Double validation flow (boardview + mb_*) — rappel §2.7

Pour toute action UI boardview qui prend un refdes, le flow est **exactement** celui de §2.7.3 :

```
Agent émet custom_tool_use highlight_component("U7")
    ↓
Backend api/tools/boardview.py intercepte
    ↓
L1 — api/board/validator.py::is_valid_refdes("U7")
      (contre le board file parsé — source Boardviewer)
    ↓ si ko : retourne {ok: false, suggestions: [...]}
    ↓ si ok :
L2 — api/memory_bank/tools.py::mb_get_component("U7")
      (contre registry.json — source knowledge pack)
    ↓ si ko : retourne {error: "not_found", closest_matches: [...]}
    ↓ si ok :
Emit message WS `boardview.highlight` suivant protocole Boardviewer spec §10
Frontend rend
    ↓
Retour à l'agent : user.custom_tool_result {"ok": true, ...}
```

**Les deux couches sont indépendantes** : un refdes peut exister dans le board file mais pas dans le registry (composant physiquement soudé mais non documenté), ou l'inverse (documenté mais pas dans le board file courant, ex. composant optionnel selon révision PCB). Les deux échecs sont des signaux différents et envoient des messages distincts à l'agent.

### 7.7 Cycle de session diagnostic

**Démarrage** :

1. Utilisateur ouvre un device depuis Home → frontend envoie `connect` sur `/ws/session/{device_id}`
2. Backend crée la session Anthropic (role `diagnostic`, memory store = celui du device)
3. Backend compose et injecte le system prompt 3 couches (§7.4)
4. Événement `session.started` émis, UI affiche panel chat prêt

**Durant la session** :

- Messages utilisateur → `user.message` vers Anthropic
- Agent réponses streamées token par token vers le frontend
- Tool calls interceptés et dispatchés (§7.2 pour `mb_*`, §7.6 pour boardview)
- Chaque event mirroré dans `events` Postgres

**Fin de session — 3 déclencheurs** :

| Déclencheur | Mécanique |
|---|---|
| Agent appelle `mb_save_case(status="resolved"\|"dead_end"\|"abandoned")` | Case écrit en DB + disque, session terminée |
| Utilisateur clique « End session » dans l'UI | UI envoie `end_session` au backend, backend ferme la session Anthropic, Memory Store reçoit automatiquement les learnings de la session (gestion Managed Agents) |
| Timeout inactivité | Deux paliers configurables — cf. ci-dessous |

**Timeouts d'inactivité** :

```python
# api/config.py
SESSION_SOFT_TIMEOUT_MINUTES: int = 45    # UI warning
SESSION_HARD_TIMEOUT_MINUTES: int = 60    # backend force close
```

- **Soft (45 min)** — aucune activité depuis 45 min → le backend émet un event WS `session.inactivity_warning` → UI affiche « Session inactive, will close in 15 min. Move something to keep it open. » L'utilisateur peut cliquer « keep session » pour reset le compteur.
- **Hard (60 min)** — aucune activité depuis 60 min → backend ferme la session Anthropic, `sessions.status = 'abandoned'`, Memory Store reçoit les learnings (Managed Agents auto).

**Rationale** : 30 min était trop court en usage réel atelier (le technicien part chercher un composant, fait une chauffe de 10 min sur un bloc, dessoude patiemment). Une session qui dort sans messages ne consomme **zéro token** — pas de raison d'être agressif sur le timeout.

**Après la fin** :

- Anthropic déclenche la sauvegarde automatique dans le memory store (feature Managed Agents)
- Backend déclenche le **cycle apprenant** (§8) : update `evidence.json`, détection de pattern, synthèse éventuelle d'une rule `learned`
- UI retourne au Home ou affiche un résumé de session (compteurs, case créé, rules proposées)

---

## 8. Cycle apprenant — case → learned rule

### 8.1 Vue d'ensemble

Le cycle apprenant transforme les **cases résolus** en **rules réutilisables**. Il est **hybride stat + LLM** par design : la détection de pattern, l'agrégation des statistiques et l'évolution de la confidence sont du **code pur déterministe** (reproductible, testable, zéro coût API) ; seule la **synthèse de la rule** (étape 4) invoque un LLM, au moment précis où la décision vaut l'investissement.

```
┌────────────────────────────────────────────────────────────────────┐
│ Trigger : fin de session diagnostic (mb_save_case ou end_session) │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
       Étape 1 (code pur) ─────▼──────
       Persistance du case          Postgres cases + disque
                                    memory/{...}/cases/case-NNN-*.{md,json}
                               │
       Étape 2 (code pur) ─────▼──────
       Update evidence.json         Pattern aggregation
                                    (observed_causes, resolution_rate,
                                     total_cases, dead_ends)
                               │
       Étape 3 (code pur) ─────▼──────
       Test seuil de trigger        total_cases ≥ MIN_CASES
                                    AND top_cause.rate ≥ MIN_RATE
                                    AND pattern.promoted_to_rule_id is None
                               │
                      seuil franchi ?
                         │         │
                       non        oui
                         │         │
                         │         ▼
                         │  Étape 4 (LLM) ────
                         │  rule_synthesizer (Opus 4.7)
                         │  tool forcé submit_learned_rule
                         │         │
                         │         ▼
                         │  Rule [LEARNED] ajoutée à rules.json
                         │  confidence initiale 0.55
                         │  needs_validation: true
                         │  pattern.promoted_to_rule_id = rule.id
                         │         │
                         └─────────┤
                                   │
       Étape 5 (code pur) ─────────▼────── (à chaque session future)
       Feedback loop                Ajustement confidence ± 0.05 / ± 0.10
                                    Désactivation auto si < 0.30
```

**Répartition coût** : 90 % du cycle est du code pur ; les 10 % LLM (étape 4) ne se déclenchent qu'au **franchissement du seuil**, soit typiquement **quelques fois par semaine** en usage actif. L'impact budgétaire est négligeable (~0.10–0.20 $ par rule synthétisée, §8.5).

### 8.2 Étape 1 — Persistance du case

Déjà couverte en §7.7 (via `mb_save_case`). Rappel rapide :

- Ligne dans Postgres `cases` (§3.2.8)
- Fichiers `memory/{vendor}/{model}/cases/case-NNN-*.{md,json}` (§4.9)

L'id `NNN` est séquentiel par device (`SELECT COUNT(*) FROM cases WHERE device_id = ? FOR UPDATE` + `+1`). Si deux cases sont créés concurremment sur un même device (rare), le FOR UPDATE sérialise proprement.

### 8.3 Étape 2 — Update `evidence.json` (pattern aggregation)

Algorithme pur Python, idempotent, exécuté **après** l'écriture du case. Lit et réécrit `memory/{vendor}/{model}/evidence.json` (fichier sous git, donc les évolutions sont tracées naturellement).

```python
def update_evidence(device_id: str, case: Case) -> None:
    """Met à jour evidence.json avec le case qui vient d'être résolu."""
    pack_path = memory_bank.path_for(device_id)
    ev = evidence.load(pack_path / "evidence.json")  # modèle Pydantic

    pattern_key = frozenset(case.symptoms)
    pattern = ev.find_pattern(key=pattern_key) or ev.create_pattern(key=pattern_key)

    pattern.total_cases += 1

    if case.status == "resolved" and case.resolution.cause_refdes:
        ref = case.resolution.cause_refdes
        pattern.observed_causes[ref] = pattern.observed_causes.get(ref, 0) + 1
    elif case.status == "dead_end":
        pattern.dead_ends += 1

    # Recompute resolution_rate per cause (rate = observed / total)
    pattern.resolution_rate = {
        ref: count / pattern.total_cases
        for ref, count in pattern.observed_causes.items()
    }
    pattern.last_update = utcnow()

    evidence.save(pack_path / "evidence.json", ev)
```

Shape du fichier rappelée (§4.6) — aucune structure nouvelle introduite ici, juste le code d'update.

### 8.4 Étape 3 — Seuil de déclenchement du `rule_synthesizer`

Test de trigger déterministe, exécuté après chaque update d'`evidence.json`. Trois conditions cumulatives :

```python
def should_synthesize_rule(pattern: Pattern) -> bool:
    if pattern.total_cases < LEARNING_PATTERN_MIN_CASES:
        return False
    top_cause_ref, top_count = pattern.top_cause()
    top_rate = top_count / pattern.total_cases
    if top_rate < LEARNING_PATTERN_MIN_RESOLUTION_RATE:
        return False
    if pattern.promoted_to_rule_id is not None:
        return False    # déjà promu, évite les doublons
    return True
```

Configuration dans `api/config.py` :

```python
LEARNING_PATTERN_MIN_CASES: int            = 3      # minimum de cases pour considérer le pattern
LEARNING_PATTERN_MIN_RESOLUTION_RATE: float = 0.60   # ratio top_cause / total_cases
```

**Pourquoi 3 cas / 60 %** :
- 3 cas est le minimum statistiquement défendable pour parler de « pattern » ; moins = anecdote.
- 60 % garantit que la cause dominante est vraiment dominante (pas un coup de chance sur 2/3 avec répartition équilibrée).
- Les deux valeurs sont tunables en J+3/J+4 si les premiers retours suggèrent un ajustement (trop strict → pas de learned rule émergente, trop laxe → rules prématurées).

Si `should_synthesize_rule` renvoie `True` → Étape 4 se déclenche de façon **asynchrone** (ne bloque pas la réponse UI au mb_save_case). Un event `learning.synthesis_triggered` est émis pour que l'UI Memory Bank affiche un badge « learning a new rule… » en arrière-plan.

### 8.5 Étape 4 — Sub-agent `rule_synthesizer` (Opus 4.7)

| Champ | Valeur |
|---|---|
| `role` | `rule_synthesizer` |
| Modèle | `claude-opus-4-7` |
| Tool forcé | `submit_learned_rule(rule_json)` |
| Inputs | `pattern` complet (JSON) + les ≥ 3 cases markdown concernés + `registry.json` du device (cached) |

**System prompt résumé** :

> Tu synthétises une rule diagnostique [LEARNED] à partir d'un pattern observé dans plusieurs cases résolus. La rule doit :
> 1. Lister les symptômes du pattern (reprendre la liste clé)
> 2. Ranker les `likely_causes` par fréquence observée
> 3. Proposer 1–3 `diagnostic_steps` concrets inspirés des actions des cases
> 4. Citer en `sources` les `case_id` concernés
> 5. Inclure `confidence: 0.55`, `needs_validation: true`, `origin: "learned"`
>
> Tu utilises uniquement les `canonical_name` du registry. Tu soumets via `submit_learned_rule` — aucun texte libre.

**Output** — conforme au schema rules §4.5 avec les champs learned spécifiques :

```json
{
  "id": "rule-pi4-learned-007",
  "origin": "learned",
  "symptoms": ["3V3 rail dead", "device doesn't boot"],
  "likely_causes": [{"refdes": "C29", "probability": 0.78, "mechanism": "short-to-ground"}],
  "diagnostic_steps": [
    {"action": "measure 3V3_RAIL at TP18", "expected": 3.3, "unit": "V"},
    {"action": "if 0V, measure resistance TP18 to GND", "pass_threshold": "> 100 ohm"}
  ],
  "confidence": 0.55,
  "needs_validation": true,
  "sources": ["case:001", "case:007", "case:012"]
}
```

**Action finale de l'étape 4** :
1. Append la rule à `memory/{vendor}/{model}/rules.json`
2. Update `pattern.promoted_to_rule_id = rule.id` dans `evidence.json`
3. Event `learning.rule_synthesized` émis au frontend (notif toast « New learned rule available for [device] »)

**Coût estimé** : ~20–40 k input (3 cases markdown + pattern + registry cached à ~70 %) + ~2–3 k output → **~0.10–0.20 $ par rule**.

### 8.6 Étape 5 — Feedback loop (évolution de confidence)

**Quand** : à la fin de chaque session diagnostic future où une rule `[LEARNED]` a été **invoquée** (retournée par `mb_get_rules_for_symptoms` et visible à l'agent).

**Tracking** : le backend loggue, pour chaque session, la liste des `rule_ids` retournés à l'agent. Stocké dans `sessions.metadata.invoked_rule_ids: list[str]`. Mis à jour à chaque appel `mb_get_rules_for_symptoms` au cours de la session.

**Algorithme post-session** :

```python
def apply_confidence_feedback(session: Session, case: Case | None) -> None:
    """Ajuste la confidence des learned rules invoquées durant la session."""
    for rule_id in session.metadata.invoked_rule_ids:
        rule = rules.load_by_id(session.device_id, rule_id)
        if rule.origin != "learned":
            continue    # les rules core ne subissent pas d'évolution

        delta = 0.0
        if case and case.status == "resolved":
            top_cause = rule.likely_causes[0].refdes
            if case.resolution.cause_refdes == top_cause:
                delta = +0.05   # rule a aidé à résoudre sur la cause qu'elle prévoyait
            else:
                delta = -0.05   # rule a proposé une mauvaise piste (mais session résolue quand même)
        elif case and case.status == "dead_end":
            delta = -0.10       # rule a activement induit en erreur

        rule.confidence = clamp(rule.confidence + delta, 0.0, 1.0)
        if rule.confidence < LEARNING_DISABLE_THRESHOLD:
            rule.disabled = True

        rules.save(session.device_id, rule)
```

Configuration :

```python
LEARNING_CONFIDENCE_DELTA_SUCCESS: float = 0.05
LEARNING_CONFIDENCE_DELTA_WRONG_CAUSE: float = -0.05
LEARNING_CONFIDENCE_DELTA_DEAD_END: float = -0.10
LEARNING_DISABLE_THRESHOLD: float = 0.30
```

**Symétrie des deltas** : les gains sont modestes (+0.05), les pénalités plus sévères (-0.10 pour dead_end) — une rule learned qui induit en erreur doit être désactivée vite. Typiquement il faut ~9 succès consécutifs pour qu'une rule passe de 0.55 à 1.00, mais **3 dead_ends successifs** suffisent à la désactiver (0.55 → 0.45 → 0.35 → 0.25 < 0.30).

### 8.7 Désactivation et réactivation de learned rules

**Désactivation automatique** : `confidence < LEARNING_DISABLE_THRESHOLD` → `rule.disabled = True` dans `rules.json`. Les rules disabled sont **exclues** des résultats de `mb_get_rules_for_symptoms` par défaut (filtré côté backend).

**Réactivation manuelle** : l'utilisateur peut réactiver une rule depuis l'UI Memory Bank > Knowledge > Rules (bouton « Re-enable »). Action simple côté backend : `rule.disabled = False` et reset `confidence = LEARNING_DISABLE_THRESHOLD + 0.05` (soit 0.35) pour relancer un cycle de feedback. Log event `learning.rule_manually_reenabled`.

**Suppression manuelle** : possible aussi, via bouton « Delete rule ». Supprime la rule de `rules.json`, log event `learning.rule_deleted`. Le pattern associé dans `evidence.json` se voit remis à `promoted_to_rule_id: null` pour permettre une éventuelle re-synthèse ultérieure si le pattern continue à grossir.

**Aucune désactivation automatique des rules `core`** — elles viennent du schematic ou du deep research, pas du feedback utilisateur. Une rule core erronée se corrige en régénérant le knowledge pack, pas via le cycle apprenant.

### 8.8 Scénario démo bout en bout — *« I've seen this 3 times »*

Le cycle apprenant est une des **3 narratives clés** de la vidéo démo (avec deep research fallback et pilotage visuel). Storyboard en 4 sessions pré-enregistrées + 1 session live :

| # | Narratif | Setup DB |
|---|---|---|
| 1 | Pre-recorded : session Pi 4 « 3V3 dead, device doesn't boot ». Agent propose U7 en 1ᵉʳ (rule core), user mesure TP17 → 5V OK, PMIC bon. Agent propose C29 en 2ᵉ (rule core probability 0.35), user mesure résistance TP18-GND → 4 Ω = short. Remplace C29. Resolved. | `cases` row, `evidence.json` updated : pattern `{3V3 rail dead, doesn't boot}`, `observed_causes.C29 = 1`, `total_cases = 1` |
| 2 | Pre-recorded : autre Pi 4, même symptômes → même chemin (U7 OK, C29 short), même résolution | `observed_causes.C29 = 2`, `total_cases = 2` — pas encore de trigger (< 3) |
| 3 | Pre-recorded : 3ᵉ Pi 4, même diag rapide (user connaît déjà, arrive avec l'hypothèse). Resolved. | `observed_causes.C29 = 3`, `total_cases = 3`, `rate = 1.0 ≥ 0.60` → **trigger** ! `rule_synthesizer` produit `rule-pi4-learned-001` avec `confidence: 0.55` |
| 4 | **LIVE pendant la démo** : user ouvre une 4ᵉ Pi 4, tape « Pi 4 won't boot, 3V3 rail seems dead » | L'agent répond en temps réel : *« I've seen this 3 times on Pi 4, likely C29 again. Want me to highlight it on the board and measure TP18 first to confirm? »* (invoque la rule learned en 1ᵉʳ rank car confidence 0.55 > top core rule C29 à 0.35 × un facteur d'historique + recency) |

Le point narratif clé : **le même setup n'aurait pas donné cette réponse il y a 10 minutes**, avant que la 3ᵉ session ne promote la rule. L'outil apprend **pendant qu'on l'utilise**, et le narratif vidéo peut le matérialiser par un split-screen (« session 3 closes » ↔ « rule synthesized toast » ↔ « session 4 opens with learned rule as top suggestion »).

**Préparation J-1 démo** : les sessions 1, 2, 3 sont enregistrées avec vrais tokens API (pour avoir les rules réellement promues dans `rules.json`), puis seeded en DB. La session 4 est live, mais l'état `evidence.json` + `rules.json` est déjà dans l'état post-session-3. Aucune simulation : la rule learned qui sert la démo live est le **vrai artefact** produit par le pipeline.

---

## 9. UI layout général

### 9.1 Shell de l'application

Grid fixe, commun à toutes les sections :

```
┌─────────────────────────────────────────────────────────────┐
│ topbar (48px) — brand · crumbs · mode-pill · actions       │
├──┬──────────────────────────────────────────────────────────┤
│R │ metabar (44px) — device id · warn · filters · search    │
│a ├──────────────────────────────────────────────────────────┤
│i │                                                          │
│l │ content-area (section courante, plein espace)           │
│  │                                                          │
│5 │                                                          │
│2 │                                                          │
│p │                                                          │
│x │                                                          │
├──┴──────────────────────────────────────────────────────────┤
│ statusbar (28px) — agent · model · counts · zoom            │
└─────────────────────────────────────────────────────────────┘
```

Le **panel LLM droit** (voir §9.3) se superpose par-dessus `content-area` en mode push quand ouvert.

### 9.2 Rail gauche — liste canonique des sections

**8 boutons** dans l'ordre :

1. **Home** — liste des devices (alias : « Bibliothèque »)
2. **PCB** — boardviewer (intégration Boardviewer spec §11)
3. **Schematic** — viewer PDF + navigation par net
4. **Graphe** — knowledge graph (v3 design déjà porté)
5. **Memory Bank** — 3 onglets Timeline / Knowledge / Stats
6. **Agent** — 4 onglets Config / Historique / Traces / Coûts
7. **Profile** — fiche utilisateur hybride (§7.5 state + §11)
8. **Aide** — raccourcis clavier + lien README (tout en bas du rail)

Au chargement initial, `Home` est actif. L'utilisateur choisit un device → bascule vers `Graphe` (vue la plus visuelle) avec le device sélectionné en query param (`?device=<slug>`).

### 9.3 Panel LLM — push on-demand (décision §Q3 brainstorming)

Panel droit, largeur fixe ~420 px, slides depuis la droite en rétrécissant le `content-area` (pas d'overlay). Toggle via bouton topbar ou raccourci `⌘J`. Fermé par défaut à l'entrée d'une section. Contient :

- Header : nom du modèle actif (Opus 4.7 par défaut), sélecteur (Opus 4.7 / Sonnet 4.6 / Haiku 4.5), bouton fermer
- Stream de messages avec rendering markdown + highlight code
- Affichage des `agent.custom_tool_use` en pastilles cliquables (qui révèlent inputs + tool_result)
- Input bas avec upload fichier + send (Enter)

### 9.4 Cible iPad landscape

- Breakpoint minimum : 1024×768 px (iPad 11" portrait) — layout reste utilisable, rail se réduit à 44 px sans labels
- Optimum : 1366×1024 px (iPad Pro 12.9" landscape) — cible principale, graphe + panel LLM côte-à-côte confortables
- Desktop ≥ 1440 px : panel LLM peut rester ouvert par défaut sans sacrifier le content

### 9.5 Organisation des fichiers

```
web/
├── index.html                     # shell + router (vanilla JS, pas de framework)
├── css/
│   ├── shell.css                  # topbar, rail, metabar, statusbar (partagés)
│   └── sections.css               # styles spécifiques par section
├── js/
│   ├── router.js                  # hash-router #home / #pcb / #graphe / ...
│   ├── api.js                     # wrapper fetch sur /pipeline/*, /devices/*, ...
│   ├── llm-panel.js               # gestion panel LLM droit
│   └── sections/
│       ├── home.js                # Home / Bibliothèque
│       ├── pcb.js                 # Boardviewer (consomme Boardviewer API)
│       ├── schematic.js           # PDF + nets
│       ├── graphe.js              # Knowledge graph (refactor du v3 inline)
│       ├── memory-bank.js         # 3 onglets
│       ├── agent.js               # 4 onglets
│       ├── profile.js             # Formulaire + stats
│       └── aide.js                # Modal raccourcis
└── boardviewer/                   # Agent Boardviewer (cf. §2.7)
```

Zéro build step maintenu : `<script type="module">` pour les imports, CDN pour D3/Alpine/PDF.js.

---

## 10. Sections UI — détail par section

### 10.1 Home / Bibliothèque

Grille de cartes (3–4 colonnes selon largeur), une carte par device connu. Chaque carte :
- Photo / render stylisé du device (fond dégradé + SVG icône famille)
- `display_name` + `vendor/model`
- Badge `origin` (verified from schematic / inferred from public sources)
- Stat : *N sessions · M cases résolus · dernière activité*
- Clic sur carte → navigue vers `#graphe?device=<slug>`
- Bouton « **+ Générer un nouveau device** » → modal avec champ `device_label` (texte libre) + optionnel upload PDF schematic → POST `/pipeline/generate`, suit le stream via `/ws/generation/{job_id}` dans un mini-panel

### 10.2 PCB (Boardviewer)

Consomme le composant `web/boardviewer/` d'Agent Boardviewer. wrench-board fournit uniquement le shell de la section :
- En-tête : device courant, toggle face Top/Bottom, reset view
- Canvas plein écran (Agent Boardviewer)
- Panel latéral droit (collapsable) : liste filtrable des refdes visibles, click → `focus_component`

Le panel LLM droit fonctionne par-dessus, comme ailleurs.

### 10.3 Schematic

Viewer PDF (PDF.js) + colonne droite avec :
- Liste des pages + miniatures
- Recherche par `net_canonical_name` → highlight sur la page courante
- Onglet secondaire : liste des nets extraits (cf. `api/schematic/net_extraction.py`)

Tool UI dédié : `open_schematic_page(page)`, `highlight_schematic_net(net_name)`.

### 10.4 Graphe (knowledge graph)

Refactor du design v3 déjà porté dans `web/index.html` inline → extraction vers `web/js/sections/graphe.js`. Data loadée via `GET /pipeline/packs/{slug}/graph` (nouvel endpoint §12). Empty state conservé si pas de device.

### 10.5 Memory Bank — 3 onglets

- **Timeline** : liste des sessions du device (`GET /sessions?device_id=...`), expandable en détail (events, tool calls, résumé).
- **Knowledge** : browser des fichiers du pack (registry.json, architecture.md, rules.json, dictionary.json) avec viewer JSON/Markdown et bouton « régénérer ce fichier » (déclenche `run_single_writer_revision`).
- **Stats** : 4 tuiles (`quality.consistency_score`, `intrinsic_effectiveness`, `cases_resolved`, `cost_total_usd`) + mini-graph de l'évolution de la qualité au fil des regénérations.

### 10.6 Agent — 4 onglets

- **Config** : sélecteur modèle (`ANTHROPIC_MODEL_MAIN`), system prompt preview (read-only), toggle tools disponibles.
- **Historique** : liste des sessions diagnostic passées (séparément des generation jobs), lien vers replay.
- **Traces** : table `subagent_calls` requêtable, durée par rôle, failures highlighted.
- **Coûts** : cf. §11.3, graphiques et totaux.

### 10.7 Profile

Deux colonnes :
- **Gauche — manuel** : formulaire éditable (`display_name`, `declared_level` radio, `declared_skills` liste avec ajout).
- **Droite — dérivé** : tuiles de stats calculées depuis `cases` (niveau estimé, spécialisations détectées, avg resolution time).

### 10.8 Aide

Modal sur `⌘?` ou clic bouton Aide. Liste des raccourcis (`⌘K` search, `⌘J` toggle panel LLM, `Esc` close inspector, `1`-`8` pour naviguer au rail), lien vers README GitHub.

---

## 11. Profil & cost tracking

### 11.1 Modèle de profil hybride

```python
class Profile(BaseModel):
    display_name: str = "Technician"
    declared_level: Literal["beginner", "intermediate", "advanced", "expert"] | None
    declared_skills: list[SkillEntry]
    derived_cache: ProfileDerivedCache
```

Le `derived_cache` est recalculé à la demande (pas à chaque `GET`) :
- `estimated_level` : fonction de `cases_resolved`, `avg_resolution_time_min`, complexité (devices touchés dont `confidence_cap`)
- `specializations` : agrégation par type de composant touché dans les cases résolus
- `last_recomputed` : timestamp

L'agent diagnostic lit `declared_level OR estimated_level` pour adapter son niveau de guidage (plus pédagogique pour beginner, plus direct pour expert — consigne explicite dans le system prompt §7.4 couche 1).

### 11.2 Pricing table (référence)

Expose dans `api/telemetry/pricing.py` :

```python
PRICING = {
    "claude-opus-4-7":  {"input":  5.00, "output": 25.00,
                         "cache_write_5m": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-sonnet-4-6":{"input":  3.00, "output": 15.00,
                         "cache_write_5m": 3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    "claude-haiku-4-5": {"input":  1.00, "output":  5.00,
                         "cache_write_5m": 1.25, "cache_write_1h":  2.00, "cache_read": 0.10},
}
WEB_SEARCH_USD_PER_CALL = 0.01
```

Prix en USD par million de tokens. `pricing_snapshot` JSONB de `cost_tracking` stocke cette table au moment du calcul (§3.2.9) → audit possible si Anthropic change ses tarifs.

### 11.3 Usage accounting

À chaque appel Anthropic, `api/telemetry/usage.py::record_usage()` INSERT une ligne dans `cost_tracking` avec :
- Tokens bruts (`input`, `output`, `cache_creation`, `cache_read`)
- Modèle + rôle (`diagnostic`, `registry_builder`, ...)
- Coût calculé en USD avec `PRICING` × snapshot
- Lien vers `session_id` + `subagent_call_id`

### 11.4 Onglet Coûts UI

Dans la section Agent :
- **Hero** : total session / total projet (gros chiffres)
- **Par modèle** : donut chart répartition Opus/Sonnet/Haiku
- **Par phase** : bar chart vertical des phases pipeline + diagnostic
- **Cache savings** : `(cache_read_tokens × normal_price - cache_read_tokens × cache_price)` cumulé, affiché en « Économies grâce au cache »
- **Table brute** : dernières 50 lignes de `cost_tracking`, filtrables par `agent_role` / `model`

---

## 12. Plan de livraison J+0 → J+5 avec gates

| Jour | Date | Objectifs | Gate fin de journée |
|---|---|---|---|
| **J+0** | 2026-04-21 (lun) ✅ | Scaffolding FastAPI + spec V1 brainstorming (§1–§8) | Repo lançable (`make run`, tests verts), spec rédigé |
| **J+1** | 2026-04-22 (mar) ✅ | Pivot V2 « knowledge factory » + pipeline 4 phases + port du knowledge graph v3 | Pipeline compile + endpoints montés, UI knowledge graph empty-state visible |
| **J+2** | 2026-04-23 (mer) | **Endpoint `/pipeline/packs/{slug}/graph`** + **Home/Bibliothèque** + **boardviewer Tier-1 integration** (avec Agent B) + **premier run réel pipeline sur Pi 4** (quand crédits API reçus) | Un device généré visible dans Home, clic charge le graphe |
| **J+3** | 2026-04-24 (jeu) | **Diagnostic agent** (`messages.create` simple, pas Managed Agents pour hackathon) + **3 premiers tools `mb_*`** (`get_component`, `get_rules_for_symptoms`, `save_case`) + **panel LLM** fonctionnel | Conversation live : *« où est le PMIC ? »* → réponse avec refdes validé + highlight boardview |
| **J+4** | 2026-04-25 (ven) | **Tournage Framework Laptop 13** (matin — cf. §1.3) · **iPhone X deep-research run** · **3 sessions seed** du cycle apprenant (§8.8) · **polish UI** des sections Memory Bank + Agent + Coûts | Scénario démo tournable bout en bout sans coupure |
| **J+5** | 2026-04-26 (sam) | **Montage vidéo 3 min** (ElevenLabs VO anglaise) · **résumé 100–200 mots** · **commit README final** · **submission Cerebral Valley avant 20:00 EST** | Vidéo + repo public + résumé soumis |

**Gates explicites — si raté, action d'urgence définie :**
- Fin J+1 : V2 pipeline ne tourne pas → report API call sur fixtures statiques mock + MVP graph-only
- Fin J+2 : Managed Agents bloque ou crédits non reçus → `LocalOrchestrator` (§6.8) + mock pack Pi 4 à la main
- Fin J+3 : diagnostic agent inutilisable → démo « knowledge factory only », skip la partie conversation
- Fin J+4 : démo non tournable → backup narratif « pipeline + graphe » sans boardviewer live

---

## 13. Risques & mitigations

| Risque | Impact | Probabilité | Mitigation |
|---|---|---|---|
| **Managed Agents beta instable** | Pipeline échoue, impossible de générer pack | Moyenne | Interface `SubAgentOrchestrator` permet bascule `LocalOrchestrator` en ~2-3 h (cf. §6.8 + plan détaillé ci-dessous). V2 déjà livré utilise `messages.create` direct — ce risque n'affecte que la migration future. |
| **Rate limit `web_search_20250305`** | Phase 1/3 deep research ralentit ou échoue | Moyenne | Cache Postgres `web_search_cache` TTL 7 j. Permet de re-rouler la démo sans repayer. En urgence : pré-générer les résultats web de la démo avant J+5 et les injecter. |
| **Scope creep UI** (envie d'ajouter des sections) | Ne pas finir la démo | Élevée | Liste canonique des 8 sections §9.2 figée. Toute demande d'ajout → post-hackathon. |
| **Contenu propriétaire** (schematic Apple dans repo) | Disqualification hackathon | Basse | Checklist `CLAUDE.md` règle #4 + review git diff avant chaque commit sur `memory/apple/*`. Dossier `memory/apple/iphone-x/sources/` reste vide (cf. §4.1). |
| **Budget 500 $ crédit API épuisé** | Impossible de tourner la démo | Moyenne | Prompt caching 3 couches (§5.10) obligatoire. Haiku pour Phase 5 (facts) et classification symptômes §7.4. Monitoring via onglet Coûts — alarm si > 80 % budget. |
| **Pi 4 physique non-disponible pour tournage live** | Pas de B-roll board réelle | Élevée | Plans statiques atelier + microscope USB + interface capturée en screen-record. Storyboard ne dépend pas d'une board filmée live. |
| **Anglais parlé insuffisant pour voix off démo** | Vidéo non-soumettable | Basse | **ElevenLabs** voice generation, scripts écrits en amont (J+4 soir). |
| **Crédits API Anthropic pas reçus avant J+2** | Pipeline non-testable en réel | Moyenne | Crédits perso en backup, fixture mock pack Pi 4 pour débloquer frontend. |

### 13.1 Plan de bascule `LocalOrchestrator` détaillé

Si à J+2 le bootstrap Managed Agents échoue (4xx persistants, beta header rejeté, etc.), bascule en ~2-3 h :

**Étape 1** — créer `api/orchestration/local.py` :

```python
class LocalOrchestrator(SubAgentOrchestrator):
    """Fallback implementation using messages.create directly.
    No managed agents, no beta. Each sub-agent = one messages.create call
    with role-specific system prompt + forced tool."""

    def __init__(self, client: AsyncAnthropic, role_configs: dict[str, RoleConfig]):
        self._client = client
        self._roles = role_configs  # role → {model, system_prompt, tools}

    async def run_registry_builder(self, *, session_id, device_id, sources, mode):
        config = self._roles["registry_builder"]
        response = await self._client.messages.create(
            model=config.model,
            max_tokens=16000,
            system=config.system_prompt,
            messages=self._compose_registry_user_message(sources, mode),
            tools=config.tools,
            tool_choice={"type": "tool", "name": "submit_registry"},
        )
        return self._extract_and_validate(response, Registry)
    # ... mêmes wrappers pour les 6 autres rôles
```

**Étape 2** — bascule runtime via env var `ORCHESTRATION_MODE=local` :

```python
# api/orchestration/__init__.py
def get_orchestrator(client: AsyncAnthropic) -> SubAgentOrchestrator:
    mode = os.environ.get("ORCHESTRATION_MODE", "managed")
    if mode == "local":
        return LocalOrchestrator(client, load_role_configs())
    return ManagedAgentOrchestrator(client, load_agent_ids())
```

**Étape 3** — mentionner dans README : *« Pipeline runs on Claude API + tool use (local orchestration) for hackathon reliability; production target is Managed Agents. »*

Temps estimé : 2-3 h grâce à l'interface `SubAgentOrchestrator` déjà définie.

---
