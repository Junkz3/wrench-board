/**
 * PCBViewerOptimized — High-performance Three.js PCB visualization.
 * Uses InstancedMesh for massive draw-call reduction on large PCBs
 * (20k-50k+ objects: 30k → 50-100 draw calls, 60 FPS constant, ~100 MB RAM).
 *
 * Consumes the JSON payload emitted by `api/board/render.py::to_render_payload`.
 * Exported on `window.PCBViewerOptimized` for the bridge layer
 * (`web/js/pcb_viewer_bridge.js`) to instantiate.
 */

// Net category regexes — extended over the backend's strict patterns
// to handle Apple-style XZZ naming (PPBUS_G3H, PP1V8_CODEC,
// GND_AUDIO_CODEC, L83_VCP_FILT_GND, …) which iPhone/MacBook
// boardviews use heavily. Without this, every Apple power rail and
// every suffixed ground net falls through to 'signal' and renders
// uniformly grey-white.
//
// Priority: reset > clock > power > ground > signal, so CLK_3V3 reads
// as clock (more specific cue).
const PCB_NET_CLOCK_RE = /(^|[_\-/.])(CLK|CLOCK|XTAL|X_?IN|X_?OUT|OSC(IN|OUT)?|SCLK|SCK|SYSCLK|[MHP]CLK)([_\-/.0-9]|$)/i;
const PCB_NET_RESET_RE = /(^|[_\-/.])(N_?RESET|N_?RST|RESET_?N|RST_?N|POR|PWR_?(GOOD|OK)|RESET|RST)([_\-/.0-9]|$)/i;
const PCB_NET_POWER_RE = new RegExp([
    '^\\+?\\d+V\\d*(_[A-Z0-9_]+)?$',          // +3V3, 5V0_USB, 1V8
    '^VCC[A-Z0-9_]*$',                         // VCC, VCC_3V3, VCCIO
    '^VDD[A-Z0-9_]*$',                         // VDD, VDD_CORE
    '^VBAT[A-Z0-9_]*$',                        // VBAT, VBAT_RTC
    '^VBUS[A-Z0-9_]*$',                        // VBUS, VBUS_USB
    '^V_[A-Z0-9_]+$',                          // V_AUDIO, V_3V3
    '^PP[A-Z0-9][A-Z0-9_]*$',                  // Apple: PPBUS_G3H, PP1V8_CODEC
    '^PWR[A-Z0-9_]*$',                         // PWR_GOOD, PWR_EN (rail-side)
    '^PVDD[A-Z0-9_]*$',                        // PVDD, PVDD_CPU
].join('|'), 'i');
// Ground: anchored AND token-boundaried — catches GND, VSS plus
// composite names like GND_AUDIO_CODEC, AVDD_GND, L83_VCP_FILT_GND.
const PCB_NET_GROUND_RE = /(^|[_\-/.])(GND|VSS|AGND|DGND|PGND)([_\-/.]|$)/i;

// Net category palette — KEEP IN SYNC with `web/brd_viewer.js`'s
// DEFAULT_NET_HEX + NET_COLOR_STORAGE_KEY. The two viewers share the
// 'msa.pcb.netColors' localStorage entry so the picker (Tweaks panel)
// stays consistent across SVG fallback and WebGL renderers. We can't
// import from brd_viewer.js because pcb_viewer.js loads as a classic
// script before the deferred ES modules; if a third file ever needs
// this map, lift it into a tiny shared `js/pcb_net_palette.js` script
// loaded ahead of both viewers and read from `window.PCB_NET_DEFAULTS`.
const PCB_DEFAULT_NET_HEX = {
    signal:   '#a9b6cc',
    power:    '#B16628',
    ground:   '#40455C',
    clock:    '#c084fc',
    reset:    '#f58278',
    'no-net': '#e6edf7',
    // Entity-typed pseudo-categories — not net-name driven, but still
    // surfaced in the Tweaks picker so the technician can retune the
    // visually prominent test pads and vias the same way.
    testPad:  '#d4af37',
    via:      '#c084fc',
    // Board chrome — outline is the closed-polygon contour (cyan by
    // default), fill is the substrate behind it (default matches
    // bg-deep so it stays effectively invisible until the user picks
    // a colour).
    boardOutline: '#67d4f5',
    boardFill:    '#07101f',
};
const PCB_NET_COLOR_STORAGE_KEY = 'msa.pcb.netColors';

function loadPcbNetColors() {
    try {
        const raw = localStorage.getItem(PCB_NET_COLOR_STORAGE_KEY);
        if (!raw) return { ...PCB_DEFAULT_NET_HEX };
        return { ...PCB_DEFAULT_NET_HEX, ...JSON.parse(raw) };
    } catch (_) {
        return { ...PCB_DEFAULT_NET_HEX };
    }
}

function savePcbNetColors(hexMap) {
    try { localStorage.setItem(PCB_NET_COLOR_STORAGE_KEY, JSON.stringify(hexMap)); } catch (_) {}
}

function pcbHexStringToInt(hex) {
    const s = (hex || '').replace('#', '').padEnd(6, '0').slice(0, 6);
    return parseInt(s, 16) | 0;
}

/**
 * Pre-blend a foreground hex toward a background hex at `alpha` opacity.
 * The InstancedMesh path renders pins at full opacity (one shared
 * material), so we can't lean on the alpha-blending trick the SVG
 * renderer uses to darken ground pins. Bake the blend into the colour
 * up-front instead.
 */
function pcbBlendTowardBg(fg, bg, alpha) {
    const fr = (fg >> 16) & 0xff, fgG = (fg >> 8) & 0xff, fb = fg & 0xff;
    const br = (bg >> 16) & 0xff, bgG = (bg >> 8) & 0xff, bb = bg & 0xff;
    const r = Math.round(fr * alpha + br * (1 - alpha));
    const g = Math.round(fgG * alpha + bgG * (1 - alpha));
    const b = Math.round(fb * alpha + bb * (1 - alpha));
    return (r << 16) | (g << 8) | b;
}

/**
 * Resolve OKLCH design tokens from `tokens.css` to RGB hex once at init.
 * Returns hex numbers consumable by THREE.Color.
 *
 * Why canvas: getComputedStyle().color on `var(--cyan)` may return any of
 *   "rgb(r, g, b)" / "rgba(...)" / "color(srgb r g b)" / "oklch(...)"
 * depending on browser support and gamut. A naive regex over digits
 * collapses the OKLCH string ("oklch(0.82 0.14 210)") to (0, 82, 0) =
 * dark green for every token. Setting canvas2d.fillStyle to the same
 * string always normalizes to "#rrggbb" via the browser's colour pipeline.
 */
function readDesignTokens() {
    const probe = document.createElement('span');
    probe.style.position = 'absolute';
    probe.style.visibility = 'hidden';
    document.body.appendChild(probe);

    const ctx = document.createElement('canvas').getContext('2d');

    const resolve = (cssVar, fallback) => {
        probe.style.color = '';
        probe.style.color = `var(${cssVar})`;
        const css = getComputedStyle(probe).color;
        if (!css) return fallback;
        try {
            ctx.fillStyle = '#000000';
            ctx.fillStyle = css;          // throws / no-op on unparseable input
            const hex = ctx.fillStyle;    // canonicalised to "#rrggbb"
            if (typeof hex === 'string' && hex.startsWith('#') && hex.length === 7) {
                return parseInt(hex.slice(1), 16);
            }
        } catch (_) { /* fallthrough */ }
        return fallback;
    };

    const tokens = {
        bgDeep:  resolve('--bg-deep', 0x07101f),
        cyan:    resolve('--cyan',    0x67d4f5),
        amber:   resolve('--amber',   0xe8b85a),
        emerald: resolve('--emerald', 0x6cd49a),
        violet:  resolve('--violet',  0xb89ce8),
        text:    resolve('--text',    0xe6edf7),
        text2:   resolve('--text-2',  0xa9b6cc),
        text3:   resolve('--text-3',  0x6e7d96),
    };
    document.body.removeChild(probe);
    return tokens;
}

class PCBViewerOptimized {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        this.container = document.getElementById('pcb-canvas-container');
        this.boardData = null;
        this.selectedItem = null;
        this.highlightedItems = [];
        this.layers = { top: true, bottom: true };
        // Vias and copper traces OFF by default — GenCAD (.cad) boards
        // ship thousands of routing/stitching via dots and 30k+ routed
        // copper segments that crush perf and clutter the canvas during
        // diagnosis. The board outline still renders (createTrace forces
        // `isOutline` segments visible regardless). The toolbar toggles
        // reveal them on demand.
        this.showVias = false;
        this.showTraces = false;
        this.isFlipped = false;
        // Scene rotation in degrees (0/90/180/270). Applied at the
        // scene root so every mesh follows; the camera is recentred
        // on the visible bbox through the same rotation after each
        // toggle so the board stays in frame.
        this.rotationDeg = 0;

        // Dual view mode
        this.isDualView = false;
        this.dualViewGroup = null;

        // Dual-outline (XZZ) — when the parser surfaces top + bottom
        // views in the same coordinate space, `dualOutline` holds the
        // two polygons and the split axis. `sideMode` cycles through
        // 'top' / 'bottom' / 'both' to filter visibility per face.
        this.dualOutline = null;
        this.sideMode = 'both';
        // Outlines tracked separately so the side filter can hide /
        // show each face's contour independently.
        this._outlineMeshes = [];
        // Inspection markers (XZZ type_03) tracked so the side filter
        // can hide / show fill + border per face.
        this._markerMeshes = [];
        // Per-rect-pin border lines, tracked for the side filter
        // (they're built one-per-pin since THREE.Line has no instancing).
        this._pinBorderLines = [];
        // Standalone component fills + silkscreen body-line segments
        // emitted by `createComponent` for XZZ parts that ship body_lines:
        // they live OUTSIDE the per-part `group` (so XZZ-baked rotation
        // doesn't double-rotate), so the side filter has to track them
        // separately rather than rely on the Group's visibility.
        this._componentExtras = [];

        // Performance: render on demand
        this.needsRender = true;
        this.lastHoverCheck = 0;
        this.hoverThrottleMs = 33;
        this.lastMouseMoveTime = 0;
        this.mouseMoveThrottleMs = 16;

        // === INSTANCED MESH SYSTEM ===
        this._circularPinInstance = null;      // InstancedMesh for all circular pins
        this._circularPinBorderInstance = null;
        this._rectPinInstances = new Map();    // Map<sizeKey, {body: InstancedMesh, border: InstancedMesh}>
        this._viasOuterInstance = null;
        this._viasInnerInstance = null;
        this._testPadsInstance = null;
        this._testPadsBorderInstance = null;

        // Instance ID to data mapping for hover/selection
        this._pinInstanceData = [];            // Array[instanceId] = pinData
        this._viaInstanceData = [];
        this._testPadInstanceData = [];

        // Component groups (kept as individual meshes - fewer objects)
        this.meshGroups = { components: [], traces: [] };

        // Per-component refdes label sprites — populated in createComponent,
        // visibility toggled by _updateComponentLabelVisibility() based on
        // current zoom so they don't clutter the full-board view.
        this._componentLabels = [];
        // Big silkscreen labels for 0-pin parts (BADGE / REFORM / etc.) —
        // tracked separately so we can clamp their screen pixel height on
        // zoom (they would otherwise grow unbounded).
        this._silkscreenLabels = [];
        this._labelMinPx = 32;
        this._refdesPixelHeight = 14;
        this._silkscreenMaxPixelHeight = 32;

        // === SPATIAL GRID for O(1) hover ===
        this._spatialGrid = {};
        this._gridCellSize = 5;
        this._hoverableItems = []; // Flat array for hover checks

        // === AGENT OVERLAY STATE ===
        // Annotations + arrows + measurements painted by the diagnostic
        // agent's bv_* tool calls. Distinct from the user's hover/select
        // path so a chat-driven scene survives a manual click and vice
        // versa. Tracked as Maps keyed by an id (annotation id, arrow id,
        // measurement id) so the legacy event flow can address them
        // individually for incremental clear/replace; cleared as a group
        // by the bv_reset_view path.
        this._agentAnnotations = new Map();   // id → { sprite, refdes, label }
        this._agentArrows = new Map();        // id → THREE.Group (line + head)
        this._agentMeasurements = new Map();  // id → THREE.Group (line + label)
        // Protocol step badges — numbered cyan/amber pins above each step's
        // target component, set by bv_propose_protocol via the bridge.
        // Keyed by step.id (string) → { sprite, refdes }. Cleared as a
        // group by clearProtocolBadges; rebuilt on every setProtocolBadges
        // call (no diff — protocol updates are infrequent enough that a
        // full repaint is simpler than an incremental update).
        this._protocolBadges = new Map();     // stepId → { sprite, refdes }
        // Refdes-prefix filter applied by the bv_filter_by_type tool.
        // Null = no filter (everything visible). When set, components +
        // pins whose part_refdes does not start with the prefix get
        // dimmed / hidden via _applyRefdesFilter.
        this._agentFilterPrefix = null;
        // Active dim-unrelated state — when truthy, every component /
        // pin not currently highlighted by the agent is dimmed. The
        // current selectedItem + agent highlights stay full-bright.
        this._agentDimActive = false;

        // Design tokens — single source of truth for the scene palette.
        // Resolved once at init from tokens.css; no live reactivity.
        this.tokens = readDesignTokens();

        // Net-category palette — shared with the SVG renderer via
        // localStorage so user customizations carry over.
        this.netColors = loadPcbNetColors();

        // Scene palette — sourced from tokens.css for chrome (background,
        // outlines, highlight) and from PCB_DEFAULT_NET_HEX for pins so
        // the WebGL viewer stays in lockstep with brd_viewer.js's
        // user-customizable net palette. Ground gets pre-blended toward
        // bg-deep to reproduce the SVG renderer's alpha-0.55 darkening
        // (otherwise GND #6e7d96 lands too close to signal #a9b6cc on
        // screen — both read as 'medium grey').
        const nc = this.netColors;
        const bg = this.tokens.bgDeep;
        this.colors = {
            background:       bg,
            boardOutline:     pcbHexStringToInt(nc.boardOutline || '#67d4f5'),
            boardFill:        pcbHexStringToInt(nc.boardFill    || '#07101f'),
            componentOutline: this.tokens.cyan,
            copper:           this.tokens.text,
            silkscreen:       this.tokens.text,
            pinDefault:       pcbHexStringToInt(nc['no-net']),
            // Explicit NC pins (string == 'NC') get hardcoded black so
            // they drop out of the active net family. Looser fallbacks
            // (null / N/A / UNCONNECTED) keep the user-customisable
            // 'no-net' shade above.
            pinNC:            0x000000,
            // Net-category pins: read from the user-customisable
            // palette (loaded from localStorage / falling back to
            // PCB_DEFAULT_NET_HEX). Earlier revisions hardcoded
            // pinPower / pinGround for visual distinctness, which made
            // edits to the defaults silently no-op until the user
            // touched the picker. Now defaults flow through.
            pinPower:         pcbHexStringToInt(nc.power),
            pinSignal:        pcbHexStringToInt(nc.signal),
            pinGround:        pcbHexStringToInt(nc.ground),
            pinClock:         pcbHexStringToInt(nc.clock),
            pinReset:         pcbHexStringToInt(nc.reset),
            via:              pcbHexStringToInt(nc.via || '#c084fc'),
            // Unified "test pad" colour — applies to the XZZ type_09
            // solid pad InstancedMesh (rare), TEST_POINT pins (XW /
            // TPxxx), AND mounting-hole vias. The previous split
            // (orange testPad + hardcoded gold pinTestPadSignal) made
            // the picker only update one slot at a time, leaving the
            // visually dominant yellow probe pads stuck.
            testPad:          pcbHexStringToInt(nc.testPad || '#d4af37'),
            pinTestPadSignal: pcbHexStringToInt(nc.testPad || '#d4af37'),
            highlight:        this.tokens.cyan,
            hover:            this.tokens.text2,
            netLine:          this.tokens.cyan,
        };

        // Shared geometries (created once)
        this._sharedGeometries = {};
        // Dash texture for fly-lines — built once, reused as the
        // `map` of every fly-line material. UV repeat per plane
        // controls how many dash cycles tile along each segment.
        this._flyDashTexture = this._createFlyDashTexture();
        this._sharedMaterials = {};

        this.init();
    }

    init() {
        this.scene = new THREE.Scene();
        this.scene.background = new THREE.Color(this.colors.background);

        const w = this.container.clientWidth;
        const h = this.container.clientHeight;
        const aspect = w / h;
        this.frustumSize = 100;
        this.camera = new THREE.OrthographicCamera(
            -this.frustumSize * aspect / 2, this.frustumSize * aspect / 2,
            this.frustumSize / 2, -this.frustumSize / 2, 0.1, 1000
        );
        this.camera.position.z = 100;

        this.renderer = new THREE.WebGLRenderer({
            canvas: this.canvas,
            antialias: true,
            powerPreference: 'high-performance'
        });
        this.renderer.setSize(w, h, false);
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2)); // Cap at 2x for performance

        this.mouse = new THREE.Vector2();
        this.isPanning = false;
        this.lastMousePos = { x: 0, y: 0 };
        this.zoom = 1;

        this._initSharedGeometries();
        this.setupEvents();
        this.animate();
    }

    /**
     * Pre-create shared geometries used by instanced meshes
     */
    _initSharedGeometries() {
        // Standard circular pin (most common)
        // 32 segments: smooth round pads at any zoom; the InstancedMesh
        // shares a single geometry so the per-instance cost is negligible.
        this._sharedGeometries.circlePin = new THREE.CircleGeometry(1, 32);
        // Thin border ring around each pin (~6% of the pad radius).
        // Drawn behind the fill in a darker tone so each pad reads as
        // a defined object (matches FlexBV's per-pad outline).
        this._sharedGeometries.circlePinRing = new THREE.RingGeometry(0.94, 1.0, 32);

        // Via geometries — outer ring + inner hole
        this._sharedGeometries.viaOuter = new THREE.RingGeometry(0.6, 1, 32);
        this._sharedGeometries.viaInner = new THREE.CircleGeometry(0.4, 32);

        // Test pad — solid circle, no border ring
        this._sharedGeometries.testPad = new THREE.CircleGeometry(1, 32);

        // Shared materials. Per-instance color is set via setColorAt on
        // the InstancedMesh, so the base material's colour is mostly
        // illustrative.
        this._sharedMaterials.pinFill = new THREE.MeshBasicMaterial({ color: this.colors.pinDefault });
        this._sharedMaterials.via = new THREE.MeshBasicMaterial({ color: this.colors.via, side: THREE.DoubleSide });
        this._sharedMaterials.viaHole = new THREE.MeshBasicMaterial({ color: this.colors.background });
        this._sharedMaterials.testPad = new THREE.MeshBasicMaterial({ color: this.colors.testPad });
    }

    /**
     * Classify a net name into one of the brd-color-grid categories so
     * pins can be coloured accordingly. Mirrors:
     *   - reset / clock regexes from web/brd_viewer.js (word-boundaried,
     *     priority reset > clock > power > ground > signal so CLK_3V3
     *     reads as clock)
     *   - power / ground regexes from
     *     api/board/parser/test_link.py (_POWER_RE, _GROUND_RE) — the
     *     canonical patterns the backend uses to set Net.is_power /
     *     Net.is_ground.
     *
     * Without word boundaries, the previous heuristic ('^[+-]?\\d' for
     * power) misclassified XZZ nets like '12_DATA_BUS' as power and
     * showed them in amber.
     */
    _netCategory(name) {
        if (name === 'NC') return 'nc';
        if (!name || name === 'N/A' || name === 'UNCONNECTED') {
            return 'default';
        }
        if (PCB_NET_RESET_RE.test(name)) return 'reset';
        if (PCB_NET_CLOCK_RE.test(name)) return 'clock';
        if (PCB_NET_POWER_RE.test(name)) return 'power';
        if (PCB_NET_GROUND_RE.test(name)) return 'ground';
        return 'signal';
    }

    _pinColorForCategory(cat) {
        switch (cat) {
            case 'power':  return this.colors.pinPower;
            case 'ground': return this.colors.pinGround;
            case 'signal': return this.colors.pinSignal;
            case 'clock':  return this.colors.pinClock;
            case 'reset':  return this.colors.pinReset;
            case 'nc':     return this.colors.pinNC;
            default:       return this.colors.pinDefault;
        }
    }

    /**
     * Resolve a pin's display colour, factoring in the parent
     * component_type. Test points (XW probe pads, TEST_PAD_*, TPxxx)
     * carrying a non-ground signal use the dedicated gold colour —
     * mirrors Apple/MacBook board markings where probe pads on
     * active signals are hand-tinted gold while GND probe pads stay
     * grey/inert. Pins on regular components fall through to the
     * net-category colour.
     */
    _resolvePinColor(pin, cat) {
        if (pin.component_type === 'TEST_POINT' && cat !== 'ground') {
            return this.colors.pinTestPadSignal;
        }
        return this._pinColorForCategory(cat);
    }

    /**
     * Reassign the colour of every pin instance whose net falls in
     * `category`. Persists the change to the same localStorage entry
     * the SVG renderer reads, so the picker / WebGL / SVG paths stay
     * in sync. `category` accepts the SVG-side names too ('no-net').
     */
    setNetCategoryColor(category, hex) {
        const hexStr = typeof hex === 'string'
            ? (hex.startsWith('#') ? hex : '#' + hex)
            : '#' + hex.toString(16).padStart(6, '0');
        const hexInt = pcbHexStringToInt(hexStr);

        const storeKey = category === 'default' ? 'no-net' : category;
        const targetCat = category === 'no-net' ? 'default' : category;
        if (storeKey in this.netColors) {
            this.netColors[storeKey] = hexStr;
            savePcbNetColors(this.netColors);
        }

        // Entity-typed pseudo-categories — recolour every instance of
        // the matching mesh type, regardless of net name.
        if (category === 'testPad') {
            this.colors.testPad = hexInt;
            this._recolorAllTestPads(hexInt);
            this.requestRender();
            return;
        }
        if (category === 'via') {
            this.colors.via = hexInt;
            this._recolorElectricalVias(hexInt);
            this.requestRender();
            return;
        }
        if (category === 'boardOutline') {
            this._recolorBoardOutline(hexInt);
            this.requestRender();
            return;
        }
        if (category === 'boardFill') {
            this._recolorBoardFill(hexInt);
            this.requestRender();
            return;
        }

        const colorKey = 'pin' + (targetCat === 'default' ? 'Default' :
            targetCat[0].toUpperCase() + targetCat.slice(1));
        // No more bg-blend on ground — the user's picked hex is taken
        // verbatim, matching the constructor path. A previous revision
        // pre-blended ground at 55% toward bg-deep "to keep it inert",
        // but that meant the picker output never matched what the
        // user clicked.
        const sceneInt = hexInt;
        this.colors[colorKey] = sceneInt;

        const newColor = new THREE.Color(sceneInt);
        // Walk every hoverable pin / pad — `_pinInstanceData` only
        // contains circular pins, so iterating it alone left rect SMD
        // pads (the bulk of pins on most boards) and test pads stuck
        // at the previous colour until reload. Iterate `_hoverableItems`
        // instead so the picker is live across all instance types.
        const dirtyRect = new Set();
        let dirtyCircular = false;
        let dirtyTestPad = false;
        for (const item of this._hoverableItems) {
            const t = item._instanceType;
            if (t !== 'pin' && t !== 'rectPin' && t !== 'testPad') continue;
            const cat = item.is_gnd ? 'ground' : this._netCategory(item.net);
            if (cat !== targetCat) continue;
            item.originalColor = sceneInt;
            if (item._restColor != null) item._restColor = sceneInt;
            if (t === 'pin' && this._circularPinInstance) {
                this._circularPinInstance.setColorAt(item._instanceId, newColor);
                dirtyCircular = true;
            } else if (t === 'rectPin' && item._sizeKey) {
                const rect = this._rectPinInstances.get(item._sizeKey);
                if (rect && rect.body) {
                    rect.body.setColorAt(item._instanceId, newColor);
                    dirtyRect.add(rect.body);
                }
            } else if (t === 'testPad' && this._testPadsInstance) {
                this._testPadsInstance.setColorAt(item._instanceId, newColor);
                dirtyTestPad = true;
            }
        }
        if (dirtyCircular) this._circularPinInstance.instanceColor.needsUpdate = true;
        dirtyRect.forEach((mesh) => { mesh.instanceColor.needsUpdate = true; });
        if (dirtyTestPad) this._testPadsInstance.instanceColor.needsUpdate = true;
        this.requestRender();
    }

    /**
     * Repaint every "test-pad-like" entity to a single colour. The
     * picker bucket "Test pad" historically split across three
     * internal colour slots:
     *   - colors.testPad         → XZZ type_09 solid InstancedMesh
     *   - colors.pinTestPadSignal → TEST_POINT pins (XW, TPxxx) + mounting-hole vias
     * The user sees them as one family ("the gold/yellow pads"), so
     * we unify the recolour: every entity that was painted with one
     * of those two colours flips to the picked hex in one go.
     */
    _recolorAllTestPads(hexInt) {
        const newColor = new THREE.Color(hexInt);
        this.colors.testPad = hexInt;
        this.colors.pinTestPadSignal = hexInt;

        let dirtyTestPad = false;
        let dirtyCircular = false;
        const dirtyRect = new Set();
        let dirtyVia = false;

        for (const item of this._hoverableItems) {
            const t = item._instanceType;
            // 1. XZZ type_09 solid test pads (their own InstancedMesh).
            if (t === 'testPad' && this._testPadsInstance) {
                item.originalColor = hexInt;
                if (item._restColor != null) item._restColor = hexInt;
                this._testPadsInstance.setColorAt(item._instanceId, newColor);
                dirtyTestPad = true;
                continue;
            }
            // 2. Pins inside a TEST_POINT component (the visually
            //    dominant "yellow probe pads").
            if ((t === 'pin' || t === 'rectPin')
                && item.component_type === 'TEST_POINT'
                && !item.is_gnd) {
                item.originalColor = hexInt;
                if (item._restColor != null) item._restColor = hexInt;
                if (t === 'pin' && this._circularPinInstance) {
                    this._circularPinInstance.setColorAt(item._instanceId, newColor);
                    dirtyCircular = true;
                } else if (t === 'rectPin' && item._sizeKey) {
                    const rect = this._rectPinInstances.get(item._sizeKey);
                    if (rect && rect.body) {
                        rect.body.setColorAt(item._instanceId, newColor);
                        dirtyRect.add(rect.body);
                    }
                }
                continue;
            }
            // 3. Mounting-hole vias (no net — drawn in the same gold
            //    by `_createViasInstanced`, line 1995).
            if (t === 'via' && this._viasOuterInstance) {
                const isMounting = !item.net || item.net === '' || item.net === 'NC';
                if (isMounting) {
                    item.originalColor = hexInt;
                    if (item._restColor != null) item._restColor = hexInt;
                    this._viasOuterInstance.setColorAt(item._instanceId, newColor);
                    dirtyVia = true;
                }
            }
        }

        if (dirtyTestPad && this._testPadsInstance.instanceColor) {
            this._testPadsInstance.instanceColor.needsUpdate = true;
        }
        if (dirtyCircular && this._circularPinInstance.instanceColor) {
            this._circularPinInstance.instanceColor.needsUpdate = true;
        }
        dirtyRect.forEach(m => { if (m.instanceColor) m.instanceColor.needsUpdate = true; });
        if (dirtyVia && this._viasOuterInstance.instanceColor) {
            this._viasOuterInstance.instanceColor.needsUpdate = true;
        }
    }

    /**
     * Repaint electrical vias only — vias whose `net` is set to a
     * meaningful name. Mounting holes (no net) keep their gold ring
     * because they're a distinct affordance, not part of an
     * electrical net family.
     */
    _recolorElectricalVias(hexInt) {
        if (!this._viasOuterInstance) return;
        const newColor = new THREE.Color(hexInt);
        for (const item of this._hoverableItems) {
            if (item._instanceType !== 'via') continue;
            const isMounting = !item.net || item.net === '' || item.net === 'NC';
            if (isMounting) continue;
            item.originalColor = hexInt;
            if (item._restColor != null) item._restColor = hexInt;
            this._viasOuterInstance.setColorAt(item._instanceId, newColor);
        }
        if (this._viasOuterInstance.instanceColor) {
            this._viasOuterInstance.instanceColor.needsUpdate = true;
        }
    }

    setupEvents() {
        window.addEventListener('resize', () => this.onResize());
        this.canvas.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
        this.canvas.addEventListener('mousedown', (e) => this.onMouseDown(e));
        this.canvas.addEventListener('mouseup', () => this.onMouseUp());
        this.canvas.addEventListener('mousemove', (e) => this.onMouseMove(e));
        this.canvas.addEventListener('click', (e) => this.onClick(e));
        this.canvas.addEventListener('contextmenu', (e) => e.preventDefault());

        const closeBtn = document.getElementById('info-close');
        if (closeBtn) closeBtn.addEventListener('click', () => this.clearSelection());
    }

    onResize() {
        const w = this.container.clientWidth, h = this.container.clientHeight;
        const aspect = w / h;
        this.camera.left = -this.frustumSize * aspect / 2;
        this.camera.right = this.frustumSize * aspect / 2;
        this.camera.top = this.frustumSize / 2;
        this.camera.bottom = -this.frustumSize / 2;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(w, h, false);
        this._updateComponentLabelVisibility();
        this._updateRefdesLabelScale();
        this._updateSilkscreenLabelScale();
        this._updateFlyLineDashes();
        this._updateSideArrowScale();
        this.requestRender();
    }

    onWheel(e) {
        e.preventDefault();

        // Zoom-to-cursor: convert the cursor's pixel position to the
        // world point BEFORE the frustum change, apply the zoom, then
        // shift the camera so the same world point still sits under the
        // cursor. Default behaviour was zoom-to-centre which forced the
        // user to click-pan after every scroll wheel step.
        const rect = this.container.getBoundingClientRect();
        const offsetX = e.clientX - rect.left;
        const offsetY = e.clientY - rect.top;
        const worldBefore = this.screenToWorld(offsetX, offsetY);

        const zoomFactor = e.deltaY > 0 ? 1.1 : 0.9;
        this.frustumSize *= zoomFactor;
        // Allow generous zoom-out (10× the board diagonal) so dense
        // boards like the MSI V300 (46 mm GPU section, 5k+ vias) can
        // be panned/explored from a comfortable distance instead of
        // hitting the wall after one wheel notch.
        const maxSize = this.boardData ? Math.max(this.boardData.board_width, this.boardData.board_height) * 10 : 2000;
        this.frustumSize = Math.max(0.5, Math.min(maxSize, this.frustumSize));
        this.zoom = 100 / this.frustumSize;

        // Recompute frustum extents and renderer size, then read the
        // post-zoom world position of the cursor and apply the offset.
        this.onResize();
        const worldAfter = this.screenToWorld(offsetX, offsetY);
        this.camera.position.x += worldBefore.x - worldAfter.x;
        this.camera.position.y += worldBefore.y - worldAfter.y;

        const zoomEl = document.getElementById('zoom-level');
        if (zoomEl) zoomEl.textContent = Math.round(this.zoom * 100);
        this.requestRender();
    }

    onMouseDown(e) {
        // Stash the press position so `onClick` can tell a genuine
        // click ("click in empty space to deselect") from the tail of
        // a pan drag — the browser fires `click` after both, but we
        // only want to deselect on the former.
        this._pressStart = { x: e.clientX, y: e.clientY };

        // Right click, middle click, or shift+left click = always pan
        if (e.button === 2 || e.button === 1 || (e.button === 0 && e.shiftKey)) {
            this.isPanning = true;
            this.lastMousePos = { x: e.clientX, y: e.clientY };
            this.canvas.style.cursor = 'grabbing';
            return;
        }

        // Left click: pan if clicking in empty space (no hovered item)
        if (e.button === 0 && !this.hoveredItem) {
            this.isPanning = true;
            this.lastMousePos = { x: e.clientX, y: e.clientY };
            this.canvas.style.cursor = 'grabbing';
        }
    }

    onMouseUp() {
        this.isPanning = false;
        this.canvas.style.cursor = 'default';
    }

    onMouseMove(e) {
        const now = performance.now();

        if (!this.isPanning && now - this.lastMouseMoveTime < this.mouseMoveThrottleMs) {
            return;
        }
        this.lastMouseMoveTime = now;

        const containerRect = this.container.getBoundingClientRect();
        const w = this.container.clientWidth;
        const h = this.container.clientHeight;

        const offsetX = e.clientX - containerRect.left;
        const offsetY = e.clientY - containerRect.top;

        this.mouse.x = (offsetX / w) * 2 - 1;
        this.mouse.y = -(offsetY / h) * 2 + 1;

        if (this.isPanning) {
            const aspect = w / h;
            const dx = (e.clientX - this.lastMousePos.x) * (this.frustumSize * aspect) / w;
            const dy = (e.clientY - this.lastMousePos.y) * this.frustumSize / h;
            this.camera.position.x -= dx;
            this.camera.position.y += dy;
            this.lastMousePos = { x: e.clientX, y: e.clientY };
            this.requestRender();
        }

        // Update cursor position display
        if (!this.isPanning) {
            this._pendingCursorUpdate = { offsetX, offsetY };
            if (!this._cursorUpdateScheduled) {
                this._cursorUpdateScheduled = true;
                requestAnimationFrame(() => {
                    if (this._pendingCursorUpdate) {
                        const worldPos = this.screenToWorld(this._pendingCursorUpdate.offsetX, this._pendingCursorUpdate.offsetY);
                        document.getElementById('cursor-x').textContent = worldPos.x.toFixed(2);
                        document.getElementById('cursor-y').textContent = worldPos.y.toFixed(2);
                    }
                    this._cursorUpdateScheduled = false;
                });
            }
        }

        // Throttle hover check
        if (!this.isPanning && now - this.lastHoverCheck >= this.hoverThrottleMs) {
            this.lastHoverCheck = now;
            this.checkHover();
        }
    }

    onClick(e) {
        // Empty-space click → deselect, BUT only when the cursor
        // didn't actually drag. The browser still fires `click` after
        // a small pan, so we gate on the mousedown→mouseup distance:
        // ≤ 4 px = genuine click, > 4 px = drag tail (preserve view).
        const start = this._pressStart;
        const dragged = start && (
            Math.abs(e.clientX - start.x) > 4
            || Math.abs(e.clientY - start.y) > 4
        );
        if (dragged) return;

        // Side-flip arrows sit at the dive-under-board fly-line
        // endpoints in TOP / BOTTOM mode. Clicking one switches the
        // viewer to the other face — handle them upfront so they
        // never get stuck in the cycle-through-stack logic below.
        if (this.hoveredItem && this.hoveredItem._instanceType === 'sideArrow') {
            this.setSideMode(this.hoveredItem._targetSide, { keepView: true });
            return;
        }

        // Stack-picking: collect ALL items hit at the cursor location,
        // sorted by tier (pin > component) then by AABB size (smaller
        // first). A click cycles through the stack — repeat clicks at
        // the same spot move to the item under the previous one. This
        // is how Altium / Cadence / KiCad let you reach a body sitting
        // under a pad, or a DNP alternate sitting under a placed body.
        const stack = this._collectClickStack();
        if (stack.length === 0) {
            if (this.selectedItem) this.clearSelection();
            return;
        }
        const cursorKey = `${e.clientX}_${e.clientY}`;
        const sameSpot = this._lastClickKey
            && Math.abs(e.clientX - this._lastClickPx) <= 4
            && Math.abs(e.clientY - this._lastClickPy) <= 4;
        if (sameSpot) {
            this._clickStackIndex = (this._clickStackIndex + 1) % stack.length;
        } else {
            this._clickStackIndex = 0;
        }
        this._lastClickKey = cursorKey;
        this._lastClickPx = e.clientX;
        this._lastClickPy = e.clientY;
        this.selectItem(stack[this._clickStackIndex]);
    }

    /**
     * Return every hoverable item the cursor is currently over, sorted
     * pin-tier-first then by AABB size. Same hit-test rules as
     * `checkHover` (side filter, DNP filter, layer filter, slop), but
     * instead of returning the single winner it returns the full stack
     * so `onClick` can cycle through it.
     */
    _collectClickStack() {
        const w = this.container.clientWidth;
        const h = this.container.clientHeight;
        const aspect = w / h;
        const worldX = this.camera.position.x + this.mouse.x * (this.frustumSize * aspect / 2);
        const worldY = this.camera.position.y + this.mouse.y * (this.frustumSize / 2);
        const local = this._worldToLocal(worldX, worldY);
        const localX = local.x, localY = local.y;
        const nearby = this._getNearbyItems(localX, localY);
        const pixelSize = this.frustumSize / h;
        const pinSlop = 8 * pixelSize;
        const pinThresholdSq = pinSlop * pinSlop;
        const pinHits = [];
        const compHits = [];
        for (const item of nearby) {
            if (item.layer && !this.layers[item.layer]) continue;
            if (item._side && this.sideMode !== 'both' && item._side !== this.sideMode) continue;
            const t = item._instanceType;
            if (!this._showDnp && (item.is_dnp || t === 'dnpComp')) continue;
            const isPinTier = t === 'pin' || t === 'rectPin' || t === 'testPad'
                || t === 'via' || t === 'marker' || t === 'sideArrow';
            const dx = item.x - localX, dy = item.y - localY;
            const halfW = (item.width || 0) / 2, halfH = (item.height || 0) / 2;
            const ex = Math.max(0, Math.abs(dx) - halfW);
            const ey = Math.max(0, Math.abs(dy) - halfH);
            const distSq = ex * ex + ey * ey;
            const size = (item.width || 0.05) * (item.height || 0.05);
            if (isPinTier) {
                if (distSq > pinThresholdSq) continue;
                pinHits.push({ item, distSq, size });
            } else {
                if (distSq > 0) continue;
                compHits.push({ item, distSq, size });
            }
        }
        const sortFn = (a, b) => a.distSq - b.distSq || a.size - b.size;
        pinHits.sort(sortFn);
        compHits.sort(sortFn);
        return [...pinHits, ...compHits].map((h) => h.item);
    }

    screenToWorld(offsetX, offsetY) {
        const w = this.container.clientWidth;
        const h = this.container.clientHeight;
        const aspect = w / h;

        const ndcX = (offsetX / w) * 2 - 1;
        const ndcY = -(offsetY / h) * 2 + 1;

        const worldX = this.camera.position.x + ndcX * (this.frustumSize * aspect / 2);
        const worldY = this.camera.position.y + ndcY * (this.frustumSize / 2);

        return { x: worldX, y: worldY };
    }

    /**
     * Invert the scene rotation on a (worldX, worldY) point so the
     * result is comparable against item coordinates (which were stashed
     * pre-rotation at parse time). Used by hover and zoom-around-cursor
     * so they keep working after a rotate toggle.
     */
    _worldToLocal(wx, wy) {
        const rad = -(this.rotationDeg || 0) * Math.PI / 180;
        const cos = Math.cos(rad);
        const sin = Math.sin(rad);
        return {
            x: wx * cos - wy * sin,
            y: wx * sin + wy * cos,
        };
    }

    /**
     * Spatial grid hover detection - O(1) lookup
     */
    checkHover() {
        const w = this.container.clientWidth;
        const h = this.container.clientHeight;
        const aspect = w / h;

        const worldX = this.camera.position.x + this.mouse.x * (this.frustumSize * aspect / 2);
        const worldY = this.camera.position.y + this.mouse.y * (this.frustumSize / 2);

        // Items live in scene-local coords (their original XY). When
        // the scene is rotated / flipped, the cursor's world XY needs
        // to be inverted through the same transform before we can
        // match it against the spatial grid.
        const local = this._worldToLocal(worldX, worldY);
        const localX = local.x;
        const localY = local.y;

        // Get nearby items from spatial grid
        const nearbyItems = this._getNearbyItems(localX, localY);

        const pixelSize = this.frustumSize / h;
        // Pins / pads / vias / markers / arrows are small "interactive"
        // targets — generous slop (8 px) so the tech doesn't have to
        // pixel-hunt at low zoom. Components are large (mm-scale) and
        // would steal hovers from anything sitting on top of them, so
        // they only hover when the cursor is INSIDE their AABB (slop=0).
        const pinSlop = 8 * pixelSize;
        const compSlop = 0;
        const pinThresholdSq = pinSlop * pinSlop;

        // Two-tier search: pins first, components only as a fallback.
        // Within each tier, smallest AABB-distance wins. Pins always
        // beat components when both could match — fixes the long-
        // standing bug where clicking a pin sitting on top of a part
        // would select the part instead.
        let bestPin = null;
        let bestPinDistSq = Infinity;
        let bestPinSize = Infinity;
        let bestComp = null;
        let bestCompDistSq = Infinity;
        let bestCompSize = Infinity;

        for (const item of nearbyItems) {
            if (item.layer && !this.layers[item.layer]) continue;
            if (item._side && this.sideMode !== 'both' && item._side !== this.sideMode) continue;

            const t = item._instanceType;
            // DNP pins are baked into the placed-pin meshes with an
            // `is_dnp` flag carried on the hoverable item — when the
            // toggle is off, the matrix is zero-scaled so the pad is
            // invisible AND uninteractive. Same gate for DNP component
            // bodies (the `dnpComp` overlays).
            if (!this._showDnp && (item.is_dnp || t === 'dnpComp')) continue;

            const isPinTier = t === 'pin' || t === 'rectPin' || t === 'testPad'
                || t === 'via' || t === 'marker' || t === 'sideArrow';

            const dx = item.x - localX;
            const dy = item.y - localY;
            const halfW = (item.width || 0) / 2;
            const halfH = (item.height || 0) / 2;
            const ex = Math.max(0, Math.abs(dx) - halfW);
            const ey = Math.max(0, Math.abs(dy) - halfH);
            const distSq = ex * ex + ey * ey;

            if (isPinTier) {
                if (distSq > pinThresholdSq) continue;
                // Tie-breaker on identical distSq (very common — both
                // candidates are inside their AABB and distSq=0): prefer
                // the smaller item. The tighter AABB is the better
                // visual match; otherwise a small SMD pad always loses
                // to a wider rect pad it sits next to.
                const size = (item.width || 0.05) * (item.height || 0.05);
                if (distSq < bestPinDistSq
                    || (distSq === bestPinDistSq && size < bestPinSize)) {
                    bestPinDistSq = distSq;
                    bestPinSize = size;
                    bestPin = item;
                }
            } else {
                // Components / generic meshes — strict inside-AABB only.
                if (distSq > compSlop * compSlop) continue;
                const size = (item.width || 0.05) * (item.height || 0.05);
                if (distSq < bestCompDistSq
                    || (distSq === bestCompDistSq && size < bestCompSize)) {
                    bestCompDistSq = distSq;
                    bestCompSize = size;
                    bestComp = item;
                }
            }
        }

        const nearest = bestPin || bestComp;

        // Update hover state
        if (this.hoveredItem && this.hoveredItem !== nearest) {
            // Reset previous hover
            this._setItemHighlight(this.hoveredItem, false);
            this.requestRender();
        }

        if (nearest && nearest !== this.selectedItem) {
            this.hoveredItem = nearest;
            this._setItemHighlight(nearest, true);
            this.canvas.style.cursor = 'pointer';
            this.requestRender();
        } else if (!nearest) {
            this.hoveredItem = null;
            if (!this.isPanning) this.canvas.style.cursor = 'default';
        }
    }

    /**
     * Set highlight state for an item (works with instanced meshes)
     * @param {object} item - The item data
     * @param {boolean} highlighted - Whether to highlight or restore original color
     * @param {number} [customColor] - Optional custom highlight color (default: 0x666666 for hover)
     */
    /**
     * Push every pin / rect-pin / test-pad NOT on `netName` toward the
     * deep background colour so the highlighted net stands alone. The
     * selected pin and same-net pins were already recoloured by
     * `_setItemHighlight` calls above and are skipped here. One pass
     * over `_hoverableItems`, then a single instanceColor.needsUpdate
     * per InstancedMesh.
     */
    _dimUnrelatedPins(netName) {
        if (!netName || netName === 'NC') return;
        this._dimActive = true;
        const dimFactor = 0.18;  // 18% of original brightness — keeps a
                                 // hint of colour without making the
                                 // unrelated pins competitively bright.
        const bg = new THREE.Color(this.tokens.bgDeep || 0x07101f);
        const tmp = new THREE.Color();
        for (const item of this._hoverableItems) {
            const t = item._instanceType;
            if (t !== 'pin' && t !== 'rectPin' && t !== 'testPad') continue;
            if (item.net === netName) continue;
            tmp.setHex(item.originalColor || 0x808080);
            // lerp(original, bg, 1 - factor) → factor=0.18 keeps 18%
            // of the original, blends 82% into bg-deep.
            tmp.lerp(bg, 1 - dimFactor);
            // Stash the dimmed hex so hover-out snaps back to dim, not
            // to the pin's original full-brightness colour.
            item._restColor = tmp.getHex();
            this._writeInstanceColor(item, tmp);
        }
        this._markPinInstanceColorDirty();
    }

    /**
     * Boost the copper traces on `netName` to the net's family colour
     * at full opacity. Other traces are LEFT UNTOUCHED — every copper
     * trace belongs to some net by definition, so dimming the rest
     * would arbitrarily fade out other nets' paths the user might
     * still want to read alongside. Outline / silkscreen layers stay
     * out of scope.
     */
    _highlightNetTraces(netName, lineColor) {
        if (!this.meshGroups || !this.meshGroups.traces) return;
        this._tracesNetActive = true;
        const lineColorObj = new THREE.Color(lineColor);
        for (const line of this.meshGroups.traces) {
            const ud = line.userData;
            if (!ud || ud._kind !== 'copper') continue;
            if (!line.material) continue;
            if (ud.net && ud.net === netName) {
                line.material.color.set(lineColorObj);
                line.material.opacity = 1.0;
                line.material.needsUpdate = true;
                // Force visible — net highlight overrides the toolbar
                // showTraces toggle for matched traces so the user
                // always sees the routing of the clicked net even when
                // the global trace layer is off. _undimAllTraces puts
                // the toolbar gate back when the highlight clears.
                line.visible = true;
            }
        }
    }

    /**
     * Push a hex toward white until its luminance reaches ~210/255.
     * Fly-lines render as 1-pixel dashed segments on Chromium (the
     * `linewidth` GL hint is ignored), so anything darker than mid-grey
     * effectively disappears against the navy canvas. Compute the
     * exact white-blend ratio that hits the target luminance instead
     * of capping at a fixed factor — Power #B16628 (lum 117) needs
     * ~50 % white, Ground #40455C (lum 70) needs ~75 %, while bright
     * hues (cyan netLine lum 183, peach reset lum 156) stay close to
     * their family colour.
     */
    _brightenForFlyLine(hexInt) {
        const r = (hexInt >> 16) & 0xff;
        const g = (hexInt >> 8) & 0xff;
        const b = hexInt & 0xff;
        const lum = 0.299 * r + 0.587 * g + 0.114 * b;
        const target = 210;
        if (lum >= target) return hexInt;
        // White luminance is 255. Solve: lum + (255 - lum) * t = target
        //   → t = (target - lum) / (255 - lum)
        const t = (target - lum) / (255 - lum);
        const nr = Math.round(r + (255 - r) * t);
        const ng = Math.round(g + (255 - g) * t);
        const nb = Math.round(b + (255 - b) * t);
        return (nr << 16) | (ng << 8) | nb;
    }

    _undimAllTraces() {
        if (!this._tracesNetActive) return;
        this._tracesNetActive = false;
        if (!this.meshGroups || !this.meshGroups.traces) return;
        for (const line of this.meshGroups.traces) {
            const ud = line.userData;
            if (!ud || ud._kind !== 'copper') continue;
            if (!line.material) continue;
            line.material.color.setHex(ud.origColor);
            line.material.opacity = ud.origOpacity;
            line.material.needsUpdate = true;
            // Restore the toolbar showTraces gate. Outline / silkscreen
            // layers stay visible regardless (createTrace forces them
            // visible at parse time and the loop above skips non-copper
            // _kinds).
            line.visible = this.showTraces;
        }
    }

    _undimAllPins() {
        if (!this._dimActive) return;
        this._dimActive = false;
        const tmp = new THREE.Color();
        for (const item of this._hoverableItems) {
            const t = item._instanceType;
            if (t !== 'pin' && t !== 'rectPin' && t !== 'testPad') continue;
            tmp.setHex(item.originalColor || 0x808080);
            // Drop the dim resting override so subsequent hovers snap
            // back to the parse-time colour again.
            delete item._restColor;
            this._writeInstanceColor(item, tmp);
        }
        this._markPinInstanceColorDirty();
    }

    _writeInstanceColor(item, color) {
        if (item._instanceType === 'pin' && this._circularPinInstance) {
            this._circularPinInstance.setColorAt(item._instanceId, color);
        } else if (item._instanceType === 'rectPin' && item._sizeKey) {
            const rect = this._rectPinInstances.get(item._sizeKey);
            if (rect && rect.body) rect.body.setColorAt(item._instanceId, color);
        } else if (item._instanceType === 'testPad' && this._testPadsInstance) {
            this._testPadsInstance.setColorAt(item._instanceId, color);
        }
    }

    _markPinInstanceColorDirty() {
        if (this._circularPinInstance && this._circularPinInstance.instanceColor) {
            this._circularPinInstance.instanceColor.needsUpdate = true;
        }
        this._rectPinInstances.forEach(({ body }) => {
            if (body && body.instanceColor) body.instanceColor.needsUpdate = true;
        });
        if (this._testPadsInstance && this._testPadsInstance.instanceColor) {
            this._testPadsInstance.instanceColor.needsUpdate = true;
        }
    }

    _setItemHighlight(item, highlighted, customColor = 0x666666) {
        if (!item) return;

        const highlightColor = new THREE.Color(customColor);
        // Resting (un-hovered) colour — `_restColor` overrides the
        // parse-time `originalColor` when the item is currently in a
        // non-default state (member of a highlighted net, dimmed because
        // an unrelated net is selected, …). Without this, hover-out on
        // such an item snaps it back to the wrong colour and breaks the
        // visual story (dimmed pins look hover-stuck-bright; same-net
        // pins lose their net colour on hover-out).
        const restingHex = item._restColor != null
            ? item._restColor
            : item.originalColor;
        const restingColor = new THREE.Color(restingHex);
        const targetColor = highlighted ? highlightColor : restingColor;

        // Circular pins
        if (item._instanceType === 'pin' && this._circularPinInstance) {
            this._circularPinInstance.setColorAt(item._instanceId, targetColor);
            this._circularPinInstance.instanceColor.needsUpdate = true;
        }
        // Rectangular pins
        else if (item._instanceType === 'rectPin' && item._sizeKey) {
            const rectInstance = this._rectPinInstances.get(item._sizeKey);
            if (rectInstance && rectInstance.body) {
                rectInstance.body.setColorAt(item._instanceId, targetColor);
                rectInstance.body.instanceColor.needsUpdate = true;
            }
        }
        // Test pads
        else if (item._instanceType === 'testPad' && this._testPadsInstance) {
            this._testPadsInstance.setColorAt(item._instanceId, targetColor);
            this._testPadsInstance.instanceColor.needsUpdate = true;
        }
        // Components (regular meshes) — pass `null` on hover-out so
        // each child material restores its OWN userData.origColor
        // instead of all of them snapping to the parent's single
        // restingHex (which used to recolour the component fill cyan
        // and leave it stuck there).
        else if (item._mesh) {
            this.setMeshColor(item._mesh, highlighted ? customColor : null);
        }
    }

    /**
     * Walk a mesh tree and recolour every Mesh / Line material it
     * contains. When `color` is the sentinel `null`, each child is
     * restored to its individually-stashed `userData.origColor` —
     * needed because a component group on KiCad / BRD bundles a cyan
     * Line outline AND a grey-blue Mesh fill: snapping both back to
     * the same hex on hover-out used to leave the fill stuck in
     * outline cyan ("component stays lit up after hover").
     */
    setMeshColor(mesh, color) {
        const applyColor = (obj) => {
            if (obj.material && (obj.material.type === 'MeshBasicMaterial' ||
                                 obj.material.type === 'LineBasicMaterial')) {
                let target;
                if (color === null) {
                    target = (obj.userData && obj.userData.origColor != null)
                        ? obj.userData.origColor
                        : obj.material.color.getHex();
                } else {
                    target = color;
                }
                obj.material.color.setHex(target);
            }
            if (obj.children) {
                obj.children.forEach(child => applyColor(child));
            }
        };
        applyColor(mesh);
    }

    _getGridKey(x, y) {
        const cellX = Math.floor(x / this._gridCellSize);
        const cellY = Math.floor(y / this._gridCellSize);
        return `${cellX},${cellY}`;
    }

    _getNearbyItems(worldX, worldY) {
        const items = [];
        const cellX = Math.floor(worldX / this._gridCellSize);
        const cellY = Math.floor(worldY / this._gridCellSize);

        for (let dx = -1; dx <= 1; dx++) {
            for (let dy = -1; dy <= 1; dy++) {
                const key = `${cellX + dx},${cellY + dy}`;
                if (this._spatialGrid[key]) {
                    items.push(...this._spatialGrid[key]);
                }
            }
        }
        return items;
    }

    _buildSpatialGrid() {
        this._spatialGrid = {};

        for (const item of this._hoverableItems) {
            const key = this._getGridKey(item.x, item.y);
            if (!this._spatialGrid[key]) {
                this._spatialGrid[key] = [];
            }
            this._spatialGrid[key].push(item);
        }

        console.log(`[PERF] Spatial grid: ${Object.keys(this._spatialGrid).length} cells for ${this._hoverableItems.length} items`);
    }

    selectItem(item) {
        this.clearSelection();
        this.selectedItem = item;

        // Apply cyan highlight color for selection
        this._setItemHighlight(item, true, this.colors.highlight);

        // Show inspector and populate fields. The DOM lives in
        // index.html under <aside class="brd-inspector">.
        const inspector = document.getElementById('component-info');
        if (inspector) inspector.hidden = false;

        const setText = (id, txt) => {
            const el = document.getElementById(id);
            if (el) el.textContent = txt;
        };
        setText('info-id', item.id || '—');
        setText('info-value', item.value || item.net || '—');
        setText('info-type', (item.type || 'Pin').toUpperCase());
        setText('info-layer', (item.layer || 'top').toUpperCase());
        setText('info-pos',
            `X: ${(item.x || 0).toFixed(2)}  Y: ${(item.y || 0).toFixed(2)}`);
        setText('info-size', item.width
            ? `${item.width.toFixed(1)} × ${(item.height || 0).toFixed(1)} mm`
            : '');

        // Component metadata — only meaningful when the selected item
        // is a part (footprint name from the source format, pin
        // count). Pins / vias / test pads / markers don't carry
        // these so the rows stay hidden. `item.footprint` and
        // `item.pin_count` come straight from the render payload's
        // `components[]` shape (see api/board/render.py).
        const fpEl = document.getElementById('info-footprint');
        const pcEl = document.getElementById('info-pincount');
        const hasFootprint = !!(item.footprint && typeof item.footprint === 'string');
        if (fpEl) {
            fpEl.hidden = !hasFootprint;
            if (hasFootprint) fpEl.textContent = `Footprint: ${item.footprint}`;
        }
        const hasPinCount = Number.isFinite(item.pin_count) && item.pin_count > 0;
        if (pcEl) {
            pcEl.hidden = !hasPinCount;
            if (hasPinCount) {
                pcEl.textContent = item.pin_count > 1
                    ? `${item.pin_count} pins`
                    : `1 pin`;
            }
        }

        // Net row visibility
        const netLabel = document.getElementById('info-net-label');
        const netRow = document.getElementById('info-net-row');
        const hasNet = item.net && item.net !== 'NC' &&
            item.net !== 'N/A' && item.net !== 'UNCONNECTED';
        if (netLabel) netLabel.hidden = !hasNet;
        if (netRow) netRow.hidden = !hasNet;
        if (hasNet) setText('info-net', item.net);

        // Manufacturer-tagged diagnostic expectation for this net,
        // when the source format ships one (XZZ post-v6 block on
        // iPad/iPhone dumps, see api/board/parser/_xzz_engine_extras
        // .py). Hides the whole block when the net is unknown or
        // carries no expectation.
        const diagLabel = document.getElementById('info-diag-label');
        const diagRow = document.getElementById('info-diag-row');
        const diagR = document.getElementById('info-diag-resistance');
        const diagV = document.getElementById('info-diag-voltage');
        const diag = (hasNet && this._netDiagnostics)
            ? this._netDiagnostics.get(item.net) : null;
        const hasDiagR = diag && (diag.expected_resistance_ohms !== null || diag.expected_open);
        const hasDiagV = diag && diag.expected_voltage_v !== null;
        if (diagLabel) diagLabel.hidden = !(hasDiagR || hasDiagV);
        if (diagRow)   diagRow.hidden   = !(hasDiagR || hasDiagV);
        if (diagR) {
            diagR.hidden = !hasDiagR;
            if (hasDiagR) {
                if (diag.expected_open) {
                    diagR.textContent = 'Résistance : OL (≥1 kΩ ou circuit ouvert)';
                } else {
                    const ohms = diag.expected_resistance_ohms;
                    // Format: 0 → court / GND ; <1000 → "Ω" ; ≥1000 → "kΩ".
                    let r;
                    if (ohms === 0)        r = 'court (~0 Ω, GND ou rail)';
                    else if (ohms < 1000)  r = `${ohms.toFixed(0)} Ω`;
                    else                   r = `${(ohms / 1000).toFixed(2)} kΩ`;
                    diagR.textContent = `Résistance : ${r}`;
                }
            }
        }
        if (diagV) {
            diagV.hidden = !hasDiagV;
            if (hasDiagV) diagV.textContent = `Tension : ${diag.expected_voltage_v.toFixed(2)} V`;
        }

        // DFM alternate (DNP) section. The placed sibling carries a list
        // of refdes that share its physical seat but are not stuffed on
        // this board variant. We look each one up in the components
        // index to surface its footprint / value when known.
        const altLabel = document.getElementById('info-alt-label');
        const altRow = document.getElementById('info-alt-row');
        const altList = document.getElementById('info-alt-list');
        const alternates = Array.isArray(item.dnp_alternates) ? item.dnp_alternates : [];
        const hasAlternates = alternates.length > 0;
        if (altLabel) altLabel.hidden = !hasAlternates;
        if (altRow)   altRow.hidden   = !hasAlternates;
        if (altList) {
            altList.innerHTML = '';
            if (hasAlternates) {
                const compIndex = this._compIndex || new Map();
                for (const refdes of alternates) {
                    const alt = compIndex.get(refdes);
                    const li = document.createElement('li');
                    if (alt) {
                        const fp = alt.footprint || alt.id;
                        const v = alt.value || '(non listé au BOM — DNP)';
                        li.innerHTML =
                            `<span class="mono">${refdes}</span> ` +
                            `<span class="muted">${fp}</span><br>` +
                            `<span class="muted">${v}</span>`;
                    } else {
                        li.innerHTML = `<span class="mono">${refdes}</span> <span class="muted">(DNP)</span>`;
                    }
                    altList.appendChild(li);
                }
            }
        }

        this.updateConnectionsPanel(item.net, item.id);

        // For a pin selection, light up every other pin on the same net
        // automatically — that's the action the user expects when they
        // click a pad. Components don't trigger net highlight (would
        // colour every pin tied to whichever net the first pin happened
        // to be on, which is misleading).
        if (item._instanceType === 'pin' || item._instanceType === 'rectPin'
            || item._instanceType === 'dnpPin') {
            if (hasNet) this.highlightNet();
        }

        this.requestRender();
    }

    updateConnectionsPanel(netName, currentId) {
        const label = document.getElementById('info-connections-label');
        const list = document.getElementById('connections-list');
        const countEl = document.getElementById('connections-count');
        if (!list) return;

        list.innerHTML = '';

        if (!netName || netName === 'N/A' || netName === 'NC' ||
            netName === 'UNCONNECTED') {
            if (label) label.hidden = true;
            if (countEl) countEl.hidden = true;
            return;
        }

        const connectedPins = this._hoverableItems.filter(it =>
            it.net === netName && it.id !== currentId
        );
        if (!connectedPins.length) {
            if (label) label.hidden = true;
            if (countEl) countEl.hidden = true;
            return;
        }

        // Group by component
        const byComponent = {};
        for (const pin of connectedPins) {
            const c = pin.component || pin.id;
            (byComponent[c] = byComponent[c] || []).push(pin);
        }
        const names = Object.keys(byComponent).sort();
        const maxDisplay = 12;

        if (label) label.hidden = false;
        for (const compName of names.slice(0, maxDisplay)) {
            const pins = byComponent[compName];
            const li = document.createElement('li');
            li.className = 'brd-ins-link';
            const ref = document.createElement('span');
            ref.className = 'brd-ins-link-ref';
            ref.textContent = compName;
            const cnt = document.createElement('span');
            cnt.className = 'brd-ins-link-count';
            cnt.textContent = pins.length > 1 ? `${pins.length} pins` : '1 pin';
            li.appendChild(ref);
            li.appendChild(cnt);
            li.addEventListener('click', () => this.focusOnItem(pins[0]));
            list.appendChild(li);
        }

        if (countEl) {
            if (names.length > maxDisplay) {
                countEl.hidden = false;
                countEl.textContent = `+ ${names.length - maxDisplay} autres composants`;
            } else {
                countEl.hidden = true;
            }
        }
    }

    focusOnItem(item) {
        if (!item) return;

        this.camera.position.x = item.x;
        this.camera.position.y = item.y;
        this.onResize();
        this.selectItem(item);
    }

    clearSelection() {
        if (this.selectedItem) {
            this._setItemHighlight(this.selectedItem, false);
            this.selectedItem = null;
            this.requestRender();
        }
        this.clearNetHighlight();
        const inspector = document.getElementById('component-info');
        if (inspector) inspector.hidden = true;
    }

    highlightNet() {
        if (!this.selectedItem) return;
        const netName = this.selectedItem.net;
        if (!netName || netName === 'NC') return;

        this.clearNetHighlight();

        const selectedX = this.selectedItem.x;
        const selectedY = this.selectedItem.y;

        // Only PINS belong on the same net — _hoverableItems also holds
        // components, whose `net` field mirrors the first pin's net but
        // are not themselves nodes on that net. Without this filter, the
        // selected pin's net "highlights" every component that happens to
        // have any pin on the same net, with bogus star-pattern lines to
        // each component centroid (looks like extra connections that
        // don't exist).
        const sameNetItems = this._hoverableItems.filter(item =>
            (item._instanceType === 'pin' || item._instanceType === 'rectPin' ||
             item._instanceType === 'testPad') &&
            item.net === netName && item !== this.selectedItem
        );

        this.netLines = [];
        this.highlightedItems = [];

        // Fly-lines tint by the net's category for the meaningful
        // ones (power=amber, ground=darkened, clock=violet, reset=peach).
        // For 'default' (no-net) and 'signal' the category colours are
        // grey/text — they'd disappear against the navy background and
        // the grey pins themselves — so fall back to cyan as the agent
        // overlay colour.
        const cat = this.selectedItem.is_gnd
            ? 'ground'
            : this._netCategory(netName);
        const lineColor = (cat === 'default' || cat === 'signal')
            ? this.colors.netLine
            : this._pinColorForCategory(cat);

        // Recolour every linked pin to the trace colour so the family
        // reads as a unit — without this, signal pins on a clicked
        // signal net stay grey and visually disappear among the rest.
        // The selected pin keeps the cyan highlight (set in selectItem
        // above) as the click anchor.
        sameNetItems.forEach(item => {
            // _restColor: the colour hover-out should snap back to
            // (the net trace colour, not the pin's parse-time original).
            item._restColor = lineColor;
            this._setItemHighlight(item, true, lineColor);
            this.highlightedItems.push(item);
        });

        // Dim every pin / pad NOT on this net toward the deep
        // background colour so the net family reads cleanly without
        // the surrounding pins competing visually. Skipped on GND /
        // unnamed nets above already.
        this._dimUnrelatedPins(netName);
        // Symmetrically push the copper traces — bring matching-net
        // traces up to net-family colour at full opacity, fade the
        // unrelated ones down so the active net's path stands out.
        // Outline (layer 28) and silkscreen (layer 17) are left alone.
        this._highlightNetTraces(netName, lineColor);

        // Draw fly-lines only for sane-sized nets. On GND / NC /
        // multi-thousand-pin planes the star pattern is unreadable AND
        // tanks frame rate. Cap at 200 — covers nearly every named
        // power rail on iPhone-class boards (PP1V25_S2=144,
        // PPVDD_PCPU_AWAKE=142, PP0V6_S1_VDDQL=170, …) while still
        // protecting against GND (~4348 pins on X1799).
        const RATNEST_MAX_PINS = 200;
        if (sameNetItems.length + 1 > RATNEST_MAX_PINS) {
            this.requestRender();
            return;
        }

        // Dashed lines with additive blending: pointillés so the trace
        // reads as 'agent overlay' rather than real copper, plus
        // additive on the navy bg-deep makes the cyan dashes glow and
        // stay visible even at 1px width (WebGL ignores
        // LineBasicMaterial.linewidth on most drivers). depthWrite=false
        // so overlapping lines blend cleanly.
        //
        // Fly-lines are rendered as PlaneGeometry rectangles with a
        // tiled dash TEXTURE (one plane per segment, NOT one plane
        // per dash). Why: WebGL on Chromium clamps `linewidth` to
        // 1 device pixel, which on HiDPI ≈ 0.5 CSS pixels — too
        // thin to register, so we use real-mesh thickness instead.
        // And tiling via texture UV repeat keeps a 200-pin net at
        // 200 meshes total (vs 600+ when each dash was its own
        // plane), which lets onResize update them in place.
        const material = new THREE.MeshBasicMaterial({
            color: lineColor,
            map: this._flyDashTexture,
            opacity: 1.0,
            transparent: true,
            alphaTest: 0.5,
            depthWrite: false,
            depthTest: false,
            side: THREE.DoubleSide,
        });

        // Per-line z bumped well above pin instances (z=2/2.5) so
        // the fly-net floats over everything else.
        const flyZ = 6;
        // When the viewer is filtered to a single face (TOP / BOTTOM)
        // and the board ships dual outline, the hidden face's pins live
        // off in the side-by-side gap area — pointing fly-lines straight
        // at them looks broken (the lines fly into empty space). Project
        // them onto the visible face's frame and drop the endpoint
        // behind the board substrate so the line visibly "dives under"
        // the board, telling the tech the connection continues on the
        // hidden side. In BOTH mode, keep the original side-by-side
        // behaviour — the hidden pin is right there on screen.
        const startPos = this._netEndpointPos(this.selectedItem, flyZ);
        if (!this._netHiddenMarkers) this._netHiddenMarkers = [];
        const thickness = this._flyThicknessWorld();
        const { dashWorld, gapWorld } = this._flyDashWorld();
        sameNetItems.forEach(item => {
            const endPos = this._netEndpointPos(item, flyZ);
            this._createFlyLineDashed(
                startPos, endPos, material, thickness, dashWorld, gapWorld
            );
            // Cross-face endpoint? Drop a clickable down-arrow marker
            // at the projected XY so the tech sees "this connection
            // continues on the other face" and can flip to it directly.
            const isHidden = endPos.z < 0
                && item._side
                && this.sideMode !== 'both'
                && item._side !== this.sideMode;
            if (isHidden) {
                this._addSideFlipArrow(endPos.x, endPos.y, item._side);
            }
        });
        // Also drop an arrow at the start side if (rare) the selected
        // pin itself ended up on the hidden face — usually the agent
        // picks a visible pin, but `bv_highlight` from a refdes can
        // land on a hidden pin too.
        if (startPos.z < 0
            && this.selectedItem._side
            && this.sideMode !== 'both'
            && this.selectedItem._side !== this.sideMode) {
            this._addSideFlipArrow(startPos.x, startPos.y, this.selectedItem._side);
        }

        // Pin the chevrons to a fixed screen pixel height now that
        // the lot has been added.
        this._updateSideArrowScale();
        this.requestRender();
    }

    /**
     * Keep the side-flip chevrons at a fixed pixel height regardless
     * of zoom — same approach as the refdes labels — so they stay
     * easy to click without growing huge when the user is zoomed in.
     */
    _updateSideArrowScale() {
        if (!this._netHiddenMarkers || !this._netHiddenMarkers.length) return;
        const h = (this.container && this.container.clientHeight) || 800;
        const pixelSize = this.frustumSize / h;
        const targetPx = 28;
        const worldSize = targetPx * pixelSize;
        for (const sprite of this._netHiddenMarkers) {
            sprite.scale.set(worldSize, worldSize, 1);
            if (sprite.userData) {
                sprite.userData.width = worldSize;
                sprite.userData.height = worldSize;
            }
        }
    }

    /**
     * Paint a small clickable down-chevron at (x, y) marking a fly-line
     * endpoint that physically lives on the hidden face. Click flips
     * the viewer to that face. Tracked in `_netHiddenMarkers` so
     * `clearNetHighlight` removes them along with the lines.
     */
    _addSideFlipArrow(x, y, targetSide) {
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const canvas = document.createElement('canvas');
        canvas.width = 64 * dpr;
        canvas.height = 64 * dpr;
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        // Glass-bg disc so the chevron reads on busy areas.
        ctx.fillStyle = 'rgba(15, 22, 35, 0.85)';
        ctx.beginPath();
        ctx.arc(32, 32, 26, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = 'rgba(95, 199, 255, 0.95)';
        ctx.lineWidth = 2.5;
        ctx.stroke();
        // Down chevron.
        const chevColor = '#5fc7ff';
        ctx.strokeStyle = chevColor;
        ctx.lineWidth = 4;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.beginPath();
        ctx.moveTo(20, 26);
        ctx.lineTo(32, 40);
        ctx.lineTo(44, 26);
        ctx.stroke();

        const texture = new THREE.CanvasTexture(canvas);
        texture.minFilter = THREE.LinearFilter;
        texture.magFilter = THREE.LinearFilter;
        const material = new THREE.SpriteMaterial({
            map: texture,
            transparent: true,
            depthTest: false,
        });
        const sprite = new THREE.Sprite(material);
        // Sit above the fly-line z so the disc is opaque over the
        // dashed cyan; world size in mm is rescaled with zoom by
        // `_updateSideArrowScale` (kept ~22 px tall).
        sprite.position.set(x, y, 7);
        const worldSize = this.frustumSize ? this.frustumSize * 0.04 : 2.5;
        sprite.scale.set(worldSize, worldSize, 1);
        sprite.userData = {
            _instanceType: 'sideArrow',
            _targetSide: targetSide,
            x, y,
            width: worldSize,
            height: worldSize,
            type: 'SIDE_FLIP',
            id: '',
            net: '',
            // _side null so the side filter doesn't hide the arrow.
            _side: null,
            _mesh: sprite,
        };
        this._hoverableItems.push(sprite.userData);
        this._netHiddenMarkers.push(sprite);
        this.scene.add(sprite);
        // The spatial grid was built once at load; without inserting
        // the arrow's proxy into it, `_getNearbyItems` returns no
        // candidates around the cursor and the click never registers.
        this._addToSpatialGrid(sprite.userData);
    }

    _addToSpatialGrid(item) {
        if (!this._spatialGrid) return;
        const key = this._getGridKey(item.x, item.y);
        if (!this._spatialGrid[key]) this._spatialGrid[key] = [];
        this._spatialGrid[key].push(item);
    }

    /**
     * Resolve a pin / test-pad's fly-line endpoint XYZ given the current
     * side filter. When the pin lives on the hidden face (e.g. selected
     * pin is TOP-side, sideMode='top', target pin is BOTTOM-side), the
     * pin's display XY is in the empty gap region next to the visible
     * face — not where the tech expects the connection to go. Project
     * it onto the visible face's frame (subtract the inter-face delta
     * along the dual-outline axis) and drop z below the board so the
     * line dives under the substrate, signalling "this net continues
     * on the other side".
     */
    _netEndpointPos(item, defaultZ) {
        const visibleZ = defaultZ;
        const hiddenZ = -2;  // behind the board outline at z=0.5
        if (this.sideMode === 'both' || !item._side) {
            return { x: item.x, y: item.y, z: visibleZ };
        }
        if (item._side === this.sideMode) {
            return { x: item.x, y: item.y, z: visibleZ };
        }
        // Item on the hidden face. Without a dualOutline layout
        // (single-board formats: .fz / .cad GenCAD / KiCad / BRD), the
        // pin's display X/Y already matches its physical position on
        // the board — same XY for top-side and bottom-side. Drop Z
        // below the substrate so the fly-line "dives under" and the
        // side-flip chevron lands at the visible spot, letting the
        // tech click to flip to the actual face the pad sits on.
        if (!this.dualOutline) {
            return { x: item.x, y: item.y, z: hiddenZ };
        }
        // Pin is on the hidden face. Map its display position onto the
        // visible face's coordinate frame following the FlexBV /
        // OpenBoardView convention: for a side-by-side X split the
        // bottom view is the result of flipping the physical board
        // around its vertical edge, so the X axis is MIRRORED between
        // faces (left of top = right of bottom, same physical pin).
        // For a stacked Y split, mirror Y instead.
        //
        // The mirror formula `T.x + B.x + B.w - pin.x` is symmetric:
        // it works equally well for top→bottom and bottom→top because
        // a mirror is its own inverse. Y (the non-split axis under X
        // split) just translates by the inter-face origin delta — and
        // that delta flips sign depending on which face is the target.
        const dual = this.dualOutline;
        const top = dual.bbox_top;
        const bot = dual.bbox_bottom;
        let tx, ty;
        if (dual.axis === 'x') {
            tx = top.x + bot.x + bot.w - item.x;
            const tyOffset = this.sideMode === 'top'
                ? (top.y - bot.y)
                : (bot.y - top.y);
            ty = item.y + tyOffset;
        } else {
            ty = top.y + bot.y + bot.h - item.y;
            const txOffset = this.sideMode === 'top'
                ? (top.x - bot.x)
                : (bot.x - top.x);
            tx = item.x + txOffset;
        }
        return { x: tx, y: ty, z: hiddenZ };
    }

    clearNetHighlight() {
        if (this.highlightedItems && this.highlightedItems.length > 0) {
            // Drop the per-net resting override BEFORE restoring colour
            // so `_setItemHighlight(item, false)` snaps to originalColor
            // instead of looping back to the just-cleared net colour.
            this.highlightedItems.forEach(item => {
                delete item._restColor;
                this._setItemHighlight(item, false);
            });
            this.highlightedItems = [];
        }
        // Restore the dimmed pins back to their original colour.
        this._undimAllPins();
        // And restore the copper traces back to their parse-time
        // colour + opacity so the next net selection starts clean.
        this._undimAllTraces();

        if (this.netLines && this.netLines.length > 0) {
            this.netLines.forEach(line => this.scene.remove(line));
            this.netLines = [];
        }

        if (this._netHiddenMarkers && this._netHiddenMarkers.length > 0) {
            // Drop the side-flip arrows + their hoverable proxies so a
            // fresh selection doesn't accumulate stale chevrons or keep
            // the click target after the underlying line is gone.
            const markerProxies = new Set(
                this._netHiddenMarkers.map((s) => s.userData)
            );
            this._netHiddenMarkers.forEach((s) => this.scene.remove(s));
            this._netHiddenMarkers = [];
            this._hoverableItems = this._hoverableItems.filter(
                (it) => !markerProxies.has(it)
            );
            // Strip the same proxies out of every spatial grid cell
            // that referenced them — otherwise stale entries keep
            // matching `_getNearbyItems` after the chevron is gone.
            if (this._spatialGrid) {
                for (const key of Object.keys(this._spatialGrid)) {
                    this._spatialGrid[key] = this._spatialGrid[key].filter(
                        (it) => !markerProxies.has(it)
                    );
                }
            }
        }

        this.requestRender();
    }

    // ========================
    // BOARD LOADING - OPTIMIZED
    // ========================

    loadBoard(data) {
        console.time('[PERF] loadBoard');

        this.boardData = data;
        this.clearScene();

        if (data.error) {
            console.error('Load error:', data.message);
            alert('Error: ' + data.message);
            return;
        }

        this.offsetX = data.board_offset_x || 0;
        this.offsetY = data.board_offset_y || 0;

        // XZZ-style dual layout (top + bottom views in the same coord
        // space). When present, the backend has already tagged every
        // entity with `_side` and the side toggle defaults to 'both'.
        this.dualOutline = data.dual_outline || null;
        this.sideMode = 'both';

        // Net diagnostic expectations (XZZ post-v6 block on iPad/iPhone
        // manufacturer dumps). Indexed by net name for O(1) lookup at
        // selection time. Empty Map on boards without diagnostic data
        // — the inspector keeps the rows hidden in that case.
        this._netDiagnostics = new Map();
        for (const d of (data.net_diagnostics || [])) {
            this._netDiagnostics.set(d.name, d);
        }

        // 1. Create board outline
        this.createBoard(data);

        // 2. Create components (individual meshes - usually < 500)
        console.time('[PERF] components');
        // Build a refdes -> component lookup once so the info panel can
        // resolve `dnp_alternates` refdes back to their footprint /
        // value without re-scanning the array on every selection.
        this._compIndex = new Map();
        if (data.components) {
            for (const c of data.components) this._compIndex.set(c.id, c);
            data.components.forEach(c => this.createComponent(c));
        }
        console.timeEnd('[PERF] components');

        // 3. Create pins using InstancedMesh (MAJOR optimization)
        console.time('[PERF] pins-instanced');
        if (data.pins) {
            this._createPinsInstanced(data.pins);
        }
        console.timeEnd('[PERF] pins-instanced');

        // 4. Create vias using InstancedMesh
        console.time('[PERF] vias-instanced');
        if (data.vias && data.vias.length > 0) {
            this._createViasInstanced(data.vias);
        }
        console.timeEnd('[PERF] vias-instanced');

        // 5. Create test pads using InstancedMesh
        console.time('[PERF] testpads-instanced');
        if (data.test_pads) {
            this._createTestPadsInstanced(data.test_pads);
        }
        console.timeEnd('[PERF] testpads-instanced');

        // 6. Create traces (lines)
        if (data.traces) {
            data.traces.forEach(t => this.createTrace(t));
        }

        // 6b. Manufacturer inspection markers (XZZ type_03 overlays).
        // Empty on most boards — iPad Air 3 ships 15 such rectangles
        // that the OEM tagged as zones of interest for diagnosis.
        // Render as semi-transparent amber-stroked rectangles above
        // the board layer, below the pins, no fill.
        if (data.markers && data.markers.length > 0) {
            this._createInspectionMarkers(data.markers);
        }

        // 6c. Mechanical holes — `$MECH HOLE` from GenCAD `.cad` files.
        // Fixation holes (4 PCB-corner screws) render as a steel-grey
        // ring around a hollow centre at full radius; centre fiducials
        // render as a small filled gold dot. Always visible, can't be
        // toggled — they're board structure, not signal data.
        if (data.mech_holes && data.mech_holes.length > 0) {
            this._createMechHoles(data.mech_holes);
        }

        // 7. Build spatial grid for O(1) hover
        console.time('[PERF] spatial-grid');
        this._buildSpatialGrid();
        console.timeEnd('[PERF] spatial-grid');

        // Center camera
        const centerX = this.offsetX + data.board_width / 2;
        const centerY = this.offsetY + data.board_height / 2;
        this.camera.position.x = centerX;
        this.camera.position.y = centerY;
        this.frustumSize = Math.max(data.board_width, data.board_height) * 1.2;
        this.zoom = 100 / this.frustumSize;
        this.onResize();
        // Restore the technician's preferred side mode. Falls back to
        // 'both' on first run or when localStorage is unavailable.
        let savedMode = 'both';
        try {
            const v = localStorage.getItem('pcb.sideMode');
            if (v === 'top' || v === 'bottom' || v === 'both') savedMode = v;
        } catch (_) {}
        this.setSideMode(savedMode);

        // Update stats
        document.getElementById('stats-components').textContent = data.components_count || 0;
        document.getElementById('stats-pins').textContent = data.pins_count || 0;
        document.getElementById('stats-nets').textContent = data.nets_count || 0;
        document.getElementById('zoom-level').textContent = Math.round(this.zoom * 100);
        document.getElementById('no-file-message').classList.add('hidden');

        // Handle format-specific UI
        const formatType = (data.format_type || '').toLowerCase();
        const dualBtn = document.getElementById('dual-view');
        const flipBtn = document.getElementById('flip-board');

        if (formatType === 'brd') {
            if (dualBtn) dualBtn.classList.remove('hidden');
            if (flipBtn) flipBtn.classList.remove('hidden');
            if (!this.isDualView) {
                this.isDualView = true;
                if (dualBtn) {
                    dualBtn.classList.remove('bg-dark-600', 'text-gray-400', 'border-dark-500');
                    dualBtn.classList.add('bg-green-500/20', 'text-green-400', 'border-green-500/30');
                }
                this.createDualViewGroup();
                this.centerDualView();
            }
        } else if (formatType === 'pcb' || formatType === 'xzz') {
            if (dualBtn) dualBtn.classList.add('hidden');
            if (flipBtn) flipBtn.classList.add('hidden');
            if (this.isDualView) {
                this.isDualView = false;
                this.removeDualViewGroup();
            }
        } else {
            if (dualBtn) dualBtn.classList.remove('hidden');
            if (flipBtn) flipBtn.classList.remove('hidden');
            if (this.isDualView) {
                this.createDualViewGroup();
                this.centerDualView();
            }
        }

        this.requestRender();
        console.timeEnd('[PERF] loadBoard');

        const totalObjects = (data.pins?.length || 0) + (data.vias?.length || 0) + (data.test_pads?.length || 0);
        console.log(`[PERF] Loaded ${totalObjects} objects with InstancedMesh optimization`);
    }

    /**
     * Create all pins using InstancedMesh - single draw call for all circular pins
     */
    _createPinsInstanced(pins) {
        // Single pipeline for placed AND DFM-alternate (DNP) pins.
        // Each pin carries an `is_dnp` flag in the payload; the
        // builders below stash it per-instance in `_dnpFlags` so
        // `setShowDnp` flips the matrices via `_applySideToInstanced`
        // (same zero-scale mechanism as the side filter), and every
        // standard pipeline (hover, colour by net, info panel, net
        // highlight) handles DNP pins as ordinary pins automatically.
        const circularPins = [];
        const rectPinsBySize = new Map();
        const kindMap = { square: 's', oval: 'o', rect: 'r' };

        pins.forEach((pin, index) => {
            if (pin.shape === 'circle') {
                circularPins.push({ ...pin, _originalIndex: index });
            } else {
                const kind = kindMap[pin.shape] || 'r';
                const sizeKey = `${kind}_${Math.round(pin.width * 1000)}_${Math.round(pin.height * 1000)}`;
                if (!rectPinsBySize.has(sizeKey)) rectPinsBySize.set(sizeKey, []);
                rectPinsBySize.get(sizeKey).push({ ...pin, _originalIndex: index });
            }
        });

        if (circularPins.length > 0) this._createCircularPinsInstanced(circularPins);
        rectPinsBySize.forEach((rectPins, sizeKey) => {
            this._createRectPinsInstanced(rectPins, sizeKey);
        });
    }

    // _createDnpCircularPinsMesh / _createDnpRectPinsMesh are kept for
    // back-compat against any external caller, but the main pipeline
    // now bakes DNP pins straight into `_circularPinInstance` /
    // `_rectPinInstances` with a `_dnpFlags` mask, so these helpers
    // are no-ops in the standard load path.
    _createDnpCircularPinsMesh(pins) {
        const count = pins.length;
        const geometry = this._sharedGeometries.circlePin;
        const material = new THREE.MeshBasicMaterial({
            color: 0x6b7280,
            transparent: true,
            opacity: 0.4,
            depthWrite: false,
        });
        const mesh = new THREE.InstancedMesh(geometry, material, count);
        mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        const matrix = new THREE.Matrix4();
        pins.forEach((pin, i) => {
            const r = pin.width / 2;
            matrix.makeScale(r, r, 1);
            const z = pin.layer === 'top' ? 2 : (pin.layer === 'bottom' ? 1 : 1.5);
            matrix.setPosition(pin.x, pin.y, z);
            if (pin.rotation) {
                const rot = new THREE.Matrix4().makeRotationZ(pin.rotation * Math.PI / 180);
                matrix.multiply(rot);
            }
            mesh.setMatrixAt(i, matrix);
            // Register hoverable so click on a DNP pin opens the info
            // panel just like a placed pin would. The hover code reads
            // `_instanceType` to dispatch to the right mesh; we use a
            // dedicated 'dnpPin' kind so hover-colour ops don't try to
            // recolour the DNP mesh's instances (we want them to stay
            // uniform grey).
            this._hoverableItems.push({
                ...pin,
                _instanceType: 'dnpPin',
                _instanceId: i,
                _side: pin._side || pin.layer || null,
                originalColor: 0x6b7280,
                type: 'Pin',
            });
        });
        mesh.instanceMatrix.needsUpdate = true;
        mesh.visible = !!this._showDnp;
        mesh.userData._dnpLayer = true;
        this.scene.add(mesh);
        this._dnpMeshes.push(mesh);
    }

    /**
     * Standalone InstancedMesh for DFM-alternate rectangular pads.
     * Same shape geometry as the placed rect-pin builder (rounded
     * corners with the absolute-cap to keep big pads visibly square),
     * but uniform muted grey + low opacity + no per-pin border line.
     */
    _createDnpRectPinsMesh(pins, sizeKey) {
        const count = pins.length;
        const w = pins[0].width;
        const h = pins[0].height;
        const kind = sizeKey ? sizeKey.charAt(0) : 'r';
        let radiusFactor = 0.15;
        let radiusCapMm = 0.12;
        if (kind === 's') { radiusFactor = 0; radiusCapMm = 0; }
        else if (kind === 'o') { radiusFactor = 0.5; radiusCapMm = Infinity; }
        const shape = new THREE.Shape();
        const r = Math.min(Math.min(w, h) * radiusFactor, radiusCapMm);
        this._createRoundedRectShape(shape, w, h, r);
        const geometry = new THREE.ShapeGeometry(shape);

        const material = new THREE.MeshBasicMaterial({
            color: 0x6b7280,
            transparent: true,
            opacity: 0.4,
            depthWrite: false,
        });
        const mesh = new THREE.InstancedMesh(geometry, material, count);
        mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        const matrix = new THREE.Matrix4();
        pins.forEach((pin, i) => {
            matrix.identity();
            const z = pin.layer === 'top' ? 2 : (pin.layer === 'bottom' ? 1 : 1.5);
            matrix.setPosition(pin.x, pin.y, z);
            if (pin.rotation) {
                const rot = new THREE.Matrix4().makeRotationZ(pin.rotation * Math.PI / 180);
                matrix.multiply(rot);
            }
            mesh.setMatrixAt(i, matrix);
            this._hoverableItems.push({
                ...pin,
                _instanceType: 'dnpPin',
                _instanceId: i,
                _side: pin._side || pin.layer || null,
                originalColor: 0x6b7280,
                type: 'Pin',
            });
        });
        mesh.instanceMatrix.needsUpdate = true;
        mesh.visible = !!this._showDnp;
        mesh.userData._dnpLayer = true;
        this.scene.add(mesh);
        this._dnpMeshes.push(mesh);
    }

    _createCircularPinsInstanced(pins) {
        const count = pins.length;
        const geometry = this._sharedGeometries.circlePin;
        const material = this._sharedMaterials.pinFill.clone();

        // Create instanced mesh
        const instancedMesh = new THREE.InstancedMesh(geometry, material, count);
        instancedMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

        // Enable per-instance colors
        instancedMesh.instanceColor = new THREE.InstancedBufferAttribute(
            new Float32Array(count * 3), 3
        );

        // Per-instance state for the side filter: keep the matrix that
        // was set at build time so we can swap between it and a zero
        // matrix when the user toggles top/bottom/both. `_dnpFlags`
        // tracks DFM-alternate (DNP) instances so the same zero-scale
        // mechanism gates them when the DNP toggle is off.
        instancedMesh.userData._matrices = new Array(count);
        instancedMesh.userData._sides = new Array(count);
        instancedMesh.userData._dnpFlags = new Array(count);
        // Parent-component refdes per slot — drives the bv_filter_by_type
        // refdes-prefix filter. `_applySideToInstanced` ANDs this in
        // alongside the side + DNP gates.
        instancedMesh.userData._components = new Array(count);

        const matrix = new THREE.Matrix4();
        const color = new THREE.Color();

        pins.forEach((pin, i) => {
            const radius = pin.width / 2;
            const cat = (pin.is_gnd || pin.net === 'GND') ? 'ground' : this._netCategory(pin.net);
            const pinColor = this._resolvePinColor(pin, cat);

            // Set position and scale. DNP instances start zero-scaled
            // until the DNP toggle is enabled.
            const isDnp = !!pin.is_dnp;
            if (isDnp && !this._showDnp) {
                matrix.identity();
                matrix.makeScale(0, 0, 0);
            } else {
                matrix.makeScale(radius, radius, 1);
                const zPos = pin.layer === 'top' ? 2 : (pin.layer === 'bottom' ? 1 : 1.5);
                matrix.setPosition(pin.x, pin.y, zPos);
                if (pin.rotation) {
                    const rotMatrix = new THREE.Matrix4().makeRotationZ(pin.rotation * Math.PI / 180);
                    matrix.multiply(rotMatrix);
                }
            }

            instancedMesh.setMatrixAt(i, matrix);

            // Store the *full-scale* matrix in `_matrices` regardless
            // so the side+DNP refresh routine restores the pin to its
            // real position when both filters allow it.
            const fullMatrix = new THREE.Matrix4().makeScale(radius, radius, 1);
            const zPos = pin.layer === 'top' ? 2 : (pin.layer === 'bottom' ? 1 : 1.5);
            fullMatrix.setPosition(pin.x, pin.y, zPos);
            if (pin.rotation) {
                fullMatrix.multiply(new THREE.Matrix4().makeRotationZ(pin.rotation * Math.PI / 180));
            }
            instancedMesh.userData._matrices[i] = fullMatrix;
            instancedMesh.userData._sides[i] = pin._side || null;
            instancedMesh.userData._dnpFlags[i] = isDnp;
            instancedMesh.userData._components[i] = pin.component || null;

            // Set color
            color.setHex(pinColor);
            instancedMesh.setColorAt(i, color);

            // Store data for hover/selection
            const itemData = {
                ...pin,
                _instanceType: 'pin',
                _instanceId: i,
                _side: pin._side || null,
                originalColor: pinColor,
                type: 'Pin'
            };
            this._pinInstanceData.push(itemData);
            this._hoverableItems.push(itemData);
        });

        instancedMesh.instanceMatrix.needsUpdate = true;
        instancedMesh.instanceColor.needsUpdate = true;

        this._circularPinInstance = instancedMesh;
        this.scene.add(instancedMesh);

        // Per-pin thin border ring drawn just above the fill in a
        // mid-grey that contrasts both the navy background and most
        // pad colours — so each pad reads as a defined object even on
        // densely-packed connectors / ICs (matches FlexBV's per-pad
        // outline).
        const ringGeom = this._sharedGeometries.circlePinRing;
        const ringMat = new THREE.MeshBasicMaterial({
            color: 0x4b5563,  // tailwind gray-600 — readable on both pad fill and bg-deep
            transparent: true,
            opacity: 0.95,
            side: THREE.DoubleSide,
            depthTest: false,
        });
        const ringMesh = new THREE.InstancedMesh(ringGeom, ringMat, count);
        ringMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        ringMesh.userData._matrices = new Array(count);
        ringMesh.userData._sides = new Array(count);
        ringMesh.userData._dnpFlags = new Array(count);
        ringMesh.userData._components = new Array(count);
        pins.forEach((pin, i) => {
            const radius = pin.width / 2;
            const isDnp = !!pin.is_dnp;
            const fullMatrix = new THREE.Matrix4().makeScale(radius, radius, 1);
            const zPos = pin.layer === 'top' ? 2.05 : (pin.layer === 'bottom' ? 1.05 : 1.55);
            fullMatrix.setPosition(pin.x, pin.y, zPos);
            if (pin.rotation) {
                fullMatrix.multiply(new THREE.Matrix4().makeRotationZ(pin.rotation * Math.PI / 180));
            }
            // Initial render-time matrix: zero-scale DNP rings until
            // the toggle is on, full-scale otherwise.
            const visible = !isDnp || this._showDnp;
            ringMesh.setMatrixAt(i, visible
                ? fullMatrix
                : new THREE.Matrix4().makeScale(0, 0, 0));
            ringMesh.userData._matrices[i] = fullMatrix;
            ringMesh.userData._sides[i] = pin._side || null;
            ringMesh.userData._dnpFlags[i] = isDnp;
            ringMesh.userData._components[i] = pin.component || null;
        });
        ringMesh.instanceMatrix.needsUpdate = true;
        this._circularPinBorderInstance = ringMesh;
        this.scene.add(ringMesh);

        console.log(`[PERF] Created ${count} circular pins + borders with 2 draw calls`);
    }

    _createRectPinsInstanced(pins, sizeKey) {
        const count = pins.length;
        const [kind, widthStr, heightStr] = sizeKey.split('_');
        // sizeKey buckets in micrometres (see _createPinsInstanced).
        const w = parseFloat(widthStr) / 1000;
        const h = parseFloat(heightStr) / 1000;

        // Square = sharp corners (board-to-board connector lands).
        // Rect   = soft 15% rounded corners (standard SMD pads), but
        //          capped to a small absolute value so large pads
        //          (MOSFET DRAIN, electrolytic SMD lands at 4-6 mm)
        //          stay visibly rectangular instead of shading into a
        //          disk. Without the cap, 15% of a 4.5 mm pad gives a
        //          0.7 mm chamfer that reads as a round corner under
        //          antialias.
        // Oval   = full half-axis radius (pill / capsule for oblong
        //          test pad landings).
        let radiusFactor = 0.15;
        let radiusCapMm = 0.12;     // ~5 mil — chamfer barely visible on big pads
        if (kind === 's') {
            radiusFactor = 0;
            radiusCapMm = 0;
        } else if (kind === 'o') {
            radiusFactor = 0.5;
            radiusCapMm = Infinity; // pill caps want full half-axis
        }
        const shape = new THREE.Shape();
        const r = Math.min(Math.min(w, h) * radiusFactor, radiusCapMm);
        this._createRoundedRectShape(shape, w, h, r);
        const geometry = new THREE.ShapeGeometry(shape);

        const material = this._sharedMaterials.pinFill.clone();
        const instancedMesh = new THREE.InstancedMesh(geometry, material, count);
        instancedMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

        instancedMesh.instanceColor = new THREE.InstancedBufferAttribute(
            new Float32Array(count * 3), 3
        );
        instancedMesh.userData._matrices = new Array(count);
        instancedMesh.userData._sides = new Array(count);
        instancedMesh.userData._dnpFlags = new Array(count);
        instancedMesh.userData._components = new Array(count);

        const matrix = new THREE.Matrix4();
        const color = new THREE.Color();

        pins.forEach((pin, i) => {
            const cat = (pin.is_gnd || pin.net === 'GND') ? 'ground' : this._netCategory(pin.net);
            const pinColor = this._resolvePinColor(pin, cat);
            const isDnp = !!pin.is_dnp;

            const fullMatrix = new THREE.Matrix4();
            fullMatrix.identity();
            const zPos = pin.layer === 'top' ? 2 : (pin.layer === 'bottom' ? 1 : 1.5);
            fullMatrix.setPosition(pin.x, pin.y, zPos);
            if (pin.rotation) {
                fullMatrix.multiply(new THREE.Matrix4().makeRotationZ(pin.rotation * Math.PI / 180));
            }

            const visible = !isDnp || this._showDnp;
            instancedMesh.setMatrixAt(i, visible
                ? fullMatrix
                : new THREE.Matrix4().makeScale(0, 0, 0));
            instancedMesh.userData._matrices[i] = fullMatrix;
            instancedMesh.userData._sides[i] = pin._side || null;
            instancedMesh.userData._dnpFlags[i] = isDnp;
            instancedMesh.userData._components[i] = pin.component || null;
            color.setHex(pinColor);
            instancedMesh.setColorAt(i, color);

            const itemData = {
                ...pin,
                _instanceType: 'rectPin',
                _instanceId: i,
                _sizeKey: sizeKey,
                _side: pin._side || null,
                originalColor: pinColor,
                type: 'Pin'
            };
            this._hoverableItems.push(itemData);
        });

        instancedMesh.instanceMatrix.needsUpdate = true;
        instancedMesh.instanceColor.needsUpdate = true;

        // Per-pin thin border drawn just above the fill in a dark
        // tone, so each rect/square/oval pad reads as a defined object
        // (matches FlexBV's per-pad outline). Build a closed Line from
        // the same Shape used for the fill — Three.js sees it as a
        // separate buffer geometry, so we instance it the same way.
        const borderPts = shape.getPoints(48);
        if (borderPts.length) {
            // Close the loop
            borderPts.push(borderPts[0].clone());
            const borderGeom = new THREE.BufferGeometry().setFromPoints(
                borderPts.map(p => new THREE.Vector3(p.x, p.y, 0))
            );
            const borderMat = new THREE.LineBasicMaterial({
                color: 0x4b5563,  // grey contrasting both pad fill and bg-deep
                transparent: true,
                opacity: 0.95,
                depthTest: false,
            });
            // No InstancedMesh equivalent for THREE.Line — clone per pin
            // (count is small enough; rect pins are O(thousands) max).
            pins.forEach((pin) => {
                const line = new THREE.Line(borderGeom, borderMat);
                const zPos = (pin.layer === 'top' ? 2.05 : (pin.layer === 'bottom' ? 1.05 : 1.55));
                line.position.set(pin.x, pin.y, zPos);
                if (pin.rotation) line.rotation.z = pin.rotation * Math.PI / 180;
                line.userData._side = pin._side || null;
                line.userData._isDnp = !!pin.is_dnp;
                line.userData._component = pin.component || null;
                if (pin.is_dnp) line.visible = !!this._showDnp;
                this.scene.add(line);
                this._pinBorderLines.push(line);
            });
        }

        this._rectPinInstances.set(sizeKey, { body: instancedMesh, geometry });
        this.scene.add(instancedMesh);
    }

    _createRoundedRectShape(shape, w, h, r) {
        const hw = w / 2, hh = h / 2;
        r = Math.min(r, hw, hh);
        shape.moveTo(-hw + r, -hh);
        shape.lineTo(hw - r, -hh);
        shape.quadraticCurveTo(hw, -hh, hw, -hh + r);
        shape.lineTo(hw, hh - r);
        shape.quadraticCurveTo(hw, hh, hw - r, hh);
        shape.lineTo(-hw + r, hh);
        shape.quadraticCurveTo(-hw, hh, -hw, hh - r);
        shape.lineTo(-hw, -hh + r);
        shape.quadraticCurveTo(-hw, -hh, -hw + r, -hh);
    }

    /**
     * Ghost-rendering of a DFM-alternate (DNP) component. Pre-built at
     * load time but `visible=false` until the toolbar toggle flips
     * `_showDnp`. We draw three things so the tech reads it as a real
     * (just-not-stuffed) component:
     *   1. Semi-transparent grey body fill — same shape as the placed
     *      sibling but quarter opacity, so the alternate's footprint
     *      bbox is visually obvious.
     *   2. Dashed cyan outline — convention for "alternate / DNP" in
     *      every PCB CAD tool.
     *   3. Faded refdes label sprite at the centre.
     * No pads — the populated sibling already owns the physical pads
     * at the same physical seat; rendering them again would just
     * stack circles on top of the placed pads.
     */
    _createDnpOutline(comp) {
        if (!this._dnpMeshes) this._dnpMeshes = [];
        const w = comp.width, h = comp.height;
        const hw = w / 2, hh = h / 2;
        const cx = comp.x + hw, cy = comp.y + hh;
        const z = comp.layer === 'top' ? 1.05 : (comp.layer === 'bottom' ? 0.55 : 0.8);

        // Body fill — semi-transparent grey-blue rectangle. Same colour
        // family as the regular component fill (#2a3140) but opacity
        // 0.22 so the placed sibling beneath stays the visually
        // dominant element.
        const fillGeom = new THREE.PlaneGeometry(w, h);
        const fillMat = new THREE.MeshBasicMaterial({
            color: 0x2a3140,
            transparent: true,
            opacity: 0.22,
            depthWrite: false,
        });
        const fill = new THREE.Mesh(fillGeom, fillMat);
        fill.position.set(cx, cy, z - 0.02);
        fill.visible = !!this._showDnp;
        this.scene.add(fill);
        this._dnpMeshes.push(fill);

        // Dashed outline — solid LineSegments built from explicit pairs
        // (avoids the per-mesh computeLineDistances() cost; one shared
        // LineDashedMaterial across all DNPs and we still get the
        // dashed look thanks to the manual pair geometry).
        const dashLen = Math.max(0.15, Math.min(w, h) * 0.06);
        const gapLen = dashLen * 0.7;
        const points = [];
        const sides = [
            [-hw, -hh, hw, -hh],   // bottom
            [hw, -hh, hw, hh],     // right
            [hw, hh, -hw, hh],     // top
            [-hw, hh, -hw, -hh],   // left
        ];
        for (const [x1, y1, x2, y2] of sides) {
            const len = Math.hypot(x2 - x1, y2 - y1);
            const ux = (x2 - x1) / len, uy = (y2 - y1) / len;
            let t = 0;
            while (t < len) {
                const a = t;
                const b = Math.min(t + dashLen, len);
                points.push(new THREE.Vector3(x1 + ux * a, y1 + uy * a, 0));
                points.push(new THREE.Vector3(x1 + ux * b, y1 + uy * b, 0));
                t = b + gapLen;
            }
        }
        const dashGeom = new THREE.BufferGeometry().setFromPoints(points);
        const dashMat = new THREE.LineBasicMaterial({
            color: this.colors.componentOutline || 0x80c0d0,
            transparent: true,
            opacity: 0.85,
        });
        const dashed = new THREE.LineSegments(dashGeom, dashMat);
        dashed.position.set(cx, cy, z);
        dashed.visible = !!this._showDnp;
        dashed.userData = {
            _instanceType: 'dnpOutline',
            _refdes: comp.id,
            _comp: comp,
        };
        this.scene.add(dashed);
        this._dnpMeshes.push(dashed);

        // Hover-target — same shape as a placed component's hoverable
        // entry (`type: 'Component'`) so the info panel pipes through
        // unchanged. `x` and `y` here are the BBOX CENTRE (matching
        // the placed-component path on line ~2660), so the cursor-
        // distance check reduces to 0 inside the rectangle.
        this._hoverableItems.push({
            ...comp,
            x: cx,
            y: cy,
            type: 'Component',
            _instanceType: 'dnpComp',
            _side: comp._side || comp.layer || null,
        });

        // Refdes label — reuses the placed-component label path. The
        // sprite is pushed into `_componentLabels` and shows/hides via
        // `_updateComponentLabelVisibility` (zoom + side filter), so
        // we mark it `_dnpLabel` and gate visibility on `_showDnp`
        // alongside the rest of the DNP overlay.
        if (typeof this._addComponentRefdesLabel === 'function') {
            const before = this._componentLabels.length;
            this._addComponentRefdesLabel(comp);
            for (let i = before; i < this._componentLabels.length; i++) {
                const sprite = this._componentLabels[i];
                sprite.userData._dnpLabel = true;
                sprite.visible = !!this._showDnp;
                if (sprite.material) sprite.material.opacity = 0.6;
                this._dnpMeshes.push(sprite);
            }
        }
    }

    /**
     * Toggle the DNP overlay layer. Pure visibility flip — no scene
     * rebuild, no payload re-fetch.
     */
    setShowDnp(show) {
        this._showDnp = !!show;
        // Body fills + dashed outlines tracked in `_dnpMeshes` —
        // simple visibility flip.
        if (this._dnpMeshes) {
            for (const m of this._dnpMeshes) m.visible = this._showDnp;
        }
        // Per-instance DNP pads live in the standard pin meshes with
        // `_dnpFlags`. `_applySideToInstanced` honours the flag, so
        // running it across all relevant meshes is enough to flip the
        // DNP pads' matrices to/from zero-scale.
        this._applySideToInstanced(this._circularPinInstance);
        this._applySideToInstanced(this._circularPinBorderInstance);
        if (this._rectPinInstances) {
            this._rectPinInstances.forEach(({ body }) => {
                this._applySideToInstanced(body);
            });
        }
        // Per-pin rect borders are individual Lines — flip their
        // visibility against the side filter and the refdes-prefix
        // filter so the three axes (side, DNP, refdes) compose cleanly.
        if (this._pinBorderLines) {
            const allow = (side) => this.sideMode === 'both'
                || side == null
                || side === this.sideMode;
            const prefix = this._agentFilterPrefix;
            const refdesOk = (id) => !prefix
                || (id && id.toUpperCase().startsWith(prefix));
            for (const m of this._pinBorderLines) {
                if (!m.userData || !m.userData._isDnp) continue;
                m.visible = this._showDnp
                    && allow(m.userData._side)
                    && refdesOk(m.userData._component);
            }
        }
        if (typeof this._updateComponentLabelVisibility === 'function') {
            this._updateComponentLabelVisibility();
        }
        if (typeof this.requestRender === 'function') this.requestRender();
    }

    /**
     * Toggle the vias layer. GenCAD boards ship thousands of vias
     * (every BGA ball + routing stitching) which can clutter the
     * canvas around dense areas. The user toggles them off when
     * diagnosing pad-level work, on when looking at routing.
     */
    setShowVias(show) {
        this._showVias = !!show;
        if (this._viasOuterInstance) this._viasOuterInstance.visible = this._showVias;
        if (this._viasInnerInstance) this._viasInnerInstance.visible = this._showVias;
    }

    _createViasInstanced(vias) {
        const count = vias.length;

        // Outer ring
        const outerGeom = this._sharedGeometries.viaOuter;
        const outerMat = this._sharedMaterials.via.clone();
        const outerMesh = new THREE.InstancedMesh(outerGeom, outerMat, count);
        outerMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        // Per-instance colour so mounting holes (no net) render in
        // gold while electrical vias keep the standard violet.
        outerMesh.instanceColor = new THREE.InstancedBufferAttribute(
            new Float32Array(count * 3), 3
        );
        outerMesh.userData._matrices = new Array(count);
        outerMesh.userData._sides = new Array(count);

        // Inner hole
        const innerGeom = this._sharedGeometries.viaInner;
        const innerMat = this._sharedMaterials.viaHole.clone();
        const innerMesh = new THREE.InstancedMesh(innerGeom, innerMat, count);
        innerMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        innerMesh.userData._matrices = new Array(count);
        innerMesh.userData._sides = new Array(count);

        const matrix = new THREE.Matrix4();
        const innerMatrix = new THREE.Matrix4();
        const tmpColor = new THREE.Color();

        vias.forEach((via, i) => {
            const radius = via.radius || 0.3;

            matrix.makeScale(radius, radius, 1);
            matrix.setPosition(via.x, via.y, 0.8);
            outerMesh.setMatrixAt(i, matrix);
            outerMesh.userData._matrices[i] = matrix.clone();
            outerMesh.userData._sides[i] = via._side || null;

            innerMatrix.makeScale(radius, radius, 1);
            innerMatrix.setPosition(via.x, via.y, 0.81);
            innerMesh.setMatrixAt(i, innerMatrix);
            innerMesh.userData._matrices[i] = innerMatrix.clone();
            innerMesh.userData._sides[i] = via._side || null;

            // A via with no net is a mechanical drill (mounting hole,
            // screw hole) — paint its outer ring in the same gold as
            // signal test pads to mirror the iPhone/iPad silkscreen
            // convention. Electrical vias inherit their net's category
            // colour so they visually integrate with the rails / signals
            // they belong to (power vias copper-orange, ground vias dark
            // grey, signal vias light grey, etc.).
            const isMounting = !via.net || via.net === '' || via.net === 'NC';
            let ringHex;
            if (isMounting) {
                ringHex = this.colors.pinTestPadSignal;  // gold
            } else {
                const cat = (via.is_gnd || via.net === 'GND')
                    ? 'ground'
                    : this._netCategory(via.net);
                ringHex = this._pinColorForCategory(cat);
            }
            tmpColor.setHex(ringHex);
            outerMesh.setColorAt(i, tmpColor);

            this._viaInstanceData.push({
                ...via,
                _instanceType: 'via',
                _instanceId: i,
                _side: via._side || null,
                originalColor: ringHex,
                type: 'VIA'
            });
        });

        outerMesh.instanceMatrix.needsUpdate = true;
        outerMesh.instanceColor.needsUpdate = true;
        innerMesh.instanceMatrix.needsUpdate = true;

        outerMesh.visible = this.showVias;
        innerMesh.visible = this.showVias;

        this._viasOuterInstance = outerMesh;
        this._viasInnerInstance = innerMesh;
        this.scene.add(outerMesh);
        this.scene.add(innerMesh);

        console.log(`[PERF] Created ${count} vias with 2 draw calls (was ${count * 2})`);
    }

    /**
     * Render `$MECH HOLE` entries from a GenCAD `.cad` file.
     *
     * - Fixation holes (corner screws, ø > 100 mils): outer steel-grey
     *   ring on top of a black drill centre. Drawn slightly above the
     *   via z-plane so they read as physical structure even when there
     *   are vias right next to them.
     * - Fiducials (centre dots, ø ≤ 100 mils): single filled gold disc
     *   to mirror the optical reference dots on the silkscreen.
     *
     * Holes are mechanical, not electrical — no net, no hover/select
     * intent at this stage. Five-or-so per board so plain `THREE.Mesh`
     * is enough; no need for InstancedMesh.
     */
    _createMechHoles(holes) {
        const fixationColor = 0xa1a1aa;  // slate-400 — steel pad finish
        const drillColor = this.colors.background;  // hollow centre
        const fiducialColor = this.colors.pinTestPadSignal;  // gold

        // Lazy-create the materials once. Re-use across renders.
        if (!this._mechHoleMaterials) {
            this._mechHoleMaterials = {
                fixationRing: new THREE.MeshBasicMaterial({
                    color: fixationColor,
                    side: THREE.DoubleSide,
                }),
                fixationDrill: new THREE.MeshBasicMaterial({
                    color: drillColor,
                    side: THREE.DoubleSide,
                }),
                fiducial: new THREE.MeshBasicMaterial({
                    color: fiducialColor,
                    side: THREE.DoubleSide,
                }),
            };
        }
        const M = this._mechHoleMaterials;
        const meshes = [];

        // Z-plane: above board substrate (0.5), below pins (1.0).
        const Z_RING = 0.85;
        const Z_DRILL = 0.86;

        holes.forEach((h) => {
            const r = (h.diameter || 0.5) / 2;
            if (h.is_fiducial) {
                const geom = new THREE.CircleGeometry(r, 32);
                const mesh = new THREE.Mesh(geom, M.fiducial);
                mesh.position.set(h.x, h.y, Z_RING);
                this.scene.add(mesh);
                meshes.push(mesh);
            } else {
                // Outer ring: an annulus from r*0.55 to r so the drill
                // hole reads. Inner drill on top in board background
                // colour for the "see-through" look.
                const ringGeom = new THREE.RingGeometry(r * 0.55, r, 32);
                const drillGeom = new THREE.CircleGeometry(r * 0.55, 32);
                const ringMesh = new THREE.Mesh(ringGeom, M.fixationRing);
                const drillMesh = new THREE.Mesh(drillGeom, M.fixationDrill);
                ringMesh.position.set(h.x, h.y, Z_RING);
                drillMesh.position.set(h.x, h.y, Z_DRILL);
                this.scene.add(ringMesh);
                this.scene.add(drillMesh);
                meshes.push(ringMesh, drillMesh);
            }
        });

        this._mechHoleMeshes = meshes;
        console.log(`[PERF] Created ${holes.length} mechanical holes (${meshes.length} meshes)`);
    }

    _createTestPadsInstanced(testPads) {
        const count = testPads.length;

        const geom = this._sharedGeometries.testPad;
        const mat = this._sharedMaterials.testPad.clone();
        const instancedMesh = new THREE.InstancedMesh(geom, mat, count);
        instancedMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

        // Enable per-instance colors for highlight support
        instancedMesh.instanceColor = new THREE.InstancedBufferAttribute(
            new Float32Array(count * 3), 3
        );
        instancedMesh.userData._matrices = new Array(count);
        instancedMesh.userData._sides = new Array(count);
        instancedMesh.userData._components = new Array(count);

        const matrix = new THREE.Matrix4();
        const color = new THREE.Color(this.colors.testPad);

        testPads.forEach((tp, i) => {
            const radius = tp.radius || 0.5;
            const zPos = tp.layer === 'top' ? 2.5 : (tp.layer === 'bottom' ? 2 : 2.25);

            matrix.makeScale(radius, radius, 1);
            matrix.setPosition(tp.x, tp.y, zPos);
            instancedMesh.setMatrixAt(i, matrix);
            instancedMesh.userData._matrices[i] = matrix.clone();
            instancedMesh.userData._sides[i] = tp._side || null;
            instancedMesh.userData._components[i] = tp.component || tp.id || null;
            instancedMesh.setColorAt(i, color);

            const itemData = {
                ...tp,
                _instanceType: 'testPad',
                _instanceId: i,
                _side: tp._side || null,
                originalColor: this.colors.testPad,
                type: 'TEST_PAD'
            };
            this._testPadInstanceData.push(itemData);
            this._hoverableItems.push(itemData);
        });

        instancedMesh.instanceMatrix.needsUpdate = true;
        instancedMesh.instanceColor.needsUpdate = true;

        this._testPadsInstance = instancedMesh;
        this.scene.add(instancedMesh);

        console.log(`[PERF] Created ${count} test pads with 1 draw call`);
    }

    /**
     * Render XZZ type_03 blocks as undocumented-component overlays.
     * Each one is a real part on the physical board (verified by the
     * user against an iPad Air 3 motherboard he owns) — but the file
     * ships only its bounding box, not its pins or net assignments.
     * Apple appears to strip the pin layout the same way it strips
     * the real refdes (U1 placeholder seen on every component name).
     *
     * Render style mirrors a regular "headless" component (no
     * silkscreen body_lines) — soft grey-blue fill + cyan outline —
     * so the tech reads it as a component, not as a probe-pad
     * highlight. We keep the orange `testPad` accent off these to
     * avoid the misleading "diagnostic zone" cue we used initially.
     */
    _createInspectionMarkers(markers) {
        // Per-marker fill material so the highlight branch in
        // `_setItemHighlight` (which patches `material.color` on the
        // mesh) doesn't bleed into every other marker via a shared
        // material.
        const outlineMat = new THREE.LineBasicMaterial({
            color: this.colors.componentOutline,
            transparent: true,
            opacity: 0.7,
        });
        const baseFillHex = 0x2a3140;
        const z = 0.7;
        const zOutline = 0.72;
        markers.forEach((m, i) => {
            const xMin = m.x_min, yMin = m.y_min;
            const xMax = m.x_max, yMax = m.y_max;
            const w = xMax - xMin, h = yMax - yMin;
            if (w <= 0 || h <= 0) return;

            const cx = (xMin + xMax) / 2;
            const cy = (yMin + yMax) / 2;

            // Per-mesh material so per-instance hover highlight is
            // isolated.
            const fillMat = new THREE.MeshBasicMaterial({
                color: baseFillHex,
                transparent: true,
                opacity: 0.45,
                depthWrite: false,
            });
            const fillGeom = new THREE.PlaneGeometry(w, h);
            const fillMesh = new THREE.Mesh(fillGeom, fillMat);
            fillMesh.position.set(cx, cy, z);
            fillMesh.userData._side = m._side || null;
            fillMesh.userData.origColor = baseFillHex;
            this.scene.add(fillMesh);
            this._markerMeshes.push(fillMesh);

            const pts = [
                new THREE.Vector3(xMin, yMin, zOutline),
                new THREE.Vector3(xMax, yMin, zOutline),
                new THREE.Vector3(xMax, yMax, zOutline),
                new THREE.Vector3(xMin, yMax, zOutline),
                new THREE.Vector3(xMin, yMin, zOutline),
            ];
            const lineGeom = new THREE.BufferGeometry().setFromPoints(pts);
            const lineMesh = new THREE.Line(lineGeom, outlineMat);
            lineMesh.userData._side = m._side || null;
            this.scene.add(lineMesh);
            this._markerMeshes.push(lineMesh);

            // Wire into the existing hover/select machinery: spatial
            // grid lookup uses `x, y` (centre), `_setItemHighlight`
            // patches `_mesh.material.color`, `selectItem` reads id /
            // value / type / layer / width / height / net to populate
            // the inspector. Marker is anonymous (Apple ships the
            // bbox without pinout), so id is synthesized "IC_N" and
            // value tells the tech the data is stripped.
            const itemData = {
                id: `IC_${i + 1}`,
                x: cx,
                y: cy,
                width: w,
                height: h,
                layer: "top",
                type: "IC (pinout stripped)",
                value: "Pinout indisponible (stripé par le constructeur)",
                net: "",
                _instanceType: "marker",
                _mesh: fillMesh,
                _side: m._side || null,
                originalColor: baseFillHex,
                marker_id: m.marker_id,
            };
            this._hoverableItems.push(itemData);
        });
        console.log(`[PERF] Rendered ${markers.length} stripped-pin parts`);
    }

    createBoard(data) {
        // Source the polygons. When the backend tagged a dual-outline
        // (XZZ side-by-side / stacked layout), use the explicit per-face
        // polygons so each contour mesh carries the correct `_side` tag
        // and the side toggle can hide / show them independently.
        const tagged = [];
        if (data.dual_outline && data.dual_outline.top && data.dual_outline.bottom) {
            tagged.push({ poly: data.dual_outline.top, side: 'top' });
            tagged.push({ poly: data.dual_outline.bottom, side: 'bottom' });
        } else if (data.outline) {
            let polys = [];
            if (data.outline.polygons) {
                polys = data.outline.polygons;
            } else if (Array.isArray(data.outline) && data.outline.length >= 3) {
                polys = [data.outline];
            }
            polys.forEach(p => tagged.push({ poly: p, side: null }));
        }

        tagged.forEach(({ poly, side }) => {
            if (!poly || poly.length < 3) return;

            const cleanedOutline = [poly[0]];
            for (let i = 1; i < poly.length; i++) {
                const prev = cleanedOutline[cleanedOutline.length - 1];
                const curr = poly[i];
                if (Math.abs(curr.x - prev.x) > 0.01 || Math.abs(curr.y - prev.y) > 0.01) {
                    cleanedOutline.push(curr);
                }
            }

            if (cleanedOutline.length >= 3) {
                const outlinePoints = cleanedOutline.map(p => new THREE.Vector3(p.x, p.y, 0.5));
                outlinePoints.push(new THREE.Vector3(cleanedOutline[0].x, cleanedOutline[0].y, 0.5));

                const outlineGeom = new THREE.BufferGeometry().setFromPoints(outlinePoints);
                const outlineMat = new THREE.LineBasicMaterial({
                    color: this.colors.boardOutline,
                    transparent: true,
                    opacity: 0.95,
                });
                const outline = new THREE.Line(outlineGeom, outlineMat);
                outline.userData._side = side;
                this.scene.add(outline);
                this._outlineMeshes.push(outline);

                // Filled substrate behind the contour. Hidden when the
                // user's picked fill colour matches the canvas
                // background — that's the "no fill" state. A real
                // pick (different from bg-deep) makes the substrate
                // visible. Always created so the colour picker can
                // show / hide it live without rebuilding the board.
                try {
                    const fillShape = new THREE.Shape(
                        cleanedOutline.map(p => new THREE.Vector2(p.x, p.y))
                    );
                    const fillGeom = new THREE.ShapeGeometry(fillShape);
                    const fillMat = new THREE.MeshBasicMaterial({
                        color: this.colors.boardFill,
                        transparent: true,
                        opacity: this.colors.boardFill === this.colors.background ? 0 : 0.85,
                        depthWrite: false,
                    });
                    const fillMesh = new THREE.Mesh(fillGeom, fillMat);
                    fillMesh.position.z = 0.4;  // just below the outline line at 0.5
                    fillMesh.userData._side = side;
                    this.scene.add(fillMesh);
                    this._outlineMeshes.push(fillMesh);
                } catch (err) {
                    console.warn("[PCBViewer] board fill triangulation skipped", err);
                }
            }
        });
    }

    /**
     * Recolour the board outline (line meshes) — called from the
     * Tweaks picker. Walks every entry in `_outlineMeshes` and updates
     * the one(s) that are line geometries (Mesh fills are touched by
     * `_recolorBoardFill`).
     */
    _recolorBoardOutline(hexInt) {
        this.colors.boardOutline = hexInt;
        for (const m of this._outlineMeshes) {
            if (m.material && m.material.type === 'LineBasicMaterial') {
                m.material.color.setHex(hexInt);
            }
        }
    }

    /**
     * Recolour the board substrate fill. When the picked colour
     * matches the canvas background (the "no fill" sentinel), the
     * fill mesh is hidden via opacity:0.
     */
    _recolorBoardFill(hexInt) {
        this.colors.boardFill = hexInt;
        const isInvisible = hexInt === this.colors.background;
        for (const m of this._outlineMeshes) {
            if (m.material && m.material.type === 'MeshBasicMaterial') {
                m.material.color.setHex(hexInt);
                m.material.opacity = isInvisible ? 0 : 0.85;
                m.material.transparent = true;
                m.material.needsUpdate = true;
            }
        }
    }

    createComponent(comp) {
        // DFM-alternate / DNP — the populated sibling carries the BOM
        // value and gets drawn normally. The non-stuffed footprint
        // gets a separate dashed-outline render that's hidden by
        // default and toggled via `_showDnp`. Pads aren't drawn for
        // the DNP (the placed sibling already covers the same physical
        // pads), so the overlay is just the alternate body footprint.
        if (comp.is_dnp) {
            this._createDnpOutline(comp);
            return;
        }

        const group = new THREE.Group();
        const w = comp.width;
        const h = comp.height;
        const hw = w / 2;
        const hh = h / 2;
        const isTestPoint = comp.type === 'TEST_POINT';

        // Whether to fall back to the AABB outline rectangle. We only
        // draw the AABB when the part has NO silkscreen body_lines —
        // otherwise the silkscreen segments below already trace the
        // package outline and the AABB rectangle just stacks a second
        // outline on top of them (visible as a 'double contour' on
        // RF107 / RF770 / CF730 / etc.). XZZ populates body_lines for
        // most parts; KiCad/BRD don't.
        const hasBodyLines = Array.isArray(comp.body_lines) && comp.body_lines.length > 0;

        // Body fill — soft grey rectangle behind the pads so each
        // component reads as a defined object, mirroring FlexBV's
        // 'tinted body' look. For XZZ parts with body_lines we fill
        // the body_lines bbox in absolute coords (no group rotation,
        // since body_lines are absolute too); for KiCad/BRD parts
        // (no body_lines) we fill the AABB inside the rotated group.
        if (!isTestPoint) {
            const fillMat = new THREE.MeshBasicMaterial({
                color: 0x2a3140,            // dim grey-blue, between bg-deep and panel
                transparent: true,
                opacity: 0.55,
                depthWrite: false,
            });
            if (hasBodyLines) {
                let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
                for (const seg of comp.body_lines) {
                    if (seg.x1 < minX) minX = seg.x1;
                    if (seg.x2 < minX) minX = seg.x2;
                    if (seg.x1 > maxX) maxX = seg.x1;
                    if (seg.x2 > maxX) maxX = seg.x2;
                    if (seg.y1 < minY) minY = seg.y1;
                    if (seg.y2 < minY) minY = seg.y2;
                    if (seg.y1 > maxY) maxY = seg.y1;
                    if (seg.y2 > maxY) maxY = seg.y2;
                }
                const fw = maxX - minX, fh = maxY - minY;
                if (fw > 0 && fh > 0) {
                    const fillGeom = new THREE.PlaneGeometry(fw, fh);
                    const fillMesh = new THREE.Mesh(fillGeom, fillMat);
                    fillMesh.position.set((minX + maxX) / 2, (minY + maxY) / 2, 0.6);
                    fillMesh.userData._side = comp._side || null;
                    this.scene.add(fillMesh);
                    this._componentExtras.push(fillMesh);
                }
            } else {
                // No body_lines — fill the AABB inside the rotated
                // group so the fill rotates with the part.
                const fillGeom = new THREE.PlaneGeometry(w, h);
                const fillMesh = new THREE.Mesh(fillGeom, fillMat);
                fillMesh.position.set(0, 0, 0.01);
                // Stash own original colour so hover-out via the null-
                // sentinel branch in `setMeshColor` restores the fill
                // back to grey-blue instead of inheriting the parent
                // component's outline cyan.
                fillMesh.userData.origColor = 0x2a3140;
                group.add(fillMesh);
            }
        }

        if (isTestPoint) {
            // No special centred overlay — the per-pin instanced
            // renderer now colours TEST_POINT pins in orange directly
            // (component_type field flows through the pin payload).
        } else if (!hasBodyLines) {
            // Outline-only rectangle, no fill, no per-type shape
            // variants. Matches the SVG renderer's reference look.
            const points = [
                new THREE.Vector3(-hw, -hh, 0.02),
                new THREE.Vector3( hw, -hh, 0.02),
                new THREE.Vector3( hw,  hh, 0.02),
                new THREE.Vector3(-hw,  hh, 0.02),
                new THREE.Vector3(-hw, -hh, 0.02),
            ];
            const borderGeom = new THREE.BufferGeometry().setFromPoints(points);
            const borderMat = new THREE.LineBasicMaterial({
                color: this.colors.componentOutline,
                transparent: true,
                opacity: 0.85,
            });
            const borderLine = new THREE.Line(borderGeom, borderMat);
            // Stash for the per-mesh hover-out restore path.
            borderLine.userData.origColor = this.colors.componentOutline;
            group.add(borderLine);
        }

        const compZ = comp.layer === 'top' ? 1 : (comp.layer === 'bottom' ? 0.5 : 0.75);
        group.position.set(comp.x + w / 2, comp.y + h / 2, compZ);

        // Silkscreen body lines stay in absolute board coordinates and
        // render as a separate scene mesh — XZZ bakes rotation into the
        // segment endpoints, so adding them inside the rotated group
        // would double-rotate them. Coloured in componentOutline (cyan)
        // not the white silkscreen token: otherwise XZZ components
        // (which DO populate body_lines) read as a wash of white over
        // the cyan AABB box, making them look greyish — KiCad/MNT
        // (which DON'T populate body_lines) stays pure cyan, and the
        // mismatch was visible side by side.
        if (hasBodyLines) {
            const silkPts = [];
            for (const seg of comp.body_lines) {
                silkPts.push(new THREE.Vector3(seg.x1, seg.y1, compZ + 0.04));
                silkPts.push(new THREE.Vector3(seg.x2, seg.y2, compZ + 0.04));
            }
            const silkGeom = new THREE.BufferGeometry().setFromPoints(silkPts);
            const silkMat = new THREE.LineBasicMaterial({
                color: this.colors.componentOutline,
                transparent: true,
                opacity: 0.7,
            });
            const silkMesh = new THREE.LineSegments(silkGeom, silkMat);
            silkMesh.userData._side = comp._side || null;
            this.scene.add(silkMesh);
            this._componentExtras.push(silkMesh);
        }

        if (comp.rotation) {
            group.rotation.z = comp.rotation * Math.PI / 180;
        }

        // Silkscreen label sprite for 0-pin parts (logos, badges,
        // region labels — BADGE / REFORM / CPU / MPCIE on the MNT
        // Reform, NOTOUCH zones, etc.). Mirrors brd_viewer.js's
        // 'annotations' loop. Renders the label as a flat sprite at
        // the bbox centre in absolute board coords (so silkscreen text
        // doesn't double-rotate with the part group).
        if (!comp.pin_count) {
            this._addSilkscreenLabel(comp);
        } else {
            // Real components get a refdes sprite that auto-hides at low zoom.
            this._addComponentRefdesLabel(comp);
        }

        // Hover/select uses `item.x, item.y` as the *centre* (so the
        // AABB-distance check in `checkHover` reduces to 0 when the
        // cursor sits inside the rectangle). The render payload's
        // `comp.x, comp.y` is the bbox bottom-left — pass the centre
        // explicitly so hover lights up the entire component, not
        // just a 20-px radius around its corner.
        const itemData = {
            ...comp,
            x: comp.x + w / 2,
            y: comp.y + h / 2,
            _mesh: group,
            _side: comp._side || null,
            originalColor: isTestPoint ? this.colors.testPad : this.colors.componentOutline,
        };
        group.userData = itemData;
        this._hoverableItems.push(itemData);

        this.scene.add(group);
        this.meshGroups.components.push(group);
    }

    _addComponentRefdesLabel(comp) {
        const refdes = comp.id;
        if (!refdes) return;

        const w = comp.width;
        const h = comp.height;
        const longSide = Math.max(w, h);
        // No size cutoff: XZZ passives can be 0.25 mm × 0.15 mm (smaller
        // than half a millimetre) but readable when zoomed in. Visibility
        // is exclusively driven by the screen-pixel threshold in
        // _updateComponentLabelVisibility, so the label hides at low zoom
        // and pops in once the part covers enough on-screen pixels.

        // Higher resolution canvas + DPR-aware so crisp at any zoom.
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const canvas = document.createElement('canvas');
        canvas.width = 256 * dpr;
        canvas.height = 64 * dpr;
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);

        const hex = '#' + this.colors.silkscreen.toString(16).padStart(6, '0');
        ctx.font = "800 38px 'Inter', system-ui, sans-serif";
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        // Subtle dark stroke under the fill so the text reads cleanly
        // against bright pads / busy areas. Anthropic-style: weighted
        // sans, crisp edges, soft glow via additive blending below.
        ctx.lineWidth = 4;
        ctx.strokeStyle = 'rgba(7,16,31,0.85)';   // bg-deep
        ctx.strokeText(refdes, 128, 32);
        ctx.fillStyle = hex;
        ctx.fillText(refdes, 128, 32);

        const texture = new THREE.CanvasTexture(canvas);
        texture.minFilter = THREE.LinearFilter;
        texture.magFilter = THREE.LinearFilter;
        const material = new THREE.SpriteMaterial({
            map: texture,
            transparent: true,
            depthTest: false,
            opacity: 0.95,
            blending: THREE.AdditiveBlending,
        });
        const sprite = new THREE.Sprite(material);

        // Scale set to a fixed screen pixel height by
        // _updateRefdesLabelScale, refreshed on every zoom change so
        // refdes labels stay readable (~14 px tall) regardless of zoom
        // level. Initial scale is just a placeholder.
        const aspect = canvas.width / canvas.height;  // 4
        sprite.scale.set(1, 1 / aspect, 1);
        sprite.position.set(comp.x + w / 2, comp.y + h / 2, 3);
        sprite.visible = false;  // _updateComponentLabelVisibility will flip

        // Stash long side for the zoom-driven visibility check + canvas
        // aspect for the pixel-height scaler.
        sprite.userData.compLong = longSide;
        sprite.userData.aspect = aspect;
        sprite.userData._refdes = refdes;
        sprite.userData._side = comp._side || null;
        this._componentLabels.push(sprite);
        this.scene.add(sprite);
    }

    /**
     * Refdes labels render at a fixed screen pixel height (~14 px) so
     * they stay readable at any zoom. World units are recomputed each
     * zoom from the current pixelSize.
     */
    _updateRefdesLabelScale() {
        if (!this._componentLabels.length) return;
        const h = (this.container && this.container.clientHeight) || 800;
        const pixelSize = this.frustumSize / h;
        const targetH = this._refdesPixelHeight * pixelSize;
        for (const sprite of this._componentLabels) {
            const aspect = sprite.userData.aspect || 4;
            sprite.scale.set(targetH * aspect, targetH, 1);
        }
    }

    /**
     * Compute world-unit dash + gap size targeting ~10 px dashes and
     * ~7 px gaps regardless of zoom. Used by highlightNet at create-time
     * and by _updateFlyLineDashes on every zoom change.
     */
    _flyDashWorld() {
        const h = (this.container && this.container.clientHeight) || 800;
        const pixelSize = this.frustumSize / h;
        // Heavy dash, light gap: 14 px ON / 6 px OFF.
        return { dashWorld: 14 * pixelSize, gapWorld: 6 * pixelSize };
    }

    _flyThicknessWorld() {
        const h = (this.container && this.container.clientHeight) || 800;
        const pixelSize = this.frustumSize / h;
        // ~1.5 CSS pixels thick — fine but still visibly above the
        // 1-device-px floor that THREE.Line would land on.
        return 1.5 * pixelSize;
    }

    /**
     * Build the dashed-pattern alpha texture used by fly-lines. 20 px
     * wide canvas: 14 px opaque white, 6 px transparent (70 % duty).
     * The texture is set to wrap horizontally so plane UV repeats
     * tile the dashes along each fly-line segment.
     */
    _createFlyDashTexture() {
        const c = document.createElement('canvas');
        c.width = 20; c.height = 4;
        const ctx = c.getContext('2d');
        ctx.fillStyle = 'rgba(255,255,255,1)';
        ctx.fillRect(0, 0, 14, 4);
        // Last 6 pixels stay transparent → the gap between dashes.
        const tex = new THREE.CanvasTexture(c);
        tex.wrapS = THREE.RepeatWrapping;
        tex.wrapT = THREE.ClampToEdgeWrapping;
        tex.minFilter = THREE.LinearFilter;
        tex.magFilter = THREE.NearestFilter;
        tex.generateMipmaps = false;
        tex.premultiplyAlpha = true;
        return tex;
    }

    /**
     * Set a PlaneGeometry's X-axis UVs so the bound texture tiles
     * `repeatX` times across the plane. PlaneGeometry vertex order:
     * [bl, br, tl, tr] — we keep Y untouched and stretch X.
     */
    _setUVRepeatX(geometry, repeatX) {
        const uvs = geometry.attributes.uv;
        if (!uvs) return;
        uvs.setXY(0, 0, 0);
        uvs.setXY(1, repeatX, 0);
        uvs.setXY(2, 0, 1);
        uvs.setXY(3, repeatX, 1);
        uvs.needsUpdate = true;
    }

    /**
     * Draw a dashed segment between two endpoints as a sequence of
     * short PlaneGeometry rectangles, oriented along the segment and
     * spaced with `gapWorld` gaps. Each plane is `thickness` units
     * wide. We can't use THREE.Line / LineDashedMaterial because
     * WebGL on Chromium ignores `linewidth` (clamps to 1 px), making
     * the dashes effectively invisible at HiDPI; PlaneGeometry has
     * real world-unit width that scales correctly.
     *
     * `material` is shared across all planes for one fly-line group
     * so the colour update path stays cheap.
     */
    /**
     * Single-plane fly-line: one PlaneGeometry stretched between
     * start and end, with a dashed alpha-mapped material. UV repeat
     * tiles the dash pattern along the segment so we don't have to
     * spawn one mesh per dash. On zoom, only `scale.y` (thickness)
     * and the UV repeat need refreshing — `_updateFlyLineDashes`
     * does both in place without rebuilding the scene graph.
     */
    _createFlyLineDashed(startPos, endPos, material, thickness, dashWorld, gapWorld) {
        const dx = endPos.x - startPos.x;
        const dy = endPos.y - startPos.y;
        const length2D = Math.sqrt(dx * dx + dy * dy);
        if (length2D < 1e-4) return;
        const angle = Math.atan2(dy, dx);
        const stride = dashWorld + gapWorld;

        // Unit-square plane scaled to (length, thickness) — keeps a
        // single shared geometry vertex layout while letting each
        // mesh stretch independently. UVs default to [0..1]; we
        // stretch X only so the dash texture tiles `repeatX` times.
        const geom = new THREE.PlaneGeometry(1, 1);
        const repeatX = length2D / stride;
        this._setUVRepeatX(geom, repeatX);
        const mesh = new THREE.Mesh(geom, material);
        mesh.position.set(
            (startPos.x + endPos.x) / 2,
            (startPos.y + endPos.y) / 2,
            (startPos.z + endPos.z) / 2,
        );
        mesh.rotation.z = angle;
        mesh.scale.set(length2D, thickness, 1);
        // Stash the segment length so `_updateFlyLineDashes` can
        // recompute the UV repeat at the new dash stride on zoom
        // without re-walking the original endpoints.
        mesh.userData._flyLength = length2D;
        this.scene.add(mesh);
        this.netLines.push(mesh);
    }

    /**
     * Recompute dashSize / gapSize on every active fly-line so the
     * dashed trace stays at a consistent ~10 px dash regardless of
     * zoom. Without this, zooming in turns each dash into a huge slab
     * (the world-unit dashSize stays fixed but the pixel size grows),
     * and zooming out makes them sub-pixel and effectively solid.
     */
    _updateFlyLineDashes() {
        if (!this.netLines || !this.netLines.length) return;
        // In-place update: each fly-line is now a single Plane mesh
        // with a tiled dash texture, so a zoom step just needs to
        // refresh the world-unit thickness (mesh.scale.y) and the
        // X-axis UV repeat (so dashes stay ~14 px ON / 6 px OFF on
        // screen at the new zoom). No add / remove on the scene
        // graph — fast even at the 200-fly-line cap.
        const thickness = this._flyThicknessWorld();
        const { dashWorld, gapWorld } = this._flyDashWorld();
        const stride = dashWorld + gapWorld;
        for (const mesh of this.netLines) {
            const len = mesh.userData && mesh.userData._flyLength;
            if (!len) continue;
            mesh.scale.y = thickness;
            this._setUVRepeatX(mesh.geometry, len / stride);
        }
    }


    /**
     * Flip refdes-label visibility based on current zoom: a label only
     * shows once its component is at least `_labelMinPx` pixels wide on
     * screen. Without this, zooming out paints the entire board with
     * unreadable refdes spam.
     */
    _updateComponentLabelVisibility() {
        if (!this._componentLabels.length) return;
        const h = this.container.clientHeight;
        if (!h) return;
        const pixelSize = this.frustumSize / h;       // world units per pixel
        const minWorld = this._labelMinPx * pixelSize;
        const mode = this.sideMode;
        const prefix = this._agentFilterPrefix;
        const refdesOk = (id) => !prefix
            || (id && id.toUpperCase().startsWith(prefix));
        for (const sprite of this._componentLabels) {
            const side = sprite.userData._side;
            const sideOk = mode === 'both' || side == null || side === mode;
            // DNP-alternate labels are gated additionally by `_showDnp`
            // so the alternates' refdes only appear when the user has
            // toggled the DFM overlay on.
            if (sprite.userData._dnpLabel && !this._showDnp) {
                sprite.visible = false;
                continue;
            }
            // Refdes-prefix filter (set by bv_filter_by_type) hides
            // labels whose refdes does not match the active prefix.
            // Applied as the third axis in the side AND DNP AND refdes
            // composite gate so all three filters compose cleanly.
            if (!refdesOk(sprite.userData._refdes)) {
                sprite.visible = false;
                continue;
            }
            sprite.visible = sideOk && sprite.userData.compLong >= minWorld;
        }
    }

    _addSilkscreenLabel(comp) {
        const raw = (comp.value || comp.id || '').replace(/^(LABEL_|LOGO_)/, '');
        if (!raw) return;

        const w = comp.width;
        const h = comp.height;
        const longSide = Math.max(w, h);
        const shortSide = Math.min(w, h);
        if (longSide < 1) return;  // bbox too small to render readably
        const landscape = w >= h;

        // Render text into a high-DPR canvas. Hex from token
        // (--text = #e6edf7). No background fill, but a thin dark
        // stroke around the glyphs so the text reads on busy areas.
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const canvas = document.createElement('canvas');
        canvas.width = 512 * dpr;
        canvas.height = 128 * dpr;
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);

        const hex = '#' + this.colors.silkscreen.toString(16).padStart(6, '0');
        ctx.font = "800 64px 'Inter', system-ui, sans-serif";
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.lineWidth = 6;
        ctx.strokeStyle = 'rgba(7,16,31,0.85)';
        ctx.strokeText(raw, 256, 64);
        ctx.fillStyle = hex;
        ctx.fillText(raw, 256, 64);

        const texture = new THREE.CanvasTexture(canvas);
        texture.minFilter = THREE.LinearFilter;
        texture.magFilter = THREE.LinearFilter;
        const material = new THREE.MeshBasicMaterial({
            map: texture,
            transparent: true,
            depthTest: false,
            opacity: 0.95,
            blending: THREE.AdditiveBlending,
        });

        // Plane sized to ~85% of long side x proportional short side.
        // Using a Plane (not a Sprite) so we can rotate it -90° for
        // portrait bboxes — same rule as brd_viewer.js: KiCad footprint
        // rotation is implicit in the bbox proportions, so a tall bbox
        // means the silkscreen text was printed vertically.
        const aspect = canvas.width / canvas.height;  // 4
        const geom = new THREE.PlaneGeometry(1, 1 / aspect);
        const mesh = new THREE.Mesh(geom, material);
        // +PI/2 (CCW) so the text reads upward / top-to-bottom in the
        // Three.js Y-up scene — brd_viewer.js's -PI/2 was for canvas 2D
        // Y-down, the rotation sign flips between the two coord systems.
        if (!landscape) mesh.rotation.z = Math.PI / 2;
        mesh.position.set(comp.x + w / 2, comp.y + h / 2, 4);

        // Stash measurements; _updateSilkscreenLabelScale picks the
        // smaller of (bbox-fit world width) and (cap world width from
        // the screen pixel ceiling) on every zoom change.
        mesh.userData.longSide = longSide;
        mesh.userData.aspect = aspect;
        mesh.userData._side = comp._side || null;
        this._silkscreenLabels.push(mesh);
        this.scene.add(mesh);
    }

    /**
     * Silkscreen labels stay anchored to the bbox (so a small zone reads
     * with smaller text, a big zone with bigger text), but cap the
     * pixel height at `_silkscreenMaxPixelHeight` so they don't blow
     * up to comic-sans size when zoomed in close on a large region
     * label like BADGE.
     */
    _updateSilkscreenLabelScale() {
        if (!this._silkscreenLabels.length) return;
        const h = (this.container && this.container.clientHeight) || 800;
        const pixelSize = this.frustumSize / h;
        const maxLong = this._silkscreenMaxPixelHeight * pixelSize *
            (this._silkscreenLabels[0].userData.aspect || 4);
        for (const mesh of this._silkscreenLabels) {
            const aspect = mesh.userData.aspect || 4;
            const fitLong = mesh.userData.longSide * 0.85;
            const targetLong = Math.min(fitLong, maxLong);
            mesh.scale.set(targetLong, targetLong / aspect, 1);
        }
    }


    createTrace(trace) {
        if (trace.points.length < 2) return;

        // Layer 28 in XZZ is the board edge / outline. Render it brighter
        // (silkscreen white) and slightly thicker than copper traces so
        // the board contour pops on the dark scene background. Layer 17
        // is silkscreen — same neutral white. Copper traces (layers
        // 1..16) inherit their net's category colour (power copper-orange,
        // ground dark grey, signal light grey, etc.) so each net's
        // routing reads visually consistent with its pins. Traces with
        // no net (rare — connector strips or mechanical) fall back to
        // the neutral copper hue.
        const isOutline = trace.layer === 28;
        const isSilkscreen = trace.layer === 17;
        let color;
        if (isOutline || isSilkscreen) {
            color = this.colors.silkscreen;
        } else if (trace.net) {
            const cat = (trace.is_gnd || trace.net === 'GND')
                ? 'ground'
                : this._netCategory(trace.net);
            color = this._pinColorForCategory(cat);
        } else {
            color = this.colors.copper;
        }
        const opacity = isOutline ? 0.95 : 0.6;
        const zPos = isOutline ? 0.5 : 0.3;

        const points = trace.points.map(p => new THREE.Vector3(p.x, p.y, zPos));
        const geom = new THREE.BufferGeometry().setFromPoints(points);
        const mat = new THREE.LineBasicMaterial({
            color: color,
            transparent: !isOutline,
            opacity: opacity,
            linewidth: isOutline ? 2 : 1,
        });
        const line = new THREE.Line(geom, mat);
        line.userData = {
            ...trace,
            type: 'TRACE',
            _side: trace._side || null,
            // Stash parse-time material values so the net-highlight /
            // unhighlight path can restore them without a global
            // colors.copper lookup (which would already have changed
            // if the user retuned the palette).
            origColor: color,
            origOpacity: opacity,
            _kind: isOutline ? 'outline' : (isSilkscreen ? 'silkscreen' : 'copper'),
        };
        // Always show the board outline, even when the user disabled
        // copper-trace overlay.
        line.visible = isOutline ? true : this.showTraces;

        this.scene.add(line);
        this.meshGroups.traces.push(line);
    }

    clearScene() {
        // Remove instanced meshes
        if (this._circularPinInstance) {
            this.scene.remove(this._circularPinInstance);
            this._circularPinInstance.dispose();
            this._circularPinInstance = null;
        }
        if (this._circularPinBorderInstance) {
            this.scene.remove(this._circularPinBorderInstance);
            this._circularPinBorderInstance.dispose();
            this._circularPinBorderInstance = null;
        }
        this._rectPinInstances.forEach(({ body, geometry }) => {
            this.scene.remove(body);
            body.dispose();
            geometry.dispose();
        });
        this._rectPinInstances.clear();

        if (this._viasOuterInstance) {
            this.scene.remove(this._viasOuterInstance);
            this._viasOuterInstance.dispose();
            this._viasOuterInstance = null;
        }
        if (this._viasInnerInstance) {
            this.scene.remove(this._viasInnerInstance);
            this._viasInnerInstance.dispose();
            this._viasInnerInstance = null;
        }
        if (this._testPadsInstance) {
            this.scene.remove(this._testPadsInstance);
            this._testPadsInstance.dispose();
            this._testPadsInstance = null;
        }
        if (this._testPadsBorderInstance) {
            this.scene.remove(this._testPadsBorderInstance);
            this._testPadsBorderInstance.dispose();
            this._testPadsBorderInstance = null;
        }
        // GenCAD `$MECH HOLE` meshes — plain `THREE.Mesh` (not Instanced),
        // dispose their geometry on clear so reload doesn't leak.
        if (this._mechHoleMeshes && this._mechHoleMeshes.length) {
            this._mechHoleMeshes.forEach((m) => {
                this.scene.remove(m);
                if (m.geometry) m.geometry.dispose();
            });
            this._mechHoleMeshes = [];
        }

        // Clear data arrays
        this._outlineMeshes = [];
        this._markerMeshes = [];
        this._pinBorderLines = [];
        this._componentExtras = [];
        this._componentLabels = [];
        this._silkscreenLabels = [];
        this._netHiddenMarkers = [];
        this.dualOutline = null;
        this.sideMode = 'both';
        // Reset scene rotation so a fresh board doesn't inherit the
        // previous board's orientation.
        this.rotationDeg = 0;
        this.scene.rotation.z = 0;
        this._pinInstanceData = [];
        this._viaInstanceData = [];
        this._testPadInstanceData = [];
        this._hoverableItems = [];
        this._spatialGrid = {};

        // Remove component groups
        this.meshGroups.components.forEach(m => this.scene.remove(m));
        this.meshGroups.components = [];
        this.meshGroups.traces.forEach(m => this.scene.remove(m));
        this.meshGroups.traces = [];

        // Remove all remaining children
        while (this.scene.children.length > 0) {
            this.scene.remove(this.scene.children[0]);
        }

        this.selectedItem = null;
        this.hoveredItem = null;
    }

    // ========================
    // UI CONTROLS
    // ========================

    toggleLayer(layer) {
        this.layers[layer] = !this.layers[layer];
        const btn = document.getElementById(`layer-${layer}`);

        if (this.layers[layer]) {
            btn.classList.remove('bg-dark-600', 'text-gray-400', 'border-dark-500');
            btn.classList.add(
                layer === 'top' ? 'bg-blue-500/20' : 'bg-red-500/20',
                layer === 'top' ? 'text-blue-400' : 'text-red-400',
                layer === 'top' ? 'border-blue-500/30' : 'border-red-500/30'
            );
        } else {
            btn.classList.remove('bg-blue-500/20', 'bg-red-500/20', 'text-blue-400', 'text-red-400', 'border-blue-500/30', 'border-red-500/30');
            btn.classList.add('bg-dark-600', 'text-gray-400', 'border-dark-500');
        }

        // Update component visibility
        this.meshGroups.components.forEach(mesh => {
            const meshLayer = mesh.userData.layer;
            if (meshLayer === layer) {
                mesh.visible = this.layers[layer];
            } else if (meshLayer === 'both') {
                mesh.visible = this.layers.top || this.layers.bottom;
            }
        });

        // For instanced meshes, we'd need to update visibility per-instance
        // For now, we rely on hover filtering by layer
        this.requestRender();
    }

    toggleVias() {
        this.showVias = !this.showVias;
        const btn = document.getElementById('toggle-vias');

        if (this.showVias) {
            btn.classList.remove('bg-dark-600', 'text-gray-400', 'border-dark-500');
            btn.classList.add('bg-fuchsia-500/20', 'text-fuchsia-400', 'border-fuchsia-500/30');
        } else {
            btn.classList.remove('bg-fuchsia-500/20', 'text-fuchsia-400', 'border-fuchsia-500/30');
            btn.classList.add('bg-dark-600', 'text-gray-400', 'border-dark-500');
        }

        if (this._viasOuterInstance) this._viasOuterInstance.visible = this.showVias;
        if (this._viasInnerInstance) this._viasInnerInstance.visible = this.showVias;
        this.requestRender();
    }

    toggleTraces() {
        // Pure model toggle — the bridge owns the button styling and
        // owns it under the current design system (`brdToggleTraces`,
        // `active` class). Old code here referenced a legacy
        // `toggle-traces` id with Tailwind orange/dark classes that no
        // longer exist, which crashed the new toolbar button.
        this.showTraces = !this.showTraces;
        this.meshGroups.traces.forEach(t => t.visible = this.showTraces);
        this.requestRender();
    }

    flipBoard() {
        // Legacy left/right mirror (BRD format chrome). Independent of
        // the rotation state — multiplies into scene.scale.x.
        this.isFlipped = !this.isFlipped;
        this.scene.scale.x = this.isFlipped ? -1 : 1;
        const btn = document.getElementById('flip-board');
        if (btn) btn.classList.toggle('active', this.isFlipped);
        this.requestRender();
    }

    rotateLeft() {
        // CCW from the viewer's POV: +90° around Z, which visually
        // tips the board's top edge toward the left of the screen.
        this.rotationDeg = (this.rotationDeg + 90) % 360;
        this._applyTransform();
    }

    rotateRight() {
        // CW: -90° (equivalently +270° mod 360).
        this.rotationDeg = (this.rotationDeg + 270) % 360;
        this._applyTransform();
    }

    /**
     * Apply the rotation to the scene root, then recentre the camera
     * on the side-mode bbox through the same rotation so the visible
     * content stays roughly under the cursor.
     */
    _applyTransform() {
        this.scene.rotation.z = this.rotationDeg * Math.PI / 180;
        this._recentreOnSideMode();
        this.requestRender();
    }

    // ========================
    // DUAL VIEW (simplified for now)
    // ========================

    toggleDualView() {
        if (!this.boardData) return;

        this.isDualView = !this.isDualView;
        const btn = document.getElementById('dual-view');

        if (this.isDualView) {
            btn.classList.remove('bg-dark-600', 'text-gray-400', 'border-dark-500');
            btn.classList.add('bg-green-500/20', 'text-green-400', 'border-green-500/30');
            this.createDualViewGroup();
        } else {
            btn.classList.remove('bg-green-500/20', 'text-green-400', 'border-green-500/30');
            btn.classList.add('bg-dark-600', 'text-gray-400', 'border-dark-500');
            this.removeDualViewGroup();
        }

        this.centerDualView();
        this.requestRender();
    }

    createDualViewGroup() {
        // Simplified dual view - just add labels for now
        // Full dual view with instanced meshes would require more complex handling
        if (!this.boardData) return;

        const data = this.boardData;
        const boardWidth = data.board_width;
        const boardHeight = data.board_height;
        const gap = boardWidth * 0.15;
        const offsetX = data.board_offset_x || 0;
        const offsetY = data.board_offset_y || 0;

        // TOP label
        const topCanvas = document.createElement('canvas');
        const topCtx = topCanvas.getContext('2d');
        topCanvas.width = 128;
        topCanvas.height = 48;
        topCtx.fillStyle = 'rgba(59, 130, 246, 0.8)';
        topCtx.roundRect(4, 4, 120, 40, 8);
        topCtx.fill();
        topCtx.fillStyle = 'white';
        topCtx.font = 'bold 24px Inter, Arial';
        topCtx.textAlign = 'center';
        topCtx.textBaseline = 'middle';
        topCtx.fillText('TOP', 64, 24);

        const topTexture = new THREE.CanvasTexture(topCanvas);
        const topMat = new THREE.SpriteMaterial({ map: topTexture, transparent: true, depthTest: false });
        this.topLabel = new THREE.Sprite(topMat);
        const labelScale = boardWidth * 0.15;
        this.topLabel.scale.set(labelScale, labelScale * 0.375, 1);
        this.topLabel.position.set(offsetX + boardWidth / 2, offsetY + boardHeight + labelScale * 0.3, 10);
        this.scene.add(this.topLabel);
    }

    removeDualViewGroup() {
        if (this.topLabel) {
            this.scene.remove(this.topLabel);
            this.topLabel = null;
        }
        if (this.bottomLabel) {
            this.scene.remove(this.bottomLabel);
            this.bottomLabel = null;
        }
        if (this.dualViewGroup) {
            this.scene.remove(this.dualViewGroup);
            this.dualViewGroup = null;
        }
    }

    centerDualView() {
        if (!this.boardData) return;

        const data = this.boardData;
        const offsetX = data.board_offset_x || 0;
        const offsetY = data.board_offset_y || 0;

        this.camera.position.x = offsetX + data.board_width / 2;
        this.camera.position.y = offsetY + data.board_height / 2;
        this.frustumSize = Math.max(data.board_width, data.board_height) * 1.2;

        this.zoom = 100 / this.frustumSize;
        this.onResize();
        document.getElementById('zoom-level').textContent = Math.round(this.zoom * 100);
    }

    // ========================
    // SIDE FILTER (XZZ dual outline)
    // ========================

    /**
     * Switch between rendering only the top face, only the bottom face,
     * or both. Only takes effect when the board ships dual outline data
     * (XZZ side-by-side / stacked layout); on single-outline formats
     * every entity has `_side === null` and stays visible regardless.
     *
     * Camera is recentred and rezoomed to fit the visible bbox so the
     * tech doesn't lose the board off-screen between toggles.
     */
    setSideMode(mode, opts = {}) {
        if (mode !== 'top' && mode !== 'bottom' && mode !== 'both') return;
        const prevMode = this.sideMode;
        this.sideMode = mode;
        // Persist across reloads — the tech usually settles on a
        // preferred face and expects every subsequent board to come up
        // in the same mode rather than reset to 'both' on load.
        try { localStorage.setItem('pcb.sideMode', mode); } catch (_) {}
        // Reflect the mode on the toolbar segment so calls that come from
        // outside the bridge (e.g. fresh board load resetting to 'both')
        // keep the visual state in sync with the actual filter.
        const segMap = {
            top: 'brdLayerTop',
            both: 'brdLayerBoth',
            bottom: 'brdLayerBottom',
        };
        Object.entries(segMap).forEach(([m, id]) => {
            const el = document.getElementById(id);
            if (el) el.classList.toggle('active', m === mode);
        });
        const allow = (side) => mode === 'both' || side == null || side === mode;

        // Plain meshes — flip visibility directly. DNP-layer meshes
        // are gated by `_showDnp` instead of the side filter; skip
        // them here so they don't reappear when the board switches
        // back to "top" or any side mode.
        const dnp = (m) => m && m.userData && m.userData._dnpLayer;
        this._outlineMeshes.forEach((m) => { m.visible = allow(m.userData._side); });
        this._markerMeshes.forEach((m) => { m.visible = allow(m.userData._side); });
        this._pinBorderLines.forEach((m) => {
            if (dnp(m)) return;
            // Skip DNP-flagged borders when the toggle is off; otherwise
            // follow the side filter (and the refdes-prefix filter when
            // bv_filter_by_type is active) for any other pin border.
            if (m.userData && m.userData._isDnp && !this._showDnp) {
                m.visible = false;
                return;
            }
            const sideOk = allow(m.userData ? m.userData._side : null);
            const refOk = !this._agentFilterPrefix
                || (m.userData && m.userData._component
                    && m.userData._component.toUpperCase()
                        .startsWith(this._agentFilterPrefix));
            m.visible = sideOk && refOk;
        });
        this._componentExtras.forEach((m) => {
            if (dnp(m)) return;
            m.visible = allow(m.userData._side);
        });
        const prefix = this._agentFilterPrefix;
        const refdesOk = (id) => !prefix
            || (id && id.toUpperCase().startsWith(prefix));
        this.meshGroups.components.forEach((m) => {
            if (dnp(m)) return;
            m.visible = allow(m.userData._side) && refdesOk(m.userData.id);
        });
        this.meshGroups.traces.forEach((m) => {
            const isOutline = m.userData && m.userData.layer === 28;
            // Outline traces are tied to the per-face polygon and follow
            // the side filter; copper traces follow the trace toggle too.
            const sideOk = allow(m.userData ? m.userData._side : null);
            m.visible = sideOk && (isOutline ? true : this.showTraces);
        });
        // Silkscreen labels (0-pin parts: BADGE / LOGO / region tags)
        // — visibility is driven solely by the side filter; they have
        // no zoom threshold. Refdes labels (`_componentLabels`) get
        // re-evaluated by `_updateComponentLabelVisibility()` below.
        this._silkscreenLabels.forEach((s) => {
            s.visible = allow(s.userData ? s.userData._side : null);
        });

        // InstancedMesh — zero-scale unwanted instances. The original
        // matrices are stashed in userData._matrices at build time.
        // DNP-layer meshes are gated by `_showDnp` instead of the side
        // filter; skip them here.
        this._applySideToInstanced(this._circularPinInstance);
        this._applySideToInstanced(this._circularPinBorderInstance);
        this._rectPinInstances.forEach(({ body }) => {
            if (dnp(body)) return;
            this._applySideToInstanced(body);
        });
        this._applySideToInstanced(this._viasOuterInstance);
        this._applySideToInstanced(this._viasInnerInstance);
        this._applySideToInstanced(this._testPadsInstance);
        this._applySideToInstanced(this._testPadsBorderInstance);

        // `keepView` flips between the two faces while preserving zoom
        // and the on-screen position — used by the dive-under-board
        // chevron click. Two cases:
        //   1. Dual-outline layout (XZZ side-by-side / stacked): the
        //      two faces live in different XY regions of the canvas,
        //      so keepView calls `_mirrorCameraAcrossFaces` to project
        //      the cursor onto the new face's frame.
        //   2. Single-outline layout (.fz / .cad / KiCad / BRD): both
        //      faces share the same XY coordinate space — flipping the
        //      filter just changes which pins are visible, the camera
        //      stays put. Skipping `_recentreOnSideMode` preserves
        //      zoom and pan.
        // We bail to a recentre when keepView is OFF, switching to /
        // from BOTH (no specific face to centre on), or staying on the
        // same face.
        const isFlip = opts.keepView
            && prevMode !== 'both'
            && mode !== 'both'
            && prevMode !== mode;
        if (isFlip && this.dualOutline) {
            this._mirrorCameraAcrossFaces(prevMode, mode);
        } else if (isFlip) {
            // Single-outline — nothing to do; camera position + frustum
            // size already match the new face's coordinate space.
        } else {
            this._recentreOnSideMode();
        }
        // Re-apply zoom-driven label visibility now that components flipped.
        if (this._updateComponentLabelVisibility) {
            this._updateComponentLabelVisibility();
        }
        // If a net is currently highlighted, rebuild its fly-lines so
        // cross-face endpoints get the dive-under-board treatment (or
        // the original side-by-side path) matching the new mode.
        if (this.selectedItem && this.selectedItem.net
            && this.selectedItem.net !== 'NC') {
            this.clearNetHighlight();
            this.highlightNet();
        }
        this.requestRender();
    }

    /**
     * Translate the current camera position from `fromMode`'s display
     * frame to `toMode`'s display frame using the same FlexBV flip-over
     * mirror that `_netEndpointPos` applies to fly-line endpoints.
     * Frustum size is unchanged — zoom level stays exactly where it
     * was. The mirror is computed in scene-local coords so it composes
     * cleanly with the active rotation (we round-trip through
     * `_worldToLocal` and the forward rotation matrix).
     */
    _mirrorCameraAcrossFaces(fromMode, toMode) {
        if (!this.dualOutline) return;
        // Camera position lives in world (post-rotation) coords. Take
        // it back to scene-local before mirroring across faces.
        const localCam = this._worldToLocal(
            this.camera.position.x, this.camera.position.y
        );
        const dual = this.dualOutline;
        const top = dual.bbox_top;
        const bot = dual.bbox_bottom;
        let lx, ly;
        if (dual.axis === 'x') {
            lx = top.x + bot.x + bot.w - localCam.x;
            const tyOffset = toMode === 'top'
                ? (top.y - bot.y)
                : (bot.y - top.y);
            ly = localCam.y + tyOffset;
        } else {
            ly = top.y + bot.y + bot.h - localCam.y;
            const txOffset = toMode === 'top'
                ? (top.x - bot.x)
                : (bot.x - top.x);
            lx = localCam.x + txOffset;
        }
        // Re-rotate back to world.
        const rad = (this.rotationDeg || 0) * Math.PI / 180;
        const cos = Math.cos(rad);
        const sin = Math.sin(rad);
        this.camera.position.x = lx * cos - ly * sin;
        this.camera.position.y = lx * sin + ly * cos;
        this.onResize();
    }

    _applySideToInstanced(mesh) {
        if (!mesh || !mesh.userData || !mesh.userData._matrices) return;
        const sides = mesh.userData._sides;
        const dnpFlags = mesh.userData._dnpFlags;
        const components = mesh.userData._components;
        const matrices = mesh.userData._matrices;
        const allow = (side) => this.sideMode === 'both'
            || side == null
            || side === this.sideMode;
        const prefix = this._agentFilterPrefix;
        const refdesOk = (id) => !prefix
            || (id && id.toUpperCase().startsWith(prefix));
        const zero = new THREE.Matrix4().makeScale(0, 0, 0);
        for (let i = 0; i < matrices.length; i++) {
            const sideOk = allow(sides[i]);
            const dnpOk = !dnpFlags || !dnpFlags[i] || this._showDnp;
            // Refdes filter only applies if the mesh recorded a parent
            // component per slot. Vias / mounting holes carry no parent
            // and stay visible regardless of the filter.
            const refOk = !components || refdesOk(components[i]);
            const ok = sideOk && dnpOk && refOk;
            mesh.setMatrixAt(i, ok ? matrices[i] : zero);
        }
        mesh.instanceMatrix.needsUpdate = true;
    }

    _recentreOnSideMode() {
        if (!this.boardData) return;
        const data = this.boardData;
        const dual = this.dualOutline;
        let bbox;
        if (dual && this.sideMode === 'top' && dual.bbox_top) {
            bbox = dual.bbox_top;
        } else if (dual && this.sideMode === 'bottom' && dual.bbox_bottom) {
            bbox = dual.bbox_bottom;
        } else {
            bbox = {
                x: data.board_offset_x || 0,
                y: data.board_offset_y || 0,
                w: data.board_width,
                h: data.board_height,
            };
        }
        // Project the bbox centre through the current scene rotation
        // so the camera keeps the same content visible after a rotate
        // toggle.
        const localCx = bbox.x + bbox.w / 2;
        const localCy = bbox.y + bbox.h / 2;
        const rad = (this.rotationDeg || 0) * Math.PI / 180;
        const cos = Math.cos(rad);
        const sin = Math.sin(rad);
        this.camera.position.x = localCx * cos - localCy * sin;
        this.camera.position.y = localCx * sin + localCy * cos;
        // 90° / 270° swaps the visible width/height; flips don't change
        // extent. Math.max stays the same for any rotation, so the
        // frustum fits regardless.
        this.frustumSize = Math.max(bbox.w, bbox.h) * 1.2;
        this.zoom = 100 / this.frustumSize;
        this.onResize();
        const zoomEl = document.getElementById('zoom-level');
        if (zoomEl) zoomEl.textContent = Math.round(this.zoom * 100);
    }

    // ========================
    // ZOOM CONTROLS
    // ========================

    zoomIn() {
        this.frustumSize *= 0.75;
        // Match the wheel-zoom floor (0.5 mm) so the +/- buttons can
        // reach the same close-up as the wheel — 10 mm was too coarse
        // to actually inspect a 0402 pin pair.
        this.frustumSize = Math.max(0.5, this.frustumSize);
        this.zoom = 100 / this.frustumSize;
        this.onResize();
        document.getElementById('zoom-level').textContent = Math.round(this.zoom * 100);
    }

    zoomOut() {
        this.frustumSize *= 1.25;
        const maxSize = this.boardData ? Math.max(this.boardData.board_width, this.boardData.board_height) * 10 : 2000;
        this.frustumSize = Math.min(maxSize, this.frustumSize);
        this.zoom = 100 / this.frustumSize;
        this.onResize();
        document.getElementById('zoom-level').textContent = Math.round(this.zoom * 100);
    }

    resetView() {
        if (this.boardData) {
            const ox = this.boardData.board_offset_x || 0;
            const oy = this.boardData.board_offset_y || 0;
            this.camera.position.x = ox + this.boardData.board_width / 2;
            this.camera.position.y = oy + this.boardData.board_height / 2;
            this.frustumSize = Math.max(this.boardData.board_width, this.boardData.board_height) * 1.2;
            this.zoom = 100 / this.frustumSize;
            this.onResize();
            document.getElementById('zoom-level').textContent = Math.round(this.zoom * 100);
        }
    }

    focusComponent(id) {
        const comp = this.meshGroups.components.find(m => m.userData.id === id);
        if (comp) {
            this.camera.position.x = comp.position.x;
            this.camera.position.y = comp.position.y;
            this.frustumSize = 25;
            this.zoom = 100 / this.frustumSize;
            this.onResize();
            this.selectItem(comp.userData);
            document.getElementById('zoom-level').textContent = Math.round(this.zoom * 100);
        }
    }

    // ========================
    // DIAGNOSTIC HIGHLIGHT API
    // ========================

    highlightComponents(componentIds) {
        if (!componentIds || componentIds.length === 0) return;

        this.clearDiagnosticHighlights();
        this.diagnosticHighlightedItems = [];

        let foundCount = 0;
        let firstFound = null;

        componentIds.forEach(compId => {
            const compIdUpper = compId.toUpperCase();

            // Check components (regular meshes)
            this.meshGroups.components.forEach(mesh => {
                const meshId = (mesh.userData.id || '').toUpperCase();
                if (meshId === compIdUpper || meshId.startsWith(compIdUpper + '.')) {
                    mesh.userData.diagnosticHighlighted = true;
                    this._setItemHighlight(mesh.userData, true, this.colors.highlight);
                    this.diagnosticHighlightedItems.push(mesh.userData);
                    foundCount++;
                    if (!firstFound) firstFound = mesh.userData;
                }
            });

            // Check hoverable items (pins, test pads) - supports all instance types
            this._hoverableItems.forEach(item => {
                const itemId = (item.id || '').toUpperCase();
                if (itemId === compIdUpper || itemId.startsWith(compIdUpper + '.')) {
                    item.diagnosticHighlighted = true;
                    this._setItemHighlight(item, true, this.colors.highlight);
                    this.diagnosticHighlightedItems.push(item);
                    foundCount++;
                    if (!firstFound) firstFound = item;
                }
            });
        });

        // Center on found items
        if (firstFound && foundCount > 0) {
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;

            this.diagnosticHighlightedItems.forEach(item => {
                const x = item.x || (item._mesh ? item._mesh.position.x : 0);
                const y = item.y || (item._mesh ? item._mesh.position.y : 0);
                minX = Math.min(minX, x);
                minY = Math.min(minY, y);
                maxX = Math.max(maxX, x);
                maxY = Math.max(maxY, y);
            });

            const centerX = (minX + maxX) / 2;
            const centerY = (minY + maxY) / 2;
            const viewSize = Math.max(maxX - minX, maxY - minY) + 10;

            this.camera.position.x = centerX;
            this.camera.position.y = centerY;
            this.frustumSize = Math.max(viewSize * 1.5, 30);
            this.zoom = 100 / this.frustumSize;
            this.onResize();
            document.getElementById('zoom-level').textContent = Math.round(this.zoom * 100);
        }

        this.requestRender();
        console.log(`Highlighted ${foundCount} items for: ${componentIds.join(', ')}`);
    }

    highlightComponent(componentId) {
        if (componentId) {
            this.highlightComponents([componentId]);
        }
    }

    clearDiagnosticHighlights() {
        if (this.diagnosticHighlightedItems && this.diagnosticHighlightedItems.length > 0) {
            // Restore original colors using unified highlight method
            this.diagnosticHighlightedItems.forEach(item => {
                item.diagnosticHighlighted = false;
                this._setItemHighlight(item, false);
            });

            this.diagnosticHighlightedItems = [];
            this.requestRender();
        }
    }

    searchAndFocus(query) {
        if (!query) return false;

        const queryUpper = query.toUpperCase();

        // Search components
        for (const mesh of this.meshGroups.components) {
            const id = (mesh.userData.id || '').toUpperCase();
            const net = (mesh.userData.net || '').toUpperCase();
            const value = (mesh.userData.value || '').toUpperCase();

            if (id.includes(queryUpper) || net.includes(queryUpper) || value.includes(queryUpper)) {
                this.highlightComponent(mesh.userData.id);
                return true;
            }
        }

        // Search hoverable items
        for (const item of this._hoverableItems) {
            const id = (item.id || '').toUpperCase();
            const net = (item.net || '').toUpperCase();

            if (id.includes(queryUpper) || net.includes(queryUpper)) {
                this.focusOnItem(item);
                return true;
            }
        }

        return false;
    }

    // ========================
    // RENDER LOOP
    // ========================

    animate() {
        requestAnimationFrame(() => this.animate());
        if (this.needsRender) {
            this.renderer.render(this.scene, this.camera);
            this.needsRender = false;
        }
    }

    requestRender() {
        this.needsRender = true;
    }

    // ========================================================================
    // AGENT OVERLAY API — driven by bv_* tool events through pcb_viewer_bridge.
    // All distances inbound here are in millimetres (the viewer's world units).
    // The bridge converts mil-based event payloads before calling these.
    // ========================================================================

    /**
     * Lookup a component or pin by refdes from the existing hover index.
     * Returns the first match, preferring an actual component over a
     * pin-as-id (rare). Used by the bridge's bv_focus / bv_highlight /
     * bv_show_pin paths.
     */
    findItemByRefdes(refdes) {
        if (!refdes || !this._hoverableItems) return null;
        const target = String(refdes).trim();
        // Prefer the component's own group (no _instanceType set on it,
        // since createComponent pushes the bare comp object) over a pin.
        let best = null;
        for (const it of this._hoverableItems) {
            if (it.id !== target) continue;
            if (!it._instanceType) return it;  // it's a component
            if (!best) best = it;
        }
        return best;
    }

    /**
     * Toggle between the top and bottom faces. If currently in 'both',
     * pick the opposite of whatever face most of the camera centre lives
     * on (defaults to flipping to 'top' on first call). Mirrors the
     * legacy SVG renderer's flip semantics — a single agent gesture
     * swaps the visible side.
     */
    flipSide() {
        const next = this.sideMode === 'top' ? 'bottom'
                   : this.sideMode === 'bottom' ? 'top'
                   : 'top';  // 'both' → land on 'top' first
        if (typeof this.setSideMode === 'function') {
            this.setSideMode(next);
        }
        this.requestRender();
    }

    /**
     * Place a small label sprite pinned above a component, tracked under
     * `id` so subsequent calls / a reset_view can remove it. Replaces
     * any existing annotation with the same id (the backend reuses ids
     * when the agent revises a label).
     */
    addAnnotation(refdes, label, id) {
        if (!id) id = `ann-${Math.random().toString(36).slice(2, 10)}`;
        // Drop a previous annotation with the same id so callers can
        // edit-in-place without leaking sprites into the scene.
        this.removeAnnotation(id);
        const item = this.findItemByRefdes(refdes);
        if (!item) return;
        const text = String(label || '').trim();
        if (!text) return;

        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const canvas = document.createElement('canvas');
        canvas.width = 512 * dpr;
        canvas.height = 96 * dpr;
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        // Pill background — bg-deep glass with a cyan rim, matching the
        // chat-callout aesthetic. Width auto-fits the rendered text.
        ctx.font = "600 28px 'Inter', system-ui, sans-serif";
        const metrics = ctx.measureText(text);
        const padX = 14, padY = 8;
        const tw = Math.min(metrics.width, 484) + padX * 2;
        const th = 40;
        const bx = (256 - tw / 2);
        const by = 48 - th / 2;
        ctx.fillStyle = 'rgba(7, 16, 31, 0.92)';  // bg-deep
        ctx.strokeStyle = '#67d4f5';              // cyan accent
        ctx.lineWidth = 1.5;
        const r = 6;
        ctx.beginPath();
        ctx.moveTo(bx + r, by);
        ctx.lineTo(bx + tw - r, by);
        ctx.quadraticCurveTo(bx + tw, by, bx + tw, by + r);
        ctx.lineTo(bx + tw, by + th - r);
        ctx.quadraticCurveTo(bx + tw, by + th, bx + tw - r, by + th);
        ctx.lineTo(bx + r, by + th);
        ctx.quadraticCurveTo(bx, by + th, bx, by + th - r);
        ctx.lineTo(bx, by + r);
        ctx.quadraticCurveTo(bx, by, bx + r, by);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = '#e2e8f0';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, 256, 48, 484);

        const texture = new THREE.CanvasTexture(canvas);
        texture.minFilter = THREE.LinearFilter;
        texture.magFilter = THREE.LinearFilter;
        const material = new THREE.SpriteMaterial({
            map: texture,
            transparent: true,
            depthTest: false,
        });
        const sprite = new THREE.Sprite(material);
        // Anchor above the component bbox top edge. item.x/item.y is
        // the centre (set by createComponent), so back out half-height.
        const halfH = (item.height || 1) / 2;
        const offsetY = halfH + 1.5;  // sit ~1.5 mm above the part body
        sprite.position.set(item.x, item.y + offsetY, 8);
        // Initial scale — recomputed every zoom by the legacy refdes
        // updater would be ideal, but a static world size keeps the
        // implementation contained. ~6 mm wide reads at typical
        // diagnostic zoom levels.
        const aspect = canvas.width / canvas.height;
        const baseH = 1.5;
        sprite.scale.set(baseH * aspect, baseH, 1);
        sprite.userData = { _agentAnnotation: id, _side: item._side || null };
        this.scene.add(sprite);
        this._agentAnnotations.set(id, { sprite, refdes, label: text });
        this.requestRender();
    }

    removeAnnotation(id) {
        const entry = this._agentAnnotations.get(id);
        if (!entry) return;
        this.scene.remove(entry.sprite);
        if (entry.sprite.material) {
            if (entry.sprite.material.map) entry.sprite.material.map.dispose();
            entry.sprite.material.dispose();
        }
        this._agentAnnotations.delete(id);
        this.requestRender();
    }

    clearAgentAnnotations() {
        for (const id of Array.from(this._agentAnnotations.keys())) {
            this.removeAnnotation(id);
        }
    }

    /**
     * Paint a numbered badge sprite per protocol step above the step's
     * target component. The active step (`currentId`) gets a saturated
     * cyan halo; pending/done steps render in muted cyan; failed/skipped
     * in amber. `steps` is an array of { id, target, status } objects
     * from `protocol.js`. Multiple steps targeting the same refdes are
     * stacked vertically (newer steps climb upward) so each step keeps
     * its own visible badge — mirrors the SVG renderer's grouping in
     * brd_viewer.js:702-764.
     *
     * Re-painted in full on every call (no diff). Old badges are dropped
     * via clearProtocolBadges() before the new set is laid down so a
     * mid-protocol revision (status flip, current_step_id change) lands
     * cleanly without leaking sprites or textures.
     */
    setProtocolBadges(steps, currentId) {
        this.clearProtocolBadges();
        if (!Array.isArray(steps) || steps.length === 0) return;

        // Group by target refdes so steps sharing a part stack vertically.
        const grouped = new Map();   // refdes → [{ step, displayIndex }]
        for (let i = 0; i < steps.length; i++) {
            const st = steps[i];
            if (!st || !st.target) continue;
            const arr = grouped.get(st.target) || [];
            arr.push({ step: st, displayIndex: i + 1 });
            grouped.set(st.target, arr);
        }

        const cyan = '#' + (this.tokens.cyan || 0x67d4f5).toString(16).padStart(6, '0');
        const amber = '#' + (this.tokens.amber || 0xe8b85a).toString(16).padStart(6, '0');
        const bgDeep = '#' + (this.tokens.bgDeep || 0x07101f).toString(16).padStart(6, '0');

        for (const [refdes, group] of grouped) {
            const item = this.findItemByRefdes(refdes);
            if (!item) continue;
            const halfH = (item.height || 1) / 2;
            // First badge sits ~1.5 mm above the bbox top edge; later
            // badges climb in 2 mm increments (matches SVG's 22-px stack
            // gap at typical zoom).
            for (let k = 0; k < group.length; k++) {
                const { step: st, displayIndex } = group[k];
                const isActive = st.id === currentId;
                const isDone   = st.status === 'done';
                const isFail   = st.status === 'failed';
                const isSkip   = st.status === 'skipped';
                const fill     = (isFail || isSkip) ? amber : cyan;
                const glyph    = isDone ? '✓'
                              : isFail ? '✗'
                              : isSkip ? '·'
                              : String(displayIndex);

                // Canvas-texture pattern (same as addAnnotation): a small
                // square texture, drawn with DPR scaling, then mapped to
                // a Sprite sized in world (mm) units.
                const dpr = Math.min(window.devicePixelRatio || 1, 2);
                const canvasSize = 64;
                const canvas = document.createElement('canvas');
                canvas.width = canvasSize * dpr;
                canvas.height = canvasSize * dpr;
                const ctx = canvas.getContext('2d');
                ctx.scale(dpr, dpr);

                const cx = canvasSize / 2;
                const cy = canvasSize / 2;

                // Active step: outer halo ring before the solid disc, so
                // it reads as "this is the one to do now" at a glance.
                if (isActive) {
                    ctx.fillStyle = fill;
                    ctx.globalAlpha = 0.35;
                    ctx.beginPath();
                    ctx.arc(cx, cy, 22, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.globalAlpha = 1.0;
                }

                ctx.fillStyle = fill;
                if (isDone && !isActive) ctx.globalAlpha = 0.7;
                ctx.beginPath();
                ctx.arc(cx, cy, 14, 0, Math.PI * 2);
                ctx.fill();
                ctx.globalAlpha = 1.0;

                ctx.fillStyle = bgDeep;
                ctx.font = "600 18px 'JetBrains Mono', monospace";
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(glyph, cx, cy + 1);

                const texture = new THREE.CanvasTexture(canvas);
                texture.minFilter = THREE.LinearFilter;
                texture.magFilter = THREE.LinearFilter;
                const material = new THREE.SpriteMaterial({
                    map: texture,
                    transparent: true,
                    depthTest: false,
                });
                const sprite = new THREE.Sprite(material);

                // Stack offset: 2 mm per slot above the bbox top.
                const offsetY = halfH + 1.5 + k * 2.0;
                sprite.position.set(item.x, item.y + offsetY, 9);
                // Badge size in world (mm) units. ~2 mm wide reads at
                // typical diagnostic zoom without dwarfing 0402s.
                sprite.scale.set(2.2, 2.2, 1);
                sprite.userData = { _protocolStep: st.id, _side: item._side || null };
                this.scene.add(sprite);
                this._protocolBadges.set(st.id, { sprite, refdes });
            }
        }
        this.requestRender();
    }

    /**
     * Drop every protocol badge sprite, dispose textures, and clear the
     * tracking Map. Called by setProtocolBadges before a repaint and by
     * protocol.js when the protocol ends / aborts.
     */
    clearProtocolBadges() {
        for (const [, entry] of this._protocolBadges) {
            this.scene.remove(entry.sprite);
            if (entry.sprite.material) {
                if (entry.sprite.material.map) entry.sprite.material.map.dispose();
                entry.sprite.material.dispose();
            }
        }
        this._protocolBadges.clear();
        this.requestRender();
    }

    /**
     * Project a refdes's part bbox-top centre through the camera and
     * return the result in viewport (page) pixel coordinates so callers
     * (e.g. protocol.js's floating refdes chip) can position a CSS
     * absolute-positioned element on top of the WebGL canvas. Returns
     * null when the refdes is not in the current board, when the part
     * is on the hidden face, or when the projected NDC is outside
     * [-1, 1] (off-screen).
     */
    refdesScreenPos(refdes) {
        if (!refdes) return null;
        const item = this.findItemByRefdes(refdes);
        if (!item) return null;
        // Hidden-face culling: when the user has constrained to one side,
        // the part is invisible and a screen-pos answer would point at
        // empty space. brd_viewer.js skips badge rendering on the hidden
        // side too — same rule here.
        if (item._side && this.sideMode !== 'both' && item._side !== this.sideMode) {
            return null;
        }
        // Build a world-space point at the bbox top-centre. Items live
        // in scene-local coords; the scene rotation (this.scene.rotation.z)
        // is baked into world by Three.js's matrix update on render, so
        // we let `Vector3.project(camera)` walk the full chain.
        const halfH = (item.height || 0) / 2;
        const localX = item.x;
        const localY = item.y + halfH;
        // Apply scene rotation manually since the scene's matrix may not
        // be up to date yet on the very first frame after a setSideMode
        // / rotate. updateMatrixWorld() makes the projection robust.
        this.scene.updateMatrixWorld();
        const v = new THREE.Vector3(localX, localY, 0);
        v.applyMatrix4(this.scene.matrixWorld);
        v.project(this.camera);
        // Off-screen guard — treat NDC outside [-1, 1] as "not visible"
        // so callers can hide the chip rather than pinning it to a canvas
        // edge.
        if (v.x < -1 || v.x > 1 || v.y < -1 || v.y > 1) return null;
        const rect = this.canvas.getBoundingClientRect();
        const x = rect.left + (v.x * 0.5 + 0.5) * rect.width;
        const y = rect.top  + (v.y * -0.5 + 0.5) * rect.height;
        return { x, y };
    }

    /**
     * Highlight every pin on `netName` and dim the rest. Uses the
     * existing user-side highlightNet() pipeline by selecting an
     * arbitrary pin on the net first, then triggering the same code
     * path the inspector net-row click runs. Falls back to a no-op if
     * no pin matches the name.
     */
    highlightNetByName(netName) {
        if (!netName) return;
        const target = String(netName).trim();
        if (!target || target === 'NC') return;
        // Find the first pin / test-pad on this net and route through
        // the existing select+highlight chain so the dim + fly-line +
        // trace-recolour pipeline runs identically to a user click.
        const anchor = (this._hoverableItems || []).find((it) => {
            const t = it._instanceType;
            return (t === 'pin' || t === 'rectPin' || t === 'testPad')
                && it.net === target;
        });
        if (!anchor) return;
        if (typeof this.selectItem === 'function') {
            this.selectItem(anchor);
        }
        if (typeof this.highlightNet === 'function') {
            this.highlightNet();
        }
        this.requestRender();
    }

    /**
     * Move the camera to a specific pin position (mils → mm conversion
     * already applied by the caller) and select the parent component
     * so the inspector populates and the cyan halo paints. Used by
     * bv_show_pin to direct the tech to a specific probe point.
     */
    showPinAt(refdes, posMm) {
        if (!refdes) return;
        const item = this.findItemByRefdes(refdes);
        if (!item) return;
        if (typeof this.selectItem === 'function') {
            this.selectItem(item);
        }
        // Centre on the pin position when supplied, otherwise on the
        // component centre. Tighten zoom so the pin reads clearly.
        const cx = (posMm && Number.isFinite(posMm.x)) ? posMm.x : item.x;
        const cy = (posMm && Number.isFinite(posMm.y)) ? posMm.y : item.y;
        this.camera.position.x = cx;
        this.camera.position.y = cy;
        const longSide = Math.max(item.width || 4, item.height || 4);
        this.frustumSize = Math.max(longSide * 8, 12);
        this.zoom = 100 / this.frustumSize;
        if (this.onResize) this.onResize();
        this.requestRender();
    }

    /**
     * Draw a directional arrow from one component centre to another in
     * world coordinates (mm). Tracks the resulting Group under `id` so
     * a future event or reset_view can drop it cleanly. Renders the
     * shaft as a Line and the head as two short converging Lines so it
     * stays sharp under the orthographic camera at any zoom.
     */
    addAgentArrow(fromMm, toMm, id) {
        if (!id) id = `arr-${Math.random().toString(36).slice(2, 10)}`;
        this.removeAgentArrow(id);
        if (!fromMm || !toMm) return;
        const dx = toMm.x - fromMm.x;
        const dy = toMm.y - fromMm.y;
        const len = Math.hypot(dx, dy);
        if (len < 0.01) return;
        const ux = dx / len;
        const uy = dy / len;
        const headLen = Math.min(Math.max(len * 0.12, 1.5), 4);
        const halfHead = headLen * 0.45;
        // Stop the shaft at the base of the arrowhead so the head's V
        // sits cleanly without overlapping a thick shaft tip.
        const tipBaseX = toMm.x - ux * headLen;
        const tipBaseY = toMm.y - uy * headLen;
        // Perpendicular for the head's two flanks.
        const px = -uy;
        const py = ux;

        const color = 0xc084fc;  // violet — matches the action / arrow family
        const mat = new THREE.LineBasicMaterial({
            color,
            transparent: true,
            opacity: 0.95,
            depthTest: false,
        });
        const group = new THREE.Group();

        const shaftGeom = new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(fromMm.x, fromMm.y, 6),
            new THREE.Vector3(tipBaseX, tipBaseY, 6),
        ]);
        group.add(new THREE.Line(shaftGeom, mat));

        const headGeom = new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(tipBaseX + px * halfHead, tipBaseY + py * halfHead, 6),
            new THREE.Vector3(toMm.x, toMm.y, 6),
            new THREE.Vector3(tipBaseX - px * halfHead, tipBaseY - py * halfHead, 6),
        ]);
        group.add(new THREE.Line(headGeom, mat));

        group.userData = { _agentArrow: id };
        this.scene.add(group);
        this._agentArrows.set(id, group);
        this.requestRender();
    }

    removeAgentArrow(id) {
        const group = this._agentArrows.get(id);
        if (!group) return;
        group.traverse((obj) => {
            if (obj.geometry) obj.geometry.dispose();
            if (obj.material) obj.material.dispose();
        });
        this.scene.remove(group);
        this._agentArrows.delete(id);
        this.requestRender();
    }

    clearAgentArrows() {
        for (const id of Array.from(this._agentArrows.keys())) {
            this.removeAgentArrow(id);
        }
    }

    /**
     * Draw a measurement line + distance label between two components.
     * Stored under `id` for later removal. The label text is provided
     * by the caller (typically the agent already computed mm and ships
     * it in the WS event), so this method just renders.
     */
    addMeasurement(fromRefdes, toRefdes, label, id) {
        if (!id) id = `mes-${Math.random().toString(36).slice(2, 10)}`;
        this.removeMeasurement(id);
        const a = this.findItemByRefdes(fromRefdes);
        const b = this.findItemByRefdes(toRefdes);
        if (!a || !b) return;

        const color = 0xfbbf24;  // amber — measurement / informational
        const mat = new THREE.LineBasicMaterial({
            color, transparent: true, opacity: 0.9, depthTest: false,
        });
        const group = new THREE.Group();
        const lineGeom = new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(a.x, a.y, 5),
            new THREE.Vector3(b.x, b.y, 5),
        ]);
        group.add(new THREE.Line(lineGeom, mat));

        const text = String(label || '').trim();
        if (text) {
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const canvas = document.createElement('canvas');
            canvas.width = 256 * dpr;
            canvas.height = 64 * dpr;
            const ctx = canvas.getContext('2d');
            ctx.scale(dpr, dpr);
            ctx.font = "600 26px 'JetBrains Mono', monospace";
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            const metrics = ctx.measureText(text);
            const tw = Math.min(metrics.width, 232) + 20;
            ctx.fillStyle = 'rgba(7, 16, 31, 0.9)';
            ctx.strokeStyle = '#fbbf24';
            ctx.lineWidth = 1.4;
            const bx = 128 - tw / 2, by = 32 - 16, th = 32, r = 5;
            ctx.beginPath();
            ctx.moveTo(bx + r, by);
            ctx.lineTo(bx + tw - r, by);
            ctx.quadraticCurveTo(bx + tw, by, bx + tw, by + r);
            ctx.lineTo(bx + tw, by + th - r);
            ctx.quadraticCurveTo(bx + tw, by + th, bx + tw - r, by + th);
            ctx.lineTo(bx + r, by + th);
            ctx.quadraticCurveTo(bx, by + th, bx, by + th - r);
            ctx.lineTo(bx, by + r);
            ctx.quadraticCurveTo(bx, by, bx + r, by);
            ctx.closePath();
            ctx.fill();
            ctx.stroke();
            ctx.fillStyle = '#fde68a';
            ctx.fillText(text, 128, 32, 232);

            const texture = new THREE.CanvasTexture(canvas);
            texture.minFilter = THREE.LinearFilter;
            texture.magFilter = THREE.LinearFilter;
            const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
                map: texture, transparent: true, depthTest: false,
            }));
            sprite.position.set((a.x + b.x) / 2, (a.y + b.y) / 2, 7);
            const aspect = canvas.width / canvas.height;
            const baseH = 1.2;
            sprite.scale.set(baseH * aspect, baseH, 1);
            group.add(sprite);
        }

        group.userData = { _agentMeasurement: id };
        this.scene.add(group);
        this._agentMeasurements.set(id, group);
        this.requestRender();
    }

    removeMeasurement(id) {
        const group = this._agentMeasurements.get(id);
        if (!group) return;
        group.traverse((obj) => {
            if (obj.geometry) obj.geometry.dispose();
            if (obj.material) {
                if (obj.material.map) obj.material.map.dispose();
                obj.material.dispose();
            }
        });
        this.scene.remove(group);
        this._agentMeasurements.delete(id);
        this.requestRender();
    }

    clearAgentMeasurements() {
        for (const id of Array.from(this._agentMeasurements.keys())) {
            this.removeMeasurement(id);
        }
    }

    /**
     * Apply / clear a refdes-prefix filter. When set, every component
     * whose id does not start with the prefix has its outline group
     * hidden, its refdes label sprite hidden, and its pins zero-scaled
     * out of the shared InstancedMesh slots (same idiom the DNP toggle
     * uses — see `_applySideToInstanced`). Filter axes compose with AND:
     * a pin is visible iff (side OK) AND (DNP OK) AND (refdes prefix OK).
     * Clearing the filter restores all three classes from their saved
     * `_matrices` / per-sprite zoom-aware visibility path.
     */
    setRefdesFilter(prefix) {
        const p = prefix ? String(prefix).trim().toUpperCase() : null;
        this._agentFilterPrefix = p || null;
        this._applyRefdesFilter();
        this.requestRender();
    }

    _applyRefdesFilter() {
        const p = this._agentFilterPrefix;
        const allow = (id) => !p || (id && id.toUpperCase().startsWith(p));
        // Component groups
        if (this.meshGroups && Array.isArray(this.meshGroups.components)) {
            for (const g of this.meshGroups.components) {
                const ud = g.userData || {};
                // Honour the side filter as before — only override the
                // visibility on the prefix axis when the side filter
                // would have allowed the part.
                const sideOk = (this.sideMode === 'both' || !ud._side
                    || ud._side === this.sideMode);
                g.visible = sideOk && allow(ud.id);
            }
        }
        // Pin / pad InstancedMeshes — `_applySideToInstanced` now ANDs
        // in `this._agentFilterPrefix` against each slot's stored
        // `_components[i]`, so a single pass per mesh recomputes the
        // composite (side AND DNP AND refdes) visibility. Same idiom as
        // setShowDnp uses.
        this._applySideToInstanced(this._circularPinInstance);
        this._applySideToInstanced(this._circularPinBorderInstance);
        if (this._rectPinInstances) {
            this._rectPinInstances.forEach(({ body }) => {
                this._applySideToInstanced(body);
            });
        }
        this._applySideToInstanced(this._testPadsInstance);
        if (this._testPadsBorderInstance) {
            this._applySideToInstanced(this._testPadsBorderInstance);
        }
        // Per-pin rect borders are individual Lines — flip their
        // visibility against the composite (side + DNP + refdes) gate.
        if (Array.isArray(this._pinBorderLines)) {
            const sideAllow = (side) => this.sideMode === 'both'
                || side == null
                || side === this.sideMode;
            for (const m of this._pinBorderLines) {
                const ud = m.userData || {};
                const sideOk = sideAllow(ud._side);
                const dnpOk = !ud._isDnp || this._showDnp;
                const refOk = allow(ud._component);
                m.visible = sideOk && dnpOk && refOk;
            }
        }
        // Refdes labels — gate by side AND prefix. The zoom-aware
        // updater below decides the final visibility based on screen
        // size, so we delegate to it whenever the prefix permits the
        // sprite (it already honours side + DNP + zoom).
        if (Array.isArray(this._componentLabels)) {
            for (const sprite of this._componentLabels) {
                const ud = sprite.userData || {};
                if (!allow(ud._refdes)) {
                    sprite.visible = false;
                }
            }
            if (typeof this._updateComponentLabelVisibility === 'function') {
                this._updateComponentLabelVisibility();
            }
        }
    }

    /**
     * Dim every component / pin not currently selected or highlighted
     * by the agent. The current `selectedItem` and any ongoing net
     * highlight stay full-bright. Mirrors the legacy SVG dim_unrelated
     * behaviour at the InstancedMesh level by lowering the unrelated
     * components' group opacity. Pins fall under the existing
     * `_dimUnrelatedPins(netName)` path when a net is active; with no
     * net this is a body-only dim, which is the SVG renderer's
     * behaviour too.
     */
    dimUnrelated() {
        this._agentDimActive = true;
        // Components: drop opacity on every group whose id is not the
        // selected item AND not on the current net's anchor refdes.
        const selId = this.selectedItem ? this.selectedItem.id : null;
        const selRefdes = this.selectedItem
            ? (this.selectedItem.component || this.selectedItem.id)
            : null;
        if (this.meshGroups && Array.isArray(this.meshGroups.components)) {
            for (const g of this.meshGroups.components) {
                const ud = g.userData || {};
                const keep = (ud.id === selId) || (ud.id === selRefdes);
                g.traverse((m) => {
                    if (!m.material) return;
                    // First time we touch the material, snapshot the
                    // pre-dim state so clearDim can restore precisely.
                    if (m.material._origOpacity == null) {
                        m.material._origOpacity = m.material.opacity == null ? 1 : m.material.opacity;
                        m.material._origTransparent = !!m.material.transparent;
                    }
                    if (!keep) {
                        m.material.transparent = true;
                        m.material.opacity = 0.18;
                    } else {
                        m.material.transparent = m.material._origTransparent;
                        m.material.opacity = m.material._origOpacity;
                    }
                    m.material.needsUpdate = true;
                });
            }
        }
        this.requestRender();
    }

    clearDim() {
        if (!this._agentDimActive) return;
        this._agentDimActive = false;
        if (this.meshGroups && Array.isArray(this.meshGroups.components)) {
            for (const g of this.meshGroups.components) {
                g.traverse((m) => {
                    if (!m.material) return;
                    if (m.material._origOpacity != null) {
                        m.material.opacity = m.material._origOpacity;
                        m.material.transparent = !!m.material._origTransparent;
                        delete m.material._origOpacity;
                        delete m.material._origTransparent;
                        m.material.needsUpdate = true;
                    }
                });
            }
        }
        this.requestRender();
    }

    /**
     * Toggle a single layer's visibility. The viewer's primary
     * face filter is `setSideMode('top'|'bottom'|'both')`, so map
     * (layer, visible) → side mode by treating "show top, hide
     * bottom" / "hide top, show bottom" as a side flip and otherwise
     * returning to 'both'.
     */
    setLayerVisibility(layer, visible) {
        if (layer !== 'top' && layer !== 'bottom') return;
        const other = layer === 'top' ? 'bottom' : 'top';
        // Maintain a per-layer flag so two consecutive calls (e.g. hide
        // top, then hide bottom) collapse to the right composite mode.
        if (!this._layerVisible) this._layerVisible = { top: true, bottom: true };
        this._layerVisible[layer] = !!visible;
        const anyVisible = this._layerVisible.top || this._layerVisible.bottom;
        if (!anyVisible) {
            // Both hidden — fall back to 'both' so the user isn't
            // staring at an empty canvas. This is a degenerate request.
            this.setSideMode('both');
            return;
        }
        if (this._layerVisible.top && this._layerVisible.bottom) {
            this.setSideMode('both');
        } else {
            this.setSideMode(this._layerVisible.top ? 'top' : 'bottom');
        }
    }

    /**
     * Reset every agent-driven overlay in one shot. Called by the
     * bv_reset_view path. Leaves the user's selection, the side mode,
     * and the camera position intact — the SVG renderer's reset has
     * the same scope.
     */
    resetAgentOverlays() {
        this.clearAgentAnnotations();
        this.clearAgentArrows();
        this.clearAgentMeasurements();
        this.clearProtocolBadges();
        this.clearDim();
        if (this._agentFilterPrefix) {
            this.setRefdesFilter(null);
        }
    }
}

// Export for use — bridge consumes via window.PCBViewerOptimized.
if (typeof window !== 'undefined') {
    window.PCBViewerOptimized = PCBViewerOptimized;
}
if (typeof module !== 'undefined' && module.exports) {
    module.exports = PCBViewerOptimized;
}
