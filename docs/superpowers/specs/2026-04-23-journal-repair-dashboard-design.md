# Journal — dashboard de réparation — design spec

**Date :** 2026-04-23
**Scope :** transformer la section `#home` (« Journal des réparations ») en interface à deux états. État **liste** inchangé (cards brand > model, comme aujourd'hui). Nouvel état **dashboard** qui s'active quand une réparation est ouverte (`?device=<slug>&repair=<rid>`) — remplace la liste par une vue focalisée sur la session (symptôme, device, findings, timeline, pack status, conversations, raccourcis vers PCB/Graphe/Schematic/Memory Bank). Ajout d'une **pastille de session** persistante dans le topbar, toujours visible pendant qu'une session est active. Ajout d'un bouton **Quitter la session** (dans le dashboard et sur la pastille topbar) qui nettoie l'URL et revient à la liste. Nouvelle route d'entrée : cliquer une carte depuis la liste n'envoie plus sur `#graphe` mais sur le dashboard.
**Hors scope :** protocole WS `/ws/diagnostic/{slug}`, runtimes `api/agent/runtime_*.py`, manifest/tools, sanitizer, changement du panel chat lui-même (le panel garde sa forme actuelle et son overlay droit). Aucun nouveau sous-système backend — seulement une route HTTP supplémentaire (findings par device). Aucune modification du modèle de données sur disque. Le graphe D3 (`#graphe`) et le boardview (`#pcb`) restent tels quels.

---

## 1. Contexte

Aujourd'hui, la section `#home` est purement une **liste plate** de cards `repair_card` regroupées par brand > model (`web/js/home.js::renderHome`). Chaque carte pointe sur `?device=<slug>&repair=<rid>` — sans hash. Le bootstrap `web/js/main.js` applique alors la règle `slug ? "graphe" : "home"` et envoie l'user direct sur `#graphe`, puis `openLLMPanelIfRepairParam()` ouvre le panel chat à droite. Résultat : l'user quitte la liste et atterrit sur le graphe du device, avec le chat en overlay. Rien ne matérialise le fait qu'une **session** (au sens « ticket client en cours ») est active.

**Ce qui marche.** La liste est claire, le classement brand > model tient avec plusieurs devices, les badges de statut (`open` / `in_progress` / `closed`) donnent l'état en un coup d'œil. L'URL `?device=&repair=` est déjà la source de vérité — bookmarkable, shareable.

**Ce qui manque.**

1. **Zéro sensation de session active.** Une fois sur `#graphe`, la mode-pill affiche `MÉMOIRE · Graphe de connaissances` (pareil qu'une simple visite du graphe sans session), le breadcrumb mentionne « Réparations / iPhone X / Graphe » mais passe inaperçu. Rien ne dit « tu es dans la session `a1b2c3d4` ». Pareil sur `#pcb`, `#schematic`, `#memory-bank`.
2. **Pas de dashboard de session.** Le symptôme (`repair.symptom`) tapé à la création n'est visible que sur la carte de la liste — il disparaît dès qu'on ouvre la session. Les findings enregistrés par l'agent (cross-session mémoire) ne sont visibles que via `mb_list_findings` côté agent, jamais côté tech. Le tech n'a aucune vue unifiée « voici où en est cette réparation ».
3. **Pas de sortie franche de session.** Pour quitter, il faut manuellement éditer l'URL ou cliquer une autre section (mais les query params restent !) puis revenir sur `#home`. Le bouton **×** du panel chat ferme le panel mais pas la session. Le concept même de « quitter une session pour en ouvrir une autre » n'existe pas dans l'UI.
4. **Rail Journal peu informatif.** Cliquer l'icône Journal dans le rail ouvre `#home` + liste complète. Même quand une session est active, on revient sur la liste plate — on ne voit pas du tout que *notre* session est différente des autres dans le grid.

On transforme donc le Journal en **hub de session** : un dashboard dédié quand une session est ouverte, une liste classique sinon. La transition est portée par l'URL (`?device=&repair=` → dashboard ; absent → liste), sans variable globale ni localStorage.

---

## 2. Décisions directrices

1. **URL = source unique de vérité pour la session active.** La présence *simultanée* de `?device=<slug>&repair=<rid>` dans la query string définit une session ouverte. Pas de nouvel état en mémoire JS, pas de persistance côté navigateur. Bookmark + partage de lien restent valides.
2. **Journal à deux états, dérivés de l'URL.** `renderHome()` ajoute une branche : si `device+repair` sont présents → render dashboard ; sinon → render liste. Aucun changement au layout du topbar ou du rail.
3. **Entrée dans une session = dashboard, pas graphe.** Cliquer une carte de la liste envoie désormais sur `?device=X&repair=R#home` (hash explicite). Le bootstrap `main.js` gagne la règle : `device+repair → "home"` (avant la règle `slug seul → "graphe"` qui reste pour la navigation « voir le graphe d'un pack »).
4. **Dashboard riche.** Contenu (section §4) : header (device + symptôme + statut + bouton Quitter) → 4 tuiles raccourcis (PCB, Graphe, Schematic, Memory Bank) → conversations de la réparation → findings du device → timeline d'activité → statut du pack. Priorité à la densité info : vision « workshop bench », pas splash screen.
5. **Pastille de session dans le topbar.** Chip persistante à droite de la mode-pill existante, style cyan (famille « component »), format `SESSION · {brand-model} · {repair_id[:8]}` + icône `[×]`. Clic corps = navigation vers `#home` en gardant les query params (retour dashboard). Clic `[×]` = **Quitter la session** global. Cachée quand aucune session active.
6. **Chat auto-ouvert comme aujourd'hui.** L'entrée dans un dashboard déclenche `openLLMPanelIfRepairParam()` comme aujourd'hui — le panel s'ouvre à droite, la session WS démarre. Pas de changement dans la logique `llm.js`.
7. **Quitter la session = nettoyage URL.** Retire `device` et `repair` des query params, remet le hash à `#home`, ferme le panel chat, rafraîchit la liste. Un seul `history.replaceState` + un `hashchange` émis manuellement pour déclencher la dérivation.
8. **Rail Journal contextuel.** Cliquer l'icône Journal quand une session est active = dashboard (hash `#home`, params préservés). Sans session = liste. Le comportement est dérivé automatiquement par la même branche dans `renderHome()`.
9. **Aucune variable CSS ou token repeint.** Réutilise `--cyan` / `--bg-2` / `--panel` / `--border`. Les tuiles raccourcis reprennent le style des cards existantes. Rien ne sort du design system établi.
10. **Pas de régression sur la liste.** Le mode liste est *exactement* ce qui existe aujourd'hui — code, markup, CSS. On ajoute à côté, on ne réécrit pas.

---

## 3. Modèle de routage & état

### 3.1 Matrice URL → rendu

| URL                                       | `currentSection()` | Journal rend       | Pastille topbar | Chat auto-ouvert | Mode-pill                            |
|-------------------------------------------|--------------------|--------------------|-----------------|------------------|--------------------------------------|
| `/#home` (ou `/`)                         | `home`             | **liste**          | cachée          | non              | `JOURNAL · Réparations` (cyan)       |
| `/?device=X#home` (rare — slug sans repair) | `home`             | **liste** + filtre device (*optionnel, §9.2*) | cachée | non              | `JOURNAL · Réparations` (cyan)       |
| `/?device=X&repair=R#home`                | `home`             | **dashboard**      | visible         | oui              | `JOURNAL · Session` (cyan)           |
| `/?device=X&repair=R#graphe`              | `graphe`           | (pas rendu)        | visible         | oui              | `MÉMOIRE · Graphe` (cyan, inchangé)  |
| `/?device=X&repair=R#pcb`                 | `pcb`              | (pas rendu)        | visible         | oui              | `OUTIL · Boardview` (cyan, inchangé) |
| `/?device=X#graphe` (pack browsing)       | `graphe`           | (pas rendu)        | cachée          | non              | `MÉMOIRE · Graphe` (cyan, inchangé)  |

Dérivation de la session active (utilitaire à exporter depuis `router.js`, nouvelle fonction `currentSession()`) :

```js
export function currentSession() {
  const params = new URLSearchParams(window.location.search);
  const device = params.get("device");
  const repair = params.get("repair");
  if (device && repair) return { device, repair };
  return null;
}
```

Les modules frontend existants qui utilisent `new URLSearchParams(window.location.search)` à la main (`llm.js::currentDeviceSlug`, `llm.js::currentRepairId`, `pipeline_progress.js`) gardent leur logique locale ; la nouvelle fonction est ajoutée sans retrait, on n'impose pas une refacto globale.

### 3.2 Entrée : cliquer une card de liste

Dans `home.js::repairCardHTML`, remplacer :

```js
const href = `?device=${encodeURIComponent(repair.device_slug)}&repair=${encodeURIComponent(repair.repair_id)}`;
```

par :

```js
const href = `?device=${encodeURIComponent(repair.device_slug)}&repair=${encodeURIComponent(repair.repair_id)}#home`;
```

Dans `main.js::bootstrap` :

```js
const hash = window.location.hash;
const params = new URLSearchParams(window.location.search);
const device = params.get("device");
const repair = params.get("repair");

const initial = hash
  ? currentSection()
  : (device && repair ? "home"      // ← NEW: session → dashboard
     : device        ? "graphe"      // pack browsing → graphe
     :                 "home");       // cold start → list
```

### 3.3 Sortie : « Quitter la session »

Fonction unique exportée par `router.js` et appelée depuis 2 endroits (bouton dashboard + `[×]` pastille topbar) :

```js
export function leaveSession() {
  const url = new URL(window.location.href);
  url.searchParams.delete("device");
  url.searchParams.delete("repair");
  url.hash = "#home";
  window.history.replaceState({}, "", url.toString());
  // Fermer le panel chat s'il est ouvert.
  document.getElementById("llmClose")?.click();
  // Déclencher la dérivation — refreshChrome() + renderHome() en mode liste.
  navigate("home");
  // Recharger les données pour avoir la liste à jour.
  Promise.all([loadHomePacks(), loadTaxonomy(), loadRepairs()])
    .then(([packs, tax, reps]) => renderHome(packs, tax, reps));
}
```

Le `llmClose` synthétique fonctionne parce que le handler existant (`llm.js::closePanel`) tolère l'appel en no-op quand le panel n'est pas ouvert.

---

## 4. Anatomie du dashboard

### 4.1 Hiérarchie DOM

Nouveau bloc `#repairDashboard` inséré dans `<section class="home" id="homeSection">`, sibling de `#homeSections` et `#homeEmpty`. `renderHome()` montre un seul des trois à la fois :

```html
<section class="home hidden" id="homeSection">
  <header class="home-head">…</header>

  <!-- État LISTE (existant) -->
  <div class="home-sections" id="homeSections"></div>
  <div class="home-empty hidden" id="homeEmpty">…</div>

  <!-- État DASHBOARD (nouveau) -->
  <div class="repair-dashboard hidden" id="repairDashboard">
    <header class="rd-head">
      <div class="rd-head-left">
        <span class="rd-slug mono">iphone-x-logic-board</span>
        <h1 class="rd-device">iPhone X · Logic Board</h1>
        <p class="rd-symptom">pas d'image, vibreur OK, pas de charge</p>
        <div class="rd-badges">
          <span class="badge warn">en cours</span>
          <span class="badge mono">a1b2c3d4</span>
          <span class="rd-created mono">créée il y a 2 h</span>
        </div>
      </div>
      <div class="rd-head-right">
        <button class="btn ghost" id="rdLeaveBtn">
          <svg class="icon icon-sm" …/><span>Quitter la session</span>
        </button>
      </div>
    </header>

    <!-- Raccourcis outils -->
    <section class="rd-tiles">
      <a class="rd-tile" data-tool="pcb" href="?…#pcb">
        <div class="rd-tile-head">
          <svg class="rd-tile-icon" …/>
          <span class="rd-tile-title">Boardview</span>
        </div>
        <p class="rd-tile-meta">iphone-x-logic-board.kicad_pcb</p>
      </a>
      <a class="rd-tile" data-tool="graphe" href="?…#graphe">…</a>
      <a class="rd-tile" data-tool="schematic" href="?…#schematic">…</a>
      <a class="rd-tile" data-tool="memory-bank" href="?…#memory-bank">…</a>
    </section>

    <!-- Corps 2 colonnes -->
    <div class="rd-body">
      <aside class="rd-col rd-col-primary">
        <section class="rd-block" id="rdBlockConvs">
          <header class="rd-block-head">
            <span class="rd-block-tag mono">conversations</span>
            <h2>Fils de diagnostic</h2>
            <span class="rd-block-count mono">2</span>
          </header>
          <div class="rd-block-body">
            <!-- items injectés par JS, shape = llm.js::renderConvItems -->
          </div>
        </section>

        <section class="rd-block" id="rdBlockFindings">
          <header class="rd-block-head">
            <span class="rd-block-tag mono">field_reports/</span>
            <h2>Findings enregistrés</h2>
            <span class="rd-block-count mono">3</span>
          </header>
          <div class="rd-block-body">…</div>
        </section>
      </aside>

      <aside class="rd-col rd-col-secondary">
        <section class="rd-block" id="rdBlockTimeline">
          <header class="rd-block-head">
            <span class="rd-block-tag mono">activité</span>
            <h2>Timeline</h2>
          </header>
          <ol class="rd-timeline">…</ol>
        </section>

        <section class="rd-block" id="rdBlockPack">
          <header class="rd-block-head">
            <span class="rd-block-tag mono">pack</span>
            <h2>Mémoire du device</h2>
          </header>
          <div class="rd-block-body">…</div>
        </section>
      </aside>
    </div>
  </div>
</section>
```

### 4.2 En-tête du dashboard (`.rd-head`)

- **Gauche** :
  - `rd-slug` — le `device_slug` en JetBrains Mono, `--text-3`, 11 px, uppercase — pattern « workshop label » (voir `tokens.css`).
  - `rd-device` — le nom humain (`deviceName()` incluant brand, réutilisé depuis `home.js`), Inter, 22 px, `--text`.
  - `rd-symptom` — le symptôme tel que tapé à la création, Inter 14 px, `--text-2`, max 2 lignes puis ellipsis.
  - `rd-badges` — statut (`open` / `in_progress` / `closed`, réutilise `statusBadgeHTML` de `home.js`), repair_id court en chip mono, date relative.
- **Droite** :
  - Bouton `rdLeaveBtn` — style `.btn.ghost` existant (voir `agent-chat-panel-design.md`), icône porte-de-sortie, hover → border cyan, clic → `leaveSession()`.

### 4.3 Tuiles raccourcis (`.rd-tiles`)

Grid 4 colonnes (passe à 2 si `body.llm-open` réduit la largeur — `@media (max-width: 960px)` en fait 2, les tuiles restent cliquables en full-card).

Chaque tuile est un `<a class="rd-tile" href="?device=X&repair=R#{section}">`. Le hash pointe sur la section correspondante ; les query params sont conservés, donc la session reste active.

Contenu d'une tuile :

- Icône 20 px stroke (PCB : rectangle avec traces ; Graphe : 3 nœuds + liens ; Schematic : capacitor + résistance ; Memory Bank : livre ouvert avec signet).
- Titre (Inter 14 px, `--text`).
- Ligne de meta (mono 11 px, `--text-3`) — pour PCB : le nom du fichier board si détecté (`SessionState.from_device()`), pour Graphe : « APPROUVÉ » / « en construction » depuis `pack.audit_verdict`, pour Schematic : « 12 pages · 847 composants » si `electrical_graph.json` existe, sinon « Non importé », pour Memory Bank : nombre de rules + nombre de findings.

Hover : élévation standard (`--panel` → `--panel-2`, border `--cyan`), transition .15s. Focus-visible : outline cyan 1.5 px.

### 4.4 Bloc conversations (`.rd-block#rdBlockConvs`)

Source : `GET /pipeline/repairs/{rid}/conversations` (existe déjà, voir `api/pipeline/__init__.py:516`).

Chaque ligne : tier chip coloré (fast/normal/deep, réutilise `.conv-item-tier` de `llm.css`), titre (`conv.title` ou « Conversation N »), meta (`turns`, `cost_usd`, `last_turn_at` relatif). Clic → ouvre le panel chat sur cette conv (appelle le handler existant de `llm.js::switchConv(convId)`, exposé via `window.openConversation(convId)` ou `import { switchConv } from './llm.js'` selon faisabilité — §7.2).

Bouton « Nouvelle conversation » en bas du bloc, même action que le bouton `+` du popover conv actuel. Utile quand la session a plusieurs threads (ex. tech veut tester une autre piste sans polluer le fil principal).

### 4.5 Bloc findings (`.rd-block#rdBlockFindings`)

**Source : nouvelle route** `GET /pipeline/packs/{slug}/findings` (§5) — renvoie tous les `FieldReport` du device, tels que `mb_list_findings` les voit côté agent.

Chaque ligne :

```
[U12 · no-boot]  confirmed_cause: via impedance on PP_VBUS_MAIN          2026-03-02
                 session a1b2c3d4 · ce repair                                     ↗
```

Format : refdes + symptôme en chip mono cyan (cliquable → focus boardview si session en cours), `confirmed_cause` en ligne principale, bas de ligne : badge `ce repair` (violet — action) si `session_id == current_repair_id`, sinon `report.session_id[:8]` en mono gris. Icône ↗ en bout de ligne → expand pour voir `notes` + `mechanism`.

Empty state : « Aucun finding pour ce device. L'agent en enregistre via `mb_record_finding` quand tu confirmes une panne. »

### 4.6 Bloc timeline (`.rd-block#rdBlockTimeline`)

Dérivé **côté frontend** à partir des données déjà chargées — pas d'endpoint dédié. Collecte 4 à 8 événements :

1. `repair.created_at` → « Session ouverte »
2. `pack.audit_verdict_created_at` (si disponible) → « Pack audité » + badge ok/warn/err
3. Pour chaque conv : `conv.last_turn_at` → « Dernière activité · `{tier}` · `{turns}` turns »
4. Pour chaque finding de ce repair : `finding.created_at` → « Finding `{refdes}` confirmé »

Tri desc (newest first), max 8 entrées, ellipsis « `+N plus anciens` » si plus.

Rendu : rail vertical simple (`.rd-timeline`), chaque item a un nœud (6 px rond, couleur famille : cyan pour Journal, emerald pour diagnostic, violet pour action). Format : heure relative (mono 11 px) + phrase Inter 13 px.

### 4.7 Bloc pack (`.rd-block#rdBlockPack`)

Source : `GET /pipeline/packs/{slug}` (existe).

Affichage compact :

- Statut global — `APPROUVÉ` (ok) / `en construction` (warn) / `non audité` (warn).
- 5 pastilles d'artefacts : `registry`, `graph`, `rules`, `dictionary`, `audit` — chacune `✓` ok si `pack.has_*` est true, sinon `·` gris. Clic `✓` graph → navigue vers `#graphe` (mêmes params). Clic `✓` rules / dictionary → navigue vers `#memory-bank`.
- Bouton « Étendre la mémoire » (violet/action) → appelle `mb_expand_knowledge` via un endpoint HTTP dédié (*optionnel, §9.1*), ou reste « conversation avec l'agent » si on laisse l'extension au tool-call runtime.

---

## 5. Extension backend — nouvelle route findings

Une seule route à ajouter, dans `api/pipeline/__init__.py` :

```python
@router.get("/packs/{device_slug}/findings")
async def list_device_findings(device_slug: str) -> list[dict]:
    """Return every field report recorded for this device, newest first.

    Mirrors what `mb_list_findings` sees at agent-tool scope, but exposes it
    to the web UI so the Journal dashboard can render the cross-session
    memory without going through a WS round-trip. No new shape: same
    FieldReport dataclass already defined in api/agent/field_reports.py.
    """
    reports = list_field_reports(device_slug=_validate_slug(device_slug))
    return [r.to_dict() for r in reports]
```

Où `list_field_reports()` existe déjà (`api/agent/field_reports.py`). `FieldReport.to_dict()` à ajouter si absent — champs publics uniquement : `report_id`, `refdes`, `symptom`, `confirmed_cause`, `mechanism`, `notes`, `session_id`, `created_at`. Pas de version MA memory-store ici — strictement la source JSON sur disque.

Pas d'autre route. Le reste réutilise l'existant (`/pipeline/repairs/{rid}`, `/pipeline/repairs/{rid}/conversations`, `/pipeline/packs/{slug}`).

---

## 6. Pastille de session topbar

### 6.1 Markup

Ajout dans `web/index.html` au topbar (`<div class="topbar">`), juste avant la section `.topbar-right` existante :

```html
<button class="session-pill hidden" id="sessionPill" aria-label="Session active — clic pour ouvrir le dashboard">
  <svg class="session-pill-icon" …/>
  <span class="session-pill-text">
    <span class="session-pill-tag mono">SESSION</span>
    <span class="session-pill-device">iPhone X</span>
    <span class="session-pill-sep">·</span>
    <span class="session-pill-rid mono">a1b2c3d4</span>
  </span>
  <button class="session-pill-close" id="sessionPillClose" aria-label="Quitter la session" title="Quitter la session">
    <svg class="icon icon-sm" …/>
  </button>
</button>
```

Note : bouton imbriqué dans bouton n'est pas HTML valide. Implémentation réelle : le wrapper est un `<div class="session-pill">` avec tabindex=0 et rôle button, le corps (device + rid) est cliquable via handler JS, l'icône `[×]` est un vrai `<button>`. Ça évite l'imbrication invalide tout en gardant les deux cibles de clic distinctes pour le tech.

### 6.2 Style

Réutilise la grammaire `.mode-pill` (voir `web/styles/tokens.css`). Cyan — pastille d'information. `--panel-2` au repos, `--cyan` border, pulse subtil 2.4 s quand la session vient d'être ouverte (1 cycle seulement, puis statique). Hover : background `--panel` (légèrement plus clair), border `--cyan` saturé. L'icône `[×]` a son propre hover → background `--panel-2` + couleur `--amber`.

### 6.3 Wiring

Dans `router.js::updateChrome()` :

```js
const session = currentSession();
const pill = document.getElementById("sessionPill");
if (session) {
  pill.classList.remove("hidden");
  pill.querySelector(".session-pill-device").textContent = prettifySlug(session.device);
  pill.querySelector(".session-pill-rid").textContent = session.repair.slice(0, 8);
} else {
  pill.classList.add("hidden");
}
```

Handlers wired une seule fois dans `main.js::wireTopLevelControls()` :

```js
document.getElementById("sessionPill")?.addEventListener("click", (ev) => {
  if (ev.target.closest("#sessionPillClose")) return;
  // Navigation vers #home en gardant les params → dashboard.
  window.location.hash = "#home";
});
document.getElementById("sessionPillClose")?.addEventListener("click", (ev) => {
  ev.stopPropagation();
  leaveSession();
});
```

---

## 7. Fichiers touchés

### 7.1 Nouveaux fichiers

- **`web/styles/repair_dashboard.css`** — tout le style du mode dashboard + de la pastille topbar. Nouveau fichier pour isoler le bloc, ~300 lignes attendues. Importé depuis `web/index.html`.
- **Section DOM `#repairDashboard`** — inline dans `web/index.html`, à l'intérieur de `<section class="home" id="homeSection">`. Pas de partial externe (le reste de la home est déjà inline).

### 7.2 Fichiers modifiés

- **`web/js/router.js`** — exporter `currentSession()`, `leaveSession()`. Mettre à jour `updateChrome()` pour montrer/cacher la pastille. Nouveau chemin dans `navigate()` : quand `section === "home"` et session active, appeler `renderRepairDashboard()` au lieu de `renderHome()` liste. *Alternative : laisser `main.js` dispatcher sur section + session, garder `router.js` purement chrome.* Préférer la deuxième variante pour respecter la séparation actuelle (router = chrome, main = data dispatch).
- **`web/js/main.js`** — règle de bootstrap mise à jour (§3.2). Nouveau dispatch `section === "home" && session → renderRepairDashboard(session)`. Listener `hashchange` : inchangé modulo la même règle.
- **`web/js/home.js`** — `repairCardHTML()` ajoute `#home` au `href` (§3.2). Nouvel export `renderRepairDashboard(session)` qui fait `fetch` en parallèle de `/pipeline/repairs/{rid}`, `/pipeline/repairs/{rid}/conversations`, `/pipeline/packs/{slug}`, `/pipeline/packs/{slug}/findings`, puis popule les 5 blocs du dashboard. Appelé depuis `main.js` au boot + `hashchange`. La liste reste rendue par `renderHome()` intact.
- **`web/js/llm.js`** — ajouter un export nommé `switchConv(convId)` (la fonction existe déjà comme locale privée `switchConv` dans le module) pour que le bloc Conversations du dashboard puisse l'appeler via `import { switchConv } from './llm.js'`. Pas d'autre changement.
- **`web/index.html`** — ajouter la pastille topbar (§6.1), ajouter le bloc `#repairDashboard` (§4.1), importer `repair_dashboard.css`.
- **`api/pipeline/__init__.py`** — nouvelle route `GET /packs/{device_slug}/findings` (§5).
- **`api/agent/field_reports.py`** — ajouter `FieldReport.to_dict()` si la méthode manque (vérifier avant). Shape publique seulement.

### 7.3 Fichiers **non** touchés

- `web/js/graph.js`, `web/brd_viewer.js`, `web/js/memory_bank.js`, `web/js/schematic.js` — aucun. Les sections cibles reçoivent la session via URL, elles n'en savent rien de plus.
- `api/agent/*` — aucun changement runtime. Le panel chat et le protocole WS restent strictement identiques.
- `api/board/*`, `api/session/*` — aucun.

---

## 8. Parcours utilisateur (walkthrough)

### 8.1 Ouverture d'une session depuis la liste

1. Tech arrive sur `/` → bootstrap détecte `!device || !repair` → `initial = "home"` → `renderHome(...liste)`. Topbar : `JOURNAL · Réparations`.
2. Tech clique la card `iphone-x · a1b2c3d4`. Href : `/?device=iphone-x-logic-board&repair=a1b2c3d4#home`. Navigation full-page (pas SPA ; on garde le comportement `<a href>` actuel pour la simplicité).
3. Bootstrap détecte `device+repair` + hash `#home` → `initial = "home"` (règle gardée dès qu'il y a hash explicite). `main.js` dispatch : `section === "home" && session → renderRepairDashboard()`.
4. En parallèle, `openLLMPanelIfRepairParam()` ouvre le panel chat à droite (inchangé).
5. `updateChrome("home", ...)` voit la session active → pastille topbar visible, mode-pill devient `JOURNAL · Session` (nouveau libellé dérivé).
6. Tech voit : symptôme + device au centre du dashboard, 4 tuiles raccourcis, ses conversations, ses findings, sa timeline, son pack. Panel chat prêt pour parler à l'agent.

### 8.2 Saut vers un outil

1. Tech clique la tuile **PCB**. Href : `?device=iphone-x-logic-board&repair=a1b2c3d4#pcb`. Navigation intra-page (hash change uniquement).
2. `hashchange` → `navigate("pcb")` → `initBoardview(...)` comme aujourd'hui.
3. Pastille topbar reste visible, chat reste ouvert. Breadcrumb : `microsolder-agent / Réparations / iPhone X · Logic Board / Boardview`.

### 8.3 Retour au dashboard

1. Tech clique la pastille topbar (corps, pas le `[×]`). Handler fait `window.location.hash = "#home"`.
2. `hashchange` → `navigate("home")` → voit session active → render dashboard (pas liste).

### 8.4 Sortie

1. Tech clique **Quitter la session** (bouton dashboard ou `[×]` pastille topbar). Même handler `leaveSession()`.
2. URL réécrite : `?device=&repair=` retirés, hash `#home`.
3. Panel chat fermé, pastille topbar cachée.
4. Liste rerendue à jour (reload du trio `packs / taxonomy / repairs`).
5. Tech peut cliquer une autre card ou **Nouvelle réparation**.

---

## 9. Extensions optionnelles (hors scope, notés pour référence)

### 9.1 Endpoint HTTP pour `mb_expand_knowledge`

Aujourd'hui `mb_expand_knowledge` n'est callable que via l'agent (tool-use). Exposer une route `POST /pipeline/packs/{slug}/expand?scope={component|symptom}` rendrait le bouton « Étendre la mémoire » du bloc pack un one-click. Intéressant mais pas bloquant — le tech peut juste demander à l'agent en chat.

### 9.2 Filtre liste par device

Quand l'URL a `?device=X` sans `repair`, on rend la liste complète aujourd'hui. Variation : pré-filtrer sur le device ciblé. Intérêt marginal (cas d'usage : share-a-link-to-device-without-repair), à reporter.

### 9.3 Badge « nouveau finding depuis ta dernière visite »

Stocker un `last_seen_at` par repair côté frontend (localStorage) et badger les findings postérieurs. Pas critique en solo-tech, pertinent si multi-tech plus tard.

---

## 10. Tests

### 10.1 Backend (`tests/pipeline/`)

- `test_list_device_findings_empty` — device sans field report → `[]`.
- `test_list_device_findings_newest_first` — 3 reports avec `created_at` staggered → tri desc.
- `test_list_device_findings_unknown_device` — slug inconnu → `[]` (pas 404, cohérent avec les autres routes `packs/`).

### 10.2 Frontend

Pas de framework de test JS dans le repo aujourd'hui. On valide manuellement via une check-list structurée dans le plan d'implémentation (navigation, rendu, URL clean-up, pastille visibility, chat auto-open) et par une vérification browser (requise par `feedback_visual_changes_require_user_verify`).

### 10.3 Smoke E2E (optionnel)

Un test léger (pytest + httpx + un navigateur headless si disponible) qui scripte : POST /pipeline/repairs → recevoir `repair_id` → GET avec `?device=&repair=#home` → parser le HTML rendu → assert `#repairDashboard:not(.hidden)`. À budgéter séparément ; pas bloquant si le repo n'a pas déjà ce harness.

---

## 11. Risques & anti-patterns évités

1. **Double source de vérité pour la session.** Risque : cacher `currentSession` en variable module-level dans `router.js` ou `main.js`. Parade : la fonction `currentSession()` **re-dérive à chaque appel** depuis l'URL. Zéro état caché. Un `history.replaceState` suffit à tout synchroniser.
2. **Changement du contrat WS.** Non touché. La session MA existante (`llm.js`) reste binding-compatible. Si le tech navigue entre sections avec session active, le WS tient (déjà le cas aujourd'hui).
3. **Bundle d'un refactor de la home.** Parade : le mode liste n'est **pas** réécrit. Le dashboard est un bloc DOM ajouté à côté. Les deux coexistent sous `#homeSection`, switchés via `.hidden`.
4. **Casse de deep-link de pack.** Un lien bookmarké `/?device=X#graphe` (sans repair) continue à fonctionner — le bootstrap voit hash présent → respecte la section pointée. Aucun régression.
5. **Commits trop larges.** Parade : 4 commits séparés dans le plan d'implémentation (backend route + shape, DOM + CSS du dashboard, logic JS + wiring, pastille topbar + leaveSession). Chaque commit est indépendamment reviewable.
6. **Surcharge d'infos sur le dashboard.** Parade : les 4 blocs principaux (conversations, findings, timeline, pack) sont en deux colonnes (`.rd-col-primary` / `.rd-col-secondary`), ce qui garde le scroll raisonnable et le regard hiérarchisé (conversations + findings = « ce qu'on fait », timeline + pack = « ce qu'on sait »).

---

## 12. Résumé exécutif

Le Journal passe d'une liste plate à un **hub à deux états** piloté par l'URL. Quand `?device=&repair=` sont présents, il rend un **dashboard dédié à la session** avec symptôme, device, raccourcis outils, conversations, findings cross-session, timeline et statut pack. Une **pastille de session persistante** dans le topbar rend la session visible depuis n'importe quelle section et offre un bouton de sortie global. L'entrée dans une session atterrit désormais sur le dashboard (plus sur le graphe). La sortie (« Quitter la session ») nettoie l'URL et revient à la liste.

Coût : ~300 LOC CSS + ~250 LOC JS + 1 nouvelle route HTTP (findings par device) + 1 bloc DOM. Zéro changement agent/runtime/tools. Zéro changement dans le modèle de données disque. Quatre commits granulaires.
