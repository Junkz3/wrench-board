# Profil technicien — design spec

**Date :** 2026-04-23
**Scope :** remplacer le stub `#profile` par un vrai sous-système de **profil technicien dynamique**. Le technicien édite son identité, son outillage et son niveau ; l'agent de diagnostic (Opus 4.7 / Sonnet 4.6 / Haiku 4.5) **lit** le profil pour adapter son ton et ses propositions, et **écrit** dans le profil pour faire progresser les compétences au fil des réparations. Nouveau module backend `api/profile/`, nouveaux tools `profile_*`, nouveau module frontend `web/js/profile.js` + DOM + CSS.
**Hors scope :** multi-technicien / auth (solo-tech assumé) ; gamification avec niveaux XP globaux hors des skills ; export/import de profil ; comparaison inter-tech.

---

## 1. Contexte

La section `#profile` existe dans le shell (`web/index.html:157`, rail + crumb + mode-pill déjà câblés) mais n'est qu'un stub `<section class="stub">` avec le texte « Stats dérivées + overrides éditables — à faire en V2. » L'agent de diagnostic n'a aucune notion de qui est en face de lui — il parle au même niveau de détail à un débutant et à un expert, et peut proposer un reflow BGA à quelqu'un qui n'a pas de hot air station.

On transforme cette section en **profil vivant** piloté à deux niveaux :

1. **Édition manuelle par le tech** — identité légère, spécialités, **outillage disponible**, override optionnel du niveau global.
2. **Progression automatique par l'agent** — pour chaque réparation, l'agent détecte les compétences mobilisées, les trace avec evidence, et promeut leur statut (unlearned → learning → practiced → mastered) en fonction d'un compteur d'usages.

Côté runtime : le profil est **lu** au début de chaque session (injecté dans le system prompt) pour conditionner la verbosité et les actions proposées ; il est **écrit** à deux moments précis par l'agent via des tool calls dédiés.

---

## 2. Modèle de données

Source de vérité sur disque : `memory/_profile/technician.json` (un seul fichier, solo-tech). Préfixe `_` pour ne pas collisionner avec les `device_slug` sous `memory/`.

### 2.1 Forme JSON

```jsonc
{
  "schema_version": 1,
  "identity": {
    "name": "Alexis",
    "avatar": "AC",               // 2 lettres ou 1 emoji
    "years_experience": 5,
    "specialties": ["apple", "consoles"],   // ids du catalogue
    "level_override": null        // null = dérivé, sinon "beginner"|"intermediate"|"confirmed"|"expert"
  },
  "preferences": {
    "verbosity": "auto",          // "auto" (dérivé du niveau) | "concise" | "normal" | "teaching"
    "language": "fr"              // "fr" | "en"
  },
  "tools": {
    "soldering_iron": true,
    "hot_air": true,
    "microscope": true,
    "oscilloscope": false,
    "multimeter": true,
    "bga_rework": false,
    "preheater": true,
    "bench_psu": true,
    "thermal_camera": false,
    "reballing_kit": false,
    "uv_lamp": true,
    "stencil_printer": false
  },
  "skills": {
    "reflow_bga": {
      "usages": 12,
      "first_used": "2026-03-14T10:02:00Z",
      "last_used": "2026-04-22T17:41:00Z",
      "evidences": [
        {
          "repair_id": "rep_a1b2",
          "device_slug": "iphone-x-logic-board",
          "symptom": "no_boot",
          "action_summary": "Reflow du PMIC U2 après diag court-circuit sur PP_VDD_MAIN",
          "date": "2026-04-22T17:41:00Z"
        }
        // …cap à 20 entrées, FIFO
      ]
    },
    "reballing":     { "usages": 4, "first_used": …, "last_used": …, "evidences": [...] },
    "jumper_wire":   { "usages": 18, … },
    "microsolder_0201": { "usages": 1, … }
    // skills non pratiqués absents du dict (pas de stub à 0)
  },
  "updated_at": "2026-04-22T17:41:12Z"
}
```

### 2.2 Catalogue fermé

Deux catalogues **statiques** (code — pas user-extensible) vivent dans `api/profile/catalog.py` :

**Outils** (id → label FR, groupe) — environ 12 entrées :
`soldering_iron` → « Fer à souder », `hot_air` → « Hot air », `microscope` → « Microscope », `oscilloscope` → « Oscilloscope », `multimeter` → « Multimètre », `bga_rework` → « BGA rework », `preheater` → « Preheater », `bench_psu` → « Alim de labo », `thermal_camera` → « Caméra thermique », `reballing_kit` → « Kit reballing », `uv_lamp` → « Lampe UV », `stencil_printer` → « Stencil / pochoir ».

**Compétences** (id → label FR, tools requis, tags) — environ 15 entrées :
`reflow_bga` → « Reflow BGA » (requires: `hot_air`), `reballing` → « Reballing » (requires: `bga_rework`, `reballing_kit`), `jumper_wire` → « Jumper wires » (requires: `soldering_iron`, `microscope`), `microsolder_0201` → « Microsoudure 0201 » (requires: `soldering_iron`, `microscope`), `pop_rework` → « Rework PoP » (requires: `hot_air`, `preheater`), `trace_repair` → « Réparation pistes gravées » (requires: `soldering_iron`, `microscope`), `stencil_application` → « Pose de stencil » (requires: `stencil_printer`, `preheater`), `short_isolation` → « Isolation court-circuit » (requires: `multimeter`), `voltage_probing` → « Mesure tensions de rails » (requires: `multimeter`), `signal_probing` → « Mesure signaux scope » (requires: `oscilloscope`), `thermal_imaging` → « Imagerie thermique diag » (requires: `thermal_camera`), `power_sequencing` → « Analyse power sequencing » (requires: `oscilloscope`), `flux_cleaning` → « Nettoyage flux/résidus », `cold_joint_rework` → « Rework soudure froide » (requires: `soldering_iron`), `connector_replacement` → « Remplacement connecteur » (requires: `hot_air`, `microscope`).

Le catalogue est **versionné via `schema_version`**. Si une version ultérieure ajoute une skill, les profils existants se chargent sans migration (dict key manquante = skill non pratiquée).

### 2.3 Dérivations

Trois dérivations vivent dans `api/profile/derive.py`, pures, testables :

- **Statut d'une skill** : `usages == 0` → `unlearned` ; `1-2` → `learning` ; `3-9` → `practiced` ; `>= 10` → `mastered`.
- **Niveau global** (quand `level_override == null`) : compte des skills en `mastered` — `0` → `beginner`, `1-2` → `intermediate`, `3-7` → `confirmed`, `8+` → `expert`.
- **Verbosité effective** (quand `verbosity == "auto"`) : `beginner` → `teaching`, `intermediate` → `teaching`, `confirmed` → `normal`, `expert` → `concise`.

Seuils exposés comme constantes en tête de `derive.py` — si on les tune on le fait à un seul endroit, avec tests dédiés.

### 2.4 Pydantic v2

`api/profile/model.py` exporte :

- `ToolId`, `SkillId` — `StrEnum` fermés (source = catalogue).
- `SkillEvidence` — `repair_id`, `device_slug`, `symptom`, `action_summary`, `date`.
- `SkillRecord` — `usages: int`, `first_used`, `last_used`, `evidences: list[SkillEvidence]` (max 20, FIFO).
- `Identity`, `Preferences`, `ToolInventory` (BaseModel avec un bool par tool), `TechnicianProfile` (agrégat), `schema_version: Literal[1]`.

Les modèles sont à la fois validateurs runtime ET source du JSON Schema exposé aux tools (pattern déjà utilisé par `api/pipeline/schemas.py`).

---

## 3. Store sur disque

`api/profile/store.py`. API très fine, thread-safe via un `asyncio.Lock` module-level (solo-tech, pas de contention réelle — le lock est un safety net).

- `load_profile() -> TechnicianProfile` — lit `memory/_profile/technician.json`, hydrate Pydantic. Fichier absent → retourne `TechnicianProfile.default()` (profil vide, tous tools à `false`, skills dict vide). Jamais d'exception sur I/O.
- `save_profile(profile: TechnicianProfile) -> None` — écrit atomiquement (`.tmp` + `os.replace`), met `updated_at`.
- `update_profile(**patches) -> TechnicianProfile` — helper interne pour modifier un sous-bloc (identity / tools / preferences) sans toucher aux skills — utilisé par les endpoints PUT (qui remplacent le bloc ciblé de façon atomique).
- `bump_skill(skill_id, evidence) -> SkillRecord` — incrémente `usages`, append à `evidences` (FIFO cap 20), met à jour `last_used`, initialise `first_used` si absent, persiste.

**Pas de cache en mémoire.** Le fichier est petit (< 50KB max), relu à chaque requête. Simplifie la cohérence quand plusieurs processus (pipeline + API) s'y frottent.

---

## 4. Surface HTTP

`api/profile/router.py`, monté sous `/profile`. Pas d'auth (solo-tech). Inclus dans `api/main.py` à côté des routers existants.

| Méthode | Chemin              | Rôle                                                                |
|---------|---------------------|---------------------------------------------------------------------|
| GET     | `/profile`          | Retourne le profil complet + catalogue + dérivations (niveau calculé, verbosité effective, status par skill) — tout ce qu'il faut au frontend en un seul appel. |
| PUT     | `/profile/identity` | Remplace le bloc `identity` (nom, avatar, years_experience, specialties, level_override). |
| PUT     | `/profile/tools`    | Remplace le bloc `tools` (bitmap complet, envoi du dict entier pour être idempotent). |
| PUT     | `/profile/preferences` | Remplace `preferences` (verbosity, language). |
| GET     | `/profile/catalog`  | Renvoie le catalogue tools + skills (labels FR, requires). Pour hydrater le frontend. Pas nécessaire si `/profile` inclut déjà le catalogue — **retenu dans `/profile` pour économiser un round-trip**. Route `/catalog` supprimée. |

Le `GET /profile` est la route structurante. Sa réponse :

```jsonc
{
  "profile": TechnicianProfile,
  "derived": {
    "level": "confirmed",
    "verbosity_effective": "normal",
    "skills_by_status": {
      "mastered":  ["reflow_bga", "jumper_wire"],
      "practiced": ["reballing"],
      "learning":  ["microsolder_0201"],
      "unlearned": ["pop_rework", "thermal_imaging", …]
    }
  },
  "catalog": {
    "tools":  [{id, label, group}, …],
    "skills": [{id, label, requires: [tool_id…]}, …]
  }
}
```

---

## 5. Tools agent — famille `profile_*`

Trois tools, déclarés dans `api/agent/manifest.py`, toujours présents (pas conditionnels à la `session.board`). Préfixe `profile_` (et pas `mb_`) parce que le profil est un domaine à part — ni memory bank device, ni boardview.

### 5.1 `profile_get()`

**Quand** : en début de session, OU quand l'agent juge utile de rafraîchir (ex : après une longue pause).
**Entrée** : aucune (pas de paramètre).
**Retour** :
```jsonc
{
  "identity": {"name": "Alexis", "years_experience": 5, "specialties": [...]},
  "level": "confirmed",
  "verbosity_effective": "normal",
  "tools_available": ["soldering_iron", "hot_air", "microscope", ...],
  "tools_missing":   ["bga_rework", "oscilloscope", ...],
  "skills_summary": {
    "mastered":  [{"id": "reflow_bga", "usages": 12}, ...],
    "practiced": [...],
    "learning":  [...]
  }
}
```

### 5.2 `profile_check_skills(candidate_skills: list[str])`

**Quand** : l'agent vient de construire un plan d'action et veut savoir à quel niveau le tech les maîtrise. Lecture seule.
**Entrée** : `candidate_skills` = liste d'ids de skills du catalogue.
**Retour** par skill :
```jsonc
{
  "reflow_bga":    {"status": "mastered",  "usages": 12, "tools_ok": true},
  "reballing":     {"status": "practiced", "usages": 4,  "tools_ok": false,
                    "missing_tools": ["bga_rework", "reballing_kit"]},
  "pop_rework":    {"status": "unlearned", "usages": 0,  "tools_ok": false,
                    "missing_tools": ["hot_air"]},
  "unknown_skill_xx": {"error": "not_in_catalog"}
}
```

Cette info permet à l'agent d'**adapter la verbosité par étape** (skill mastered → brief, unlearned → pas-à-pas) et de **filtrer les actions impossibles** (outil manquant → propose alternative ou demande si achat prévu).

### 5.3 `profile_track_skill(skill_id: str, evidence: SkillEvidence)`

**Quand** : l'agent a une **confirmation explicite** du tech qu'une action nécessitant cette skill a été exécutée avec succès. PAS après simple mention dans la conversation.
**Entrée** : `skill_id` + bloc `evidence` avec `repair_id`, `device_slug`, `symptom`, `action_summary`.
**Retour** :
```jsonc
{
  "skill_id": "reflow_bga",
  "usages_before": 11,
  "usages_after": 12,
  "status_before": "mastered",
  "status_after": "mastered",
  "promoted": false  // true si le statut a changé (learning→practiced, etc.)
}
```

**Garde-fous** :
- Le tool rejette si `skill_id` n'est pas dans le catalogue (`{"error": "unknown_skill", "closest_matches": [...]}`).
- Le tool rejette si `evidence.action_summary` est vide ou < 20 caractères (`{"error": "evidence_too_thin"}`) — force l'agent à justifier.
- Le tool n'a **pas** de mode "batch" (track une skill à la fois) — évite qu'un LLM distrait lâche 5 skills en un tool call après une conversation vague.

### 5.4 Où l'agent lit le profil (en plus des tools)

Le bloc `<technician_profile>` est aussi **injecté dans le system prompt** au début de chaque session (voir §6). Ça suffit pour 90% des cas — l'agent lit une fois, n'a pas besoin de `profile_get()` systématique. Les tools servent pour (a) `check_skills` ponctuel avant action, (b) `track_skill` en écriture, (c) `get` de refresh volontaire si le tech a modifié son profil pendant la session.

---

## 6. Injection dans le runtime diagnostic

### 6.1 DIRECT runtime (`api/agent/runtime_direct.py`)

`render_system_prompt()` dans `api/agent/manifest.py` est étendu pour insérer un bloc `<technician_profile>` avant la règle anti-hallucination :

```
<technician_profile>
Nom : Alexis · 5 ans d'XP · Niveau : confirmé
Verbosité cible : normal (ajuste si le tech demande plus/moins de détail)
Spécialités : apple, consoles
Outils disponibles : soldering_iron, hot_air, microscope, multimeter, preheater, bench_psu, uv_lamp
Outils NON disponibles : bga_rework, oscilloscope, thermal_camera, reballing_kit, stencil_printer
Compétences maîtrisées (≥10×) : reflow_bga (12), jumper_wire (18)
Compétences pratiquées (3-9×) : reballing (4)
Compétences en apprentissage (1-2×) : microsolder_0201 (1)
Règles :
  - NE propose JAMAIS une action qui requiert un outil non dispo — propose un workaround ou demande.
  - Pour les compétences `mastered`, va direct au fait (refdes, geste, fin). Pour `learning` ou `unlearned`, détaille les étapes et les risques.
  - Quand le tech confirme avoir exécuté une action, appelle profile_track_skill avec une evidence claire (refdes, symptôme, geste résolu).
</technician_profile>
```

Rendu par une nouvelle fonction `render_technician_block(profile, derived)` dans `api/profile/prompt.py`. Pur, testable, pas de I/O.

### 6.2 MANAGED runtime (`api/agent/runtime_managed.py`)

La MA path porte son system prompt côté serveur via `managed_ids.json`. On **ne peut pas injecter dynamiquement** dans ce prompt server-side sans re-créer l'agent. Deux patterns candidats, à départager dans le plan d'implémentation :

1. **User message synthétique** à l'ouverture de la session, préfixé `[CONTEXTE TECHNICIEN]` puis le bloc identique à §6.1. Lu par l'agent comme n'importe quel premier tour. Coût : un message sacrificiel par session.
2. **Fichier dans la MA memory_store** (extension du pattern `memory_seed.py` déjà en place — qui pousse déjà les JSON du pack device sous `/knowledge/*`). On ajouterait `/technician/profile.md` rendu à partir du bloc, relu par l'agent via `memory_search` si son prompt l'y invite.

Le pattern 1 garantit que l'info est dans le contexte dès le 1er turn (pas besoin de tool call préalable). Le pattern 2 est plus propre mais dépend du feature flag `ma_memory_store_enabled` (encore off par défaut — accès Research Preview non accordé). **Recommandation pour le plan : pattern 1 pour MVP**, pattern 2 en follow-up quand le flag s'active.

### 6.3 Quand le profil change en cours de session

Si le tech édite son profil pendant qu'une session est ouverte (autre onglet, typiquement), l'agent ne voit pas la mise à jour jusqu'à ce qu'il appelle `profile_get()`. Acceptable pour un MVP — on documente dans le system prompt que « le profil peut évoluer, appelle `profile_get()` si tu as un doute ». Pas de push server→agent pour MVP.

---

## 7. Surface frontend

### 7.1 DOM (`web/index.html`)

Le stub actuel `<section class="stub hidden" data-section-stub="profile">` est remplacé par une section riche `<section class="profile hidden" id="profileSection">` structurée :

- **Header** : bloc avatar (2 lettres) + nom + niveau + years_experience + spécialités (chips), avec bouton « Éditer ».
- **Outillage** : grille de 12 toggle chips (un par tool du catalogue). Clic = toggle immédiat, PUT `/profile/tools`.
- **Compétences** : 4 colonnes visuelles empilées — `Maîtrisées`, `Pratiquées`, `En apprentissage`, `Non pratiquées`. Chaque skill est une ligne avec label + jauge (barre discrète styled avec `--cyan` pour usages, `--border-soft` pour le fond) + compteur mono. Clic sur une skill → drawer glass avec les `evidences` (liste de petites cartes).
- **Préférences** : verbosité (radio 4 valeurs) + langue (radio 2 valeurs).

Toutes les strings en français. Typographie : Inter pour labels et prose, JetBrains Mono pour counts (`12×`) et ids.

### 7.2 JS (`web/js/profile.js`)

Module auto-contenu :

- `initProfileSection()` — appelé par `main.js` quand `section === "profile"`. Fetch `GET /profile`, rend le DOM, câble les handlers.
- `renderProfile(state)` — state = `{profile, derived, catalog}`. Pure render.
- Handlers tools/preferences → `fetch('PUT …', {body})` puis re-render avec réponse.
- Handler « Éditer identité » → ouvre un modal (pattern déjà utilisé par `home.js`, on réutilise `styles/modal.css`).
- Handler clic-skill → drawer glass, same pattern que le Tweaks panel du boardview (`rgba(panel, .9)` + `backdrop-filter: blur(10px)` + 1px `--border`).

**Pas de WebSocket** pour MVP — les updates serveur-side pendant une session diagnostic ne se reflètent pas live dans l'UI profil. Acceptable : le tech est rarement sur l'onglet profil ET sur l'onglet diagnostic en même temps. Le rafraîchissement au `navigate('profile')` suffit.

### 7.3 Styles (`web/styles/profile.css`)

Dédié, pas d'intrusion dans les CSS existants. Palette sémantique :
- Statut skills : `--violet` pour `mastered` (c'est une action accomplie, domaine violet dans notre palette), `--emerald` pour `practiced`, `--cyan` pour `learning`, `--text-3` pour `unlearned`. Pas de nouvelle couleur inventée.
- Chips tools : style identique aux column chips du graphe (mono 10.5px, uppercase, letter-spacing .4px).
- Layout : `.profile { position: fixed; top: 92px; left: 52px; right: 0; bottom: 28px; overflow-y: auto; }` — aligné sur `.stub`, respecte `body.no-metabar`.

### 7.4 Router + Chrome

`router.js:16` a déjà `profile: {crumb: "Profil", mode: {tag: "PROFIL", sub: "Technicien", color: "cyan"}}`. On étend `navigate()` pour hide/show `#profileSection` comme `#homeSection` et on appelle `initProfileSection()` à la première entrée. Rien à changer côté mode-pill ou crumbs.

### 7.5 Avatar dans le topbar

**Hors scope MVP** — on garde le stub topbar existant. Un follow-up pourrait afficher l'avatar + niveau dans le coin haut-droit en permanence. On documente le hook, on ne l'implémente pas.

---

## 8. Flux bout-en-bout

### 8.1 Première ouverture de l'app

1. Tech navigue sur `#profile` → `GET /profile` renvoie le profil par défaut (nom vide, tools tous à `false`, skills dict vide).
2. Tech coche ses outils, édite son nom dans le modal. PUT successifs sauvent.
3. Tech retourne sur `#home`, ouvre une nouvelle réparation.

### 8.2 Session diagnostic — lecture du profil

1. WS `/ws/diagnostic/{slug}?tier=normal&repair=rep_xx` s'ouvre.
2. Runtime (managed ou direct) construit le contexte système :
   - Managed : premier message user `[CONTEXTE TECHNICIEN]\n<bloc>` avant le premier message du tech.
   - Direct : `render_system_prompt()` inclut le bloc inline.
3. L'agent voit : verbosité cible, outils dispo, skills maîtrisées. Il adapte.

### 8.3 Session diagnostic — progression des skills

1. Tech décrit un symptôme → agent consulte `mb_list_findings`, `mb_get_rules_for_symptoms`, `mb_get_component`.
2. Avant de proposer le plan d'action, agent appelle `profile_check_skills(["reflow_bga", "short_isolation"])`.
3. Retour : `reflow_bga` mastered + tools_ok → l'agent propose direct. `short_isolation` practiced + tools_ok → idem mais avec un rappel bref.
4. Agent envoie sa proposition au tech. Tech répond « ok j'ai reflow le PMIC, boot OK maintenant ».
5. Agent appelle `profile_track_skill("reflow_bga", evidence={repair_id, device_slug, symptom: "no_boot", action_summary: "Reflow du PMIC U2, boot restauré"})`.
6. Backend incrémente, append l'evidence, persiste. Retour à l'agent : `usages: 12 → 13`, promoted: false (déjà mastered).

### 8.4 Clôture de réparation — passe rétrospective

**Optionnel MVP.** Quand le tech clôture une réparation (bouton dédié, déjà prévu par le modèle `chat_history.status = "closed"`), l'agent peut faire une dernière passe : « J'ai aussi vu que tu as utilisé `flux_cleaning` et `trace_repair` aujourd'hui — tu confirmes ? » avec un UI de checkbox côté panel chat. Chaque coche → `profile_track_skill(...)`.

Pour MVP on se limite au **track immédiat** (§8.3). La passe rétrospective est un follow-up documenté, pas implémenté.

---

## 9. Layout fichiers

```
api/
  profile/
    __init__.py      # FastAPI router re-export
    model.py         # Pydantic TechnicianProfile + sub-models + Enums
    catalog.py       # TOOLS_CATALOG, SKILLS_CATALOG (constantes statiques)
    derive.py        # skill_status(), global_level(), effective_verbosity()
    store.py         # load_profile, save_profile, update_profile, bump_skill
    router.py        # GET /profile, PUT /profile/{identity|tools|preferences}
    prompt.py        # render_technician_block(profile, derived) -> str
    tools.py         # profile_get, profile_check_skills, profile_track_skill handlers
  agent/
    manifest.py      # +3 tool definitions dans un nouveau PROFILE_TOOLS, toujours incluses
    runtime_direct.py  # render_system_prompt() appelle render_technician_block()
    runtime_managed.py # session.create push un user message [CONTEXTE TECHNICIEN]
  main.py            # include profile router
web/
  index.html         # remplace stub par <section class="profile"> structurée
  js/
    profile.js       # nouveau module
    main.js          # dispatch section 'profile' → initProfileSection()
    router.js        # navigate() hide/show #profileSection
  styles/
    profile.css      # nouveau
    # index.html link rel="stylesheet" ajouté
tests/
  profile/
    test_model.py       # Pydantic round-trip, defaults, validation
    test_derive.py      # seuils skill status, niveau global, verbosité
    test_store.py       # atomic write, load absent file, bump_skill
    test_router.py      # FastAPI TestClient GET + PUTs
    test_tools.py       # profile_get/check/track, evidence validation
    test_prompt.py      # render_technician_block snapshot
memory/
  _profile/
    technician.json  # créé au premier write ; gitignored
```

Ajouter `memory/_profile/` à `.gitignore` — le profil est une donnée utilisateur, pas du code.

---

## 10. Tests

Cibles minimales :

- **`test_model.py`** — chaque Pydantic model round-trippe, `TechnicianProfile.default()` est vide-mais-valide, enum rejection pour un `skill_id` hors catalogue.
- **`test_derive.py`** — les 4 seuils de `skill_status`, les 4 seuils de `global_level`, la table de `effective_verbosity`. Parametrize pytest.
- **`test_store.py`** — load sur fichier absent = default, save + load = identité, `bump_skill` sur skill nouvelle/existante, cap à 20 evidences FIFO, atomicité (tmp + replace).
- **`test_router.py`** — GET retourne `{profile, derived, catalog}`, PUT identity/tools/preferences persistent, réponse inclut `updated_at` récent.
- **`test_tools.py`** — `profile_get` format, `profile_check_skills` par skill avec unknown, `profile_track_skill` rejette evidence < 20 chars ET skill hors catalogue, promotion detectée quand `usages_before == 9 → usages_after == 10`.
- **`test_prompt.py`** — `render_technician_block(default_profile)` produit un bloc lisible, `render_technician_block(rich_profile)` liste les bonnes sections. Snapshot comparison.

Pas de test de bout-en-bout runtime+LLM (coûte des tokens, flaky). Les tests d'injection se limitent à vérifier que `render_system_prompt()` contient bien le bloc `<technician_profile>`.

---

## 11. Règles & contraintes respectées

- **Catalogue fermé, en code** → pas de risque d'explosion combinatoire, skill ids stables pour les evidences historiques.
- **Store sur disque plat, pas de DB** → cohérent avec `memory/` pour le reste du projet.
- **Tools sont feature-flag-neutral** (toujours dans le manifest) → pas de logique conditionnelle lourde, simple à tester.
- **Evidence obligatoire + seuil de taille** → limite la dérive du LLM qui appellerait `profile_track_skill` trop vite.
- **Injection dans le system prompt + tools de lecture** → défense en profondeur, l'agent a l'info sans dépenser un tool call par turn.
- **Solo-tech** → pas d'auth, pas de multi-profil, pas de partitionnement. Si on ouvre au multi un jour, on passe `memory/_profiles/<user_id>.json` et on rebranche le store. Pas de refacto lourd anticipé.
- **UI alignée sur le design system** → tokens existants uniquement, Inter/JetBrains Mono, pas de nouveau composant étranger.

---

## 12. Questions ouvertes documentées (follow-ups)

- **Passe rétrospective à la clôture de réparation** — UX spec distincte, besoin d'un affichage checkbox cumulé côté panel chat.
- **Affichage avatar + niveau dans le topbar** — petit composant persistant coin haut-droit.
- **Rétractation / correction d'evidence** — si l'agent track par erreur, le tech n'a pas aujourd'hui de moyen de décrémenter. Route `DELETE /profile/skills/{id}/evidences/{index}` à prévoir.
- **Export / import de profil** — pertinent le jour où un tech change de poste.
- **Synchronisation live** lorsque le profil est modifié pendant qu'une WS est ouverte — push server→frontend + reprompt léger de l'agent.

Aucun de ces points ne bloque le MVP.
