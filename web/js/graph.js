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
// Strict L→R narrative — must match the visible .col-band order in the HTML.
const COL_ORDER = ["action","component","net","symptom"];

export function initGraphWithData(data) {
  DATA = data;

  const svg = d3.select("#graph");
const gRoot = d3.select("#graphRoot");
const canvasEl = document.getElementById("canvas");
const W = () => canvasEl.clientWidth;
const H = () => canvasEl.clientHeight;

// populate counts
["sym","cmp","net","act"].forEach((k,i)=>{
  const map={sym:"symptom",cmp:"component",net:"net",act:"action"};
  document.getElementById("cnt-"+k).textContent = DATA.nodes.filter(n=>n.type===map[k]).length + " nœuds";
});
document.getElementById("counts").textContent = `${DATA.nodes.length} nœuds · ${DATA.edges.length} arêtes`;
document.getElementById("avgConf").textContent = (DATA.nodes.reduce((a,n)=>a+n.confidence,0)/DATA.nodes.length).toFixed(2);

function nodeSize(n){ return 18 + n.confidence*8; }

// degrees + neighbors
const neighbors={};
DATA.edges.forEach(e=>{
  (neighbors[e.source] ||= new Set()).add(e.target);
  (neighbors[e.target] ||= new Set()).add(e.source);
});

/* ---------- COLUMN LAYOUT ---------- */
// Assign each node to a column (its type), then vertically sort by confidence desc.
function columnIndex(type){ return COL_ORDER.indexOf(type); }

function layoutNodes() {
  const w = W(), h = H();
  const pad = { top: 90, bottom: 40, sideL: 40, sideR: 40 };
  const nCols = COL_ORDER.length;
  const colW = (w - pad.sideL - pad.sideR) / nCols;

  COL_ORDER.forEach((type, ci) => {
    const arr = DATA.nodes.filter(n => n.type === type);
    // sort: within a column, by confidence desc so "strongest" is at top
    arr.sort((a,b) => b.confidence - a.confidence);

    const usableH = h - pad.top - pad.bottom;
    const gap = usableH / (arr.length + 1);
    arr.forEach((n, i) => {
      n._tx = pad.sideL + colW*ci + colW/2;
      n._ty = pad.top + gap * (i+1);
    });
  });
}
layoutNodes();
DATA.nodes.forEach(n => { n.x = n._tx; n.y = n._ty; });

/* ---------- FORCE SIM — gentle, mostly positional ---------- */
const sim = d3.forceSimulation(DATA.nodes)
  .force("link", d3.forceLink(DATA.edges).id(d=>d.id).distance(120).strength(0.02))
  .force("collide", d3.forceCollide().radius(d => nodeSize(d)+28).strength(0.9))
  .force("x", d3.forceX(d => d._tx).strength(0.8))
  .force("y", d3.forceY(d => d._ty).strength(0.25))
  .alphaDecay(0.08)
  .velocityDecay(0.5);

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
// icon factories
function iconForComponent(id){ const c="var(--cyan)";
  if (id.includes("ldo")||id.includes("charger")) return `<polygon points="-6,-5 6,-5 0,6" fill="none" stroke="${c}" stroke-width="1.4"/><line x1="-9" y1="-5" x2="-6" y2="-5" stroke="${c}" stroke-width="1.2"/><line x1="9" y1="-5" x2="6" y2="-5" stroke="${c}" stroke-width="1.2"/><line x1="0" y1="6" x2="0" y2="9" stroke="${c}" stroke-width="1.2"/>`;
  if (id.includes("connector")||id.includes("flex")) return `<rect x="-7" y="-4" width="14" height="8" fill="none" stroke="${c}" stroke-width="1.3"/><line x1="-3" y1="-4" x2="-3" y2="4" stroke="${c}" stroke-width="1"/><line x1="0" y1="-4" x2="0" y2="4" stroke="${c}" stroke-width="1"/><line x1="3" y1="-4" x2="3" y2="4" stroke="${c}" stroke-width="1"/>`;
  if (id.includes("eeprom")) return `<rect x="-5" y="-5" width="10" height="10" fill="none" stroke="${c}" stroke-width="1.4"/><circle cx="-3" cy="-3" r="1" fill="${c}"/><line x1="-8" y1="-2" x2="-5" y2="-2" stroke="${c}" stroke-width="1"/><line x1="-8" y1="2" x2="-5" y2="2" stroke="${c}" stroke-width="1"/><line x1="5" y1="-2" x2="8" y2="-2" stroke="${c}" stroke-width="1"/><line x1="5" y1="2" x2="8" y2="2" stroke="${c}" stroke-width="1"/>`;
  return `<rect x="-7" y="-7" width="14" height="14" rx="1.5" fill="none" stroke="${c}" stroke-width="1.4"/><circle cx="-3.5" cy="-3.5" r="1.1" fill="${c}"/><circle cx="0" cy="-3.5" r="1.1" fill="${c}"/><circle cx="3.5" cy="-3.5" r="1.1" fill="${c}"/><circle cx="-3.5" cy="0" r="1.1" fill="${c}"/><circle cx="0" cy="0" r="1.1" fill="${c}"/><circle cx="3.5" cy="0" r="1.1" fill="${c}"/><circle cx="-3.5" cy="3.5" r="1.1" fill="${c}"/><circle cx="0" cy="3.5" r="1.1" fill="${c}"/><circle cx="3.5" cy="3.5" r="1.1" fill="${c}"/>`;
}
function iconForNet(id){ const c="var(--emerald)";
  if (id.includes("i2s")||id.includes("bclk")) return `<polyline points="-9,3 -6,3 -6,-3 -3,-3 -3,3 0,3 0,-3 3,-3 3,3 6,3 6,-3 9,-3" fill="none" stroke="${c}" stroke-width="1.4"/>`;
  if (id.includes("bias")) return `<line x1="-9" y1="-2" x2="9" y2="-2" stroke="${c}" stroke-width="1.6"/><line x1="-6" y1="3" x2="6" y2="3" stroke="${c}" stroke-width="1.4"/><line x1="-3" y1="6" x2="3" y2="6" stroke="${c}" stroke-width="1.2"/>`;
  return `<path d="M -2 -8 L 4 -1 L 0 -1 L 2 8 L -4 1 L 0 1 Z" fill="${c}" fill-opacity="0.35" stroke="${c}" stroke-width="1.3" stroke-linejoin="round"/>`;
}
function iconForSymptom(id){ const c="var(--amber)";
  if (id.includes("boot")) return `<path d="M -6 -2 A 6 6 0 1 1 -4 4" fill="none" stroke="${c}" stroke-width="1.5" stroke-linecap="round"/><polyline points="-7,-4 -6,-2 -4,-2" fill="none" stroke="${c}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
  if (id.includes("mic")) return `<rect x="-3" y="-7" width="6" height="9" rx="3" fill="none" stroke="${c}" stroke-width="1.4"/><path d="M -5 0 A 5 5 0 0 0 5 0" fill="none" stroke="${c}" stroke-width="1.3"/><line x1="-8" y1="-8" x2="8" y2="8" stroke="${c}" stroke-width="1.5" stroke-linecap="round"/>`;
  if (id.includes("siri")) return `<path d="M -7 -5 L 7 -5 L 7 3 L 2 3 L 0 6 L -2 3 L -7 3 Z" fill="none" stroke="${c}" stroke-width="1.4" stroke-linejoin="round"/><circle cx="-3" cy="-1" r="0.9" fill="${c}"/><circle cx="0" cy="-1" r="0.9" fill="${c}"/><circle cx="3" cy="-1" r="0.9" fill="${c}"/>`;
  if (id.includes("speaker")) return `<path d="M -6 -3 L -2 -3 L 2 -7 L 2 7 L -2 3 L -6 3 Z" fill="none" stroke="${c}" stroke-width="1.4" stroke-linejoin="round"/><line x1="5" y1="-5" x2="8" y2="5" stroke="${c}" stroke-width="1.4" stroke-linecap="round"/>`;
  return `<polygon points="0,-7 7,6 -7,6" fill="none" stroke="${c}" stroke-width="1.4" stroke-linejoin="round"/><line x1="0" y1="-2" x2="0" y2="2" stroke="${c}" stroke-width="1.5" stroke-linecap="round"/><circle cx="0" cy="4" r="0.8" fill="${c}"/>`;
}
function iconForAction(id){ const c="var(--violet)";
  if (id.includes("reflow")) return `<path d="M -7 4 Q -3 -6 0 0 Q 3 6 7 -4" fill="none" stroke="${c}" stroke-width="1.5" stroke-linecap="round"/><circle cx="-7" cy="6" r="1.2" fill="${c}"/><circle cx="7" cy="-6" r="1.2" fill="${c}"/>`;
  if (id.includes("replace")) return `<path d="M -6 -5 L 6 -5 L 6 5 L -6 5 Z" fill="none" stroke="${c}" stroke-width="1.4"/><path d="M -4 -2 L -2 0 L 4 -6" fill="none" stroke="${c}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`;
  if (id.includes("clean")) return `<path d="M -5 -7 L 5 -7 L 4 5 L -4 5 Z" fill="none" stroke="${c}" stroke-width="1.4"/><line x1="-3" y1="-4" x2="3" y2="-4" stroke="${c}" stroke-width="1.1"/><path d="M -2 6 L -3 9 M 0 6 L 0 9 M 2 6 L 3 9" stroke="${c}" stroke-width="1.1" stroke-linecap="round"/>`;
  return `<path d="M -5 5 L 5 -5 M -5 -5 L 5 5" stroke="${c}" stroke-width="1.6" stroke-linecap="round"/>`;
}

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
  const r = nodeSize(d);
  const opacity = 0.55 + d.confidence * 0.45;

  g.append("circle").attr("class","conf-ring").attr("r", r+5).attr("fill","none")
    .attr("stroke", TYPE_COLORS[d.type]).attr("stroke-opacity", d.confidence*0.35)
    .attr("stroke-width", 1 + d.confidence*2).attr("filter", `url(#${TYPE_GLOW[d.type]})`);

  if (d.type==="symptom") {
    g.append("circle").attr("class","node-shape").attr("r", r)
      .attr("fill", TYPE_FILL.symptom).attr("stroke", TYPE_COLORS.symptom).attr("stroke-opacity", opacity).attr("stroke-width",1.5);
  } else if (d.type==="component") {
    const s = r*1.9;
    g.append("rect").attr("class","node-shape").attr("x",-s/2).attr("y",-s/2).attr("width",s).attr("height",s).attr("rx",4)
      .attr("fill", TYPE_FILL.component).attr("stroke", TYPE_COLORS.component).attr("stroke-opacity", opacity).attr("stroke-width",1.5);
  } else if (d.type==="net") {
    const rr = r*1.2;
    const pts = [];
    for (let i=0;i<6;i++){ const a=(Math.PI/3)*i; pts.push([Math.cos(a)*rr,Math.sin(a)*rr].join(",")); }
    g.append("polygon").attr("class","node-shape").attr("points", pts.join(" "))
      .attr("fill", TYPE_FILL.net).attr("stroke", TYPE_COLORS.net).attr("stroke-opacity", opacity).attr("stroke-width",1.5);
  } else { // action — diamond
    const rr = r*1.25;
    const pts = `0,${-rr} ${rr},0 0,${rr} ${-rr},0`;
    g.append("polygon").attr("class","node-shape").attr("points", pts)
      .attr("fill", TYPE_FILL.action).attr("stroke", TYPE_COLORS.action).attr("stroke-opacity", opacity).attr("stroke-width",1.5);
  }

  const iconHtml = d.type==="component" ? iconForComponent(d.id)
                 : d.type==="net"       ? iconForNet(d.id)
                 : d.type==="action"    ? iconForAction(d.id)
                 :                        iconForSymptom(d.id);
  const iconG = document.createElementNS("http://www.w3.org/2000/svg","g");
  iconG.setAttribute("class","node-icon");
  iconG.innerHTML = iconHtml;
  this.appendChild(iconG);

  g.append("text").attr("class","node-label").attr("dy", r + 16).text(d.label);
  g.append("text").attr("class","node-sub").attr("dy", r + 28)
    .text(TYPE_LABEL_FR[d.type].toLowerCase() + " · " + (d.confidence*100).toFixed(0) + "%");
});

/* ---------- Path: smart routing per relation ---------- */
function linkPath(d){
  const sx=d.source.x, sy=d.source.y, tx=d.target.x, ty=d.target.y;
  const dx=tx-sx, dy=ty-sy;
  // gently-curved orthogonal for columnar flow
  if (d.relation==="powers" || d.relation==="causes") {
    // forward flow — S-curve via horizontal midpoint
    const mx = (sx+tx)/2;
    return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}`;
  }
  if (d.relation==="resolves") {
    // action (col 0) → symptom (col 3) — strictly L→R, use S-curve like causes/powers
    const mx = (sx+tx)/2;
    return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}`;
  }
  // connected_to: slight curve
  const dr = Math.sqrt(dx*dx+dy*dy)*2.2;
  return `M${sx},${sy}A${dr},${dr} 0 0,1 ${tx},${ty}`;
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
    document.getElementById("zoomPct").textContent = Math.round(e.transform.k*100)+"%";
    document.getElementById("zoomReadout").textContent = `zoom ${e.transform.k.toFixed(2)}×`;
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
window.addEventListener("resize", () => {
  layoutNodes();
  sim.force("x", d3.forceX(d => d._tx).strength(0.8));
  sim.force("y", d3.forceY(d => d._ty).strength(0.25));
  sim.alpha(0.5).restart();
});

  sim.alpha(1).restart();
  for (let i=0;i<80;i++) sim.tick();
  linkSel.attr("d", linkPath);
  nodeSel.attr("transform", d => `translate(${d.x},${d.y})`);
}
