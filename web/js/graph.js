// Knowledge-graph canvas: data loader, empty-state toggle, and the D3
// force simulation that renders nodes/links/filters/tweaks/inspector
// inside #canvas. Relies on d3 being available as a global (loaded via
// the CDN <script> in index.html).

/* =========================================================
   GRAPH DATA — loaded at runtime from the backend.
   TODO: wire to `GET /pipeline/packs/{slug}/graph` once that
   endpoint exists. For now the graph starts empty and the UI
   shows an empty-state card inviting the user to run the
   pipeline. Shape must match the Pydantic v2 contract
   (component | symptom | net | action) + typed relations
   (causes | powers | connected_to | resolves).
   ========================================================= */
let DATA = { nodes: [], edges: [] };

export async function loadGraphFromBackend() {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  if (!slug) return null;
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}/graph`);
    if (!res.ok) {
      console.warn(`loadGraphFromBackend: ${res.status} for slug=${slug}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error("loadGraphFromBackend: fetch failed", err);
    return null;
  }
}

export function setEmptyState(visible) {
  const el = document.getElementById("emptyState");
  if (!el) return;
  el.classList.toggle("hidden", !visible);
}

setEmptyState(true);  // show the card synchronously; the fetch may replace it

const TYPE_COLORS = { component:"oklch(0.82 0.14 210)", symptom:"oklch(0.82 0.16 75)", net:"oklch(0.78 0.15 155)", action:"oklch(0.78 0.14 295)" };
const TYPE_FILL   = { component:"oklch(0.82 0.14 210 / 0.22)", symptom:"oklch(0.82 0.16 75 / 0.22)", net:"oklch(0.78 0.15 155 / 0.22)", action:"oklch(0.78 0.14 295 / 0.22)" };
const TYPE_GLOW   = { component:"glow-cyan", symptom:"glow-amber", net:"glow-emerald", action:"glow-violet" };
const TYPE_LABEL_FR = { component:"Composant", symptom:"Symptôme", net:"Net / Rail", action:"Action" };
const REL_LABEL_FR  = { causes:"provoque", powers:"alimente", connected_to:"connecté à", resolves:"résout" };
// Strict L→R diagnostic narrative — must match the visible .col-band order
// in the HTML. Reads problem-first: symptom → net → component → action.
const COL_ORDER = ["symptom","net","component","action"];

export function initGraphWithData(data) {
  DATA = data;

  const svg = d3.select("#graph");
const gRoot = d3.select("#graphRoot");
const canvasEl = document.getElementById("canvas");
const W = () => canvasEl.clientWidth;
const H = () => canvasEl.clientHeight;

// populate per-type counts in the legend panel (compact mono chips — no
// " nœuds" suffix; the legend copy already makes the meaning clear).
["sym","cmp","net","act"].forEach((k)=>{
  const map={sym:"symptom",cmp:"component",net:"net",act:"action"};
  const el = document.getElementById("cnt-"+k);
  if (el) el.textContent = DATA.nodes.filter(n=>n.type===map[k]).length;
});
// #counts / #avgConf used to live in a now-removed statusbar — guard so
// the graph loader doesn't throw on pages without those elements.
document.getElementById("counts")?.replaceChildren(document.createTextNode(
  `${DATA.nodes.length} nœuds · ${DATA.edges.length} arêtes`
));
const avgConfEl = document.getElementById("avgConf");
if (avgConfEl && DATA.nodes.length > 0) {
  avgConfEl.textContent = (DATA.nodes.reduce((a,n)=>a+n.confidence,0)/DATA.nodes.length).toFixed(2);
}

function nodeSize(n){ return 18 + n.confidence*8; }

// degrees + neighbors
const neighbors={};
DATA.edges.forEach(e=>{
  (neighbors[e.source] ||= new Set()).add(e.target);
  (neighbors[e.target] ||= new Set()).add(e.source);
});

/* ---------- BUBBLE MAP LAYOUT ----------
   Every subsystem is a circular "bubble" arranged by force simulation.
   Nodes are packed inside their bubble by d3.pack(), sized by confidence.
   No columns, no rows — just zones. Edges are hidden by default (see CSS)
   and surface only on hover/focus via the existing .active-link class. */

// Fallback when the backend didn't send subsystems (older payload / empty graph).
const SUBSYSTEMS = (Array.isArray(DATA.subsystems) && DATA.subsystems.length > 0)
  ? DATA.subsystems
  : [{ key: "unknown", label: "AUTRES", count: DATA.nodes.length }];

// Pad how much space each node claims inside its bubble (d3.pack padding).
const PACK_PADDING = 4;
// Flat pack weight — every node claims the same slot inside its bubble.
// Confidence is encoded via stroke/opacity at render time, not size.
function nodeWeight() { return 1; }
// Hard radius clamp on rendered nodes — stops a 1-node subsystem from
// ballooning into a 60-px disk because pack filled the whole bubble.
const NODE_R_MIN = 8;
const NODE_R_MAX = 18;

// Compute the radius each subsystem bubble wants. Proportional to the
// square root of its node count — fills the canvas non-linearly so one
// huge subsystem doesn't dwarf the others.
const bubbles = SUBSYSTEMS.map(s => {
  const nodes = DATA.nodes.filter(n => n.subsystem === s.key);
  if (nodes.length === 0) return null;
  // k tunes the bubble "scale" relative to canvas — tune if canvas feels too sparse/crammed.
  const k = 12;
  const radius = Math.max(36, Math.sqrt(nodes.length) * k);
  return { key: s.key, label: s.label, nodes, radius };
}).filter(Boolean);

// Pack nodes inside each bubble via d3.hierarchy + d3.pack, two levels
// deep so nodes cluster by TYPE inside each subsystem (symptom /net/
// component/action each form their own sub-cluster instead of mixing).
// The intermediate "type group" objects carry a `children` key; d3.pack
// treats them as containers whose size equals the sum of their children.
for (const b of bubbles) {
  const byType = new Map(COL_ORDER.map(t => [t, []]));
  for (const n of b.nodes) {
    if (byType.has(n.type)) byType.get(n.type).push(n);
  }
  const children = [...byType.entries()]
    .filter(([, arr]) => arr.length > 0)
    .map(([type, arr]) => ({ __type: type, children: arr }));
  // sum() callback: internal nodes (have children) contribute 0, leaves
  // (actual graph nodes, no children) contribute their weight. d3 adds
  // children's values to get each group's total.
  const root = d3.hierarchy({ children })
    .sum(d => (d.children ? 0 : nodeWeight(d)));
  const pack = d3.pack().size([b.radius * 2, b.radius * 2]).padding(PACK_PADDING);
  pack(root);
  b.leaves = root.leaves().map(leaf => ({
    node: leaf.data,
    relX: leaf.x - b.radius,
    relY: leaf.y - b.radius,
    r:    leaf.r,
  }));
}

// Arrange bubbles on the canvas via a tiny force simulation — forceCenter
// keeps the cluster centred, forceCollide prevents overlap.
const W_fn = W, H_fn = H;   // alias to keep the sim declaration tight
const bubbleSim = d3.forceSimulation(bubbles)
  .force("center", d3.forceCenter(W_fn() / 2, H_fn() / 2))
  .force("collide", d3.forceCollide(d => d.radius + 12).iterations(4))
  .force("x", d3.forceX(W_fn() / 2).strength(0.04))
  .force("y", d3.forceY(H_fn() / 2).strength(0.04))
  .stop();
// Seed bubble positions on a circle so the sim converges stably.
{
  const n = bubbles.length;
  const cx = W_fn() / 2, cy = H_fn() / 2;
  const seedR = Math.min(W_fn(), H_fn()) * 0.28;
  bubbles.forEach((b, i) => {
    const a = (i / Math.max(1, n)) * Math.PI * 2 - Math.PI / 2;
    b.x = cx + Math.cos(a) * seedR;
    b.y = cy + Math.sin(a) * seedR;
  });
}
for (let i = 0; i < 200; i++) bubbleSim.tick();

// Apply final positions to every node via the pre-computed pack offsets.
for (const b of bubbles) {
  for (const leaf of b.leaves) {
    const n = leaf.node;
    n._tx = b.x + leaf.relX;
    n._ty = b.y + leaf.relY;
    n._r  = leaf.r;  // actual pack-computed radius
  }
}
// Sync for the D3 node-force sim.
DATA.nodes.forEach(n => { n.x = n._tx; n.y = n._ty; });

/* ---------- BUBBLE BACKDROPS ---------- */
const bandLayer = d3.select("#layerBands");
bandLayer.selectAll("*").remove();
for (const b of bubbles) {
  bandLayer.append("circle")
    .attr("class", "bubble-bg")
    .attr("cx", b.x).attr("cy", b.y).attr("r", b.radius);
  bandLayer.append("text")
    .attr("class", "band-label")
    .attr("x", b.x).attr("y", b.y - b.radius - 10)
    .attr("text-anchor", "middle")
    .text(b.label);
}

/* ---------- FORCE SIM — gentle, mostly positional ---------- */
const sim = d3.forceSimulation(DATA.nodes)
  .force("link", d3.forceLink(DATA.edges).id(d => d.id).distance(120).strength(0.01))
  .force("x", d3.forceX(d => d._tx).strength(0.9))
  .force("y", d3.forceY(d => d._ty).strength(0.9))
  .alphaDecay(0.1)
  .velocityDecay(0.6);

/* ---------- LINKS ---------- */
const linkSel = d3.select("#layerLinks").selectAll("path")
  .data(DATA.edges)
  .join("path")
  .attr("class", d => `link ${d.relation}`)
  .attr("stroke-width", d => 0.8 + (d.weight || 0.5) * 1.8)
  .attr("marker-end", d => {
    if (d.relation==="causes")       return "url(#arrow-causes)";
    if (d.relation==="powers")       return "url(#arrow-powers)";
    if (d.relation==="connected_to") return "url(#arrow-connected)";
    if (d.relation==="resolves")     return "url(#arrow-resolves)";
    return "url(#arrow-connected)";
  });

const linkLabelSel = d3.select("#layerLinkLabels").selectAll("text")
  .data(DATA.edges)
  .join("text")
  .attr("class","link-label")
  .text(d => d.label);

/* ---------- NODES ---------- */
const nodeSel = d3.select("#layerNodes").selectAll("g.node")
  .data(DATA.nodes, d=>d.id)
  .join("g")
  .attr("class", d => `node type-${d.type}`)
  .call(d3.drag()
    .on("start",(e,d)=>{ if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
    .on("drag", (e,d)=>{ d.fx=e.x; d.fy=e.y; })
    .on("end",  (e,d)=>{ if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));

nodeSel.each(function(d){
  const g = d3.select(this);
  // Clamp rendered radius to keep sparse-bubble nodes from ballooning.
  const rawR = d._r || nodeSize(d);
  const r = Math.min(NODE_R_MAX, Math.max(NODE_R_MIN, rawR));
  const opacity = 0.6 + d.confidence * 0.4;

  g.append("circle").attr("class","conf-ring").attr("r", r+2).attr("fill","none")
    .attr("stroke", TYPE_COLORS[d.type]).attr("stroke-opacity", d.confidence*0.3)
    .attr("stroke-width", 1 + d.confidence*1.2).attr("filter", `url(#${TYPE_GLOW[d.type]})`);

  // Single circle per node — type encoded entirely by fill+stroke.
  g.append("circle").attr("class","node-shape").attr("r", r)
    .attr("fill", TYPE_FILL[d.type]).attr("stroke", TYPE_COLORS[d.type])
    .attr("stroke-opacity", opacity).attr("stroke-width", 1.3);

  // Labels stay off by default in the bubble view; CSS fades them in on
  // hover/focus via the .node-label rule (see graph.css bubble block).
  // Positioned ABOVE the node — the cursor tooltip sits below+right of the
  // cursor, so putting the label on the opposite side keeps it visible.
  const shortLabel = d.label.length > 22 ? d.label.slice(0, 20) + "…" : d.label;
  g.append("text").attr("class","node-label").attr("dy", -(r + 6)).text(shortLabel);

  if (d.type === "action" && d.meta && d.meta.count > 1) {
    g.append("text")
      .attr("class", "collapse-badge")
      .attr("x", r + 3).attr("y", -r + 1)
      .text("×" + d.meta.count);
  }
});

/* ---------- Path: curved bezier ---------- */
function linkPath(d){
  const sx = d.source.x, sy = d.source.y, tx = d.target.x, ty = d.target.y;
  // Curved bezier for every relation. Pack scatter means orthogonal routing
  // is moot — a gentle curve that clears the source/target disks is enough.
  const dx = tx - sx, dy = ty - sy;
  const dist = Math.sqrt(dx * dx + dy * dy);
  const curve = Math.min(80, dist * 0.35);
  // Perpendicular offset for the control point.
  const nx = -dy / (dist || 1), ny = dx / (dist || 1);
  const cx = (sx + tx) / 2 + nx * curve;
  const cy = (sy + ty) / 2 + ny * curve;
  return `M${sx},${sy} Q${cx},${cy} ${tx},${ty}`;
}

sim.on("tick", () => {
  linkSel.attr("d", linkPath);
  nodeSel.attr("transform", d => `translate(${d.x},${d.y})`);
  linkLabelSel
    .attr("x", d => (d.source.x + d.target.x)/2)
    .attr("y", d => (d.source.y + d.target.y)/2 - 6);
});

/* ---------- ZOOM ---------- */
const zoom = d3.zoom().scaleExtent([0.3,3])
  .on("zoom", (e) => {
    gRoot.attr("transform", e.transform);
    const zp = document.getElementById("zoomPct");
    if (zp) zp.textContent = Math.round(e.transform.k*100)+"%";
    const zr = document.getElementById("zoomReadout");
    if (zr) zr.textContent = `zoom ${e.transform.k.toFixed(2)}×`;
  });
svg.call(zoom).on("dblclick.zoom", null);
document.getElementById("zoomIn").onclick  = () => svg.transition().duration(200).call(zoom.scaleBy, 1.3);
document.getElementById("zoomOut").onclick = () => svg.transition().duration(200).call(zoom.scaleBy, 1/1.3);
document.getElementById("zoomFit").onclick = fitToScreen;

function fitToScreen(){
  if (DATA.nodes.length === 0) return;  // nothing to fit when the graph is empty
  const xs=DATA.nodes.map(n=>n.x), ys=DATA.nodes.map(n=>n.y);
  const minX=Math.min(...xs), maxX=Math.max(...xs);
  const minY=Math.min(...ys), maxY=Math.max(...ys);
  const pad=80, w=(maxX-minX)+pad*2, h=(maxY-minY)+pad*2;
  const k = Math.min(W()/w, H()/h, 1.3);
  const tx = W()/2 - k*(minX + (maxX-minX)/2);
  const ty = H()/2 - k*(minY + (maxY-minY)/2);
  svg.transition().duration(400).call(zoom.transform, d3.zoomIdentity.translate(tx,ty).scale(k));
}

/* ---------- HOVER / SELECTION ---------- */
const tooltip = document.getElementById("tooltip");
let selected = null;

nodeSel
  .on("mouseenter", (e,d) => {
    gRoot.classed("has-focus", true);
    const nb = neighbors[d.id] || new Set();
    nodeSel.classed("focus", n => n.id === d.id).classed("neighbor", n => nb.has(n.id));
    linkSel.classed("active-link", e => e.source.id === d.id || e.target.id === d.id);
    linkLabelSel.classed("active-label", e => e.source.id === d.id || e.target.id === d.id);

    tooltip.classList.add("show");
    document.getElementById("ttType").textContent = TYPE_LABEL_FR[d.type];
    document.getElementById("ttLabel").textContent = d.label;
    document.getElementById("ttDesc").textContent  = (d.description||"").length>130 ? d.description.slice(0,130)+"…" : d.description;
    document.getElementById("ttId").textContent    = d.id;
    document.getElementById("ttConf").textContent  = "conf. " + (d.confidence*100).toFixed(0) + "%";
  })
  .on("mousemove", (e) => {
    tooltip.style.left = (e.clientX+14)+"px";
    tooltip.style.top  = (e.clientY+14)+"px";
  })
  .on("mouseleave", () => {
    gRoot.classed("has-focus", selected!==null);
    if (selected){ const nb = neighbors[selected.id] || new Set();
      nodeSel.classed("focus", n => n.id === selected.id).classed("neighbor", n => nb.has(n.id));
      linkSel.classed("active-link", e => e.source.id === selected.id || e.target.id === selected.id);
      linkLabelSel.classed("active-label", e => e.source.id === selected.id || e.target.id === selected.id);
    } else {
      nodeSel.classed("focus", false).classed("neighbor", false);
      linkSel.classed("active-link", false);
      linkLabelSel.classed("active-label", false);
    }
    tooltip.classList.remove("show");
  })
  .on("click", (e,d) => { e.stopPropagation(); selectNode(d); });

canvasEl.addEventListener("click", e => {
  if (e.target===canvasEl || e.target.tagName==="svg" || e.target.classList.contains("grid-bg")) closeInspector();
});

/* ---------- INSPECTOR ---------- */
const inspector = document.getElementById("inspector");

function selectNode(d){
  selected = d;
  nodeSel.classed("selected", n => n.id === d.id);
  gRoot.classed("has-focus", true);
  const nb = neighbors[d.id] || new Set();
  nodeSel.classed("focus", n => n.id === d.id).classed("neighbor", n => nb.has(n.id));
  linkSel.classed("active-link", e => e.source.id === d.id || e.target.id === d.id);
  linkLabelSel.classed("active-label", e => e.source.id === d.id || e.target.id === d.id);

  const badge = document.getElementById("inspBadge");
  badge.className = "type-badge " + d.type;
  document.getElementById("inspBadgeText").textContent = TYPE_LABEL_FR[d.type];
  document.getElementById("inspTitle").textContent = d.label;
  document.getElementById("inspId").textContent = "id: " + d.id;
  const pct = Math.round(d.confidence*100);
  document.getElementById("confFill").style.width = pct + "%";
  document.getElementById("confValue").textContent = d.confidence.toFixed(2);
  let note = "Confiance élevée — corroboré par plusieurs sources.";
  if (d.confidence<0.6) note = "Confiance faible — source unique ou inférence indirecte.";
  else if (d.confidence<0.8) note = "Confiance modérée — recoupement partiel.";
  document.getElementById("confNote").textContent = note;
  document.getElementById("inspDesc").textContent = d.description || "—";

  const mg = document.getElementById("metaGrid"); mg.innerHTML="";
  const entries = Object.entries(d.meta || {});
  document.getElementById("metaSection").style.display = entries.length ? "" : "none";
  entries.forEach(([k,v]) => {
    const dt=document.createElement("dt"); dt.textContent=k;
    const dd=document.createElement("dd"); dd.textContent=v;
    mg.appendChild(dt); mg.appendChild(dd);
  });

  // When this node is a collapsed action, surface the list of merged rules.
  if (d.type === "action" && d.meta && Array.isArray(d.meta.rule_ids)) {
    const dt = document.createElement("dt"); dt.textContent = "rules";
    const dd = document.createElement("dd");
    dd.textContent = d.meta.rule_ids.join(", ");
    mg.appendChild(dt); mg.appendChild(dd);
    document.getElementById("metaSection").style.display = "";
  }

  const related = DATA.edges.filter(e => e.source.id===d.id || e.target.id===d.id);
  document.getElementById("edgeCount").textContent = `· ${related.length}`;
  const el = document.getElementById("edgeList"); el.innerHTML="";
  related.forEach(e => {
    const outgoing = e.source.id===d.id;
    const other = outgoing ? e.target : e.source;
    const row = document.createElement("div"); row.className="edge-item";
    row.innerHTML = `
      <span class="rel ${e.relation}">${REL_LABEL_FR[e.relation]}</span>
      <span class="arrow">${outgoing ? "→" : "←"}</span>
      <div class="edge-target">
        <div>${other.label}</div>
        <div class="edge-sub">${e.label} · poids ${(e.weight||1).toFixed(2)}</div>
      </div>`;
    row.onclick = () => selectNode(other);
    el.appendChild(row);
  });
  inspector.classList.add("open");
}
function closeInspector(){
  selected=null;
  nodeSel.classed("selected",false).classed("focus",false).classed("neighbor",false);
  gRoot.classed("has-focus",false);
  linkSel.classed("active-link",false);
  linkLabelSel.classed("active-label",false);
  inspector.classList.remove("open");
}
document.getElementById("inspectorClose").onclick = closeInspector;

/* ---------- PARTICLES on "powers" edges ---------- */
let particleSpeed = 1;
const powersEdges = DATA.edges.filter(e => e.relation==="powers");
const particles = [];
const pLayer = d3.select("#layerParticles");
powersEdges.forEach((e,i) => {
  for (let k=0;k<2;k++) particles.push({ edge:e, t:(i*0.2 + k*0.5)%1, speed:0.004 + Math.random()*0.002 });
});
const particleSel = pLayer.selectAll("circle").data(particles).join("circle")
  .attr("class","particle").attr("r",2.2).attr("fill","var(--emerald)")
  .attr("filter","drop-shadow(0 0 3px var(--emerald))");

function pointAlong(edge, t){
  // sample the S-curve by linear interp between source/target is close enough for vis
  return { x: edge.source.x + (edge.target.x - edge.source.x)*t,
           y: edge.source.y + (edge.target.y - edge.source.y)*t };
}
function animateParticles(){
  particles.forEach(p => { p.t += p.speed*particleSpeed; if (p.t>1) p.t=0; });
  particleSel.attr("cx", p => pointAlong(p.edge, p.t).x)
             .attr("cy", p => pointAlong(p.edge, p.t).y)
             .attr("opacity", p => selected ? ((p.edge.source.id===selected.id||p.edge.target.id===selected.id)?0.95:0.08) : 0.75);
  requestAnimationFrame(animateParticles);
}
requestAnimationFrame(animateParticles);

/* ---------- FILTERS (chips) ---------- */
const activeKinds = new Set(["symptom","component","net","action"]);
const activeRels = new Set(["causes","powers","connected_to","resolves"]);
let minConf = 0;

function applyFilters(){
  nodeSel.style("display", n => activeKinds.has(n.type) && n.confidence>=minConf ? null : "none");
  const edgeVisible = e =>
    activeKinds.has(e.source.type) && activeKinds.has(e.target.type) &&
    activeRels.has(e.relation) &&
    e.source.confidence>=minConf && e.target.confidence>=minConf;
  linkSel.style("display", e => edgeVisible(e) ? null : "none");
  linkLabelSel.style("display", e => edgeVisible(e) ? null : "none");
  particleSel.style("display", p => activeRels.has("powers") && edgeVisible(p.edge) ? null : "none");
}

document.querySelectorAll(".filter-chip").forEach(chip => {
  chip.onclick = () => {
    const k = chip.dataset.filter;
    if (activeKinds.has(k)) { activeKinds.delete(k); chip.classList.add("off"); }
    else { activeKinds.add(k); chip.classList.remove("off"); }
    applyFilters();
  };
});
document.querySelectorAll(".seg-rel").forEach(btn => {
  btn.onclick = () => {
    const r = btn.dataset.rel;
    if (activeRels.has(r)) { activeRels.delete(r); btn.classList.remove("on"); btn.style.opacity = "0.4"; }
    else { activeRels.add(r); btn.classList.add("on"); btn.style.opacity = "1"; }
    applyFilters();
    postEdit();
  };
});

/* ---------- SEARCH ---------- */
const searchInput = document.getElementById("searchInput");
searchInput.addEventListener("input", () => {
  const q = searchInput.value.trim().toLowerCase();
  if (!q) { nodeSel.style("opacity", null); return; }
  nodeSel.style("opacity", n => (n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q)) ? 1 : 0.15);
});
document.addEventListener("keydown", e => {
  if ((e.metaKey||e.ctrlKey) && e.key.toLowerCase()==="k"){ e.preventDefault(); searchInput.focus(); searchInput.select(); }
  if (e.key==="Escape") closeInspector();
});

/* ---------- TWEAKS ---------- */
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "labelMode": "hover",
  "minConfidence": 0,
  "particleSpeed": 1
}/*EDITMODE-END*/;

let labelMode = TWEAK_DEFAULTS.labelMode;
const tweaksPanel = document.getElementById("tweaksPanel");
document.getElementById("tweaksToggle").onclick = () => tweaksPanel.classList.toggle("show");
document.getElementById("tweaksClose").onclick  = () => tweaksPanel.classList.remove("show");

function postEdit(){
  const edits = {
    labelMode,
    minConfidence: parseFloat(document.getElementById("tConf").value),
    particleSpeed: parseFloat(document.getElementById("tParticle").value),
  };
  try { window.parent.postMessage({type:"__edit_mode_set_keys", edits}, "*"); } catch(e){}
}

document.getElementById("tConf").addEventListener("input", e => {
  minConf = parseFloat(e.target.value);
  document.getElementById("tConfVal").textContent = minConf.toFixed(2);
  applyFilters(); postEdit();
});
document.getElementById("tParticle").addEventListener("input", e => {
  particleSpeed = parseFloat(e.target.value);
  document.getElementById("tParticleVal").textContent = particleSpeed.toFixed(1) + "×";
  postEdit();
});
document.querySelectorAll("#tLabels button").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll("#tLabels button").forEach(x=>x.classList.remove("on"));
    b.classList.add("on");
    labelMode = b.dataset.val;
    if (labelMode==="all") linkLabelSel.style("opacity", 1);
    else if (labelMode==="none") linkLabelSel.style("opacity", 0);
    else linkLabelSel.style("opacity", null);
    postEdit();
  };
});
// default: hover mode
linkLabelSel.style("opacity", null);

window.addEventListener("message", e => {
  if (!e.data || typeof e.data!=="object") return;
  if (e.data.type==="__activate_edit_mode") tweaksPanel.classList.add("show");
  if (e.data.type==="__deactivate_edit_mode") tweaksPanel.classList.remove("show");
});
try { window.parent.postMessage({type:"__edit_mode_available"}, "*"); } catch(e){}

/* ---------- RESIZE ---------- */
// Bubble positions are absolute (pre-computed via bubbleSim + pack).
// On resize, just nudge the sim so nodes re-clamp to their _tx/_ty targets.
window.addEventListener("resize", () => {
  sim.alpha(0.3).restart();
});

  sim.alpha(1).restart();
  for (let i=0;i<80;i++) sim.tick();
  linkSel.attr("d", linkPath);
  nodeSel.attr("transform", d => `translate(${d.x},${d.y})`);
  fitToScreen();
}
