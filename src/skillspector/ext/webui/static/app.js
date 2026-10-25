// Skill Scanner UI. All skill-derived text is inserted with textContent, never innerHTML.
"use strict";

const SEV_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"];
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

// ---- tabs ----
document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => showTab(btn.dataset.tab))
);
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
  if (name === "history") loadHistory();
  if (name === "batch") loadJobs();
  if (name === "pins") loadPins();
}

// ---- theme ----
const THEME_KEY = "skill-scanner-theme";
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#theme-toggle").textContent = theme === "dark" ? "☀ Light" : "☾ Dark";
  try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* storage may be blocked */ }
}
$("#theme-toggle").addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
(() => {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (_) { /* ignore */ }
  applyTheme(saved === "dark" || saved === "light" ? saved : "light");
})();

// ---- icons (inline SVG, no external requests) ----
const ICONS = {
  github: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>',
  folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M3 6h7l2 2h9v11H3z"/></svg>',
  shield: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path fill="none" stroke="currentColor" stroke-width="2" d="M8.5 12l2.5 2.5 4.5-5"/></svg>',
  spark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/></svg>',
  cloud: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M7 18h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 9.5 4.3 4.3 0 0 0 7 18z"/></svg>',
  chip: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1" fill="none" stroke="currentColor" stroke-width="2"/><path stroke="currentColor" stroke-width="2" d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>',
  list: '<svg viewBox="0 0 24 24" aria-hidden="true"><path stroke="currentColor" stroke-width="2" d="M9 6h12M9 12h12M9 18h12"/><circle cx="4.5" cy="6" r="1.5" fill="currentColor"/><circle cx="4.5" cy="12" r="1.5" fill="currentColor"/><circle cx="4.5" cy="18" r="1.5" fill="currentColor"/></svg>',
  box: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M12 2l9 5v10l-9 5-9-5V7z"/><path fill="none" stroke="currentColor" stroke-width="2" d="M3 7l9 5 9-5M12 12v10"/></svg>',
  pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M9 3h6l-1 6 4 3v2H6v-2l4-3zM12 14v7"/></svg>',
};
function icon(name) {
  const span = el("span", { class: "icon" });
  span.innerHTML = ICONS[name] || ICONS.shield; // static strings defined above, never skill data
  return span;
}

// ---- repositories ----
let repos = [];
const selectedRepos = new Set();
async function loadRepos() {
  repos = await api("/api/repos");
  for (const id of [...selectedRepos]) if (!repos.find((r) => r.id === id)) selectedRepos.delete(id);
  renderRepoTiles();
  updateRunSummary();
}
function renderRepoTiles() {
  const box = $("#repo-tiles");
  box.replaceChildren();
  if (!repos.length) { box.append(el("div", { class: "empty", text: "No repositories yet. Paste one or more links above." })); return; }
  for (const r of repos) {
    const isGit = /^https?:\/\//.test(r.source);
    const status = r.status === "fetching" ? el("span", { class: "tile-status" }, el("span", { class: "spinner" }), "fetching")
      : r.status === "error" ? el("span", { class: "tile-status bad", text: "error" })
      : el("span", { class: "tile-status", text: `${r.skills.length} skill${r.skills.length === 1 ? "" : "s"}` });
    const tile = el("button", { class: "tile" + (selectedRepos.has(r.id) ? " selected" : "") + (r.status !== "ready" ? " dim" : ""), title: r.error || r.source, type: "button" },
      el("span", { class: "tile-box" }, icon(isGit ? "github" : "folder"), status),
      el("span", { class: "tile-name", text: r.name }));
    tile.addEventListener("click", () => {
      if (r.status !== "ready") return;
      selectedRepos.has(r.id) ? selectedRepos.delete(r.id) : selectedRepos.add(r.id);
      renderRepoTiles(); updateRunSummary();
    });
    const remove = el("span", { class: "tile-x", title: "Remove", text: "×" });
    remove.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/repos/${r.id}`, { method: "DELETE" });
      selectedRepos.delete(r.id); loadRepos();
    });
    tile.append(remove);
    if (r.status === "error") {
      const retry = el("span", { class: "tile-retry", title: "Fetch again", text: "↻" });
      retry.addEventListener("click", async (e) => { e.stopPropagation(); await api(`/api/repos/${r.id}/refresh`, { method: "POST" }); loadRepos(); });
      tile.append(retry);
    }
    box.append(tile);
  }
}
$("#repo-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const sources = $("#repo-input").value.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  if (!sources.length) return;
  try {
    const added = await api("/api/repos", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sources }) });
    $("#repo-input").value = "";
    for (const r of added) if (r.status === "ready") selectedRepos.add(r.id);
    autoSelect = new Set([...autoSelect, ...added.map((r) => r.id)]);
    $("#repo-error").textContent = "";
    loadRepos();
  } catch (err) { $("#repo-error").textContent = err.message; }
});
let autoSelect = new Set(); // newly added repos get selected once their fetch finishes
$("#select-all").addEventListener("click", () => { repos.filter((r) => r.status === "ready").forEach((r) => selectedRepos.add(r.id)); renderRepoTiles(); updateRunSummary(); });
$("#select-none").addEventListener("click", () => { selectedRepos.clear(); renderRepoTiles(); updateRunSummary(); });

// ---- operations ----
let ops = [];
const selectedOps = new Set();
async function loadOperations() {
  ops = await api("/api/operations");
  const box = $("#op-tiles");
  box.replaceChildren();
  for (const op of ops) {
    const tile = el("button", { class: "tile op" + (op.ready ? "" : " dim"), type: "button",
      title: op.ready ? op.sub : `Not available: needs ${op.missing}` },
      el("span", { class: "tile-box" }, icon(op.icon), el("span", { class: "tile-status", text: op.ready ? op.sub : `needs ${op.missing}` })),
      el("span", { class: "tile-name", text: op.label }));
    tile.dataset.op = op.id;
    tile.addEventListener("click", () => {
      if (!op.ready) return;
      selectedOps.has(op.id) ? selectedOps.delete(op.id) : selectedOps.add(op.id);
      tile.classList.toggle("selected", selectedOps.has(op.id));
      updateRunSummary();
    });
    box.append(tile);
  }
}
function updateRunSummary() {
  const chosenRepos = repos.filter((r) => selectedRepos.has(r.id));
  const skills = chosenRepos.reduce((n, r) => n + r.skills.length, 0);
  const chosenOps = ops.filter((o) => selectedOps.has(o.id));
  const perSkill = chosenOps.filter((o) => o.kind !== "pin").length;
  const pins = chosenOps.some((o) => o.kind === "pin") ? chosenRepos.length : 0;
  const total = skills * perSkill + pins;
  $("#run-summary").textContent = chosenRepos.length && chosenOps.length
    ? `${chosenRepos.length} repo(s) · ${skills} skill(s) · ${chosenOps.length} operation(s) → ${total} task(s)`
    : "Select repositories and operations.";
  $("#run-btn").disabled = !total;
}
$("#run-btn").addEventListener("click", async () => {
  $("#run-btn").disabled = true;
  try {
    await api("/api/batch", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repos: [...selectedRepos], operations: [...selectedOps], strict: $("#strict").checked }) });
    $("#run-error").textContent = "";
    loadJobs();
  } catch (err) { $("#run-error").textContent = err.message; }
  updateRunSummary();
});

// ---- runs ----
let jobs = [];
async function loadJobs() {
  jobs = await api("/api/jobs");
  renderRuns();
}
function fmtTokens(n) { return n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n); }

async function loadUsage() {
  let u;
  try { u = await api("/api/usage"); } catch (_) { return; }
  const box = $("#usage");
  const total = u.models.length
    ? `API usage ≈ $${u.total_cost_usd.toFixed(2)}`
    : "API usage: none yet";
  box.replaceChildren(el("summary", {}, el("strong", { text: total }), el("span", { class: "desc", text: " estimated from token counts" })),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["Model", "Provider", "Tasks", "Input tokens", "Output tokens", "Est. cost"].map((t) => el("th", { text: t })))),
      el("tbody", {}, ...u.models.map((m) => el("tr", {},
        el("td", { text: m.model }), el("td", { text: m.provider || "" }), el("td", { class: "num", text: String(m.tasks) }),
        el("td", { class: "num", text: fmtTokens(m.input_tokens) }), el("td", { class: "num", text: fmtTokens(m.output_tokens) }),
        el("td", { class: "num", text: m.priced ? `$${m.cost_usd.toFixed(3)}` : "price unknown" })))))),
    el("p", { class: "hint", text: `Estimates use list prices per million tokens (input / output): ${Object.entries(u.prices).map(([k, v]) => `${k} $${v[0]}/$${v[1]}`).join(" · ")}. Edit ${u.pricing_file} to change them. Provider dashboards show the actual bill.` }));
}

function fmtSecs(s) { s = Math.max(0, Math.round(s)); return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`; }
const REC_ORDER = ["DO_NOT_INSTALL", "CAUTION", "SAFE"];
const FINISHED = ["done", "error", "cancelled", "interrupted"];
const openItems = new Set();   // item keys whose merged issue list is expanded
const closedBatches = new Set();

function worstOf(recs) {
  return recs.filter(Boolean).sort((x, y) => REC_ORDER.indexOf(x) - REC_ORDER.indexOf(y))[0];
}

function mergeIssues(items) {
  // Union of issues across all runs of one skill, keyed by rule + file + line.
  const merged = new Map();
  for (const j of items) {
    for (const [rule, sev, file, line, title] of j.issues || []) {
      const key = `${rule}|${file}|${line}`;
      if (!merged.has(key)) merged.set(key, { rule, sev, file, line, title, by: new Set() });
      merged.get(key).by.add(j.label || j.operation);
    }
  }
  return [...merged.values()].sort((x, y) => SEV_ORDER.indexOf(x.sev) - SEV_ORDER.indexOf(y.sev));
}

function renderRuns() {
  const box = $("#runs");
  box.replaceChildren();
  if (!jobs.length) { box.append(el("div", { class: "empty", text: "No runs yet." })); return; }
  const queued = jobs.filter((j) => j.status === "queued").sort((a, b) => a.started - b.started);
  const running = jobs.filter((j) => j.status === "running").length;
  $("#queue-summary").textContent = `${running} running · ${queued.length} waiting · ${jobs.filter((j) => j.status === "done").length} done`;
  $("#stop-all").disabled = !(running || queued.length);
  const bySource = new Map();
  for (const j of jobs) {
    if (!bySource.has(j.source)) bySource.set(j.source, []);
    bySource.get(j.source).push(j);
  }
  const ordered = [...bySource.entries()].sort((a, b) =>
    Math.max(...b[1].map((j) => j.started)) - Math.max(...a[1].map((j) => j.started)));
  for (const [source, tasks] of ordered) box.append(sourceBlock(source, tasks, queued));
  loadUsage();
}

const openSources = new Set();
const closedSources = new Set();
function sourceBlock(source, tasks, queued) {
  const done = tasks.filter((j) => FINISHED.includes(j.status)).length;
  const active = tasks.some((j) => j.status === "queued" || j.status === "running");
  const failed = tasks.some((j) => ["error", "cancelled", "interrupted"].includes(j.status));
  const latestPerItemOp = new Map();
  for (const j of [...tasks].sort((a, b) => a.started - b.started)) latestPerItemOp.set(`${j.skill}|${j.operation}`, j);
  const worst = worstOf([...latestPerItemOp.values()].map((j) => j.recommendation));
  const cost = tasks.reduce((n, j) => n + (j.usage?.cost_usd || 0), 0);
  const repo = repos.find((r) => r.source === source);
  const name = tasks[0].repo_name || source;
  const itemsMap = new Map();
  for (const j of tasks) {
    const k = j.skill || j.repo_name;
    if (!itemsMap.has(k)) itemsMap.set(k, []);
    itemsMap.get(k).push(j);
  }
  const items = [...itemsMap.entries()].sort((a, b) => (a[0].startsWith("(") - b[0].startsWith("(")) || a[0].localeCompare(b[0]));

  const actions = el("span", { class: "batch-actions" });
  const button = (text, cls, title, fn) => {
    const b = el("button", { class: cls, text, title });
    b.addEventListener("click", async (e) => { e.preventDefault(); e.stopPropagation(); await fn(b); loadJobs(); });
    actions.append(b);
  };
  if (repo && repo.status === "ready") button("Run selected methods", "", "Run the operations selected above on this repository (change method)", async (b) => {
    if (!selectedOps.size) { b.textContent = "select operations above"; return; }
    try {
      await api("/api/batch", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repos: [repo.id], operations: [...selectedOps], strict: $("#strict").checked }) });
    } catch (err) { b.textContent = err.message.slice(0, 60); }
  });
  if (failed) button("Redo failed", "", "Queue failed or stopped tasks again", async () => {
    await api(`/api/redo-failed?${new URLSearchParams({ source })}`, { method: "POST" });
  });
  if (active) button("Stop", "danger", "Stop this repository's running and waiting tasks", async () => {
    await Promise.all(tasks.filter((j) => j.status === "queued" || j.status === "running")
      .map((j) => api(`/api/jobs/${j.id}/cancel`, { method: "POST" })));
  });
  button("Remove scans", "danger", "Delete every scan of this repository (the skill library keeps the skills)", async (b) => {
    if (!confirmInline(b, "Click again to remove")) return;
    await api(`/api/history?${new URLSearchParams({ source })}`, { method: "DELETE" });
  });

  const group = el("details", { class: "batch" }, el("summary", {},
    icon(/^https?:\/\//.test(source) ? "github" : "folder"),
    el("span", { class: "ftitle" }, el("strong", { text: name }), el("span", { class: "desc", text: ` ${items.length} skill(s)` })),
    el("span", { class: "rule", text: `${done}/${tasks.length} tasks${cost ? ` · ≈ $${cost.toFixed(2)}` : ""}` }),
    worst ? el("span", { class: `rec-badge ${worst}`, text: worst.replaceAll("_", " ") }) : "",
    actions),
    el("div", { class: "item-list" }, ...items.map(([k, its]) => itemBlock(source, k, its, queued))));
  group.open = active ? !closedSources.has(source) : openSources.has(source);
  group.addEventListener("toggle", () => {
    if (group.open) { openSources.add(source); closedSources.delete(source); } else { openSources.delete(source); closedSources.add(source); }
  });
  return group;
}

function confirmInline(button, text) {
  // Two-click confirmation without browser dialogs.
  if (button.dataset.armed) return true;
  button.dataset.armed = "1";
  const old = button.textContent;
  button.textContent = text;
  setTimeout(() => { delete button.dataset.armed; button.textContent = old; }, 3000);
  return false;
}

function itemBlock(batchKey, itemKey, tasks, queued) {
  tasks.sort((a, b) => a.started - b.started || (a.label || "").localeCompare(b.label || ""));
  const first = tasks[0];
  const issues = mergeIssues(tasks.filter((j) => j.status === "done"));
  const counts = SEV_ORDER.map((sev) => [sev, issues.filter((i) => i.sev === sev).length]).filter(([, n]) => n);
  const worst = worstOf(tasks.map((j) => j.recommendation));
  const finished = tasks.filter((j) => FINISHED.includes(j.status)).length;
  const fullKey = `${batchKey}::${itemKey}`;

  const toggle = el("button", { class: "link", text: openItems.has(fullKey) ? "hide issues" : `issues (${issues.length})` });
  toggle.addEventListener("click", () => { openItems.has(fullKey) ? openItems.delete(fullKey) : openItems.add(fullKey); renderRuns(); });
  const remove = el("button", { class: "icon-btn", text: "×", title: "Delete this item's runs" });
  remove.addEventListener("click", async () => {
    if (!confirmInline(remove, "delete?")) return;
    await Promise.all(tasks.map((j) => api(`/api/jobs/${j.id}`, { method: "DELETE" })));
    loadJobs();
  });

  const head = el("div", { class: "item-head" },
    el("span", { class: "task-name" }, el("strong", { text: first.skill || first.repo_name }), el("span", { class: "desc", text: first.repo_name })),
    el("span", { class: "sev-counts" }, ...counts.map(([sev, n]) => el("span", { class: `sev-mini ${sev}`, text: `${n} ${sev.toLowerCase()}` }))),
    el("span", { class: "rule", text: `${finished}/${tasks.length} runs` }),
    worst ? el("span", { class: `rec-badge ${worst}`, text: worst.replaceAll("_", " ") }) : el("span"),
    el("span", { class: "task-actions" }, issues.length ? toggle : "", remove));

  const block = el("div", { class: "item" }, head, el("div", { class: "runs-of-item" }, ...tasks.map((j) => taskRow(j, queued))));
  if (openItems.has(fullKey) && issues.length) {
    const tbody = el("tbody", {}, ...issues.map((i) => el("tr", {},
      el("td", {}, el("span", { class: `sev ${i.sev}`, text: i.sev })),
      el("td", { class: "rule", text: i.rule }),
      el("td", { class: "loc", text: i.file ? `${i.file}${i.line ? ":" + i.line : ""}` : "" }),
      el("td", { text: i.title }),
      el("td", { class: "desc", text: [...i.by].join(", ") }))));
    block.append(el("div", { class: "table-wrap merged" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["Severity", "Rule", "Where", "Issue", "Found by"].map((t) => el("th", { text: t })))), tbody)));
  }
  return block;
}

function taskRow(j, queued) {
  const stages = j.stages || [];
  const current = stages.indexOf(j.stage);
  const chips = el("span", { class: "stages" }, ...stages.map((st, i) => {
    let state = "todo";
    if (j.status === "done" || i < current) state = "ok";
    else if (i === current && j.status === "running") state = "run";
    else if (i === current && (j.status === "error" || j.status === "cancelled")) state = "bad";
    return el("span", { class: `stage ${state}`, text: st });
  }));
  const end = j.finished || Date.now() / 1000;
  const time = j.status === "queued" ? `waiting #${queued.findIndex((q) => q.id === j.id) + 1}`
    : j.began ? fmtSecs(end - j.began) : "";
  const result = j.status === "done" && j.recommendation
    ? el("span", { class: `rec-badge ${j.recommendation}`, text: `${j.score} · ${j.recommendation.replaceAll("_", " ")}` })
    : j.status === "done" ? el("span", { class: "rec-badge", text: "done" })
    : ["error", "cancelled", "interrupted"].includes(j.status) ? el("span", { class: "rec-badge DO_NOT_INSTALL", text: j.status, title: j.error || "" })
    : el("span", { class: "rec-badge", text: j.status });
  const actions = el("span", { class: "task-actions" });
  if (j.status === "done") actions.append(el("button", { text: "Open", onclick: () => openTask(j.id) }));
  if (FINISHED.includes(j.status)) actions.append(el("button", { text: "Redo", title: "Run this operation again", onclick: async (e) => {
    try { await api(`/api/jobs/${j.id}/redo`, { method: "POST" }); loadJobs(); } catch (err) { e.target.textContent = err.message.slice(0, 40); }
  } }));
  if (j.status === "queued" || j.status === "running") actions.append(el("button", { text: "Cancel", onclick: async () => { await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); loadJobs(); } }));
  const row = el("div", { class: "task sub" + (j.status === "running" ? " running" : "") },
    el("span", { class: "op-chip", text: j.label || j.operation }),
    chips, el("span", { class: "num", text: time },
      j.usage ? el("span", { class: "usage", text: `${fmtTokens(j.usage.input_tokens + j.usage.output_tokens)} tok${j.usage.cost_usd != null ? ` · $${j.usage.cost_usd.toFixed(3)}` : ""}` }) : ""),
    result, actions);
  if (j.error && j.status === "error") row.append(el("div", { class: "task-error", text: j.error }));
  return row;
}

async function openTask(id) {
  const job = await api(`/api/jobs/${id}`);
  showTab("batch");
  renderReport(job);
  $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("#stop-all").addEventListener("click", async () => {
  const button = $("#stop-all");
  if (!confirmInline(button, "Click again to stop everything")) return;
  const r = await api("/api/stop-all", { method: "POST" });
  button.textContent = `Stopped ${r.stopped}`;
  setTimeout(() => { button.textContent = "Stop all"; }, 2500);
  loadJobs();
});

// ---- polling ----
async function poll() {
  try {
    const before = repos.filter((r) => r.status === "fetching").map((r) => r.id);
    await loadRepos();
    for (const id of before) {
      const r = repos.find((x) => x.id === id);
      if (r && r.status === "ready" && autoSelect.has(id)) { selectedRepos.add(id); autoSelect.delete(id); renderRepoTiles(); updateRunSummary(); }
    }
    if ($("#tab-batch") && !$("#tab-batch").classList.contains("hidden")) await loadJobs();
  } catch (_) { /* server restarting */ }
}
setInterval(poll, 2000);

// ---- report ----
// job.report is the fork review result: {report (upstream), verdict, judge, explanation, reveal, ...}
function renderReport(job) {
  const review = job.report;
  // Scans saved before the review pipeline hold the upstream report directly.
  const report = review.report || (review.risk_assessment ? review : {});
  const verdict = review.verdict || report.risk_assessment || {};
  const root = $("#result");
  root.replaceChildren($("#tpl-result").content.cloneNode(true));
  root.classList.remove("hidden");

  $(".score-num", root).textContent = verdict.score ?? "–";
  const rec = $(".rec", root);
  rec.textContent = (verdict.recommendation || "").replaceAll("_", " ");
  rec.classList.add(verdict.recommendation || "");
  const comp = report.analysis_completeness || {};
  $(".meta", root).textContent =
    `${job.repo_name || job.source}${job.skill ? " / " + job.skill : ""} · ${job.label || ""} · ${job.strict ? "strict verdict" : "upstream verdict"} · severity ${verdict.severity || "?"} · ` +
    `${comp.scanned_components ?? "?"}/${comp.total_components ?? "?"} files scanned · ${comp.status || ""}`;
  const reasons = $(".reasons", root);
  for (const r of verdict.reasons || []) reasons.append(el("li", { text: r }));

  const md = report.metadata || {};
  const usage = md.inference_usage || [];
  const models = [...new Set(usage.map((u) => `${u.provider}/${u.model}`))];
  const tokens = usage.reduce((n, u) => n + (u.total_tokens || 0), 0);
  const stages = [];
  if (md.llm_requested) {
    stages.push(md.llm_available && usage.length
      ? `Upstream LLM stage: ${models.join(", ")} · ${usage.length} calls · ${tokens.toLocaleString()} tokens`
      : "Upstream LLM stage requested but NOT available: static analysis only.");
    const j = review.judge;
    if (j) stages.push(j.error ? `Bundle judge failed: ${j.error}` : `Bundle judge: ${j.findings.length} grounded finding(s), ${j.ungrounded.length} dropped as unquotable`);
    const e = review.explanation;
    if (e) stages.push(e.error && !e.items.length ? `Explain failed: ${e.error}` : `Explain: ${e.items.length} instructions, ${e.uncovered_lines} line(s) not described`);
  } else {
    stages.push("LLM stages off (static rules and reveal only).");
  }
  $(".llm", root).textContent = stages.join(" · ");

  const warnings = $(".warnings", root);
  for (const w of review.warnings || report._ui_warnings || []) warnings.append(el("li", { text: w.replace(/^.*?(WARNING|ERROR)\s*/, "$1 ") }));
  for (const stage of ["judge", "explanation"]) {
    const a = review[stage]?.anomaly;
    if (a) warnings.append(el("li", { text: `ANOMALY (${stage}): ${a}` }));
  }

  $(".eset", root).addEventListener("click", () => {
    if (/^https?:\/\//.test(job.source)) navigator.clipboard?.writeText(job.source).catch(() => {});
    window.open("https://www.eset.com/us/home/ai-skills-checker/", "_blank", "noopener");
  });
  $(".export", root).addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(review, null, 2)], { type: "application/json" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `skill-review-${job.id}.json` });
    a.click();
    URL.revokeObjectURL(a.href);
  });

  const pinButton = $(".pin", root);
  pinButton.addEventListener("click", async () => {
    pinButton.disabled = true;
    try {
      const p = await api(`/api/jobs/${job.id}/pin`, { method: "POST" });
      pinButton.textContent = `Pinned as ${p.name} (${p.file_count} files)`;
    } catch (err) { pinButton.textContent = `Pin failed: ${err.message}`; pinButton.disabled = false; }
  });

  const sandboxButton = $(".sandbox", root);
  sandboxButton.addEventListener("click", async () => {
    sandboxButton.disabled = true;
    sandboxButton.textContent = "Running in sandbox… (1–3 min)";
    try {
      const updated = await api(`/api/jobs/${job.id}/sandbox`, { method: "POST" });
      renderReport(updated);
      $('#result .subtab[data-panel="sandbox"]').click();
    } catch (err) { sandboxButton.textContent = `Sandbox failed: ${err.message}`; sandboxButton.disabled = false; }
  });

  // sub-tabs
  root.querySelectorAll(".subtab").forEach((b) => b.addEventListener("click", () => {
    root.querySelectorAll(".subtab").forEach((x) => x.classList.toggle("active", x === b));
    root.querySelectorAll(".subpanel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== b.dataset.panel));
  }));

  renderFindings(root, verdict.issues || report.issues || []);
  renderHidden(root, review.reveal || []);
  renderInstructions(root, job, review);
  renderSandbox(root, review.sandbox);
}

function renderSandbox(root, sandbox) {
  const box = $(".sandbox-out", root);
  if (!sandbox) {
    box.append(el("p", { class: "hint", text: "Not run yet. 'Run in sandbox' starts a real agent with this skill installed in an isolated Docker container: no internet, fake SSH/AWS/.env secrets with unique canary values, every file read, command and network attempt recorded. LLM calls go through a host gateway that holds the real key." }));
    return;
  }
  const e = sandbox.events || {};
  box.append(el("p", { class: "llm", text: `Agent model ${e.model} · ${e.llm_calls} LLM calls · ${(e.llm_tokens || 0).toLocaleString()} tokens · ${e.seconds} s · runtime ${e.runtime}` }));
  if (!sandbox.findings.length) box.append(el("p", { class: "cov", text: "No harmful behaviour observed in this run. This does not prove the skill is safe: skills can stay dormant, and the agent model may have refused." }));
  for (const f of sandbox.findings) box.append(renderFinding(f));
  for (const t of sandbox.transcripts || []) {
    const steps = el("ol", {}, ...t.steps.map((s) => el("li", {}, el("code", { text: `${s.tool} ${JSON.stringify(s.args).slice(0, 300)}` }))));
    box.append(el("details", { class: "finding" },
      el("summary", {}, el("span", { class: "rule", text: "Task" }), el("span", { class: "ftitle", text: t.prompt })),
      el("div", { class: "fbody" }, el("h4", { text: "Agent actions" }), steps,
        el("h4", { text: "Agent's final answer" }), el("pre", { class: "revealed", text: t.final || t.error || "" }))));
  }
}

function renderFindings(root, all) {
  const issues = all.slice().sort(
    (a, b) => SEV_ORDER.indexOf(a.severity) - SEV_ORDER.indexOf(b.severity) || (b.confidence || 0) - (a.confidence || 0)
  );
  $(".count", root).textContent = `(${issues.length})`;
  const active = new Set(SEV_ORDER);
  const filters = $(".filters", root);
  for (const sev of SEV_ORDER) {
    const n = issues.filter((i) => i.severity === sev).length;
    if (!n) continue;
    const b = el("button", { class: "on", text: `${sev} ${n}` });
    b.addEventListener("click", () => {
      active.has(sev) ? active.delete(sev) : active.add(sev);
      b.classList.toggle("on");
      draw();
    });
    filters.append(b);
  }
  function draw() {
    const list = $(".findings", root);
    list.replaceChildren();
    const shown = issues.filter((i) => active.has(i.severity));
    if (!shown.length) { list.append(el("div", { class: "empty", text: issues.length ? "No findings match the filter." : "No findings." })); return; }
    for (const i of shown) list.append(renderFinding(i));
  }
  draw();
}

function renderHidden(root, files) {
  const box = $(".hidden-list", root);
  const total = files.reduce((n, f) => n + f.items.length, 0);
  $(".hidden-count", root).textContent = `(${total})`;
  if (!files.length) { box.append(el("div", { class: "empty", text: "Nothing hidden: no invisible characters, look-alike letters, encoded blobs or hidden markup." })); return; }
  for (const f of files) {
    const rows = el("tbody", {}, ...f.items.map((i) => el("tr", {},
      el("td", { class: "num", text: `L${i.line}` }),
      el("td", {}, el("span", { class: `kind kind-${i.kind}`, text: i.kind })),
      el("td", { text: i.detail }),
      el("td", {}, i.original ? el("code", { text: i.original }) : ""))));
    box.append(el("details", { class: "finding", open: "" },
      el("summary", {}, el("span", { class: "loc", text: f.path }), el("span", { class: "rule", text: Object.entries(f.counts).map(([k, v]) => `${k} ${v}`).join(" · ") })),
      el("div", { class: "table-wrap" }, el("table", {}, rows)),
      el("h4", { text: "Revealed text" }),
      el("pre", { class: "revealed", text: f.revealed })));
  }
}

function renderInstructions(root, job, review) {
  const box = $(".instructions", root);
  const e = review.explanation;
  if (!e) { box.append(el("div", { class: "empty", text: "Run the scan with an LLM model to get the plain-English list of instructions." })); return; }
  if (e.error && !e.items.length) { box.append(el("div", { class: "empty", text: `Explain failed: ${e.error}` })); return; }
  $(".instr-count", root).textContent = `(${e.items.length})`;
  box.append(el("p", { class: "purpose", text: `Stated purpose: ${e.purpose}` }));
  for (const c of e.coverage) {
    box.append(el("p", { class: c.uncovered.length ? "cov warn" : "cov", text:
      `${c.file}: ${c.percent}% of lines described` + (c.uncovered.length ? ` · NOT described: lines ${c.uncovered.join(", ")}` : "") }));
  }
  const tbody = el("tbody");
  e.items.forEach((item, index) => {
    const risky = ["high", "critical"].includes(item.risk);
    const box_ = el("input", { type: "checkbox", "data-index": String(index) });
    box_.checked = !risky && item.grounded;
    tbody.append(el("tr", { class: risky ? "risky" : "" },
      el("td", {}, box_),
      el("td", { class: "num", text: `${item.file}:${item.start_line}${item.end_line !== item.start_line ? "-" + item.end_line : ""}` }),
      el("td", {}, el("span", { class: `risk risk-${item.risk}`, text: item.risk })),
      el("td", { text: item.category }),
      el("td", {}, el("div", { text: item.plain_english }), item.grounded ? "" : el("div", { class: "desc", text: "⚠ quote not found in the file" }))));
  });
  box.append(el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { text: "Keep" }), el("th", { text: "Source" }), el("th", { text: "Risk" }), el("th", { text: "Type" }), el("th", { text: "What it tells the agent" }))),
    tbody)));
  const out = el("div", { class: "rewrite-out" });
  const button = el("button", { class: "primary", text: "Build clean rewrite from kept instructions" });
  button.addEventListener("click", async () => {
    const keep = [...tbody.querySelectorAll("input:checked")].map((c) => Number(c.dataset.index));
    button.disabled = true;
    out.replaceChildren(el("span", { class: "spinner" }), "Writing a clean SKILL.md…");
    try {
      const r = await api(`/api/jobs/${job.id}/rewrite`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keep }) });
      showRewrite(out, r, job);
    } catch (err) { out.replaceChildren(el("p", { text: `Rewrite failed: ${err.message}` })); }
    button.disabled = false;
  });
  box.append(el("p", { class: "hint", text: "High and critical instructions start unticked. The rewrite uses only the ticked plain-English instructions, never the original text, then re-checks the result. Bundled scripts are not rewritten." }), button, out);
  if (review.rewrite) showRewrite(out, review.rewrite, job);
}

function showRewrite(out, r, job) {
  out.replaceChildren(
    el("p", { class: r.clean ? "cov" : "cov warn", text: r.error ? `Rewrite failed: ${r.error}` : r.clean ? "Re-check passed: no fork rule findings and nothing hidden." : `Re-check found ${r.recheck_findings.length} finding(s) and ${r.hidden_items} hidden item(s): review before use.` }),
    ...(r.recheck_findings || []).map((f) => el("p", { class: "desc", text: `${f.rule_id} line ${f.line}: ${f.message}` })),
    r.skill_md ? el("pre", { text: r.skill_md }) : "",
    r.skill_md ? el("button", { text: "Download SKILL.md", onclick: () => {
      const a = el("a", { href: URL.createObjectURL(new Blob([r.skill_md], { type: "text/markdown" })), download: "SKILL.md" });
      a.click(); URL.revokeObjectURL(a.href);
    } }) : "");
}

function renderFinding(i) {
  const loc = i.location || {};
  const where = loc.file ? `${loc.file}:${loc.start_line ?? "?"}${loc.end_line && loc.end_line !== loc.start_line ? "-" + loc.end_line : ""}` : "";
  const body = el("div", { class: "fbody" });
  const section = (title, text, pre = false) => {
    if (!text) return;
    body.append(el("h4", { text: title }), el(pre ? "pre" : "p", { text: String(text) }));
  };
  section("Category", [i.category, i.pattern].filter(Boolean).join(" · "));
  section("Explanation", i.explanation);
  section("Code", i.code_snippet, true);
  if (i.evidence && typeof i.evidence === "object" && Object.keys(i.evidence).length) {
    section("Evidence", JSON.stringify(i.evidence, null, 2), true);
  }
  section("Remediation", i.remediation);
  section("Confidence", i.confidence != null ? `${Math.round(i.confidence * 100)}%` : "");
  return el(
    "details",
    { class: "finding" },
    el(
      "summary",
      {},
      el("span", { class: `sev ${i.severity}`, text: i.severity }),
      el("span", { class: "rule", text: i.id }),
      el("span", { class: "ftitle", text: i.finding || i.pattern || "" }),
      el("span", { class: "loc", text: where })
    ),
    body
  );
}

// ---- GitHub search ----
$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#query").value.trim();
  if (!q) return;
  const out = $("#search-results");
  out.replaceChildren(el("div", { class: "card" }, el("span", { class: "spinner" }), "Searching GitHub…"));
  try {
    const repos = await api(`/api/search?q=${encodeURIComponent(q)}`);
    out.replaceChildren(renderRepos(repos));
  } catch (err) {
    out.replaceChildren(el("div", { class: "card", text: `Search failed: ${err.message}` }));
  }
});

function renderRepos(repos) {
  if (!repos.length) return el("div", { class: "card empty", text: "No repositories found." });
  const tbody = el("tbody");
  for (const r of repos) tbody.append(repoRow(r));
  return el(
    "div",
    { class: "card table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", { text: "Trust" }), el("th", { text: "Repository" }),
        el("th", { text: "Stars" }), el("th", { class: "hide-sm", text: "Forks" }),
        el("th", { class: "hide-sm", text: "Issues" }), el("th", { text: "Age" }), el("th", { text: "" }))),
      tbody)
  );
}

function repoRow(r) {
  const signals = el("ul", { class: "signals" }, ...r.signals.map((s) => el("li", { text: s })));
  const contrib = r.contributors != null ? ` · ${r.contributors} contributors` : "";
  const row = el("tr", {},
    el("td", {}, el("span", { class: `trust ${r.level}`, text: `${r.score}` })),
    el("td", {},
      el("a", { href: r.html_url, target: "_blank", rel: "noopener noreferrer", text: r.full_name }),
      el("div", { class: "desc", text: `${r.owner_type}${r.license ? " · " + r.license : ""}${contrib}` }),
      el("div", { class: "desc", text: r.description }),
      signals),
    el("td", { class: "num", text: r.stars.toLocaleString() }),
    el("td", { class: "num hide-sm", text: r.forks.toLocaleString() }),
    el("td", { class: "num hide-sm", text: r.open_issues.toLocaleString() }),
    el("td", { class: "num", text: r.age_days >= 365 ? `${(r.age_days / 365).toFixed(1)} y` : `${r.age_days} d` }),
    el("td", {},
      el("button", { text: "Details", onclick: async (e) => {
        e.target.disabled = true;
        try { row.replaceWith(repoRow(await api(`/api/repo/${r.full_name}`))); }
        catch (err) { e.target.textContent = "failed"; }
      } }),
      " ",
      el("button", { class: "primary", text: "Add to batch", onclick: async (e) => {
        e.target.disabled = true;
        const added = await api("/api/repos", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sources: [r.html_url] }) });
        for (const x of added) autoSelect.add(x.id);
        e.target.textContent = "Added ✓";
        loadRepos();
      } }))
  );
  return row;
}

// ---- skill library ----
const openLibrary = new Set();
const selectedSkills = new Map(); // repo id -> Set(skill path)
async function loadHistory() {
  const box = $("#history");
  let lib;
  try { lib = await api("/api/library"); } catch (err) { box.replaceChildren(el("div", { class: "empty", text: err.message })); return; }
  const filter = ($("#history-filter").value || "").toLowerCase();
  const showDeleted = $("#show-deleted").checked;
  const shown = lib.filter((r) => !filter || r.source.toLowerCase().includes(filter) || r.name.toLowerCase().includes(filter)
    || r.skills.some((s) => s.name.toLowerCase().includes(filter)));
  if (!shown.length) { box.replaceChildren(el("div", { class: "empty", text: lib.length ? "Nothing matches the filter." : "No repositories yet. Add them on the Scan page." })); return; }
  box.replaceChildren(...shown.map((r) => libraryBlock(r, filter, showDeleted)));
}
$("#history-filter").addEventListener("input", () => loadHistory());
$("#show-deleted").addEventListener("change", () => loadHistory());

function libraryBlock(r, filter, showDeleted) {
  const remaining = r.skills.filter((s) => s.state !== "deleted");
  const count = (rec) => remaining.filter((s) => s.recommendation === rec).length;
  const red = count("DO_NOT_INSTALL"), yellow = count("CAUTION"), green = count("SAFE");
  const selected = selectedSkills.get(r.id) || new Set();
  selectedSkills.set(r.id, selected);

  const act = async (body, button) => {
    try {
      const res = await api(`/api/library/${r.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (button) button.textContent = `${res.changed} changed`;
      selected.clear();
      loadHistory();
    } catch (err) { if (button) button.textContent = err.message.slice(0, 50); }
  };
  const groupButton = (text, cls, title, body, confirmText) => {
    const b = el("button", { class: cls, text, title });
    b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      if (confirmText && !confirmInline(b, confirmText)) return;
      act(typeof body === "function" ? body() : body, b);
    });
    return b;
  };
  const exportLink = el("a", { class: "btn-like", href: `/api/library/${r.id}/export`, text: "Export kept skills (.zip)",
    title: "Download the remaining skills with a verdict manifest" });
  exportLink.addEventListener("click", (e) => e.stopPropagation());

  const actions = el("div", { class: "lib-actions" },
    red ? groupButton(`Delete red (${red})`, "danger", "Delete every do-not-install skill", { action: "delete", mode: "red" }, "Click again to delete") : "",
    red + yellow ? groupButton(`Delete red + yellow (${red + yellow})`, "danger", "Delete every skill that is not green", { action: "delete", mode: "red_yellow" }, "Click again to delete") : "",
    groupButton("Delete selected", "danger", "Delete the ticked skills", () => ({ action: "delete", mode: "selected", skills: [...selected] })),
    groupButton("Keep selected", "", "Mark the ticked skills as reviewed and kept", () => ({ action: "keep", mode: "selected", skills: [...selected] })),
    r.deleted ? groupButton(`Restore all (${r.deleted})`, "", "Bring back every deleted skill", { action: "restore", mode: "all" }) : "",
    exportLink);

  const status = r.cleaned
    ? el("span", { class: "rec-badge SAFE big", text: "✓ CLEANED" })
    : el("span", { class: "rec-badge", text: r.unscanned ? `${r.unscanned} not scanned` : "needs review" });
  const rows = r.skills
    .filter((s) => (showDeleted || s.state !== "deleted") && (!filter || r.name.toLowerCase().includes(filter) || s.name.toLowerCase().includes(filter)))
    .map((s) => librarySkillRow(r, s, selected, act));
  const block = el("details", { class: "batch library" + (r.cleaned ? " cleaned" : "") },
    el("summary", {},
      icon(/^https?:\/\//.test(r.source) ? "github" : "folder"),
      el("span", { class: "ftitle" }, el("strong", { text: r.name }), el("span", { class: "desc", text: ` ${r.remaining} skill(s) kept${r.deleted ? ` · ${r.deleted} deleted` : ""}` })),
      el("span", { class: "sev-counts" },
        red ? el("span", { class: "sev-mini CRITICAL", text: `${red} red` }) : "",
        yellow ? el("span", { class: "sev-mini MEDIUM", text: `${yellow} yellow` }) : "",
        green ? el("span", { class: "sev-mini LOW", text: `${green} green` }) : ""),
      status,
      el("button", { class: "link", text: "view all results", onclick: (e) => { e.preventDefault(); e.stopPropagation(); showCombined(r.source, null); } })),
    actions,
    el("div", { class: "item-list" }, ...rows));
  block.open = openLibrary.has(r.id) || !!filter;
  block.addEventListener("toggle", () => { block.open ? openLibrary.add(r.id) : openLibrary.delete(r.id); });
  return block;
}

function librarySkillRow(r, s, selected, act) {
  const box = el("input", { type: "checkbox", title: "Select" });
  box.checked = selected.has(s.path);
  box.addEventListener("click", (e) => e.stopPropagation());
  box.addEventListener("change", () => { box.checked ? selected.add(s.path) : selected.delete(s.path); });
  const counts = SEV_ORDER.filter((sev) => s.counts[sev]).map((sev) => el("span", { class: `sev-mini ${sev}`, text: `${s.counts[sev]} ${sev.toLowerCase()}` }));
  const single = s.state === "deleted"
    ? el("button", { text: "Restore", onclick: (e) => { e.stopPropagation(); act({ action: "restore", mode: "selected", skills: [s.path] }); } })
    : el("button", { class: "icon-btn", text: "×", title: "Delete this skill", onclick: (e) => { e.stopPropagation(); act({ action: "delete", mode: "selected", skills: [s.path] }); } });
  const row = el("div", { class: "item-head clickable lib-row" + (s.state === "deleted" ? " deleted" : ""), title: "Show all results for this skill" },
    el("span", { class: "lib-check" }, box, el("span", { class: `dot ${s.recommendation || "none"}` })),
    el("span", { class: "task-name" }, el("strong", { text: s.name }), el("span", { class: "desc", text: s.path })),
    el("span", { class: "sev-counts" }, ...counts),
    el("span", { class: "method-badges" }, ...s.methods.map((m) => el("span", { class: `rec-badge ${m.recommendation || ""}`, text: `${m.label}: ${m.score ?? "–"}` }))),
    el("span", { class: "lib-state" }, s.state === "kept" ? el("span", { class: "rec-badge SAFE", text: "kept" }) : s.state === "deleted" ? el("span", { class: "rec-badge DO_NOT_INSTALL", text: "deleted" }) : ""),
    el("span", { class: "task-actions" }, single));
  row.addEventListener("click", () => s.methods.length && showCombined(r.source, s.name));
  return row;
}

async function showCombined(source, skill) {
  const box = $("#history-detail");
  box.replaceChildren(el("div", { class: "card" }, el("span", { class: "spinner" }), "Loading results…"));
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  let data;
  try {
    data = await api(`/api/combined?${new URLSearchParams(skill ? { source, skill } : { source })}`);
  } catch (err) { box.replaceChildren(el("div", { class: "card", text: `Could not load: ${err.message}` })); return; }
  const close = el("button", { text: "Close", onclick: () => box.replaceChildren() });
  const nav = el("div", { class: "card combined-nav" },
    el("div", { class: "findings-head" },
      el("h2", { class: "step", text: `${data.name}${skill ? " / " + skill : ` · ${data.skills.length} skill(s)`}` }), close));
  if (!skill) {
    const worstCounts = Object.fromEntries(REC_ORDER.map((r) => [r, data.skills.filter((s) => s.recommendation === r).length]));
    nav.append(el("p", { class: "hint", text: `${worstCounts.DO_NOT_INSTALL} do not install · ${worstCounts.CAUTION} caution · ${worstCounts.SAFE} safe` }));
  }
  box.replaceChildren(nav, ...data.skills.map((s, i) => combinedCard(s, data, !skill && i > 0)));
}

function combinedCard(s, data, collapsed) {
  const root = $("#tpl-combined").content.firstElementChild.cloneNode(true);
  $(".c-title", root).textContent = s.skill;
  $(".c-sub", root).textContent = `${data.name} · ${s.methods.length} method(s) · ${s.issues.length} merged finding(s)`;
  if (s.recommendation) $(".c-rec", root).replaceChildren(el("span", { class: `rec-badge big ${s.recommendation}`, text: s.recommendation.replaceAll("_", " ") }));
  $(".c-methods", root).append(...s.methods.map((m) => {
    const card = el("div", { class: "method-card" },
      el("div", { class: "method-name", text: m.label || m.operation }),
      el("div", {}, el("span", { class: `rec-badge ${m.recommendation || ""}`, text: m.recommendation ? `${m.score ?? "–"} · ${m.recommendation.replaceAll("_", " ")}` : "done" })),
      el("div", { class: "desc", text: m.finished ? new Date(m.finished * 1000).toLocaleString() : "" }),
      el("button", { class: "link", text: "open full report", onclick: () => { showTab("batch"); openTask(m.id); } }));
    return card;
  }));
  root.querySelectorAll(".subtab").forEach((b) => b.addEventListener("click", () => {
    root.querySelectorAll(".subtab").forEach((x) => x.classList.toggle("active", x === b));
    root.querySelectorAll(".subpanel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== b.dataset.panel));
  }));
  renderMergedFindings(root, s.issues);
  renderHidden(root, s.reveal || []);
  renderInstructions(root, { id: s.explain_job }, { explanation: s.explanation });
  renderSandbox(root, s.sandbox);
  if (collapsed) {
    const body = el("div", { class: "hidden" }, ...[...root.children].slice(1));
    const head = root.firstElementChild;
    const toggle = el("button", { class: "link", text: "show results" });
    toggle.addEventListener("click", () => { body.classList.toggle("hidden"); toggle.textContent = body.classList.contains("hidden") ? "show results" : "hide results"; });
    head.append(toggle);
    root.replaceChildren(head, body);
  }
  return root;
}

function renderMergedFindings(root, issues) {
  // Same filters as a single report, plus which methods found each issue.
  $(".count", root).textContent = `(${issues.length})`;
  const active = new Set(SEV_ORDER);
  const filters = $(".filters", root);
  for (const sev of SEV_ORDER) {
    const n = issues.filter((i) => i.severity === sev).length;
    if (!n) continue;
    const b = el("button", { class: "on", text: `${sev} ${n}` });
    b.addEventListener("click", () => { active.has(sev) ? active.delete(sev) : active.add(sev); b.classList.toggle("on"); draw(); });
    filters.append(b);
  }
  function draw() {
    const list = $(".findings", root);
    list.replaceChildren();
    const shown = issues.filter((i) => active.has(i.severity));
    if (!shown.length) { list.append(el("div", { class: "empty", text: issues.length ? "No findings match the filter." : "No findings from any method." })); return; }
    for (const i of shown) {
      const node = renderFinding(i);
      $("summary", node).append(el("span", { class: "found-by" }, ...(i.found_by || []).map((m) => el("span", { class: "op-chip", text: m }))));
      list.append(node);
    }
  }
  draw();
}

Promise.all([loadRepos(), loadOperations(), loadJobs()]).catch((err) => { $("#run-error").textContent = `Could not reach the server: ${err.message}`; });

// ---- pins ----
async function loadPins() {
  const box = $("#pins");
  const list = await api("/api/pins");
  if (!list.length) { box.replaceChildren(el("div", { class: "empty", text: "No pins yet. Open a finished scan and click Pin this version." })); return; }
  const tbody = el("tbody");
  for (const p of list) {
    const status = el("td", { text: "–" });
    tbody.append(el("tr", {},
      el("td", { text: p.name }),
      el("td", { class: "desc", text: p.source }),
      el("td", { class: "num", text: new Date(p.pinned_at * 1000).toLocaleString() }),
      el("td", { class: "num", text: (p.commit || "-").slice(0, 10) }),
      el("td", { text: p.verdict ? `${p.verdict.score} · ${(p.verdict.recommendation || "").replaceAll("_", " ")}` : "" }),
      status,
      el("td", {}, el("button", { text: "Verify", onclick: async (e) => {
        e.target.disabled = true;
        status.replaceChildren(el("span", { class: "spinner" }));
        try {
          const d = await api(`/api/pins/${encodeURIComponent(p.name)}/verify`, { method: "POST" });
          status.replaceChildren(d.clean
            ? el("span", { class: "cov", text: "unchanged" })
            : el("span", { class: "cov warn", text: `CHANGED: ${d.changed.length} modified, ${d.added.length} added, ${d.removed.length} removed${d.new_commit !== d.old_commit ? " · new commit " + (d.new_commit || "?").slice(0, 10) : ""}` }));
        } catch (err) { status.textContent = `failed: ${err.message}`; }
        e.target.disabled = false;
      } }))));
  }
  box.replaceChildren(el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, ...["Name", "Source", "Pinned", "Commit", "Verdict then", "Status", ""].map((t) => el("th", { text: t })))),
    tbody)));
}
