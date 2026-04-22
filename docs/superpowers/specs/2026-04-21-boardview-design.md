# Boardview — design spec

> **⚠️ ARCHIVE — brainstorming historique (2026-04-21). Ne pas implémenter depuis ce doc.**
>
> Le Boardview a été livré et a divergé de ce plan. Pour la réalité du code :
> - **Parser principal** : `.kicad_pcb` direct via `pcbnew` (cf. `api/board/parser/kicad.py`) — on est parti sur KiCad au lieu de `.brd2` comme prévu ici
> - **Fixtures** : `web/boards/mnt-reform-motherboard.kicad_pcb` (seule cible démo, plus de Pi 4 ni Framework ni iPhone)
> - **Frontend** : `web/brd_viewer.js` (ES module, canvas renderer, pan/zoom/flip/net-tracing/inspector/Tweaks)
> - **Tools agent** : `api/tools/boardview.py` — 12 handlers (`highlight_component`, `focus_component`, `highlight_net`, `flip_board`, `annotate`, `filter_by_type`, `draw_arrow`, `measure_distance`, `show_pin`, `dim_unrelated`, `layer_visibility`, `reset_view`), pas 14 comme prévu ici
>
> Ce doc reste utile comme trace du raisonnement initial (choix du format, refus de l'obfuscation OBV, règle anti-hallucination côté renderer), mais les listes de fichiers, de tools et le triptyque de devices sont périmés.

---

**Date :** 2026-04-21
**Scope :** panneau Boardview du workbench `microsolder-agent` (parser, rendu, tool surface agent, intégration session). Hors scope : agent loop Claude, panneau Schematic, pipeline knowledge.
**Hackathon :** Anthropic × Cerebral Valley « Built with Opus 4.7 » — livraison démo 2026-04-26.

---

## 1. Contexte

Le Boardview est le panneau gauche du workbench à trois colonnes. Il affiche une carte électronique réelle (composants, pins, nets, outline PCB) et est *piloté par l'agent Claude Opus 4.7 via tool calls*. Le technicien ne clique pas de boutons « find component » — il pose une question (« où est le PMIC ? »), l'agent émet `highlight_component(refdes="U7")`, le panneau s'illumine.

L'histoire démo : **un boardviewer générique multi-format** qui prouve, via drag-drop, que le parser marche sur n'importe quel `.brd` issu de l'écosystème open hardware. Pi 4 preloaded au boot, Framework Laptop hand-crafté en J1-J2, tous les autres formats (`.brd2`, `.bdv`, `.fz`) en stretch si le MVP est stable.

## 2. Règles dures (rappel CLAUDE.md)

1. Tout code écrit from scratch pendant la semaine hackathon. Zéro copie d'OpenBoardView ni d'aucune autre codebase. Le repo OBV sert de *documentation de format* ; un format de fichier n'est pas copyrightable.
2. Licence Apache 2.0 sur tout le code produit ici.
3. Dépendances permissives uniquement (MIT, Apache 2.0, BSD). Pas de GPL / AGPL / LGPL.
4. **Open hardware only.** `board_assets/` ne contient que :
   - des `.brd` hand-craftés à partir de schematics publics officiels (Pi 4, Framework) ;
   - des `.brd` issus de projets open hardware qui publient officiellement leurs boardviews.
   Aucun fichier du marché gris (ZXW, WUXINJI, iPhone, Samsung).
5. **No hallucinated refdes.** Chaque refdes que l'agent mentionne passe par `validator.is_valid_refdes(board, refdes)` avant qu'un event arrive au frontend. Un refdes invalide → l'event n'est pas émis et le tool call renvoie `{found: false, reason: "…"}`.

## 3. Scope

### In scope
- `api/board/` — parser(s), model, validator.
- `api/tools/boardview.py` — les 12 tool handlers exposés à l'agent.
- `api/session/state.py` (parties boardview) — board chargée, layer actif, highlights.
- `web/boardview/` — renderer Canvas 2D, store, dropzone.
- `board_assets/` — assets de démo + le helper dev-time `tools/brd_compile.py`.
- Tests unitaires du parser, du validator, et des tool handlers.

### Out of scope (specs séparées)
- Agent loop (`api/agent/`).
- Panneau Schematic (`api/vision/`).
- Knowledge pipeline (`api/knowledge/`) — consomme l'event `board:loaded`.
- Journal, chat UI, LLM classification secondaire.

## 4. Décisions techniques validées

| # | Décision | Justification courte |
|---|---|---|
| 1 | Parser dynamique, format-agnostique | Démo « marche avec n'importe quelle carte » |
| 2 | Format primaire = OpenBoardView `.brd` (Test_Link) | Écosystème OBV directement compatible |
| 3 | Architecture `BoardParser` abstraite, dispatch par extension | Extensibilité `.brd2` / `.bdv` / `.fz` en stretch |
| 4 | Helper YAML → `.brd` pour hand-craft (dev-time uniquement) | Ergonomie d'auteur Pi4 / Framework en J1-J2 |
| 5 | Rendu Canvas 2D (pas SVG, pas WebGL) | Perf garantie + effets glow triviaux |
| 6 | Palette P2 semantic rainbow, dark mode | Info-dense, OBV-like, démo-friendly |
| 7 | Agent tool surface = Tier 1 + 2 + 3 (12 verbes), T4 stretch | Couverture démo riche |
| 8 | Input drag-drop + preload Pi 4 au boot | Immédiatement utilisable, effet drop live en démo |
| 9 | Boardview émet `board:loaded`, pipeline knowledge écoute ailleurs | Frontière propre, testable seul |
| 10 | Unités internes : mils (1 unit = 0.025 mm) | Convention `.brd`, évite les conversions inutiles |

## 5. Architecture

```
web/
  boardview/
    renderer.js      Canvas 2D : redraw, pan/zoom, hit-test, glow
    store.js         état normalisé : parts, pins, nets, highlights
    dropzone.js      drag-drop → boardview.upload
    colors.js        palette P2 (constants)
  app.js             Alpine root, monte boardview/

api/
  board/
    __init__.py
    model.py         Pydantic : Board, Part, Pin, Net, Nail, Layer
    validator.py     is_valid_refdes, resolve_net, resolve_pin
    parser/
      __init__.py
      base.py        ABC BoardParser, parse_file(path) dispatch
      brd.py         BRDParser (Test_Link format)
      brd2.py        (stretch, NotImplementedError par défaut)
      bdv.py         (stretch)
      fz.py          (stretch)
  tools/
    boardview.py     12 handlers + schémas JSON pour l'agent
  session/
    state.py         SessionState.board, .layer, .highlights, .annotations

board_assets/
  raspberry-pi-4b.brd      preload au boot
  raspberry-pi-4b.yaml     source hand-craftée
  framework-mainboard.brd  ajouté en J2-J3
  framework-mainboard.yaml

tools/                          (dev-time, pas de runtime dep)
  brd_compile.py               YAML → .brd

tests/
  board/
    test_brd_parser.py
    test_validator.py
    test_model.py
  tools/
    test_boardview_handlers.py
  fixtures/
    minimal.brd                2 parts, 4 pins, 1 net — smoke test
    obfuscated.brd             test de détection du XOR (on ne décode pas, on refuse poliment)
```

## 6. Data model (`api/board/model.py`)

```python
from enum import IntFlag
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

class Layer(IntFlag):
    TOP    = 1
    BOTTOM = 2
    BOTH   = TOP | BOTTOM

class Point(BaseModel):
    x: int   # mils
    y: int

class Pin(BaseModel):
    part_refdes: str          # dénormalisé pour lookup rapide
    index: int                # 1-based dans le part
    pos: Point
    net: str | None           # None si non rattaché
    probe: int | None = None  # numéro de nail, None si pas testpoint
    layer: Layer              # hérité du part

class Part(BaseModel):
    refdes: str               # clé primaire
    layer: Layer
    is_smd: bool
    bbox: tuple[Point, Point] # min, max — calculée depuis les pins
    pin_refs: list[int]       # indices dans Board.pins

class Net(BaseModel):
    name: str                 # clé primaire
    pin_refs: list[int]       # indices dans Board.pins
    is_power: bool            # heuristique : name matches ^(\+?\d+V\d*|VCC|VDD|...)
    is_ground: bool           # name matches ^(GND|VSS|AGND|DGND)$

class Nail(BaseModel):
    probe: int
    pos: Point
    layer: Layer
    net: str

class Board(BaseModel):
    board_id: str             # slug unique
    file_hash: str            # sha256 du .brd d'origine
    source_format: str        # "brd" | "brd2" | ...
    outline: list[Point]      # polygone fermé du PCB
    parts: list[Part]
    pins: list[Pin]
    nets: list[Net]
    nails: list[Nail]

    # indexes calculés post-parse via @model_validator(mode="after")
    # Pydantic v2 : PrivateAttr (pas Field) pour les attrs non sérialisés.
    _refdes_index: dict[str, Part] = PrivateAttr(default_factory=dict)
    _net_index:    dict[str, Net]  = PrivateAttr(default_factory=dict)

    def part_by_refdes(self, refdes: str) -> Part | None: ...
    def net_by_name(self, name: str) -> Net | None: ...
```

**Immutabilité :** `Board` est construit post-parse et ne change plus pendant la vie de la session. Les highlights / annotations vivent dans `SessionState`, pas dans `Board`.

## 7. Parser `.brd` (`api/board/parser/brd.py`)

### Format (résumé)

Un fichier `.brd` Test_Link est texte ASCII, line-oriented, avec des blocs introduits par un marqueur. Blocs dans l'ordre :

- `str_length:` — entête interne OBV, skippé.
- `var_data:` — une ligne : `num_format num_parts num_pins num_nails`.
- `Format:` — `num_format` lignes de `x y` (points du polygone outline, mils).
- `Parts:` ou `Pins1:` — `num_parts` lignes : `name  type_layer  end_of_pins`.
  - `type_layer` est un bitfield : 0x4/0x8 → SMD vs through-hole ; 1 ou 4-7 → Top ; 2 ou ≥8 → Bottom.
  - `end_of_pins` = index exclusif du dernier pin de ce part.
- `Pins:` ou `Pins2:` — `num_pins` lignes : `x y probe part_idx net_name`. `probe=-99` → pas de probe. `net_name` vide → sera rempli via la map des nails (pattern Lenovo).
- `Nails:` — `num_nails` lignes : `probe x y side net`. `side=1` → top.

**Fichiers obfusqués** (signature `0x23 0xe2 0x63 0x28`) : on **refuse poliment** avec `InvalidBoardFile(reason="obfuscated")`. On ne décode pas — ce sont quasi toujours des fichiers du marché gris, on s'interdit de les traiter (hard rule #4).

### API

```python
# api/board/parser/base.py
class BoardParser(ABC):
    extensions: tuple[str, ...]

    def parse_file(self, path: Path) -> Board:
        """Entry point : lit le fichier, hash SHA-256, délègue à .parse()."""
        raw = path.read_bytes()
        file_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        return self.parse(raw, file_hash=file_hash, board_id=path.stem)

    @abstractmethod
    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board: ...

def parser_for(path: Path) -> BoardParser:
    """Dispatch par extension → lève UnsupportedFormatError sinon."""


# api/board/parser/brd.py
class BRDParser(BoardParser):
    extensions = (".brd",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        # refuse si obfusqué (signature 23 e2 63 28)
        # détecte plain via substring b"str_length:" et b"var_data:"
        # sinon InvalidBoardFile("unknown-encoding")
        ...
```

Erreurs structurées (toutes héritent de `InvalidBoardFile`) :
- `ObfuscatedFileError`
- `MalformedHeaderError(field)`
- `PinPartMismatchError(pin_index)`
- `DanglingNetError(pin_index, net_name)` — pin référence un net absent de la map des nails (non-fatal : on met `net=None` + warning).

### Strategy

Parseur line-by-line, state machine minimale. **Objectif : ~200-300 lignes Python**, aucune dépendance extérieure en dehors de la stdlib + Pydantic.

## 8. Validator (`api/board/validator.py`)

Garantit la règle dure #5 : zéro refdes halluciné n'atteint l'UI.

```python
def is_valid_refdes(board: Board, refdes: str) -> bool: ...
def resolve_part(board: Board, refdes: str) -> Part | None: ...
def resolve_net(board: Board, net_name: str) -> Net | None: ...
def resolve_pin(board: Board, refdes: str, pin_index: int) -> Pin | None: ...
def suggest_similar(board: Board, refdes: str, k: int = 3) -> list[str]:
    """levenshtein closest matches pour messages d'erreur."""
```

Les tool handlers **doivent** passer par ces fonctions avant d'émettre quoi que ce soit.

## 9. Agent tool surface (`api/tools/boardview.py`)

12 verbes exposés à l'agent via le schéma Anthropic tool-use. Tous retournent un dict structuré ; en cas d'échec renvoient `{"ok": false, "reason": "...", "suggestions": [...]}`.

| # | Tool | Paramètres | Tier |
|---|---|---|---|
| 1 | `highlight_component` | `refdes: str \| list[str]`, `color?: "accent" \| "warn" \| "mute"`, `additive?: bool` | 1 |
| 2 | `focus_component` | `refdes: str`, `zoom?: float` | 1 |
| 3 | `reset_view` | — | 1 |
| 4 | `highlight_net` | `net: str` | 2 |
| 5 | `flip_board` | `preserve_cursor?: bool` | 2 |
| 6 | `annotate` | `refdes: str`, `label: str` | 2 |
| 7 | `filter_by_type` | `prefix: str` (ex: `"U"`, `"C"`) | 2 |
| 8 | `draw_arrow` | `from_refdes: str`, `to_refdes: str` | 3 |
| 9 | `measure_distance` | `refdes_a: str`, `refdes_b: str` | 3 |
| 10 | `show_pin` | `refdes: str`, `pin: int` | 3 |
| 11 | `dim_unrelated` | — (utilise les highlights actifs) | 3 |
| 12 | `layer_visibility` | `layer: "top" \| "bottom"`, `visible: bool` | 3 |

**Stretch (T4)** — non spécifiés ici : `trace_connection`, `manage_probe_points`, `measure_virtual`, `export_annotated`.

Chaque handler :
1. Valide les refdes/nets/pins avec `validator`.
2. Met à jour `SessionState`.
3. Emit un event WS `boardview.<verb>` avec les paramètres **déjà résolus** (coords, bbox, pin positions — le frontend ne re-lookup pas).
4. Retourne au tool-use loop un dict `{"ok": true, "summary": "…", ...}`. Le champ `summary` est la phrase courte insérée dans le journal de la session (ex: `"Highlighted U7 (PMIC MxL7704) on top layer."`). En cas d'échec : `{"ok": false, "reason": "...", "suggestions": [...]}` — l'agent voit, choisit comment récupérer, et l'UI n'est pas touchée.

## 10. Protocole WebSocket

Toutes les enveloppes ont un champ `type` préfixé par domaine. Le boardview utilise `boardview.*`. Le chat utilise `chat.*`, le schematic `schematic.*` (autres specs).

### Backend → frontend (agent drive)

```jsonc
{ "type": "boardview.board_loaded",
  "board_id": "raspberry-pi-4b",
  "file_hash": "sha256:…",
  "parts_count": 287,
  "outline": [[x,y], …],
  "parts": [{refdes, layer, bbox, pin_refs:[…]}, …],
  "pins":  [{part_refdes, index, pos:[x,y], net, layer, probe}, …],
  "nets":  [{name, pin_refs:[…], is_power, is_ground}, …] }

{ "type": "boardview.highlight",
  "refdes": ["U7"], "color": "accent", "additive": false }

{ "type": "boardview.highlight_net",
  "net": "+3V3", "pin_refs": [12, 47, 89, …] }

{ "type": "boardview.focus",
  "refdes": "U1", "bbox": [[x1,y1],[x2,y2]], "zoom": 2.5,
  "auto_flipped": false }

{ "type": "boardview.flip",
  "new_side": "bottom", "preserve_cursor": true }

{ "type": "boardview.annotate",
  "refdes": "U7", "label": "PMIC — 3V3 rail source", "id": "ann-1" }

{ "type": "boardview.reset_view" }
{ "type": "boardview.dim_unrelated" }
{ "type": "boardview.layer_visibility", "layer": "top", "visible": true }
{ "type": "boardview.filter", "prefix": "U" }
{ "type": "boardview.draw_arrow", "from": [x,y], "to": [x,y], "id": "arr-1" }
{ "type": "boardview.measure", "from_refdes":"U1", "to_refdes":"U7", "distance_mm": 8.3 }
{ "type": "boardview.show_pin", "refdes":"U7", "pin":23, "pos":[x,y] }

{ "type": "boardview.upload_error",
  "reason": "obfuscated" | "malformed-header" | "unsupported-format" | "io-error",
  "message": "texte lisible pour l'UI" }
```

### Frontend → backend (user input)

```jsonc
{ "type": "boardview.upload",
  "filename": "framework.brd", "content_b64": "…" }

{ "type": "boardview.click_part",  "refdes": "U7" }       // informatif, pour journal
{ "type": "boardview.click_pin",   "refdes": "U7", "pin": 3 }
{ "type": "boardview.hover",       "refdes": "U7" }        // optionnel, debounced 200ms
```

**Load initial :** à la connexion WS, si une board est déjà loadée dans la session, le backend envoie immédiatement un `boardview.board_loaded`.

## 11. Renderer Canvas 2D (`web/boardview/renderer.js`)

### Boucle

- RequestAnimationFrame-driven.
- `state` : `{ pan:{x,y}, zoom, layer:"top"|"bottom", highlights:Set, netHighlight, annotations, arrows, dimUnrelated }`.
- Redraw complet à chaque frame où `state` a changé (dirty flag). Pas de diff incrémental — Canvas 2D est assez rapide pour 300 parts / 3000 pins @ 60fps.

### Pan/zoom (patterns portés d'OBV)

- Wheel → zoom logarithmique **ancré sur la souris** (le point sous le curseur reste fixe). `scale *= 2^(wheel_delta * 0.1)`.
- Click-drag → pan.
- WASD → pan clavier.
- `R` → `reset_view` local.
- `F` / Space → flip layer.
- `Escape` → clear highlights.

### Hit-test

Quad-tree construit une fois au load (sur les bbox des parts). Lookup O(log n) pour le hover/click. Pin hit-test secondaire (même quad-tree, nodes imbriqués).

### Layer flip

Porté d'OBV : X-mirror instantané via `ctx.transform`, avec rotation 180° automatique pour que le texte reste lisible. Mode `preserve_cursor` (Shift-flip) : re-compense `pan` pour que le point sous le curseur ne bouge pas. **Auto-flip si l'agent appelle `focus_component` sur un refdes de l'autre face** (pattern `AnyItemVisible`) — le renderer émet `boardview.flip` au backend puis applique le focus.

### Couleurs (palette P2 validée)

```js
export const COLORS = {
  bg_top:        "#142030",
  bg_bot:        "#05080e",
  pcb_outline:   "rgba(64, 224, 208, 0.55)",
  mounting:      "rgba(64, 224, 208, 0.7)",

  part_fill:     "linear(#1d2b3d, #111a26)",
  part_border:   "#2d4258",
  part_text:     "#a9c2dc",

  part_highlight_border: "#40e0d0",
  part_highlight_fill:   "linear(#1d3a3a, #0c2323)",
  part_highlight_text:   "#9ff3e8",
  part_highlight_glow:   "rgba(64,224,208,0.55)",

  pin_gnd:       "#6b7280",
  pin_power:     "#6ee7a7",
  pin_signal:    "#7dd3fc",
  pin_testpad:   "#c084fc",
  pin_nc:        "#f43f5e",
  pin_selected_fill:   "#ffffff",
  pin_selected_border: "#40e0d0",
  pin_same_net:        "#6ee7a7",

  net_web:       "rgba(64, 224, 208, 0.4)",  // dashed

  annotation:    "#fbbf24",  // warm orange, contraste turquoise
  arrow:         "#fbbf24",

  dimmed_opacity: 0.25,
};
```

Règles de priorité de couleur d'un pin (reproduit le pattern OBV) :
`default → type_override(gnd/power/nc/testpad) → same_net_highlight → net_highlight → selected`.
La dernière qui s'applique gagne.

## 12. Store (`web/boardview/store.js`)

Single source of truth côté frontend. Reçoit les events WS, met à jour l'état, notifie le renderer (dirty flag).

```js
const store = {
  board: null,              // Board normalisé
  layer: "top",
  pan: {x:0,y:0},
  zoom: 1,
  highlights: new Set(),    // refdes en focus
  netHighlight: null,
  annotations: {},          // id → {refdes, label, pos}
  arrows: {},               // id → {from, to}
  dimUnrelated: false,
  layerVisibility: {top:true, bottom:true},
  filter: null,             // prefix ou null

  apply(event) { ... }      // switch sur event.type
};
```

## 13. Dropzone (`web/boardview/dropzone.js`)

- Intercepte `dragover` / `drop` sur le panneau.
- Lit le fichier via `FileReader` en ArrayBuffer, base64-encode.
- Envoie `boardview.upload`.
- Affiche un skeleton `"parsing…"` pendant l'attente.
- Le backend répond `boardview.board_loaded` (succès) ou `boardview.upload_error` (échec avec `reason`).

## 14. Input pipeline & session

### Au boot
1. `api/main.py` lifespan → `SessionState.load_board("board_assets/raspberry-pi-4b.brd")`.
2. Parse + hash + indexation.
3. Emit sur le bus interne `board:loaded { board_id:"raspberry-pi-4b", is_known:true }`.
   - Le pipeline knowledge (spec séparée) vérifie si le knowledge pack existe et décide d'agir.

### Au drag-drop
1. Frontend → `boardview.upload`.
2. Backend : write temp file, dispatch `BoardParser.parse_file`, compute hash, replace `SessionState.board`.
3. Emit `board:loaded { is_known: (hash in knowledge_registry) }`.
4. Envoie `boardview.board_loaded` au frontend → nouveau Canvas.

### Session isolation
Pour le hackathon on assume **single session**. Architecture prévoit `session_id` dans les events mais un seul store global par process.

## 15. Testing strategy

### Unit (`tests/board/`)
- `test_brd_parser.py` :
  - `minimal.brd` (2 parts, 4 pins, 1 net) → 12 assertions sur Board fields.
  - Fichier obfusqué → `ObfuscatedFileError`.
  - Ligne `Parts:` malformée → `MalformedHeaderError(field="Parts")`.
  - Pin avec `part_idx` hors range → `PinPartMismatchError`.
  - Dangling net fills depuis nails map.
- `test_validator.py` : refdes valide, invalide, case-sensitive, suggestions Levenshtein.
- `test_model.py` : Pydantic validation, round-trip serialization.

### Integration (`tests/tools/`)
- `test_boardview_handlers.py` : chaque tool handler sur une board chargée. Vérifie (a) refdes invalide → `{ok:false, suggestions}` ; (b) refdes valide → event émis avec payload correct ; (c) session state mis à jour.

### E2E manuel
Le frontend n'est pas testé par pytest ; checklist manuelle avant chaque tag :
- [ ] Pan/zoom fluides avec Pi 4 chargée.
- [ ] `focus_component("U1")` zoome et glow.
- [ ] `highlight_net("+3V3")` illumine les bonnes pins.
- [ ] `flip_board()` inverse + rotation 180°.
- [ ] Drag-drop `framework-mainboard.brd` remplace live.
- [ ] Refdes halluciné `U999` → l'agent reçoit `{ok:false}` et ne ment pas dans le chat.

**Gate CI :** `make test` doit être vert avant chaque commit. Fixtures `.brd` petites et lisibles (2-5 parts).

## 16. Calendrier (7 jours)

| Jour | Objectif | Livrable |
|---|---|---|
| J1 (21) | Parser `.brd` + model Pydantic + validator + fixture `minimal.brd` | `make test` vert sur parser |
| J2 (22) | Tool handlers (T1+T2) + WS protocol + Pi 4 `.brd` hand-crafté + renderer skeleton (pan/zoom/draw parts) | Démo : chat → highlight_component visible |
| J3 (23) | Tool handlers T3 (arrows, measure, show_pin, dim, layer_vis, filter) + palette P2 + hit-test + drag-drop | Démo : drag-drop Framework `.brd` |
| J4 (24) | Auto-flip, annotations, refinement UX, anti-hallu wiring end-to-end | Golden path démo tourne de bout en bout |
| J5 (25) | Polish visuel (glow, transitions), tests integration tool handlers, knowledge pipeline integration event | Vidéo démo preview |
| J6 (26) | Bug fixes, enregistrement vidéo finale, README update | Submission |
| J7 (27) | Buffer / stretch (T4 verbes, `.brd2` parser) | Bonus |

## 17. Risques connus

- **Densité BGA** : un SoC BGA peut avoir 500+ pins. Canvas 2D gère ~10k primitives à 60 fps, largement ok. Mitigation : LOD — au zoom out < 0.5x, on skippe les pins (on ne draw que les parts).
- **`.brd` réels mal formés** : les fichiers dans la nature ont des edge cases (Lenovo variant, champs optionnels). Mitigation : parser permissif avec warnings, pas d'abandon dur sauf blocs critiques.
- **Hand-craft Framework `.brd`** : le repo public officiel Framework donne le schematic PDF mais pas forcément les coordinates XY des composants. Risque sur J2-J3. Mitigation : à défaut, Framework devient un JSON *approché* (reverse-engineeré depuis photos haute-res + schematic), émis en `.brd` par notre helper.
- **Perf drag-drop** : parsing + émission de la full board en JSON sur WS peut être lourd (~2-5 MB de JSON pour une carte de 300 parts). Mitigation initiale : vérifier si uvicorn + `websockets` supportent l'extension `permessage-deflate` (à confirmer au début J2) ; sinon, split en events séquentiels (`board_header`, puis plusieurs `board_parts_chunk`, puis `board_ready`). Décision finale prise en J2 après mesure.

## 18. Non-goals

- Pas de support `.brd2` / `.bdv` / `.fz` en MVP. Subclasses présentes mais `raise NotImplementedError`.
- Pas de routing automatique (trace-following entre deux pins).
- Pas de mesures électriques virtuelles.
- Pas de support multi-board simultané (une board par session).
- Pas d'authentification (démo locale).
- Pas de persistance cross-session des annotations.

---

## Validation

Design validé interactivement par Alexis en session de brainstorming 2026-04-21.
Décisions techniques : voir §4.
Prochaine étape : plan d'implémentation détaillé via `superpowers:writing-plans`.
