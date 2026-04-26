# Spécification — Mode Guidé (Workspace par Repair)

**Date :** 25 avril 2026
**Projet :** `wrench-board`
**Auteur :** Brainstorm Alexis ↔ Claude Code (Opus 4.7)
**Échéance d'implémentation :** dimanche 26 avril 20:00 EST (lundi 02:00 FR)
**Statut :** validé pour implémentation

---

## 1. Contexte et objectif

### 1.1 Problème

L'UI actuelle (`web/index.html` + 8 sections rail-routées) est une **workbench
pro-tool** assumée — dense, dark, mono-typo pour les refdes, palette OKLCH
sémantique. C'est cohérent avec la cible « microsoldering technician » de
`CLAUDE.md`, mais **ça crame l'effet right-to-repair** : un amateur qui ouvre
l'app voit un schéma KiCad et abandonne en 5 secondes.

La vision right-to-repair (« redonner la réparabilité à tous les
réparateurs ») demande une surface accessible. La vision hackathon (« Built
with Opus 4.7, jury technique Anthropic × Cerebral Valley ») demande une
densité technique impressionnante. **Les deux audiences existent simultanément
dimanche soir.**

### 1.2 Décision

Deux modes coexistent dans le même shell `web/index.html`, pilotés par un
toggle dans le topbar :

- **Mode guidé** (défaut, audience amateur + jury au premier coup d'œil) —
  paradigme « workspace style Claude.ai » centré sur le repair courant.
  Chat plein écran, widgets inline pop par l'agent, sidebar conversations,
  onglets repair-scoped.
- **Mode expert** (toggle ⚙, audience pro / jury technique au creusage) —
  réintroduit le rail global gauche et les vues workbench classiques
  (`#pcb`, `#schematic`, `#graphe`, `#memory-bank`, `#profile`, `#aide`).
  Aucune perte de fonctionnalité.

### 1.3 Métaphore directrice

**Le repair est un workspace persistant**, exactement comme une conversation
Claude.ai est un workspace persistant. La sidebar gauche liste *mes
diagnostics* (= autres repairs) et *les conversations sous le repair courant*
(= le multi-conv déjà câblé sous `memory/{slug}/repairs/{rid}/conversations/`).

Aucune page « accueil » globale en mode guidé : l'entrée est la landing, et
le retour à un autre repair se fait via la sidebar. La page `#home` actuelle
disparaît du flux guidé (elle reste accessible en mode expert via le rail).

---

## 2. Public visé

| Public | Mode par défaut | Ce qu'il voit en premier |
|---|---|---|
| Réparateur amateur (Pine64, Framework, RPi, etc.) | Guidé | Landing : « Décris ce qui ne marche pas ». Aucun jargon. |
| Pro microsoldering | Guidé puis bascule expert d'un clic | Idem, puis tout le workbench dense reste à 1 clic. |
| Jury Anthropic / Cerebral Valley | Guidé pour le storytelling, démo bascule expert pour le wow | Comprend l'app en 5 s, voit la profondeur en 30 s. |

**Aucun public n'est sacrifié.** Le mode expert préserve à 100 % l'expérience
actuelle ; le mode guidé est un sur-couche, pas un remplacement.

---

## 3. Architecture

### 3.1 Shell unique, deux états CSS

```
web/index.html
  └── body
      ├── (landing overlay — affiché si #landing ou aucun repair courant)
      └── div#shell
          ├── topbar  (contextuelle au repair en mode guidé, riche en mode expert)
          ├── div#workspace
          │   ├── aside#guided-sidebar  (visible en mode guidé)
          │   │   ├── section "Mes diagnostics"   (switcher de repairs)
          │   │   └── section "Conversations"     (switcher conv courant)
          │   ├── aside.rail (8 sections — visible en mode expert uniquement)
          │   ├── nav.metabar (visible en mode expert uniquement)
          │   └── main#main-area
          │       ├── (mode guidé) chat plein écran avec widgets inline
          │       └── (mode expert) sections classiques inchangées
          └── footer.statusbar
```

État global piloté par classes sur `<body>` :

- `body.guided-mode` (par défaut)
- `body.expert-mode` (après clic sur ⚙ — persisté en `localStorage`)

Aucun changement de routeur, aucun nouveau hash. Le hash routing
(`#home`, `#pcb`, etc.) **continue de fonctionner en mode expert**. En mode
guidé, le hash est ignoré pour la navigation principale (on est toujours
dans le workspace du repair courant) ; le hash sert uniquement de scope
secondaire (`#schema` ouvre l'onglet schéma de la topbar).

### 3.2 Surfaces touchées

| Fichier | Modification |
|---|---|
| `web/index.html` | Ajouter overlay landing + structure sidebar guidée + onglets topbar repair-scoped. ~150 lignes ajoutées. |
| `web/js/main.js` | Détection mode initial, gating landing, transition landing → workspace. ~40 lignes ajoutées. |
| `web/js/router.js` | Helper `setMode(mode)`, persistance localStorage, no-op du hash routing en mode guidé. ~30 lignes ajoutées. |
| `web/js/llm.js` | Promotion du popover conversations en sidebar permanente, rendu inline des widgets agent dans le fil chat. ~120 lignes ajoutées. |
| `web/js/landing.js` (nouveau) | Logique landing : champ texte, soumission, classifier call, widget de confirmation, création repair. ~180 lignes. |
| `web/styles/guided.css` (nouveau) | Tous les styles du mode guidé. Aucun override de `layout.css` / `llm.css` / `brd.css` existants. ~400 lignes. |
| `web/styles/landing.css` (nouveau) | Styles landing hero (photo board floue + champ + animation). ~100 lignes. |
| `api/pipeline/__init__.py` | Endpoint `POST /pipeline/classify-intent` — Haiku forced-tool, retourne `{candidates: [{slug, label, confidence}]}`. ~60 lignes. |
| `api/pipeline/intent_classifier.py` (nouveau) | Module métier classifier (Pydantic schemas + appel Haiku). ~120 lignes. |
| `tests/pipeline/test_intent_classifier.py` (nouveau) | Tests unitaires (mock Anthropic). ~80 lignes. |

**Total estimé : ~1 280 lignes ajoutées, ~0 ligne réécrite.**

### 3.3 Surfaces *non* touchées (volontaire)

- `api/agent/` (runtime managed/direct, sanitize, manifest, tools, memory_seed) — zéro changement.
- `api/board/` (parser, validator, model, router) — zéro changement.
- `api/pipeline/` hors `__init__.py` et le nouveau module classifier — zéro changement.
- `api/pipeline/schematic/` (simulator, hypothesize, compiler, etc.) — zéro changement. La règle CLAUDE.md « ne pas refactorer simulator.py / hypothesize.py pour le style » s'applique.
- `web/brd_viewer.js` — réutilisé tel quel pour le widget mini-board (rendu dans un container plus petit grâce à l'API publique `window.Boardview`).
- `web/js/schematic.js` (3 520 lignes), `web/js/graph.js`, `web/js/memory_bank.js`, `web/js/profile.js`, `web/js/pipeline_progress.js`, `web/js/home.js` — inchangés. En mode guidé ils s'ouvrent uniquement via les onglets topbar repair-scoped (qui basculent en mode workbench-detail).
- `web/styles/layout.css`, `llm.css`, `brd.css`, `schematic.css`, `graph.css`, `memory_bank.css`, etc. — inchangés.
- `tokens.css` — inchangé. Tous les nouveaux styles consomment les tokens existants.
- Pipeline, simulateur, hypothesize, evolve loop, benchmark — zéro changement.

---

## 4. Comportement

### 4.1 Landing hero

**Déclencheur** : l'utilisateur ouvre l'URL racine *et* aucun `repair` n'est
activé en localStorage *et* le mode est `guided`. Sinon → workspace direct.

**Visuel** :
- Plein écran, fond `--bg-deep` avec une photo de circuit floue
  (CSS `filter: blur(8px) brightness(0.4)`) en couche basse — image
  `web/assets/board-blur.jpg` (open-source, commitable, à sourcer Unsplash
  CC0 ou photo perso CC).
- Centré : titre `Wrench Board` (Inter 32 px), sous-titre
  « Ton assistant de réparation hardware ».
- Champ unique de saisie : `placeholder="Décris ce qui ne marche pas — ex. 'mon Framework ne s'allume plus'"`.
- Bouton « Diagnostiquer » (ou submit on Enter).
- Sous le champ : 3 chips d'exemples cliquables qui pré-remplissent le
  champ : `"MNT Reform — pas de boot"`, `"iPhone 11 ne charge plus"`,
  `"Framework écran noir"`. Donne au jury l'ouverture parfaite.

**Logique de soumission** :
1. POST `/pipeline/classify-intent` avec `{text: "..."}`.
2. Backend appelle Haiku forced-tool, schéma de sortie :
   ```python
   class IntentCandidate(BaseModel):
       slug: str  # ex. "mnt-reform-motherboard"
       label: str  # ex. "MNT Reform — carte mère"
       confidence: float  # 0..1
       pack_exists: bool  # vérifié contre memory/{slug}/
   class IntentClassification(BaseModel):
       symptoms: str  # ce que l'utilisateur a décrit, normalisé
       candidates: list[IntentCandidate]  # 1..3, triés par confiance
   ```
3. Backend matche les `slug`s candidats contre les packs présents sur
   disque (via `pipeline.packs.list_packs()`), set `pack_exists`.
4. Retourne 200 OK avec la classification.

**Réception côté client** :
- Si `candidates` a au moins 1 élément avec `pack_exists=true` *et*
  `confidence >= 0.7` → bascule directement vers le workspace en passant
  par la création de repair (POST `/pipeline/repairs`), sans demander
  confirmation. L'agent confirmera lui-même au premier message
  (« Je crois que tu parles de **MNT Reform**, je commence là-dessus —
  dis-moi si je me trompe »).
- Sinon (confidence < 0.7 ou aucun pack existant) → bascule workspace,
  premier message de l'agent = widget de confirmation avec les 1-3
  candidats sous forme de cards cliquables. Si l'utilisateur clique
  « aucun de ceux-ci », l'agent demande des précisions.

**Pas de pack-gen automatique** pendant la démo (coût + temps). Si le
candidat a `pack_exists=false`, l'agent dit honnêtement
« Je n'ai pas encore de fiche pour ton **iPhone 11** — je peux la
construire en arrière-plan (~2 min) ou on continue sans, tu choisis ».

### 4.2 Workspace repair

Une fois la landing franchie, l'utilisateur arrive dans le workspace du
repair. URL : `/?repair={rid}`. Le mode est `guided` par défaut, l'état
est persisté en `localStorage` (`wrench_board.mode`).

**Topbar** (48 px) :
- Gauche : logo `⚡ Wrench Board` (clic = retour landing si zéro repair courant, sinon switch repair via sidebar).
- Centre : nom du repair (« MNT Reform — pas de boot ») éditable inline au double-clic.
- Centre droit : 3 onglets `Memory | Schéma | Graphe`. Clic sur un onglet → bascule en **mode workbench-detail** sur la section correspondante (cf. §4.5).
- Droite : pill de coût ($X.YY total session), bouton ⚙ qui toggle le mode expert.

**Sidebar gauche** (220 px, permanente en mode guidé, masquée en mode expert au profit du rail 52 px + métabar) :
- Section haute « Mes diagnostics » : liste paginée (top 20) des repairs sur disque, groupés brand > model. Le repair courant est mis en valeur. Bouton « + nouveau » en bas → re-ouvre la landing en modal.
- Section basse « Conversations » : alimentée par l'API existante `/pipeline/repairs/{rid}/conversations`. Liste les conversations sous le repair courant, groupées « Aujourd'hui / Hier / Cette semaine / Plus ancien ». La conv courante est highlightée. Bouton « + nouvelle conversation » crée un nouveau `conv_id` (utilise le mécanisme déjà câblé dans `llm.js`).

**Centre — chat plein écran** :
- Le panel chat existant de `web/js/llm.js` est rendu en pleine largeur (au lieu d'être confiné à la section `#agent`).
- Le rendu Markdown safe (marked + DOMPurify) est conservé.
- Le sanitizer post-hoc des refdes (`api/agent/sanitize.py`) est conservé.
- **Nouveauté** : les tool calls de l'agent (`bv_*`, `mb_schematic_graph`) déclenchent l'apparition d'un **widget inline** dans le fil de chat, juste après le message qui les a appelés (cf. §4.3).

### 4.3 Widgets inline (MVP : board + schéma)

Quand l'agent appelle un tool qui doit afficher quelque chose, un widget
inline apparaît dans le fil de conversation.

**Mapping tool → widget** :

| Tool agent | Widget inline généré | Source de données |
|---|---|---|
| `bv_highlight_component`, `bv_focus_component`, `bv_highlight_net`, `bv_flip_board`, `bv_annotate`, `bv_filter_by_type`, `bv_draw_arrow`, `bv_measure_distance`, `bv_show_pin`, `bv_dim_unrelated`, `bv_layer_visibility`, `bv_reset_view` | **Mini-boardview** : container 480×320 px qui instancie `window.Boardview` avec l'état actuel (highlights, focus, layer). | `brd_viewer.js` réutilisé tel quel. |
| `mb_schematic_graph(query="simulate", ...)` | **Mini-schématique simulateur** : container 480×320 px avec la timeline du simulator (phases + composants morts par phase). | Fonction d'embed à extraire de `web/js/schematic.js` (sans modifier le module — juste exposer une API publique `window.SchematicMini.render(container, payload)`). |
| `mb_get_component`, `mb_get_rules_for_symptoms`, `mb_list_findings`, `mb_record_finding`, `mb_expand_knowledge` | **Card de résultat structurée** (refdes + role + confidence + lien « voir en détail »). | Rendu HTML simple, lecture du tool_result JSON. |
| `mb_schematic_graph(query="hypothesize", ...)` | **Card top-N candidats** : liste des refdes rangés par F1 score, avec preview cascade. | Lecture du tool_result. |

**Comportement commun à tous les widgets** :
- Bordure 1 px `--border`, fond `--panel`, padding 12 px, border-radius 8 px.
- Header sobre : titre du widget + bouton « voir en détail » à droite.
- **« Voir en détail »** → bascule en mode workbench-detail (§4.5) sur la section concernée, **sans changer de mode** (mode `guided` reste actif). Le retour ramène au chat.
- Animation : fade-in 200 ms à l'arrivée, sans saut visuel.

**Hors-MVP (mode placeholder seulement)** : les outils `mb_get_*` qui
ouvrent le graphe ou le memory bank ne génèrent pas de mini-graph ou de
mini-memory-bank inline. Ils génèrent une **card placeholder** (« Voir
le graphe de connaissances ↗ ») qui en clic bascule en
workbench-detail sur la section correspondante.

### 4.4 Toggle mode expert

Bouton ⚙ dans le topbar. Au clic :
1. `body.classList.toggle("guided-mode"); body.classList.toggle("expert-mode")`.
2. `localStorage.setItem("wrench_board.mode", currentMode)`.
3. Le rail 52 px + métabar 44 px réapparaissent (CSS conditionnel sur `.expert-mode`).
4. La sidebar guidée 220 px se rétracte à un mini-rail conv (40 px, visible verticalement, icône + tooltip).
5. Le centre rebascule en `#agent` panel actuel (chat sidebar + centre par défaut sur le `#pcb` ou la dernière section visitée).
6. Pas de rechargement, pas de re-fetch, pas de reset de la conversation. C'est un *changement de chrome*, pas un changement de contexte.

Au reload, le mode est restauré depuis `localStorage`. Premier visiteur =
mode `guided`.

### 4.5 Mode workbench-detail (intra-mode-guidé)

Quand l'utilisateur clique « voir en détail » sur un widget *ou* sur un
onglet repair-scoped (Memory / Schéma / Graphe) du topbar, le centre
bascule temporairement en vue plein workbench :
- Le chat se rétracte en sidebar droite 320 px.
- La section concernée occupe le reste (board, schématique, graphe ou memory bank).
- Bouton « ← retour conversation » en haut à gauche du centre, qui re-déploie le chat plein écran.

C'est exactement le pattern de la maquette « Mode expert » qu'on a vue
dans le brainstorm — sauf qu'on reste en mode guidé (rail toujours
caché). Implémentation : classe `body.detail-view` + nom de la section
en `body.dataset.detailSection`. CSS conditionnel.

---

## 5. Endpoint classifier d'intention

### 5.1 Route

`POST /pipeline/classify-intent`

**Body** :
```json
{ "text": "MNT Reform ne démarre plus, écran noir" }
```

**Réponse 200** :
```json
{
  "symptoms": "MNT Reform — pas de boot, écran noir",
  "candidates": [
    { "slug": "mnt-reform-motherboard", "label": "MNT Reform — carte mère", "confidence": 0.92, "pack_exists": true },
    { "slug": "iphone-x-logic-board",   "label": "iPhone X — carte mère",   "confidence": 0.04, "pack_exists": true }
  ]
}
```

### 5.2 Implémentation

`api/pipeline/intent_classifier.py` :
- Module pur, dépend de `anthropic` et `pipeline.tool_call.call_with_forced_tool`.
- Modèle : `claude-haiku-4-5` (rapide, peu cher).
- Prompt système court et stable (cache-controlled). Liste les packs existants par leur `slug` + `label`, obtenus via le handler existant `list_packs()` dans `api/pipeline/__init__.py` (extraction d'un helper réutilisable si besoin — décision à prendre dans le plan d'implémentation).
- Tool forcé : schéma `IntentClassification`.
- Validation Pydantic à la sortie. Garantit `len(candidates) <= 3`, tri par confidence décroissante.
- Pas de fallback dégradé : si l'appel Haiku échoue, retourne 503 avec
  un message clair. Le frontend bascule en mode B (dropdown classique
  dans la landing, listant les packs disponibles).

### 5.3 Tests

`tests/pipeline/test_intent_classifier.py` (rapide, sans `@pytest.mark.slow`) :
- Mock du client Anthropic.
- Cas heureux : 1 candidat haute confidence → retour OK.
- Cas ambigu : 3 candidats à confidence proche → retour OK trié.
- Cas vide : aucun pack matche → retour avec liste vide.
- Cas Haiku raise : remonte 503.
- Validation Pydantic : `len > 3` rejeté.

---

## 6. Stratégie de copie

Le mode guidé doit *parler comme un humain*, pas comme une console pro.
Réécriture ciblée des strings principales (en français, conformément à
`CLAUDE.md` § Frontend design » Copy »).

| String actuelle | Nouvelle string (mode guidé) |
|---|---|
| « Bibliothèque » | « Mes diagnostics » |
| « Graphe de connaissances » | « Ce que je sais de cet appareil » |
| « Memory Bank » | « Fiche appareil » |
| « Démarrer diagnostic » (modale home) | (supprimé — landing remplace) |
| « Tier : deep / normal / fast » | « Profondeur : approfondie / normale / rapide » |
| Status bar « cost: $X » | « ~ $X cette session » |
| Boutons rail (8 sections) | Inchangés (visibles en mode expert seulement) |

**Le mode expert garde les strings actuelles** — c'est le « back-stage »
du pro, pas besoin de l'humaniser.

Liste exhaustive des strings à modifier vivra dans le plan
d'implémentation, pas dans cette spec (~30 strings au total).

---

## 7. Scénario de démo (3 min)

```
0:00  [Landing]  Le présentateur tape :
                   "MNT Reform — pas de boot, écran noir"
                 Hit Enter.
0:05  Classifier renvoie {mnt-reform-motherboard, conf 0.92, pack_exists}.
      Workspace s'ouvre direct (confiance > 0.7), conv créée.
0:10  Agent (premier message, streamé) :
        "Salut. Je crois que tu parles de la MNT Reform —
         carte mère CERN-OHL-S. Je commence là-dessus.
         Dis-moi ce que tu observes en plus."
0:25  Présentateur tape : "pas de musique de boot non plus"
0:30  Agent appelle hypothesize(observations=[screen_dead, audio_dead])
0:40  Card top-3 candidats inline : U7 · D9 · U13, rangés par score
      (le score brut reste lisible au survol — pas affiché en gros pour
      ne pas perdre un amateur).
      Agent : "U7 est mon premier suspect — c'est le PMIC principal.
      Je te le montre."
0:50  Agent appelle bv_focus_component("U7")
      Mini-boardview inline avec U7 highlighté + zoom auto.
1:05  Présentateur clique "voir en détail" sur le widget board.
      Bascule workbench-detail : board pleine taille, chat sidebar droite.
1:15  Présentateur revient sur le chat plein écran.
1:20  Présentateur clique sur l'onglet "Schéma" du topbar.
      Workbench-detail s'ouvre sur le simulateur timeline.
      Phase 4 (PMIC enable) : U7 dead → cascade rails 1V8 / 3V3 / VBAT_SW.
1:40  Retour au chat.
1:45  Agent : "Action recommandée : sonder VBAT en TP3 sur U7,
              attendu 4.2V — si à 0V, U7 est mort.
              Voici la procédure étape par étape :"
              [card avec étapes numérotées]
2:15  Présentateur : "et si je passe en mode expert ?"
      Clique ⚙. Le rail 8 sections apparaît, sidebar conv rétrécit,
      board reste plein. Le jury voit "ah, en dessous c'est ça."
2:35  Bascule retour mode guidé. "Voilà, deux modes, même app."
2:50  Closing.
```

**Repli si l'agent est lent** : pré-charger le pack en cache chaud avant
la démo (un tour à blanc juste avant). Coût attendu : $1-2 sur 3 min.

---

## 8. Hors-scope (volontaire)

- **Refonte du `#schematic`** (1 215 lignes CSS + 3 520 lignes JS). Trop gros, trop risqué à T-36h. On se contente d'embed via une API publique.
- **Multi-conv côté backend** — déjà implémenté, on ne touche pas.
- **Pack-gen live** d'un device inconnu pendant la démo — coût + temps prohibitifs (cf. project-snapshot §3).
- **Refactoring `simulator.py` / `hypothesize.py`** — interdit par `CLAUDE.md` (territoire evolve loop).
- **Authentification, multi-utilisateur, partage de repairs** — projet local mono-tech.
- **Mobile / responsive** — workbench desktop uniquement. Le mode guidé serait un bon point de départ post-hackathon, mais hors-scope ici.
- **Onboarding tutoriel** (« comment ça marche »). On parie sur la lisibilité immédiate.
- **Internationalisation** — UI 100 % français, CLAUDE.md règle confirmée.

---

## 9. Risques et plans B

### 9.1 Risque : classifier retourne du n'importe quoi

**Mitigation** : seuil `confidence >= 0.7` pour auto-confirmation. Sinon
widget de confirmation explicite (l'utilisateur clique). Si Haiku
hallucine un slug inexistant, `pack_exists=false` côté backend filtre,
et l'agent demande des précisions plutôt que de planter.

**Plan B** : si l'endpoint classifier crash (e.g. quota Anthropic
saturé), le frontend détecte le 5xx et bascule la landing en mode
« dropdown classique » (option B de Q5 du brainstorm) : champ symptôme +
select des packs disponibles.

### 9.2 Risque : `window.Boardview` ne supporte pas l'embed petit

À vérifier avant d'écrire le widget. Si le viewer suppose une layout
plein écran, on ajoute un mode `embed` qui désactive les contrôles non
essentiels et tient en 480×320. Cf. `brd_viewer.js` exposant déjà
`window.Boardview` (commit `7a44108`) — l'API publique est censée
supporter ça.

### 9.3 Risque : extraction d'API mini-schématique

Le widget mini-schémathique demande d'exposer une API publique sur
`web/js/schematic.js` (3 520 lignes). C'est le point le plus fragile.

**Plan B graceful** : si on n'arrive pas à extraire proprement, le
widget « simulateur » devient une **card placeholder** comme le graphe
et le memory bank — l'utilisateur clique « voir en détail » et atterrit
sur le `#schematic` actuel. On perd le wow widget inline pour le
simulateur, mais on garde le pattern et la démo reste solide.

### 9.4 Risque : démo cassée par l'evolve loop

La loop `microsolder-evolve` modifie `simulator.py` / `hypothesize.py`
toute la nuit. Si elle introduit une régression vendredi soir → samedi
matin alors que la démo est dimanche soir, on est en danger.

**Mitigation** : **stopper la loop dimanche matin au plus tard**, comme
recommandé par project-snapshot §9. Tagger un commit stable
(`pre-demo-freeze`) qu'on peut restaurer en 30 secondes.

### 9.5 Risque : CDN externes inaccessibles le jour de la démo

L'UI dépend de `d3js.org`, `cdn.jsdelivr.net` (marked + DOMPurify),
`fonts.googleapis.com`. Si le wifi du lieu de démo filtre, le frontend
casse.

**Mitigation** : tester en hotspot 4G la veille. Plan B : copie locale
des assets dans `web/vendor/` avec un script `make pin-cdn` (15 min de
travail, à programmer dans le plan d'implémentation comme dernier
item).

### 9.6 Risque : Managed Agents API beta tombe pendant la démo

**Mitigation** : variable `DIAGNOSTIC_MODE=direct` + redémarrage uvicorn
en 5 s. À documenter dans `Makefile` comme `make demo-fallback`.

---

## 10. Critères de succès

1. **Premier-encounter test** : un visiteur non-technique ouvre l'app, comprend en moins de 10 secondes ce qu'il doit faire (taper son problème).
2. **Demo path stable** : 3 démos consécutives sans crash, sans tier-flip, sans freeze.
3. **Mode expert préservé** : toggle ⚙ → tout l'UI actuel revient strictement à l'identique. Aucun test backend ne casse.
4. **`make test` reste vert** (937 passed). Les nouveaux tests (intent classifier) sont rapides (< 1 s avec mock).
5. **Aucune dépendance ajoutée** côté frontend (pas de framework, pas de package manager — règle `CLAUDE.md`).
6. **Storyline jury lisible** : « Built with Opus 4.7 » → on montre Haiku classifier (route landing), Opus diagnostic (chat principal), forced tool use (classifier), Managed Agents (chat), 17 custom tools dont 12 qui pilotent une vraie vue. Le mode guidé ne dilue rien.

---

## 11. Hiérarchie d'implémentation suggérée

(Le plan d'implémentation détaillé sortira du skill `writing-plans` en
aval. Ici juste l'ordre logique pour que le plan soit cohérent.)

1. **Backend** classifier d'intention + tests (~2h, isolé, sans risque sur le reste).
2. **Shell** `index.html` : structure overlay landing + sidebar guidée + onglets topbar (~2h).
3. **CSS** mode guidé `guided.css` + `landing.css` (~3h).
4. **JS** `landing.js` + branchement classifier (~2h).
5. **JS** `llm.js` : promotion popover → sidebar permanente (~2h).
6. **JS** `llm.js` : rendu inline des widgets agent (~3h, le plus risqué).
7. **JS** `router.js` + `main.js` : toggle mode + état localStorage (~1h).
8. **Tests manuels** sur les 3 chemins démo (~1h).
9. **Pin CDN local** (`make pin-cdn`) en filet de sécurité (~30 min).
10. **Stop evolve + tag stable** dimanche matin (5 min).

Total estimé : **~17h de travail concentré**, à T-36h ça tient si on
travaille au moins 12h aujourd'hui et 5h dimanche matin.

---

## 12. Validation

Document validé par Alexis dans le brainstorm du 25 avril 2026.
Décisions ancrées :
- Mode guidé + mode expert dans le même shell.
- Landing hero avec classifier Haiku.
- Workspace style Claude.ai par repair (sidebar conversations existante promue).
- Widgets inline MVP : board + schématique. Reste en placeholder.
- Pas de `#home` global ; tout passe par la landing puis le repair.
- Toggle ⚙ persisté en localStorage.

Prochaine étape : invocation du skill `superpowers:writing-plans` pour
générer le plan d'exécution détaillé.
