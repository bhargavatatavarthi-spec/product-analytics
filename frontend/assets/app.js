/* Kotak PAL Journey Analyzer — front-end SPA.
   Vanilla JS, no build step. Reads everything from the FastAPI backend under
   /api and renders the six analytics screens plus a manual data-import screen. */
"use strict";

// ─────────────────────────── tiny DOM helper ───────────────────────────
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  return el;
}
const svg = (inner, attrs = {}) => {
  const s = `<svg width="${attrs.w || 18}" height="${attrs.h || attrs.w || 18}" viewBox="0 0 24 24" fill="none" stroke="${attrs.stroke || "currentColor"}" stroke-width="${attrs.sw || 1.9}" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
  const wrap = document.createElement("span");
  wrap.style.display = "inline-flex";
  wrap.innerHTML = s;
  return wrap.firstChild;
};

// ─────────────────────────── API ───────────────────────────
const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method: "DELETE" });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  },
  async upload(path, formData) {
    const r = await fetch(path, { method: "POST", body: formData });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
};

// ─────────────────────────── state ───────────────────────────
const state = {
  screen: "overview",
  range: "30d",
  milestone: "Disbursement Completed",
  aging: 21,
  attrDim: "amount",
  rangeOpen: false,
  meta: null,
};

const BUCKET_COLORS = { won: "#6F39F5", inflight: "#191132", lost: "#8A8595", unclassified: "#6F39F5" };
const BUCKET_LABELS = { won: "Won", inflight: "In-flight", lost: "Lost", unclassified: "Unclassified" };

const NAV = [
  { key: "overview", label: "Overview", icon: `<rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/>` },
  { key: "cohort", label: "Cohort Triangle", icon: `<path d="M3 3h18L3 21z"/><path d="M3 9h9"/><path d="M3 15h4"/>` },
  { key: "attribution", label: "Attribution", icon: `<line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>` },
  { key: "import", label: "Data Import", icon: `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>` },
];

const SCREEN_META = {
  overview: ["Overview", "The whole entered population, split into Won, In-flight and Lost"],
  cohort: ["Cohort Triangle", "Milestone reach by entry-date cohort and days since entry"],
  attribution: ["Attribution", "Lead metadata, call outcomes and journey-stage credit for every disbursal"],
  import: ["Data Import", "Upload a daily offer or journey drop and fold it into the analytics"],
};

// ─────────────────────────── toast ───────────────────────────
function toast(msg, kind = "") {
  const host = document.getElementById("toast-host");
  const t = h("div", { class: "toast " + kind }, msg);
  host.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 3800);
}

// ─────────────────────────── segmented control ───────────────────────────
function seg(items, active, onPick, small) {
  return h("div", { class: "seg" + (small ? " seg-sm" : "") },
    items.map((it) => h("button", {
      class: "seg-btn" + (it.val === active ? " active" : ""),
      onClick: () => onPick(it.val),
    }, it.label)));
}

// ─────────────────────────── shell renderers ───────────────────────────
function renderNav() {
  const nav = document.getElementById("nav");
  nav.innerHTML = "";
  NAV.forEach((item) => {
    nav.appendChild(h("button", {
      class: "nav-btn" + (state.screen === item.key ? " active" : ""),
      onClick: () => navigate(item.key),
    }, svg(item.icon), h("span", null, item.label)));
  });
  const footer = document.getElementById("nav-footer");
  footer.innerHTML = "";
  footer.appendChild(h("button", {
    class: "nav-btn",
    onClick: () => navigate("settings"),
  }, svg(`<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>`), h("span", null, "Settings")));
  if (state.screen === "settings") footer.firstChild.classList.add("active");
}

function renderTopbar() {
  const meta = SCREEN_META[state.screen] || ["", ""];
  document.getElementById("screen-title").textContent = meta[0];
  document.getElementById("screen-desc").textContent = meta[1];

  // Range picker
  const picker = document.getElementById("range-picker");
  picker.innerHTML = "";
  const summaries = state.meta?.summaries?.ranges || {};
  const cur = state.meta?.ranges?.find((r) => r.key === state.range) || { full: "Last 30 days", label: "30d" };
  const trigger = h("button", { class: "range-trigger", onClick: () => { state.rangeOpen = !state.rangeOpen; renderTopbar(); } },
    svg(`<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>`, { stroke: "var(--ss-lucid)", w: 17 }),
    h("div", { style: { textAlign: "left", lineHeight: "1.15" } },
      h("div", { class: "range-eyebrow" }, "Cohort window"),
      h("div", { class: "range-value" }, cur.full)),
    svg(`<polyline points="6 9 12 15 18 9"/>`, { stroke: "var(--ss-fg-subtle)", w: 15, sw: 2.2 }));
  picker.appendChild(trigger);
  if (state.rangeOpen) {
    picker.appendChild(h("div", { class: "range-backdrop", onClick: () => { state.rangeOpen = false; renderTopbar(); } }));
    picker.appendChild(h("div", { class: "range-menu" },
      h("div", { class: "range-menu-title" }, "Entry-date cohort window"),
      (state.meta?.ranges || []).map((r) => h("button", {
        class: "range-item" + (r.key === state.range ? " active" : ""),
        onClick: () => { state.range = r.key; state.rangeOpen = false; render(); },
      },
        h("div", { style: { display: "flex", flexDirection: "column", gap: "1px" } },
          h("span", { class: "range-item-label" }, r.full),
          h("span", { class: "range-item-span" }, (summaries[r.key]?.entered ?? 0).toLocaleString("en-IN") + " leads")),
        r.key === state.range ? svg(`<polyline points="20 6 9 17 4 12"/>`, { stroke: "var(--ss-lucid)", w: 16, sw: 2.4 }) : null)),
      h("div", { style: { borderTop: "1px solid var(--ss-border)", marginTop: "4px", padding: "10px 12px 6px", fontSize: "11.5px", color: "var(--ss-fg-subtle)", lineHeight: "1.4" } },
        "Filters every screen consistently by entry-date.")));
  }
}

function renderAnchor(entered, buckets) {
  const anchor = document.getElementById("population-anchor");
  anchor.innerHTML = "";
  if (state.screen === "import") { anchor.style.display = "none"; return; }
  anchor.style.display = "flex";
  const order = ["won", "inflight", "lost"];
  const total = entered || 1;
  anchor.appendChild(h("span", { class: "anchor-label" }, "Lead population"));
  anchor.appendChild(h("div", { class: "anchor-bar" },
    order.map((k) => h("div", { style: { width: (buckets[k].count / total * 100) + "%", background: BUCKET_COLORS[k] }, title: `${BUCKET_LABELS[k]} · ${buckets[k].count_label}` }))));
  anchor.appendChild(h("div", { class: "anchor-legend" },
    order.map((k) => h("div", { class: "anchor-legend-item" },
      h("span", { class: "dot", style: { background: BUCKET_COLORS[k] } }),
      h("span", { class: "anchor-count" }, buckets[k].count_label),
      h("span", { class: "anchor-cat" }, BUCKET_LABELS[k])))));
  anchor.appendChild(h("div", { style: { flex: "1" } }));
  anchor.appendChild(h("span", { class: "anchor-meta" }, entered.toLocaleString("en-IN") + " entered · as of " + (state.meta?.summaries?.as_of || "")));
}

// ─────────────────────────── navigation + render ───────────────────────────
function navigate(screen) { state.screen = screen; state.rangeOpen = false; render(); }

function setContent(node) {
  const content = document.getElementById("content");
  content.innerHTML = "";
  content.appendChild(node);
}
function loading() { setContent(h("div", { class: "loading-panel" }, "Loading…")); }

async function render() {
  renderNav();
  renderTopbar();
  loading();
  try {
    switch (state.screen) {
      case "overview": return await renderOverview();
      case "cohort": return await renderCohort();
      case "attribution": return await renderAttribution();
      case "settings": return await renderSettings();
      case "import": return await renderImport();
    }
  } catch (err) {
    setContent(h("div", { class: "loading-panel" }, "Could not load: " + err.message));
  }
}

// ═══════════════════════════ OVERVIEW ═══════════════════════════
async function renderOverview() {
  const d = await API.get(`/api/overview?range=${state.range}`);
  renderAnchor(d.entered, d.buckets);

  const dot = (c) => h("span", { class: "dot", style: { background: c } });
  const order = ["won", "inflight", "lost"];

  const cards = h("div", { class: "grid-3", style: { marginBottom: "28px" } },
    order.map((k) => {
      const b = d.buckets[k];
      const deltaTxt = b.delta == null ? "—" : (b.delta > 0 ? "▲ +" : b.delta < 0 ? "▼ " : "") + Math.abs(b.delta) + "%";
      return h("div", { class: "metric-card" },
        h("div", { class: "metric-head" }, dot(BUCKET_COLORS[k]), h("span", { class: "metric-cat" }, BUCKET_LABELS[k])),
        h("div", { class: "metric-num", style: { color: BUCKET_COLORS[k] } }, b.count_label),
        h("div", { class: "metric-foot" },
          h("span", { class: "metric-pct" }, b.pct + "% of entered"),
          h("span", { class: "metric-delta" }, deltaTxt)));
    }));

  const splitCard = h("div", { class: "card", style: { marginBottom: "28px" } },
    h("div", { class: "section-head" },
      h("div", null, h("div", { class: "ss-eyebrow" }, "Population split"), h("h3", { style: { margin: "2px 0 0", fontSize: "18px" } }, "Every lead entered, in one bar")),
      h("div", { class: "muted" }, d.entered_label + " leads entered")),
    h("div", { style: { display: "flex", height: "46px", borderRadius: "4px", overflow: "hidden" } },
      order.map((k, i) => {
        const b = d.buckets[k];
        return h("div", { style: { width: b.pct + "%", background: BUCKET_COLORS[k], display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontSize: "13px", fontWeight: "700", borderRight: i < 2 ? "2px solid #fff" : "none" }, title: `${BUCKET_LABELS[k]} · ${b.count_label} (${b.pct}%)` }, b.pct >= 12 ? b.pct + "%" : "");
      })),
    h("div", { style: { display: "flex", gap: "26px", marginTop: "16px" } },
      order.map((k) => h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
        dot(BUCKET_COLORS[k]), h("span", { style: { fontSize: "13px", fontWeight: "600" } }, BUCKET_LABELS[k]),
        h("span", { class: "muted" }, d.buckets[k].count_label + " · " + d.buckets[k].pct + "%")))));

  // Aging card
  const ag = d.aging;
  const maxC = Math.max(1, ...ag.bars.map((b) => b.count));
  const agingCtrls = h("div", { style: { display: "flex", gap: "2px", background: "var(--ss-pale-lucid-tint)", borderRadius: "6px", padding: "3px" } },
    [7, 14, 21].map((v) => h("button", { class: "seg-btn" + (v === ag.threshold ? " active" : ""), onClick: () => setAging(v) }, v + " days")));

  const barsRow = h("div", { style: { display: "flex", alignItems: "flex-end", gap: "14px", height: "150px", padding: "0 4px", borderBottom: "1px solid var(--ss-border)" } });
  ag.bars.forEach((bar) => {
    if (bar.first_stalled) {
      barsRow.appendChild(h("div", { style: { alignSelf: "stretch", flex: "none", width: "0", borderLeft: "2px dashed var(--ss-lucid)", position: "relative" } },
        h("span", { style: { position: "absolute", top: "-4px", left: "7px", whiteSpace: "nowrap", fontSize: "10px", fontWeight: "800", letterSpacing: "0.03em", textTransform: "uppercase", color: "var(--ss-lucid)" } }, `Stalled ≥ ${ag.threshold}d →`)));
    }
    barsRow.appendChild(h("div", { style: { flex: "1", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "flex-end", height: "100%", gap: "6px" } },
      h("div", { style: { fontSize: "12px", fontWeight: "700" } }, bar.count_label),
      h("div", { style: { width: "100%", height: Math.max(6, bar.count / maxC * 118) + "px", borderRadius: "4px 4px 0 0", background: bar.stalled ? "#8A8595" : "var(--ss-darkmatter)" } })));
  });
  const labelsRow = h("div", { style: { display: "flex", gap: "14px", marginTop: "8px" } });
  ag.bars.forEach((bar) => {
    if (bar.first_stalled) labelsRow.appendChild(h("div", { style: { flex: "none", width: "0" } }));
    labelsRow.appendChild(h("div", { style: { flex: "1", textAlign: "center", fontSize: "11.5px", fontWeight: "600", color: "var(--ss-fg-subtle)" } }, bar.label));
  });

  const agingCard = h("div", { class: "card" },
    h("div", { style: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "20px", marginBottom: "20px", flexWrap: "wrap" } },
      h("div", { style: { maxWidth: "560px" } },
        h("div", { class: "ss-eyebrow" }, "In-flight, aging"),
        h("h3", { style: { margin: "2px 0 6px", fontSize: "18px" } }, "Stalled leads are effectively lost"),
        h("p", { style: { margin: "0", fontSize: "13.5px", color: "var(--ss-fg-muted)", lineHeight: "1.5" }, html: `Of ${ag.inflight_count_label} In-flight leads, <strong style="color:var(--ss-fg)">${ag.stalled_count_label} (${ag.stalled_pct}%)</strong> have sat in their current stage beyond the aging threshold — treat them as at risk, not pipeline.` }),
        h("div", { style: { display: "inline-flex", alignItems: "center", gap: "8px", marginTop: "12px", padding: "6px 13px", borderRadius: "999px", background: "#efeef2", border: "1px solid #e0dde6" } },
          h("span", { style: { width: "9px", height: "9px", borderRadius: "2px", background: "#8A8595", flex: "none" } }),
          h("span", { style: { fontSize: "12.5px", fontWeight: "700" } }, ag.stalled_count_label + " leads at risk"),
          h("span", { style: { fontSize: "12.5px", fontWeight: "600", color: "var(--ss-fg-subtle)" } }, "· " + ag.stalled_pct + "% of In-flight"))),
      h("div", null,
        h("div", { style: { fontSize: "10px", fontWeight: "600", letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--ss-fg-subtle)", marginBottom: "6px", textAlign: "right" } }, "Aging threshold"),
        agingCtrls)),
    barsRow, labelsRow,
    h("div", { style: { display: "flex", gap: "22px", marginTop: "18px" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } }, h("span", { style: { width: "12px", height: "12px", borderRadius: "3px", background: "var(--ss-darkmatter)" } }), h("span", { style: { fontSize: "12.5px", fontWeight: "500" } }, "Within threshold (In-flight)")),
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } }, h("span", { style: { width: "12px", height: "12px", borderRadius: "3px", background: "#8A8595" } }), h("span", { style: { fontSize: "12.5px", fontWeight: "500" } }, "Stalled beyond threshold (at risk)"))));

  setContent(h("div", { class: "wrap" },
    h("div", { class: "callout" },
      svg(`<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>`, { stroke: "var(--ss-lucid)", w: 20, sw: 2 }),
      h("div", { class: "callout-text" }, d.takeaway)),
    cards, splitCard, agingCard));
}

async function setAging(v) {
  state.aging = v;
  try { await API.post("/api/settings", { aging_threshold: v }); } catch (e) {}
  render();
}

// Refresh the persistent population anchor for non-overview screens.
async function updateAnchor() {
  try {
    const d = await API.get(`/api/overview?range=${state.range}`);
    renderAnchor(d.entered, d.buckets);
  } catch (e) { /* leave anchor as-is */ }
}

function heat(t) {
  t = Math.max(0, Math.min(1, t));
  const a = [239, 233, 255], b = [111, 57, 245], g = Math.pow(t, 0.85);
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * g));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

// ═══════════════════════════ COHORT TRIANGLE ═══════════════════════════
async function renderCohort() {
  updateAnchor();
  const d = await API.get(`/api/cohort?milestone=${encodeURIComponent(state.milestone)}`);
  const cellBase = { display: "flex", alignItems: "center", justifyContent: "center", minWidth: "48px", flex: "1", height: "34px", fontSize: "12px", fontWeight: "600", borderRight: "1px solid rgba(255,255,255,0.55)" };

  const milestoneCtrls = h("div", { class: "seg" },
    (state.meta?.milestones || []).map((m) => h("button", { class: "seg-btn" + (m.label === state.milestone ? " active" : ""), onClick: () => { state.milestone = m.label; render(); } }, m.label)));

  const header = h("div", { class: "triangle-header" },
    h("div", { class: "triangle-th-cohort" }, "Cohort"),
    d.cols.map((c) => h("div", { class: "triangle-th", title: c.full }, c.label)));

  const rows = d.rows.map((row) => h("div", { class: "triangle-row" },
    h("div", { class: "triangle-rowlabel" },
      h("span", { style: { fontSize: "12.5px", fontWeight: "700" } }, row.date),
      h("span", { style: { fontSize: "11px", color: "var(--ss-fg-subtle)" } }, row.size_label)),
    row.cells.map((cell) => {
      if (!cell.mature) {
        return h("div", { style: Object.assign({}, cellBase, { background: "repeating-linear-gradient(45deg,#f4f2f9,#f4f2f9 4px,#e7e3f2 4px,#e7e3f2 8px)", color: "#c3bed0" }), title: "Not yet mature" }, "");
      }
      const t = cell.value / 100;
      return h("div", { style: Object.assign({}, cellBase, { background: heat(t), color: t > 0.5 ? "#fff" : "var(--ss-darkmatter)" }), title: `${row.date} cohort · ${cell.text} reached ${state.milestone}` }, cell.text);
    })));

  const summaryCard = h("div", { class: "card", style: { padding: "18px" } },
    h("div", { class: "ss-eyebrow", style: { marginBottom: "12px" } }, "Cohort summary"),
    summaryStat("Avg days to " + d.milestone_short, d.summary.avg_days, " days"),
    summaryStat("Cohorts typically plateau", "Day " + d.summary.plateau_day, ""),
    summaryStat("Mature cohorts", d.summary.mature_cohorts, " of " + d.summary.total_cohorts));

  const legendCard = h("div", { class: "card", style: { padding: "18px" } },
    h("div", { class: "ss-eyebrow", style: { marginBottom: "12px" } }, "Legend"),
    h("div", { style: { height: "12px", borderRadius: "3px", background: "linear-gradient(90deg,#EFE9FF,#6F39F5)", marginBottom: "5px" } }),
    h("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "11px", color: "var(--ss-fg-subtle)", fontWeight: "600", marginBottom: "14px" } }, h("span", null, "Low"), h("span", null, "High reach")),
    h("div", { style: { display: "flex", alignItems: "center", gap: "9px" } },
      h("span", { style: { width: "26px", height: "20px", flex: "none", borderRadius: "3px", background: "repeating-linear-gradient(45deg,#f4f2f9,#f4f2f9 4px,#e7e3f2 4px,#e7e3f2 8px)", border: "1px solid var(--ss-border)" } }),
      h("span", { style: { fontSize: "12px", fontWeight: "600", color: "var(--ss-fg-muted)" } }, "Not yet mature")));

  const needsDates = d.milestone_dated === false;
  const dateNote = needsDates ? h("div", { class: "callout", style: { marginBottom: "20px" } },
    svg(`<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>`, { stroke: "var(--ss-lucid)", w: 20, sw: 2 }),
    h("div", { class: "callout-text" }, `No “${state.milestone}” date in the imported feed yet, so this cohort can't be measured. Include that milestone's date column (e.g. ${d.date_field}) in your drop and it will populate — the other milestones with dates already work.`)) : null;

  setContent(h("div", null,
    h("div", { style: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "20px", marginBottom: "20px", flexWrap: "wrap" } },
      h("p", { style: { margin: "0", maxWidth: "640px", fontSize: "13.5px", color: "var(--ss-fg-muted)", lineHeight: "1.5" }, html: 'Rows are entry-date (Created Date) cohorts; columns are days since entry. Each cell is the share of that cohort that reached the milestone by that day, using the milestone\'s own date. Cells without enough elapsed time are <strong style="color:var(--ss-fg)">not yet mature</strong> — never counted as failure.' }),
      h("div", null, h("div", { style: { fontSize: "10px", fontWeight: "600", letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--ss-fg-subtle)", marginBottom: "6px" } }, "Milestone reached"), milestoneCtrls)),
    dateNote,
    h("div", { class: "triangle-wrap" },
      h("div", { class: "triangle" }, h("div", { class: "triangle-inner" }, header, ...rows)),
      h("div", { class: "cohort-side" }, summaryCard, legendCard))));
}

function summaryStat(label, value, suffix) {
  return h("div", { style: { marginBottom: "14px" } },
    h("div", { style: { fontSize: "11px", color: "var(--ss-fg-subtle)", fontWeight: "600" } }, label),
    h("div", { style: { fontSize: "26px", fontWeight: "800", letterSpacing: "-0.02em", color: label.startsWith("Avg") ? "var(--ss-lucid)" : "var(--ss-fg)" } }, String(value), suffix ? h("span", { style: { fontSize: "15px", color: "var(--ss-fg-muted)", fontWeight: "600" } }, suffix) : null));
}

// ═══════════════════════════ ATTRIBUTION ═══════════════════════════
async function renderAttribution() {
  updateAnchor();
  const d = await API.get(`/api/attribution?range=${state.range}&dim=${state.attrDim}`);
  const a = d.attr;

  const hero = h("div", { class: "hero-dark ss-on-dark" },
    h("div", { class: "hero-eyebrow" }, "Headline ratio"),
    h("div", { class: "hero-ratio" }, h("span", { class: "accent" }, a.ratio_pct + "%"), " of disbursals were Voice-AI-touched"),
    h("p", { class: "hero-p" }, "Every disbursal is bucketed by whether the lead was connected at least once by the Voice AI. These two numbers are never blended into one."));

  const attCard = (title, sub, block, color, note) => h("div", { class: "card", style: { borderTop: "4px solid " + color, padding: "26px" } },
    h("div", { style: { display: "flex", alignItems: "center", gap: "9px", marginBottom: "18px" } },
      h("span", { style: { width: "12px", height: "12px", borderRadius: "3px", background: color } }),
      h("span", { style: { fontSize: "13px", fontWeight: "700" } }, title),
      h("span", { class: "muted" }, sub)),
    h("div", { style: { fontSize: "11px", color: "var(--ss-fg-subtle)", fontWeight: "600" } }, "Disbursals"),
    h("div", { style: { fontSize: "34px", fontWeight: "800", letterSpacing: "-0.02em", color, lineHeight: "1.1" } }, block.count_label),
    h("div", { style: { fontSize: "11px", color: "var(--ss-fg-subtle)", fontWeight: "600", marginTop: "16px" } }, "Disbursed amount"),
    h("div", { style: { fontSize: "24px", fontWeight: "800", letterSpacing: "-0.02em" } }, block.amount),
    h("div", { class: "bar-track", style: { height: "8px", background: "#efeaf9", marginTop: "18px" } }, h("div", { style: { width: block.share + "%", height: "100%", background: color } })),
    h("div", { class: "muted", style: { marginTop: "7px" } }, block.share + "% of all disbursals"));

  const cards = h("div", { class: "grid-2" },
    attCard("Voice-AI-attributed", "connected ≥ 1×", a.voice, "var(--ss-lucid)"),
    attCard("Organic", "never connected", a.organic, "#8A8595"));

  // Call outcomes
  const outcomes = h("div", { class: "card" },
    h("div", { style: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap", gap: "16px", marginBottom: "6px" } },
      h("div", null, h("div", { class: "ss-eyebrow" }, "Call outcome data"), h("h3", { style: { margin: "2px 0 0", fontSize: "18px" } }, "How the Voice AI reached these leads")),
      h("div", { style: { display: "flex", gap: "28px" } },
        h("div", null, h("div", { class: "muted" }, "Leads dialed"), h("div", { style: { fontSize: "22px", fontWeight: "800", letterSpacing: "-0.02em" } }, d.dialed_label)),
        h("div", null, h("div", { class: "muted" }, "Connect rate"), h("div", { style: { fontSize: "22px", fontWeight: "800", letterSpacing: "-0.02em", color: "var(--ss-lucid)" } }, d.connect_rate + "%")))),
    h("div", { style: { display: "flex", gap: "22px", margin: "16px 0 4px" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } }, h("span", { style: { width: "12px", height: "12px", borderRadius: "3px", background: "var(--ss-lucid)" } }), h("span", { style: { fontSize: "12.5px", fontWeight: "600" } }, "Reached a human (connected)")),
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } }, h("span", { style: { width: "12px", height: "12px", borderRadius: "3px", background: "#8A8595" } }), h("span", { style: { fontSize: "12.5px", fontWeight: "600" } }, "Not connected"))),
    h("div", { style: { display: "flex", flexDirection: "column", gap: "8px", marginTop: "10px" } },
      d.call_outcomes.map((o) => h("div", { style: { display: "flex", alignItems: "center", gap: "14px" } },
        h("div", { style: { width: "250px", flex: "none", fontSize: "12.5px", fontWeight: "600" } }, o.label),
        h("div", { style: { flex: "1", height: "16px", borderRadius: "4px", background: "#f2f0f7", overflow: "hidden" } }, h("div", { style: { width: o.rel + "%", height: "100%", background: o.connected ? "var(--ss-lucid)" : "#8A8595" } })),
        h("div", { style: { width: "52px", flex: "none", textAlign: "right", fontSize: "12.5px", fontWeight: "700" } }, o.pct + "%"),
        h("div", { style: { width: "78px", flex: "none", textAlign: "right", fontSize: "12px", color: "var(--ss-fg-subtle)", fontWeight: "600" } }, o.count)))),
    h("div", { style: { display: "flex", gap: "16px", marginTop: "22px", paddingTop: "20px", borderTop: "1px solid var(--ss-border)" } },
      d.post_connect.map((pc) => h("div", { style: { flex: "1", background: "var(--ss-pale-lucid-tint)", borderRadius: "6px", padding: "14px 16px" } },
        h("div", { style: { fontSize: "22px", fontWeight: "800", letterSpacing: "-0.02em", color: "var(--ss-lucid)" } }, pc.value),
        h("div", { style: { fontSize: "12.5px", fontWeight: "600", marginTop: "2px" } }, pc.label)))));

  // Metadata attribution
  const dimCtrls = h("div", { class: "seg" },
    (state.meta?.dimensions || []).map((dm) => h("button", { class: "seg-btn" + (dm.key === state.attrDim ? " active" : ""), onClick: () => { state.attrDim = dm.key; render(); } }, dm.label)));
  const metaCard = h("div", { class: "card" },
    h("div", { style: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "16px", flexWrap: "wrap", marginBottom: "16px" } },
      h("div", null, h("div", { class: "ss-eyebrow" }, "Lead metadata"), h("h3", { style: { margin: "2px 0 2px", fontSize: "18px" } }, "Attribution by " + d.dim.label), h("div", { style: { fontSize: "12px", color: "var(--ss-fg-subtle)", fontWeight: "500", fontFamily: "monospace" } }, "offer field · " + d.dim.field)),
      dimCtrls),
    h("div", { style: { display: "flex", alignItems: "center", padding: "0 0 10px", borderBottom: "1px solid var(--ss-border)", fontSize: "11px", fontWeight: "700", letterSpacing: "0.03em", textTransform: "uppercase", color: "var(--ss-fg-subtle)" } },
      h("div", { style: { flex: "1" } }, d.dim.label),
      h("div", { style: { width: "110px", flex: "none", textAlign: "right" } }, "Disbursals"),
      h("div", { style: { width: "230px", flex: "none", paddingLeft: "20px" } }, "Voice-AI-touched"),
      h("div", { style: { width: "120px", flex: "none", textAlign: "right" } }, "Amount")),
    d.dim.rows.map((r) => h("div", { style: { display: "flex", alignItems: "center", padding: "12px 0", borderBottom: "1px solid var(--ss-border)" } },
      h("div", { style: { flex: "1", fontSize: "14px", fontWeight: "600" } }, r.name),
      h("div", { style: { width: "110px", flex: "none", textAlign: "right", fontSize: "14px", fontWeight: "700" } }, r.disb_label),
      h("div", { style: { width: "230px", flex: "none", paddingLeft: "20px", display: "flex", alignItems: "center", gap: "10px" } },
        h("div", { style: { flex: "1", height: "8px", borderRadius: "999px", background: "#f2f0f7", overflow: "hidden" } }, h("div", { style: { width: r.voice_pct + "%", height: "100%", background: "var(--ss-lucid)" } })),
        h("span", { style: { width: "34px", textAlign: "right", fontSize: "12.5px", fontWeight: "700", color: "var(--ss-lucid)" } }, r.voice_pct + "%")),
      h("div", { style: { width: "120px", flex: "none", textAlign: "right", fontSize: "13.5px", fontWeight: "700" } }, r.amount))));

  // Journey-stage attribution
  const maxStage = Math.max(1, ...d.stage_attr.map((s) => s.count));
  const stageCard = h("div", { class: "card" },
    h("div", { class: "ss-eyebrow" }, "Journey-stage attribution"),
    h("h3", { style: { margin: "2px 0 4px", fontSize: "18px" } }, "Where the Voice AI advanced the disbursal"),
    h("p", { style: { margin: "0 0 18px", fontSize: "13.5px", color: "var(--ss-fg-muted)", maxWidth: "660px", lineHeight: "1.5" } }, "Each voice-attributed disbursal is credited to the journey stages the AI moved the lead through, with the median calls it took."),
    h("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } },
      d.stage_attr.length ? d.stage_attr.map((s) => h("div", { style: { display: "flex", alignItems: "center", gap: "14px" } },
        h("div", { style: { width: "230px", flex: "none", fontSize: "13px", fontWeight: "600" } }, s.stage),
        h("div", { style: { flex: "1", height: "22px", borderRadius: "4px", background: "#f2f0f7", overflow: "hidden" } }, h("div", { style: { width: (s.count / maxStage * 100) + "%", height: "100%", background: "var(--ss-lucid)" } })),
        h("div", { style: { width: "64px", flex: "none", textAlign: "right", fontSize: "13px", fontWeight: "700" } }, s.count_label),
        h("div", { style: { width: "80px", flex: "none", textAlign: "right", fontSize: "12.5px", color: "var(--ss-fg-subtle)", fontWeight: "600" } }, s.calls + " calls"))) : h("div", { class: "muted" }, "No voice-attributed disbursals in this window yet.")));

  setContent(h("div", { style: { maxWidth: "1120px" } }, hero, cards, outcomes, metaCard, stageCard));
}

// ═══════════════════════════ SETTINGS ═══════════════════════════
async function renderSettings() {
  updateAnchor();
  const s = await API.get("/api/settings");
  const aging = parseInt(s.aging_threshold, 10);

  setContent(h("div", { style: { maxWidth: "720px" } },
    h("div", { class: "card", style: { marginBottom: "16px" } },
      h("h3", { style: { margin: "0 0 4px", fontSize: "17px" } }, "Aging threshold"),
      h("p", { style: { margin: "0 0 16px", fontSize: "13.5px", color: "var(--ss-fg-muted)" } }, "A global setting. Any In-flight lead stalled in its current stage beyond this many days is treated as at-risk."),
      h("div", { style: { display: "inline-flex", gap: "2px", background: "var(--ss-pale-lucid-tint)", borderRadius: "6px", padding: "3px" } },
        [7, 14, 21].map((v) => h("button", { class: "seg-btn" + (v === aging ? " active" : ""), onClick: () => setAging(v) }, v + " days")))),
    h("div", { class: "card", style: { marginBottom: "16px" } },
      h("h3", { style: { margin: "0 0 4px", fontSize: "17px" } }, "Default cohort milestone"),
      h("p", { style: { margin: "0 0 16px", fontSize: "13.5px", color: "var(--ss-fg-muted)" } }, "The milestone the Cohort Triangle measures reaching by default."),
      h("div", { style: { display: "inline-flex", flexWrap: "wrap", gap: "2px", background: "var(--ss-pale-lucid-tint)", borderRadius: "6px", padding: "3px" } },
        (state.meta?.milestones || []).map((m) => h("button", { class: "seg-btn" + (m.label === s.default_milestone ? " active" : ""), onClick: () => setMilestoneDefault(m.label) }, m.label)))),
    h("div", { style: { display: "flex", gap: "12px", background: "var(--ss-pale-lucid-tint)", borderLeft: "4px solid var(--ss-lucid)", borderRadius: "0 6px 6px 0", padding: "16px 20px" } },
      svg(`<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>`, { stroke: "var(--ss-lucid)", w: 18, sw: 2 }),
      h("div", { style: { fontSize: "13.5px", fontWeight: "500", lineHeight: "1.5" } }, "Kotak PAL is read-only analytics apart from one write action: manual Data Import."))));
}
async function setMilestoneDefault(label) {
  try { await API.post("/api/settings", { default_milestone: label }); state.milestone = label; toast("Default milestone updated", "success"); render(); } catch (e) { toast(e.message, "error"); }
}

// ═══════════════════════════ DATA IMPORT ═══════════════════════════
const importState = { busy: false, progress: null, dropDate: "" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function renderImport() {
  const drops = await API.get("/api/import/drops");
  const busy = importState.busy;

  const dz = h("div", { class: "dropzone", id: "dropzone", style: busy ? { pointerEvents: "none", opacity: "0.55" } : {} },
    svg(`<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>`, { stroke: "var(--ss-lucid)", w: 30, sw: 1.8 }),
    h("div", { style: { fontSize: "15px", fontWeight: "700", marginTop: "10px" } }, "Drop your daily CSV file(s) here, or click to browse"),
    h("div", { class: "muted", style: { marginTop: "4px" } }, "Journey and/or offer feed — columns auto-detected. Select both together and they import in sequence."));
  const input = h("input", { type: "file", accept: ".csv,text/csv", multiple: true, style: { display: "none" }, id: "file-input", onChange: (e) => onFilesPicked(e.target.files) });
  if (!busy) {
    dz.addEventListener("click", () => input.click());
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
    dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("drag"); onFilesPicked(e.dataTransfer.files); });
  }

  const pr = importState.progress;
  const progressBlock = pr ? h("div", { style: { marginTop: "20px" } },
    h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "7px" } },
      h("span", { id: "import-label", style: { fontWeight: "700", fontSize: "13.5px" } },
        "Processing…" + (pr.count > 1 ? ` (file ${pr.index} of ${pr.count})` : "")),
      h("span", { id: "import-pct", style: { fontWeight: "800", fontSize: "14px", color: "var(--ss-lucid)" } }, pr.percent + "%")),
    h("div", { style: { height: "10px", borderRadius: "999px", background: "#e7e3f2", overflow: "hidden" } },
      h("div", { id: "import-bar", style: { width: pr.percent + "%", height: "100%", background: "var(--ss-lucid)", transition: "width 0.3s ease" } })),
    h("div", { class: "muted", style: { marginTop: "7px" } }, "Keep this tab open — the dashboards update automatically when it finishes.")) : null;

  const dateRow = h("div", { style: { display: "flex", alignItems: "center", gap: "10px", marginBottom: "14px" } },
    h("span", { class: "muted" }, "Drop date (optional):"),
    h("input", { type: "date", value: importState.dropDate, class: "map-select", style: { width: "170px" }, disabled: busy,
      onChange: (e) => { importState.dropDate = e.target.value; } }),
    h("span", { class: "muted", style: { fontSize: "12px" } }, "defaults to today / the date in the filename"));

  const uploadCard = h("div", { class: "card", style: { marginBottom: "16px" } },
    h("div", { class: "ss-eyebrow", style: { marginBottom: "12px" } }, "Manual import"),
    dateRow, dz, input, progressBlock);

  const historyRows = drops.drops.length
    ? drops.drops.map((dr) => h("tr", null,
        h("td", null, dr.drop_date),
        h("td", null, dr.filename || "—"),
        h("td", null, dr.row_count.toLocaleString("en-IN")),
        h("td", null, h("span", { class: "pill " + dr.status }, dr.status)),
        h("td", null, dr.error_rows ? dr.error_rows + " skipped" : "—")))
    : [h("tr", null, h("td", { colspan: "5", style: { color: "var(--ss-fg-subtle)", padding: "18px 12px" } }, "No drops imported yet."))];

  const historyCard = h("div", { class: "card" },
    h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" } },
      h("div", null, h("div", { class: "ss-eyebrow" }, "Drop history"), h("h3", { style: { margin: "2px 0 0", fontSize: "17px" } }, drops.total_leads.toLocaleString("en-IN") + " leads across " + drops.drops.length + " drops")),
      drops.drops.length && !busy ? h("button", { class: "btn btn-danger", onClick: resetData }, "Reset all data") : null),
    h("table", { class: "drops-table" },
      h("thead", null, h("tr", null, ["Drop date", "File", "Rows", "Status", "Errors"].map((t) => h("th", null, t)))),
      h("tbody", null, historyRows)));

  setContent(h("div", { style: { maxWidth: "1000px" } },
    h("div", { class: "callout" },
      svg(`<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>`, { stroke: "var(--ss-lucid)", w: 20, sw: 2 }),
      h("div", { class: "callout-text" }, "Each file is one dated drop, joined to existing leads by offer_id. Re-importing a date updates it — it never duplicates.")),
    uploadCard, historyCard));
}

async function onFilesPicked(fileList) {
  const files = Array.from(fileList || []).filter((f) => /\.csv$/i.test(f.name) || (f.type || "").includes("csv"));
  if (!files.length || importState.busy) return;
  importState.busy = true;
  let ok = 0;
  try {
    for (let i = 0; i < files.length; i++) {
      importState.progress = { index: i + 1, count: files.length, percent: 0, filename: files[i].name };
      render();
      await importOne(files[i]);
      ok++;
    }
    state.meta = await API.get("/api/meta");
    toast(`Imported ${ok} file${ok > 1 ? "s" : ""} — dashboards updated`, "success");
  } catch (e) {
    toast("Import failed: " + e.message, "error");
  } finally {
    importState.busy = false;
    importState.progress = null;
    render();
  }
}

async function importOne(file) {
  const fd = new FormData();
  fd.append("file", file);
  if (importState.dropDate) fd.append("drop_date", importState.dropDate);
  const { job_id } = await API.upload("/api/import/start", fd);
  // Poll for progress, updating the bar in place (no full re-render).
  while (true) {
    await sleep(700);
    const s = await API.get("/api/import/status/" + job_id);
    if (importState.progress) importState.progress.percent = s.percent;
    const bar = document.getElementById("import-bar");
    const pct = document.getElementById("import-pct");
    if (bar) bar.style.width = s.percent + "%";
    if (pct) pct.textContent = s.percent + "%";
    if (s.status === "done") return s.result;
    if (s.status === "error") throw new Error(s.error || "processing error");
  }
}

async function resetData() {
  if (!confirm("Delete all imported leads, events and drops? Classifications and settings are kept.")) return;
  try { await API.del("/api/import/reset"); state.meta = await API.get("/api/meta"); toast("All imported data cleared", "success"); render(); } catch (e) { toast(e.message, "error"); }
}

// ═══════════════════════════ boot ═══════════════════════════
async function boot() {
  try {
    state.meta = await API.get("/api/meta");
    const s = state.meta.settings || {};
    if (s.default_milestone) state.milestone = s.default_milestone;
    if (s.aging_threshold) state.aging = parseInt(s.aging_threshold, 10);
    render();
  } catch (e) {
    document.getElementById("content").appendChild(h("div", { class: "loading-panel" }, "Backend unavailable: " + e.message));
  }
}
boot();



