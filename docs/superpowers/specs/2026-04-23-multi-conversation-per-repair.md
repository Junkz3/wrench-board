# Multi-conversation per repair — design spec

**Date :** 2026-04-23
**Scope :** dans un même `repair_id`, permettre plusieurs **conversations** distinctes, chacune avec sa propre histoire + session MA + modèle. Le tech peut cliver la conversation courante (bouton ou via switch de tier) et naviguer dans les anciennes sans les détruire.
**Hors scope :** streaming token-par-token (chantier séparé). Partage de mémoire MA entre conversations (chaque conv a sa propre session MA — la mémoire persistante vit au niveau `repair`). Suppression / renommage manuel de conversations.

---

## 1. Contexte actuel

- `memory/{slug}/repairs/{repair_id}/messages.jsonl` — un fichier unique par repair, tous tours mélangés.
- WS `/ws/diagnostic/{slug}?repair={id}&tier={fast|normal|deep}` — ouvre une session MA persistente par `(slug, tier, repair)` via `save_ma_session_id` / `load_ma_session_id` côté backend.
- Changer de tier dans le panel = reconnexion WS = nouvelle session MA, mais **écriture continue dans le même `messages.jsonl`** → les tours de tiers différents s'entremêlent.

**Ce qui manque :** pas de moyen pour le tech de dire « je clôture cette piste, on repart d'une page blanche » sans tout perdre.

## 2. Modèle cible

```
memory/{slug}/repairs/{repair_id}/
  conversations/
    index.json                       # ordered list (see §3)
    {conv_id}/
      messages.jsonl                 # Anthropic-shape events for this conv
      ma_session.json                # optional: MA session_id when managed
  findings.json                      # unchanged — per-repair, shared
  status.json                        # unchanged — per-repair lifecycle
  messages.jsonl                     # legacy, kept for migration (§8)
```

- **Un repair a N conversations**, ordonnées du plus vieux au plus récent.
- **Une conversation = une piste de diagnostic** : un modèle sélectionné au départ (tier), sa session MA (ou son replay direct), sa propre histoire de messages.
- **La conversation « active »** est par défaut la plus récente ; elle est celle rendue à l'ouverture du repair.
- **Changer de tier dans le panel = créer automatiquement une nouvelle conversation** (on gèle la piste existante, on repart neuf sur le nouveau tier).
- **« + Nouvelle conversation »** dans le panel = créer une nouvelle conversation au **même tier** que l'active.

## 3. `conversations/index.json`

Tableau JSON, **ordonné par `started_at` ascendant** (la dernière est l'active par défaut) :

```json
[
  {
    "id": "c1a3f",
    "started_at": "2026-04-23T06:30:11Z",
    "tier": "fast",
    "model": "claude-haiku-4-5",
    "last_turn_at": "2026-04-23T06:35:02Z",
    "cost_usd": 0.0142,
    "turns": 4,
    "title": "Teste la rail PP_VCC_MAIN…",
    "closed": false
  }
]
```

- `id` : string court (5-8 chars), généré `secrets.token_hex(4)`. Pas d'UUID pour que ça reste lisible dans les URL / logs.
- `title` : extrait des ~80 premiers chars du **premier message user** de la conv (dépouillé des retours à la ligne). Mis à jour une seule fois au 1er message.
- `closed` : booléen. `true` quand une nouvelle conversation est ouverte et que celle-ci n'est donc plus l'active. Informationnel seulement — on ne supprime jamais rien côté fichiers.
- `cost_usd`, `turns`, `last_turn_at` : mis à jour à chaque `turn_cost`.
- Pas de champ `repair_id` — la hiérarchie de dossiers suffit.

## 4. WebSocket — évolution du contrat

**Paramètre nouveau :** `conv` — string. Trois cas :

| `conv` | Comportement backend |
|--------|---------------------|
| absent | Ouvre l'active (la plus récente). Si aucune conv n'existe encore pour le repair, **en crée une** au tier passé. |
| `<id>` | Ouvre la conv dont `id == <id>`. 404 WS close si absente. |
| `new` | Crée une nouvelle conv au tier passé ; renvoie le `conv_id` dans `session_ready`. |

**Extension du payload `session_ready` :**
```json
{"type": "session_ready", "mode": "managed", "device_slug": "…",
 "repair_id": "…", "conv_id": "c1a3f", "tier": "fast",
 "model": "claude-haiku-4-5", "conversation_count": 3}
```

Tout le reste du protocole inchangé.

**Reconnexion sur tier switch (frontend) :** le panel ferme le WS courant et ré-ouvre avec `?conv=new&tier={new}` — le backend auto-clive proprement. Pas de « switch conv + change tier en plus » à gérer ; c'est une action atomique.

## 5. HTTP endpoints nouveaux

Sous `api/pipeline/__init__.py` (routeur `/pipeline/repairs/…`) :

- **`GET /pipeline/repairs/{repair_id}/conversations`** — retourne `index.json` tel quel, avec le repair metadata (device_slug notamment) pour éviter un round-trip frontend. Côté UI : charge la liste au open du panel ou au toggle du popover.
- **`POST /pipeline/repairs/{repair_id}/conversations`** (optionnel pour v1 — le WS `?conv=new` suffit ; on se contente de cette route pour le GET).

Pas de DELETE / PATCH. On garde tout.

## 6. Impact backend — fichiers modifiés

### `api/agent/chat_history.py`

- **Tous les helpers d'accès fichier** gagnent un paramètre `conv_id` :
  - `_history_file(memory_root, slug, repair_id, conv_id) -> Path`
  - `append_event(..., conv_id: str, ...)` — id obligatoire en nouveau mode
  - `load_events(..., conv_id: str, ...)`
  - `load_events_with_costs(..., conv_id: str, ...)`
  - `save_ma_session_id(..., conv_id: str, tier: str, ...)` — la clé `(slug, repair, conv, tier)` identifie une session MA unique
  - `load_ma_session_id(..., conv_id: str, tier: str, ...)`
- **Nouveaux helpers :**
  - `list_conversations(slug, repair_id) -> list[dict]` — lit `index.json`, retourne liste (vide si absente)
  - `ensure_conversation(slug, repair_id, conv_id: str | None, tier: str) -> tuple[str, bool]` — retourne `(resolved_id, created)`. Si `conv_id is None` → active ; si `"new"` → crée ; si un id existant → passe à travers.
  - `create_conversation(slug, repair_id, tier: str) -> str` — génère un id, crée le dossier, ajoute l'entrée dans `index.json`, retourne l'id.
  - `touch_conversation(slug, repair_id, conv_id, cost_usd: float | None = None, first_message: str | None = None)` — incrémente `turns`, update `last_turn_at`, accumule `cost_usd`, set `title` au premier message.
  - `close_conversation(slug, repair_id, conv_id)` — set `closed: true` dans l'index. Appelé implicitement quand `create_conversation` est appelé (la précédente active se ferme).
- **Status global (`touch_status`) inchangé** — il vit au niveau repair.

### `api/main.py`

- WS `/ws/diagnostic/{device_slug}` — lit `conv = query_params.get("conv")`, passe à chaque runtime.

### `api/agent/runtime_direct.py` + `runtime_managed.py`

- Signature des fonctions d'entrée passe à `run_session(ws, slug, tier, repair_id, conv_id=None)`.
- Au tout début, `conv_id_resolved, created = ensure_conversation(slug, repair_id, conv_id, tier)`.
- Toute la lecture/écriture de `messages.jsonl` et `ma_session.json` passe par `conv_id_resolved`.
- `session_ready` envoi : ajoute `conv_id` + `tier` + `conversation_count`.
- Sur chaque `turn_cost` : `touch_conversation(...)` pour maintenir l'index.
- Sur **l'arrivée du premier `user.message`** de la session : `touch_conversation(..., first_message=<text>)` pour fixer le `title` (une seule fois ; protégé par `if not title`).

### `api/pipeline/__init__.py`

- Ajoute route `GET /pipeline/repairs/{repair_id}/conversations` — renvoie `{device_slug, repair_id, conversations: [...]}`.

## 7. Impact frontend — `web/js/llm.js` + `web/llm_panel.html` + `web/styles/llm.css`

### 7.1 État module

- `let currentConvId = null;` — id de la conv courante, mis à jour à `session_ready`.
- `let conversationsCache = [];` — dernière liste connue, pour alimenter le popover sans appel HTTP à chaque clic.

### 7.2 UI — chip + popover dans le status strip

À droite du `device-tag`, **avant** le `cost-total` :

```html
<button class="conv-chip" id="llmConvChip" aria-haspopup="menu" aria-expanded="false" title="Conversations">
  <span class="conv-label">CONV 1/1</span>
  <svg class="chevron-down">…</svg>
</button>
<div class="conv-popover" id="llmConvPopover" role="menu" hidden>
  <div class="conv-list" id="llmConvList"><!-- items rendered from JSON --></div>
  <div class="conv-popover-sep"></div>
  <button class="conv-new" id="llmConvNew">
    <svg class="icon-sm">…</svg>
    Nouvelle conversation
  </button>
</div>
```

- **Chip label :** `CONV N/T` où `N` = index 1-based de l'active dans la liste triée, `T` = total.
- **Popover width :** ~260px. Ouvre sous le chip.
- **Item structure :**

```html
<button class="conv-item" data-conv-id="c1a3f">
  <span class="conv-item-head">
    <span class="conv-item-tier t-fast">FAST</span>
    <span class="conv-item-title">Teste la rail PP_VCC_MAIN…</span>
  </span>
  <span class="conv-item-meta">
    <span class="conv-item-turns">4 turns</span>
    <span class="conv-item-sep">·</span>
    <span class="conv-item-cost">$0.014</span>
    <span class="conv-item-sep">·</span>
    <span class="conv-item-ago">il y a 2 min</span>
  </span>
</button>
```

- Active item : `.conv-item.active` (border-left 2px violet + bg `rgba(192,132,252,.08)`).
- **Nouvelle conversation** : bouton en bas, icône `+`, `color: var(--violet)`. Clic → `switchConv("new")`.
- **Clic sur un item (pas l'active) :** `switchConv(id)`.

### 7.3 Fonctions nouvelles dans `llm.js`

- `loadConversations()` — `GET /pipeline/repairs/{id}/conversations`, met à jour `conversationsCache` + chip label + popover items.
- `renderConvItems(list, activeId)` — rend la liste dans `#llmConvList`.
- `switchConv(convId)` — ferme le WS courant, reconnecte avec `?conv={id}` (ou `?conv=new`). Idempotent — no-op si `convId === currentConvId`.
- Appels `loadConversations()` : au `session_ready`, après chaque `turn_cost` (debounced 500ms), au clic sur le chip pour forcer refresh.

### 7.4 Flow tier switch

`switchTier` aujourd'hui reconnecte avec `?tier={new}`. Nouveau comportement : reconnecte avec `?conv=new&tier={new}` — le backend crée une nouvelle conv au nouveau tier, l'ancienne se ferme automatiquement.

## 8. Migration des repairs existants

Au premier accès d'un repair dont `conversations/index.json` n'existe PAS :

1. Si `messages.jsonl` legacy existe à la racine du repair → créer `conversations/{legacy_id}/messages.jsonl` en le déplaçant, tier = best-guess (si métadonnées repair connaissent le tier du moment → sinon `fast`), `title` = premier msg user du fichier, index initialisé.
2. Si pas de `messages.jsonl` legacy → repair vierge, aucune conv. La 1re ouverture WS créera la 1re conv.

La migration vit dans `ensure_conversation()` (appelée par le runtime au début de session) pour être automatique, pas dans une commande séparée.

## 9. Tests

- `tests/agent/test_conversations.py` :
  - `create_conversation` génère un id, écrit `index.json`, crée le dossier.
  - `list_conversations` retourne l'ordre chronologique.
  - `ensure_conversation(None, …)` retourne l'active.
  - `ensure_conversation("new", …)` crée une nouvelle + marque la précédente `closed`.
  - `ensure_conversation(<existing_id>, …)` passe à travers sans création.
  - `touch_conversation(first_message=…)` set le `title` seulement à la première fois.
- `tests/agent/test_chat_history_conv.py` :
  - `append_event(..., conv_id=…)` écrit bien dans le sous-dossier.
  - `load_events(..., conv_id=…)` ne lit que les events de cette conv.
  - Migration : repair avec `messages.jsonl` legacy → après `ensure_conversation(None, …)`, les events sont accessibles via le conv_id assigné.

Pas de test JS (pas d'infra JS tests dans le projet).

## 10. Hors scope explicite

- **Streaming token-par-token** — le panel affiche toujours un message complet à la fois, pas de deltas. Le caret existant reste un indicateur de tour, pas un curseur typewriter réel.
- **Suppression / renommage de conversations** — on garde tout, à vie. Pas d'UI destructive.
- **Partage d'une session MA entre conversations** — chaque conv a sa propre session MA. La mémoire persistante MA (memory store par device) reste partagée.
- **Persistance de l'active conv sur reload du panel** — pas d'URL param, pas de localStorage. Au reload, l'active = la plus récente (même comportement que par défaut backend). On peut l'ajouter plus tard.
- **Limit de conversations par repair** — aucune. Si ça devient un problème de volume, on ajoute un cap ou une archive plus tard.
