# Panel chat diagnostic agent — redesign spec

**Date :** 2026-04-23
**Scope :** redesign visuel et narratif du panel chat `/ws/diagnostic/{slug}` (`web/js/llm.js`, `web/styles/llm.css`, markup dans `web/index.html`). Structure un « tour d'agent » comme un turn-block avec rail typé ; enrichit le rendu des tool-calls (icône + phrase française + chevron expandable) ; rend les refdes et nets cités dans la réponse cliquables pour piloter le boardview ; polit le chrome (header, tier collapse, textarea input, cost-total unifié).
**Hors scope :** protocole WebSocket, runtimes `api/agent/runtime_*.py`, tool dispatch, sanitizer, Managed Agents. Aucun changement backend. Le panel change de forme, pas de contrat. La pièce jointe image (vision input) est reportée — la place n'est pas réservée dans cette itération.

---

## 1. Contexte

Le panel existe et fonctionne (commits `0c227d4`, `b5993f9`, `7a44108`). Il est docké à droite (420px fixed), push-mode (`body.llm-open`), tier-selectable, log plat chronologique avec 4 styles de lignes : `msg.user` (cyan left-border), `msg.assistant` (panel neutre), `tool` (mono violet compact), `sys` (mono gris). Un `cost-chip` discret se pose en bas de chaque réponse agent ; un `cost-total` tourne dans le status strip.

**Ce qui marche** : cohérence avec le design system (tokens, typographie Inter/JetBrains Mono, couleurs sémantiques), push-mode, tier-switch reconnect, Stop amber, Esc-to-interrupt, replay en opacité .55.

**Ce qui manque** :

1. **Lecture narrative d'un tour.** La timeline est plate. Un tour agent typique émet `thinking → mb_get_component → bv_highlight_component → mb_get_rules_for_symptoms → message` sur 6 lignes indépendantes chronologiques. Le tech ne distingue pas visuellement « l'agent a réfléchi → cherché 2 choses → montré sur le board → répondu » d'une suite de messages sans lien. Pas de regroupement, pas de rail d'attribution.
2. **Pauvreté des tool-calls.** Ligne mono `→ bv_highlight_component {"refdes":"U12"}` tronquée. Pas de distinction sémantique MB (lecture mémoire) vs BV (action boardview). Pas d'accès au `result` retourné par le tool (il n'arrive pas à l'UI aujourd'hui).
3. **Message agent inerte.** Texte plat en Inter, refdes et nets en texte courant. Le tech doit lire `« Teste U12 sur PP_VCC_MAIN »` puis aller cliquer dans le boardview — boucle d'action cassée.
4. **Zéro feedback live.** Entre l'envoi et le premier token, silence. Le streaming est émis par le backend mais rendu comme un `logMessage` final — pas de typewriter, pas de pulse.
5. **Chrome redondant.** Cost-chip par message **et** cost-total dans le strip → même info deux fois. Tier selector 40px de hauteur alors qu'on le touche rarement. Input single-line sature pour un paragraphe technique. Modèle mono noyé dans le header.

---

## 2. Décisions directrices

1. **Narration par turn-block + rail typé.** Chaque tour agent est matérialisé par un conteneur `.turn` dont le rail vertical gauche porte l'ordre chronologique des steps (`thinking`, `tool_use`) qui ont abouti au `message` final.
2. **Tool-call visuel riche, expandable.** Icône 10px + couleur sémantique MB/BV + phrase française courte + refdes/target en mono teinté + chevron qui déplie le payload JSON complet (args + result quand disponible).
3. **Message agent rendu sémantique.** Markdown léger (bold, italic, listes puces/numérotées, inline code). Refdes validés (`board.part_by_refdes`) → chip mono cyan cliquable (focus board). Nets validés (`board.net_by_name`) → chip mono emerald cliquable (highlight net). Refdes inconnus restent wrap `⟨?U999⟩` amber (sanitizer inchangé).
4. **Typewriter caret en streaming.** Pendant le stream, un caret ▍ clignote en fin de `turn-message` ; disparaît à l'arrivée de `turn_cost`. Si pas encore de `turn-message` mais rail actif : nœud fantôme pulsant en bas du rail.
5. **Chrome compacté.** Header à deux lignes (titre = repair, sous-ligne = `model · mode · repair_id`). Tier selector collapse en chip unique à droite du header (popover au clic). Cost-total conservé dans le strip, **delta du dernier turn** affiché à côté, cost-chip inline supprimé. Input devient `<textarea>` auto-grow (Enter envoie, Shift+Enter newline).
6. **Photo-attach reporté.** Aucune place réservée cette itération.
7. **Backend inchangé.** Tous ces changements sont frontend-only. Le seul élargissement de contrat souhaitable est la remontée du `tool_result` sur le WS (voir §10 — Extensions optionnelles).

---

## 3. Anatomie du turn-block

### 3.1 Hiérarchie DOM

```html
<div class="turn" data-turn-id="t-3">
  <div class="turn-rail">
    <div class="step thinking">
      <span class="node"></span>
      <span class="step-text">l'agent réfléchit à la piste d'alim…</span>
    </div>
    <div class="step mb" data-tool="mb_get_component">
      <span class="node"></span>
      <svg class="step-icon">…</svg>
      <span class="step-phrase">Consultation de <span class="refdes">U12</span></span>
      <button class="step-expand" aria-expanded="false">
        <svg class="chevron">…</svg>
      </button>
    </div>
    <div class="step mb expanded" data-tool="mb_get_rules_for_symptoms">
      <span class="node"></span>
      <svg class="step-icon">…</svg>
      <span class="step-phrase">Lecture des règles pour « no-boot »</span>
      <button class="step-expand" aria-expanded="true">
        <svg class="chevron">…</svg>
      </button>
      <pre class="step-payload">
{
  "args": {"symptoms": ["no-boot"]},
  "result": {"rules": [...]}
}
      </pre>
    </div>
    <div class="step bv" data-tool="bv_highlight_component">
      <span class="node"></span>
      <svg class="step-icon">…</svg>
      <span class="step-phrase">Mise en évidence de <span class="refdes">Q7</span> sur le board</span>
    </div>
  </div>
  <div class="turn-message">
    <!-- markdown rendu + chips injectés -->
    <p>Le symptôme <em>no-boot</em> vient probablement de
      <button class="chip-refdes">U12</button>.
      Teste <button class="chip-refdes">U12</button> sur
      <button class="chip-net">PP_VCC_MAIN</button>…</p>
    <span class="caret"></span>
  </div>
  <div class="turn-foot">
    <span class="foot-time">0.42 s</span>
    <span class="foot-sep">·</span>
    <span class="foot-cost">$0.012</span>
    <span class="foot-sep">·</span>
    <span class="foot-model">opus-4-7</span>
  </div>
</div>
```

### 3.2 Règles de grouping (machine à états)

État porté côté JS par `currentTurn: HTMLElement | null`.

| Event reçu sur WS | Action |
|-------------------|--------|
| `message` role=`user` | Ferme le turn courant (`currentTurn = null`), append un `.msg.user` standalone (garde le langage actuel : left-border cyan, Inter, bg `rgba(56,189,248,.08)`). |
| `thinking` | Si pas de `currentTurn`, en créer un vide. Append `.step.thinking` dans `.turn-rail`. |
| `tool_use` | Si pas de `currentTurn`, en créer un. Append `.step.{mb|bv}` dans `.turn-rail`. |
| `message` role=`assistant` | Si pas de `currentTurn`, en créer un. Si `currentTurn` a déjà un `.turn-message` → **ouvrir un nouveau `currentTurn`**. Append `.turn-message`. |
| `turn_cost` | Poser `.turn-foot` sur `currentTurn`. Ne ferme PAS le turn (l'agent peut enchaîner un autre cycle sans user-message). `currentTurn` reste ouvert tant qu'aucun `user.message` n'arrive. |
| `history_replay_start` / `history_replay_end` | Toggle `.replay` sur le conteneur racine log ; les turns créés pendant le replay héritent visuellement (opacité .55 via `.llm-log.replay .turn`). |
| `session_*`, `context_loaded`, `error`, `session_terminated` | Standalone `.sys` row (inchangé par rapport à aujourd'hui). |
| `boardview.*` | Inchangé, route vers `window.Boardview.apply`. N'ajoute PAS de step dans le rail — le `tool_use` associé (émis avant) a déjà créé la step, le `boardview.*` est l'effet de bord côté renderer. |

**Replay** : les events replay créent la même structure turn-block ; l'opacité et le tag « · replay » sur le turn-foot se posent via un modifier `.replay` (plutôt que par ligne comme aujourd'hui).

### 3.3 Dimensions visuelles du rail

- Rail = `border-left: 1px solid rgba(192,132,252,.35)` sur `.turn-rail`, padding-left 14px.
- Chaque `.step` a `position: relative`. Le nœud (`.node`) : `position: absolute; left: -4px; top: 6px; width: 7px; height: 7px; border-radius: 50%`.
- Couleur du nœud selon la famille :
  - `.step.thinking .node { background: var(--text-3) }`
  - `.step.mb .node { background: var(--cyan); box-shadow: 0 0 0 2px rgba(56,189,248,.15) }`
  - `.step.bv .node { background: var(--violet); box-shadow: 0 0 0 2px rgba(192,132,252,.15) }`
- `.step` spacing : `padding: 5px 0`, pas de séparateur ligne (le rail fait le travail).
- `.turn-rail` s'arrête verticalement après la dernière step. Pas de bordure qui dépasse.
- `.turn-message` a `padding-left: 20px` (s'aligne avec le texte du rail, pas avec le nœud) et `margin-top: 8px` sous le rail.
- `.turn-foot` a `padding-left: 20px; margin-top: 6px`, mono 10px, `color: var(--text-3)`, flex row.

### 3.4 Pulse « agent travaille »

Deux cas :

1. **Rail actif, pas encore de `turn-message`** : un `.step.pending` fantôme en bas du rail, node pulsant `box-shadow: 0 0 0 3px rgba(192,132,252,.25); animation: node-pulse 1.4s ease-in-out infinite`. Retiré à l'arrivée du premier token de `turn-message`.
2. **`turn-message` en streaming** : caret ▍ clignotant en fin de texte (`animation: caret-blink 1s step-end infinite`). Retiré à l'arrivée de `turn_cost`.

```css
@keyframes node-pulse {
  0%,100% { box-shadow: 0 0 0 3px rgba(192,132,252,.15) }
  50%     { box-shadow: 0 0 0 6px rgba(192,132,252,.35) }
}
@keyframes caret-blink {
  0%,50%  { opacity: 1 }
  51%,100%{ opacity: 0 }
}
```

---

## 4. Grammaire des tool-calls

### 4.1 Paraphrases françaises

Chaque tool a une phrase française courte qui remplace la notation `tool_name {args}` brute. Le refdes / target principal du call est inline en mono teinté.

**MB (cyan) — lecture :**

| Tool | Phrase | Exemple rendu |
|------|--------|---------------|
| `mb_get_component` | `Consultation de <refdes>` | `Consultation de U12` |
| `mb_get_rules_for_symptoms` | `Lecture des règles pour « <symptoms joinés> »` | `Lecture des règles pour « no-boot »` |
| `mb_list_findings` | `Revue des findings pour <device>` | `Revue des findings pour iphone-x` |
| `mb_record_finding` | `Enregistrement d'un finding` | `Enregistrement d'un finding` |
| `mb_expand_knowledge` | `Extension du pack — <scope>` | `Extension du pack — U12 / no-boot` |

**BV (violet) — action :**

| Tool | Phrase |
|------|--------|
| `bv_highlight_component` | `Mise en évidence de <refdes>` |
| `bv_focus_component` | `Focus sur <refdes>` |
| `bv_reset_view` | `Réinitialisation de la vue` |
| `bv_highlight_net` | `Highlight du net <net>` |
| `bv_flip_board` | `Retournement du board` |
| `bv_annotate` | `Annotation près de <refdes ou x,y>` |
| `bv_filter_by_type` | `Filtrage par type — <type>` |
| `bv_draw_arrow` | `Flèche de <from> vers <to>` |
| `bv_measure_distance` | `Mesure entre <a> et <b>` |
| `bv_show_pin` | `Affichage pin <n> de <refdes>` |
| `bv_dim_unrelated` | `Atténuation des éléments non liés` |
| `bv_layer_visibility` | `Visibilité de la couche <layer>` |

**Règle de fallback** : si un tool n'a pas de paraphrase (tool inconnu remonté par MA), on retombe sur `<tool_name>` en mono, sans icône, couleur `text-3`. Pas d'erreur visuelle, juste dégradation propre. La table des paraphrases vit en constante JS dans `llm.js` (`TOOL_PHRASES: Record<string, (input) => {html, icon}>`).

### 4.2 Icônes

Jeu de deux icônes 12×12, stroke `currentColor`, width 1.6, fill none (conformément au §icons du CLAUDE.md) :

- MB → **œil** (perception / lecture).
- BV → **cible concentrique** (action / pointage).

Pas d'icônes par tool spécifique — la famille suffit. Garder le jeu minimal.

### 4.3 Expand / collapse

- Chaque `.step.mb` ou `.step.bv` porte un `button.step-expand` aligné à droite avec un chevron ▸ (rotated 90° quand `aria-expanded=true`, transition 0.15s).
- Expand révèle `.step-payload` en `<pre>` mono 10.5px, couleur `--text-2`, bg `var(--panel)`, border-left 2px de la couleur de famille, padding 8px 10px, margin-top 6px, white-space pre-wrap.
- Payload JSON pretty-printé (`JSON.stringify(obj, null, 2)`) avec :
  - `args` toujours présents (on les a dans le `tool_use`)
  - `result` présent **si** le backend le fait remonter sur le WS (voir §10). Si absent, on affiche `args` seul et une note mono gris `— result non rendu par le runtime`.
- `.step.thinking` n'a **pas** de chevron (rien à déplier — le texte complet est déjà visible dans la ligne).

### 4.4 Refdes inline dans la phrase

Dans `step-phrase`, tout refdes ou net rendu est enveloppé en `<span class="refdes">U12</span>` / `<span class="net">PP_VCC_MAIN</span>` :
- `.step.mb .refdes`, `.step.mb .net` → mono 10.5px, `color: var(--cyan)`
- `.step.bv .refdes`, `.step.bv .net` → mono 10.5px, `color: var(--violet)`
- **Pas cliquables** dans le rail (le click activerait une action BV, mais le rail est déjà un log d'actions — on évite les boucles). Le clic reste réservé à la version chip du message assistant (§5.2).

---

## 5. Rendu du message agent

### 5.1 Markdown léger

Le texte livré sur `{type:"message", role:"assistant", text}` est rendu avec un parseur markdown minimal **maison** (30 LOC) ou `marked.js` v11 en UMD via CDN (MIT). Décision retenue : **marked.js via CDN** — robuste, 30kB minifié, pas de build step (cohérent avec le reste de `web/`). Config explicite, `breaks: true`, `gfm: true`, mais HTML désactivé (`sanitize` n'existe plus dans marked ≥ 5 ; on utilise `DOMPurify` 3.x via CDN pour sanitiser la sortie avant injection).

**Subset supporté, imposé par allow-list DOMPurify :**
- `<p>`, `<br>`
- `<strong>`, `<em>`
- `<ul>`, `<ol>`, `<li>`
- `<code>` inline (mono, bg `rgba(148,163,184,.08)`, padding 1px 4px, radius 3px)
- **Pas** de `<pre>` code blocks (casse le flow narratif, rare dans les réponses agent ; si un backtick triple apparaît, il sera rendu comme du inline code concaténé)
- **Pas** de `<a>`, `<img>`, `<table>`, `<blockquote>`, `<h1-6>` — hors périmètre
- **Pas** de HTML brut dans la source markdown (DOMPurify strip)

### 5.2 Chips refdes/net cliquables

**Pipeline de post-traitement**, appliqué **après** le parsing markdown + sanitisation, sur le texte contenu dans les nœuds text (pas dans les balises structurelles) :

1. **Refdes shape** : regex `\b[A-Z]{1,3}\d{1,4}\b`.
   - Match **et** `session.board.part_by_refdes[match]` existe → wrap en `<button type="button" class="chip-refdes" data-refdes="…">U12</button>`.
   - Match **mais** refdes sanitizer-wrapped déjà présent (`⟨?U999⟩`) → laisser tel quel, aucun re-traitement.
   - Match **et** pas dans le board → laisser en texte plat (le sanitizer aurait dû le wrap ; si un refdes connu du board n'est pas mentionné et un inconnu passe, c'est déjà un bug sanitizer — pas notre problème ici).
2. **Net shape** : regex `\b(?:PP_|L\d|VCC_|GND|DVDD_|AVDD_|[A-Z][A-Z0-9_]{2,})\b` (large, mais limité par l'existence du net).
   - Match **et** `session.board.net_by_name[match]` existe → wrap en `<button type="button" class="chip-net" data-net="…">PP_VCC_MAIN</button>`.
   - Sinon → laisser en texte plat.

**Source du board côté frontend** : le frontend reçoit déjà `boardview.board_loaded` qui publie les parts et nets dans `window.Boardview`. On expose deux helpers :
- `window.Boardview.hasRefdes(refdes): boolean`
- `window.Boardview.hasNet(name): boolean`

Si aucun board n'est chargé (`Boardview.hasBoard() === false`), la substitution est désactivée — les refdes restent en texte plat, pas de chips. Symétrique avec le sanitizer backend qui est no-op sans board.

**Interaction chip** :
- `.chip-refdes` : mono 10.5px, bg `rgba(56,189,248,.08)`, `color: var(--cyan)`, border `1px solid rgba(56,189,248,.25)`, padding 1px 6px, radius 3px, cursor pointer, transition 0.15s. Hover → bg `rgba(56,189,248,.16)`, border `rgba(56,189,248,.45)`.
- `.chip-net` : même chose en emerald.
- Clic `.chip-refdes` : appelle `window.Boardview.focus(refdes)` (wrapper sur le `bv_focus_component` WS event — ou side-effect direct côté renderer). **Important** : cette action est frontend-only, elle n'envoie rien au backend. L'agent ne sait pas que le tech a cliqué. C'est voulu — lecture = pas d'intrusion dans la conversation.
- Clic `.chip-net` : `window.Boardview.highlightNet(name)`.
- Même chip apparaît plusieurs fois → chaque occurrence est cliquable (pas de déduplication).

**Refdes wrapped `⟨?U999⟩`** : restent texte plat, couleur amber via sélecteur `.turn-message :is(span,text):contains("⟨?")` (ou via un wrapping explicite fait par le sanitizer côté serveur → on peut envoyer `<span class="refdes-unknown">⟨?U999⟩</span>`, voir §10). Si on ne change pas le backend, le texte reste coloré via un second post-traitement : regex `⟨\?([A-Z]{1,3}\d{1,4})⟩` → `<span class="refdes-unknown">$&</span>`.

### 5.3 Ordre des opérations rendu

```
raw text from WS
   → marked.parse(text, {breaks:true, gfm:true})
   → DOMPurify.sanitize(html, {ALLOWED_TAGS: [...], ALLOWED_ATTR: []})
   → tmp = document.createElement('div'); tmp.innerHTML = sanitized
   → walk text nodes; for each:
       → detect refdes / net / ⟨?⟩ patterns
       → replace match with span/button nodes (using Range + insertNode)
   → for each chip-refdes / chip-net, attach click listener
   → append tmp's children to .turn-message
```

Le walker est linéaire, ignore les nœuds `<code>` (pas de substitution dans le mono inline).

---

## 6. Chrome — header, tier, status, input

### 6.1 Header (hauteur passe de ~44px à ~60px, deux lignes)

```
┌──────────────────────────────────────────────────────────┐
│ [AGENT badge]  iPhone X · rail boot      [FAST ▾]  [×]   │  ← ligne 1 : titre + tier chip + close
│                claude-opus-4-7 · managed · repair 9a3f…  │  ← ligne 2 : sous-ligne mono 10.5px
└──────────────────────────────────────────────────────────┘
```

- **Titre** (ligne 1) : le nom du repair (ex. « iPhone X · rail boot »). Source : le frontend charge déjà cette info quand il ouvre un repair depuis `#home`. Fallback si absent : `device_slug` humanisé (`iphone-x-logic-board` → `iPhone X logic board`).
- **Sous-ligne** (ligne 2) : mono 10.5px, `color: var(--text-3)`. Format : `<model> · <mode> · repair <short_id>`. Source : événement `session_ready` déjà reçu.
- Badge `AGENT` violet conservé tel quel (left).
- Bouton close `×` conservé, à l'extrême droite.

### 6.2 Tier collapse chip + popover

Remplacement complet du `llm-tiers` grid 3-boutons. Le chip vit à côté du `×`, avant lui.

```html
<button class="llm-tier-chip" id="llmTierChip" aria-haspopup="true">
  <span class="tier-label">FAST</span>
  <svg class="chevron-down">…</svg>
</button>
<div class="llm-tier-popover" role="menu" hidden>
  <button role="menuitem" data-tier="fast" class="on">Fast · Haiku 4.5</button>
  <button role="menuitem" data-tier="normal">Normal · Sonnet 4.6</button>
  <button role="menuitem" data-tier="deep">Deep · Opus 4.7</button>
</div>
```

- Chip : mono 10px, padding 3px 8px, même langage que `cost-total` et `device-tag`. Couleur suit le tier actif (`--emerald` / `--cyan` / `--violet`) via classe modifier `.tier-chip[data-tier="fast"]`.
- Popover : position absolute sous le chip, bg `rgba(panel, .96)`, backdrop-filter blur 10px, border 1px, radius 6px, shadow léger. Items alignés en liste verticale, padding 6px 10px chacun. `.on` = background tinté + color tier.
- Trigger : clic chip toggle popover. Clic hors du popover (listener `document.addEventListener('click', …, {capture:true})` avec check `!popover.contains(e.target)`) / Esc → ferme. Clic item → `switchTier(tier)` (fonction existante inchangée) + ferme popover. Le comportement « reconnect WS sur changement » est conservé.
- Le `llm-tiers` grid disparaît du markup ; le gain vertical (~40px) est redistribué au log.

### 6.3 Status strip — cost unifié avec delta

Markup quasi inchangé, contenu du `cost-total` enrichi :

```
[dot] connecté · iphone-x · fast        $0.082 · +$0.012 dernier
```

- Cost-chip inline dans les messages : **supprimé**. `attachCostChipToLastAssistant()` et la classe `.cost-chip` disparaissent. Cette info migre dans `.turn-foot` (§3.1) pour le détail par turn et reste agrégée ici pour la session.
- `.cost-total` affiche : `<total> · +<delta dernier turn> dernier` quand `sessionTurns > 0` et qu'on a un dernier cost.
- Classe `.hot` conservée (bascule ≥ $0.50), s'applique aussi si le delta du dernier turn ≥ $0.10.

### 6.4 Input — textarea auto-grow

Remplacement de `<input type="text">` par `<textarea>` :

```html
<form class="llm-input" id="llmForm">
  <textarea id="llmInput" rows="1" placeholder="Pose ta question à l'agent…" …></textarea>
  <div class="llm-input-actions">
    <button type="button" class="llm-stop" id="llmStop" title="Interrompre l'agent (Esc)" disabled>…</button>
    <button type="submit" id="llmSend" disabled>Envoyer</button>
  </div>
</form>
```

- Textarea : `resize: none`, min-height 36px (1 ligne), max-height 120px (~5 lignes), `overflow-y: auto` au-delà. Auto-grow géré par listener `input` qui met `el.style.height = 'auto'` puis `el.style.height = Math.min(el.scrollHeight, 120) + 'px'`. `field-sizing: content` n'est pas utilisé comme chemin principal (support navigateur encore partiel fin 2025) — le JS est la voie fiable.
- Enter → submit (préventé) et reset hauteur à 36px. Shift+Enter → insertion d'un `\n` (défaut navigateur).
- Stop + Envoyer passent **sous** la textarea dans une ligne d'actions flex row, Stop à gauche, Envoyer à droite. Gain : la textarea peut respirer sans partager horizontalement sa largeur.
- Focus state : border `var(--cyan)` comme aujourd'hui.
- Placeholder inchangé.

---

## 7. User message — léger polish

Gardé presque identique (cyan left-border 2px, bg `rgba(56,189,248,.08)`, Inter 12.5px) avec un seul ajustement :

- Suppression du label `role = "Toi"` au-dessus (la couleur left-border suffit comme signal). Gain de 12px vertical par message user.
- Le texte user ne reçoit PAS le pipeline markdown/chip (c'est le tech qui tape, le texte est littéral).

---

## 8. Tokens et classes — résumé des ajouts

### 8.1 Fichier `web/styles/llm.css` — additions

```css
/* Turn block */
.turn { display: grid; grid-template-columns: 1fr; padding: 6px 4px 10px; }
.turn + .turn { border-top: 1px dashed var(--border-soft); margin-top: 8px; padding-top: 12px; }
.turn-rail { border-left: 1px solid rgba(192,132,252,.35); padding-left: 14px; }
.turn-message { padding-left: 20px; margin-top: 8px; font-size: 12.5px; line-height: 1.55; }
.turn-foot { padding-left: 20px; margin-top: 6px; font-family: var(--mono); font-size: 10px;
             color: var(--text-3); display: flex; gap: 8px; }
.turn-foot .foot-sep { opacity: .5 }

/* Steps */
.step { position: relative; padding: 5px 0; display: flex; align-items: center; gap: 8px;
        font-size: 11.5px; color: var(--text-2); }
.step .node { position: absolute; left: -18px; top: 10px; width: 7px; height: 7px; border-radius: 50%;
              background: var(--text-3); }
.step.thinking { font-style: italic; color: var(--text-3); }
.step.mb .node { background: var(--cyan); box-shadow: 0 0 0 2px rgba(56,189,248,.15); }
.step.bv .node { background: var(--violet); box-shadow: 0 0 0 2px rgba(192,132,252,.15); }
.step.pending .node { animation: node-pulse 1.4s ease-in-out infinite; }
.step .step-icon { width: 12px; height: 12px; flex-shrink: 0; }
.step.mb .step-icon { color: var(--cyan); }
.step.bv .step-icon { color: var(--violet); }
.step .step-phrase { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step .refdes, .step .net { font-family: var(--mono); font-size: 10.5px; }
.step.mb .refdes, .step.mb .net { color: var(--cyan); }
.step.bv .refdes, .step.bv .net { color: var(--violet); }
.step .step-expand { background: none; border: 0; color: var(--text-3); cursor: pointer;
                     padding: 2px 4px; border-radius: 3px; transition: color .15s; }
.step .step-expand:hover { color: var(--text-2); }
.step .chevron { width: 10px; height: 10px; transition: transform .15s; }
.step[aria-expanded="true"] .chevron { transform: rotate(90deg); }
.step-payload { display: none; grid-column: 1 / -1; margin: 6px 0 2px 20px;
                background: var(--panel); border-left: 2px solid currentColor;
                padding: 8px 10px; border-radius: 4px;
                font-family: var(--mono); font-size: 10.5px; color: var(--text-2);
                white-space: pre-wrap; overflow-x: auto; }
.step.mb .step-payload { color: var(--cyan); } /* border-left */
.step.bv .step-payload { color: var(--violet); }
.step.expanded .step-payload { display: block; }

/* Caret + pulse */
.caret { display: inline-block; width: 2px; height: 1em; margin-left: 2px;
         background: var(--violet); animation: caret-blink 1s step-end infinite;
         vertical-align: text-bottom; }
@keyframes caret-blink { 0%,50% { opacity: 1 } 51%,100% { opacity: 0 } }
@keyframes node-pulse {
  0%,100% { box-shadow: 0 0 0 3px rgba(192,132,252,.15) }
  50%     { box-shadow: 0 0 0 6px rgba(192,132,252,.35) }
}

/* Chips cliquables dans turn-message */
.chip-refdes, .chip-net {
  display: inline-block; font-family: var(--mono); font-size: 10.5px;
  padding: 1px 6px; margin: 0 1px; border-radius: 3px; cursor: pointer;
  background: transparent; transition: background .15s, border-color .15s;
  vertical-align: baseline;
}
.chip-refdes { color: var(--cyan); border: 1px solid rgba(56,189,248,.25); }
.chip-refdes:hover { background: rgba(56,189,248,.12); border-color: rgba(56,189,248,.45); }
.chip-net { color: var(--emerald); border: 1px solid rgba(52,211,153,.25); }
.chip-net:hover { background: rgba(52,211,153,.12); border-color: rgba(52,211,153,.45); }
.refdes-unknown { color: var(--amber); font-family: var(--mono); font-size: 10.5px; }

/* Header polish */
.llm-head { padding: 8px 14px 10px; align-items: flex-start; flex-wrap: wrap; }
.llm-head .title-col { flex: 1; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.llm-head h3 { margin: 0; font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.llm-head .llm-subline { font-family: var(--mono); font-size: 10.5px; color: var(--text-3);
                        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Tier chip + popover */
.llm-tier-chip { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px;
                 font-family: var(--mono); font-size: 10px; letter-spacing: .4px;
                 background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
                 cursor: pointer; transition: all .15s; color: var(--text-2); }
.llm-tier-chip[data-tier="fast"] { color: var(--emerald); border-color: rgba(52,211,153,.35); background: rgba(52,211,153,.08); }
.llm-tier-chip[data-tier="normal"] { color: var(--cyan); border-color: rgba(56,189,248,.35); background: rgba(56,189,248,.08); }
.llm-tier-chip[data-tier="deep"] { color: var(--violet); border-color: rgba(192,132,252,.35); background: rgba(192,132,252,.08); }
.llm-tier-popover { position: absolute; top: calc(100% + 4px); right: 0; min-width: 180px;
                    background: rgba(26,40,61,.96); backdrop-filter: blur(10px);
                    border: 1px solid var(--border); border-radius: 6px;
                    box-shadow: 0 8px 24px rgba(0,0,0,.35);
                    padding: 4px; z-index: 40; display: flex; flex-direction: column; gap: 2px; }
.llm-tier-popover[hidden] { display: none; }
.llm-tier-popover button { text-align: left; background: transparent; border: 0;
                           padding: 6px 10px; border-radius: 4px; cursor: pointer;
                           font-size: 11.5px; color: var(--text-2); }
.llm-tier-popover button:hover { background: var(--panel-2); color: var(--text); }
.llm-tier-popover button.on { color: var(--text); background: rgba(192,132,252,.08); }

/* Input textarea */
.llm-input { flex-direction: column; gap: 8px; }
.llm-input textarea { flex: 1; background: var(--panel); border: 1px solid var(--border);
                      color: var(--text); border-radius: 5px; padding: 8px 10px;
                      font-family: inherit; font-size: 12.5px; outline: none;
                      min-height: 36px; max-height: 120px; resize: none;
                      transition: border-color .15s; line-height: 1.45; }
.llm-input textarea:focus { border-color: var(--cyan); }
.llm-input-actions { display: flex; gap: 6px; justify-content: space-between; }

/* Suppressions */
.llm-tiers { display: none; } /* remplacé par .llm-tier-chip */
.cost-chip { display: none; }  /* fusionné dans turn-foot */
.llm-log .msg .role { /* hidden on user messages */ }
.llm-log .msg.user .role { display: none; }
```

### 8.2 Fichier `web/js/llm.js` — modules ajoutés

Pas de split en nouveaux fichiers — `llm.js` reste le seul module. Sections logiques ajoutées dans le fichier :

- `TOOL_PHRASES: Record<string, (input) => { phrase, icon: "mb" | "bv", target?: string }>` — table des paraphrases (§4.1).
- `createTurn(): HTMLElement` — pose un `.turn` neuf dans le log, l'attache à `currentTurn`.
- `appendStep(turn, kind, {tool, input})` — kind ∈ `thinking` | `mb` | `bv`. Rend la phrase, attache le payload collapsed.
- `renderAssistantMarkup(text, turn)` — pipeline markdown → sanitize → walk text nodes → chips → insert.
- `Boardview.hasRefdes`, `.hasNet`, `.focus`, `.highlightNet` — ajouts à l'API publique du renderer (`web/brd_viewer.js`, additions petites).
- `openTierPopover()` / `closeTierPopover()`.
- `applyCostToTurn(turn, payload)` — remplace `attachCostChipToLastAssistant`.

---

## 9. Dépendances externes

Deux libs via CDN, chargées dans `<head>` de `web/index.html` avec `defer` :

- **marked.js 11.x** (MIT) — `https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js`
- **DOMPurify 3.x** (Apache 2.0) — `https://cdn.jsdelivr.net/npm/dompurify@3.0.8/dist/purify.min.js`

Les deux satisfont la contrainte licence du CLAUDE.md (MIT, Apache 2.0). Poids combiné : ~55kB gzipped. Pas de build step, cohérent avec l'approche vanilla du projet. Si CDN fallback nécessaire plus tard → vendor dans `web/vendor/`.

---

## 10. Extensions backend optionnelles (hors scope strict)

Deux petites améliorations backend rendraient le rendu plus riche mais **ne sont pas un pré-requis** à ce redesign :

1. **Remontée des `tool_result` sur le WS.** Aujourd'hui le runtime dispatch les tools et renvoie le `user.custom_tool_result` à MA sans écho vers le frontend. Un nouvel event `{type: "tool_result", tool_use_id, result}` permettrait de remplir le payload expand `.step-payload` avec le vrai résultat (§4.3). Si non livré, la zone expand reste ouverte avec une note « result non rendu » — aucune régression.

2. **Sanitizer qui émet du markup.** Aujourd'hui le sanitizer wrap en texte `⟨?U999⟩`. Il pourrait émettre `<span class="refdes-unknown">⟨?U999⟩</span>` directement, ce qui éviterait la regex frontend dédiée (§5.2). Mais tant que le frontend parse markdown (qui strippe les HTML inconnus), cette info devrait passer via une convention markdown (ex: `` `⟨?U999⟩` `` en inline code avec classe spéciale). Plus lourd que la regex frontend. **Décision** : garder le sanitizer tel quel, frontend fait la colorisation.

Ces deux extensions peuvent être livrées après coup, sans breakage.

---

## 11. Tests et vérifications

Pas de test Python neuf (backend inchangé). Validation manuelle UI dans le navigateur, couvrant :

- **Turn-block** : envoi d'un message, vérif que thinking + tool_uses consécutifs apparaissent sous le même rail, que l'assistant message clos le turn, que le turn_cost remplit le foot.
- **Multi-turn** : l'agent enchaîne deux messages agent sans user message (ex : message → tool_use → message). Deux turn-blocks distincts, pas un seul rail fusionné.
- **Replay** : ouvrir un repair existant. Les turns restaurés sont à opacité .55, structure identique à un turn live, cost-foot présent.
- **Chips** : réponse agent qui cite `U12` (existe dans board chargé) → cyan chip cliquable. Clic → `Boardview.focus('U12')` (vérifier focus visible sur le canvas). `PP_VCC_MAIN` → emerald chip, clic highlight le net. `U999` inconnu wrap `⟨?U999⟩` amber.
- **Typewriter** : envoi, caret clignote avant l'arrivée du premier token, persiste pendant le stream, disparaît au turn_cost.
- **Pulse** : envoi d'un message déclenchant plusieurs tools. Pendant les tools, nœud fantôme pulse en bas du rail. Disparaît au premier token du message final.
- **Tier popover** : clic sur le chip FAST ouvre le popover, sélection Deep reconnecte le WS (comportement actuel préservé), chip devient violet « DEEP ». Clic dehors ferme.
- **Textarea** : tape un paragraphe, la hauteur grandit jusqu'à 5 lignes puis scroll. Enter envoie, Shift+Enter newline, reset à 36px après envoi.
- **Cost delta** : après 3 turns, `$0.082 · +$0.012 dernier`. Classe `.hot` déclenchée par delta ≥ $0.10.
- **No board** : ouvrir le panel sans board chargé, les chips ne se forment pas, refdes restent en texte plat. Sanitizer inactif → pas de `⟨?⟩` non plus.
- **Escape-to-interrupt** : comportement existant préservé (premier Esc interrompt si agent live, sinon ferme).
- **Message envoyé en plein stream** : comportement existant préservé — `submit` envoie `{type:"message",…}` sans interrompre automatiquement. Si le tech veut interrompre d'abord, il clique Stop (bouton visible en bas de la textarea).

---

## 12. Points hors scope explicites

- Pas de nouveau event backend, pas de changement de contrat WS (sauf §10 optionnel).
- Pas de photo-attach, pas de place réservée.
- Pas de resize du panel (420px fixé).
- Pas de copier/coller enrichi, pas d'export chat.
- Pas d'affichage des citations (`citations` MA feature) — les messages restent monolithiques.
- Pas de scroll-to-latest automatique enrichi (le `scrollTop = scrollHeight` actuel suffit).
- Pas de dark/light toggle — le panel reste dark comme tout le reste de l'app.
- Pas de refactoring de `brd_viewer.js` autre que l'ajout des 4 helpers de l'API publique.
