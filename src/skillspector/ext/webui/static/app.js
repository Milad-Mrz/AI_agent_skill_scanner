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
  if (name === "pins") loadPins();
}

// ---- profiles ----
let profiles = [];
async function loadProfiles() {
  profiles = await api("/api/profiles");
  const select = $("#profile");
  select.replaceChildren();
  const groups = [
    ["No LLM", (p) => p.id === "static"],
    ["Hosted frontier models", (p) => ["anthropic", "openai"].includes(p.provider)],
    ["Open-weight models (hosted)", (p) => p.provider === "nv_build"],
    ["Open-weight models (local)", (p) => p.provider === "ollama"],
  ];
  for (const [title, match] of groups) {
    const group = el("optgroup", { label: title });
    for (const p of profiles.filter(match)) {
      const label = p.ready ? p.label : `${p.label} (needs ${p.missing})`;
      const opt = el("option", { value: p.id, text: label });
      if (!p.ready) opt.disabled = true;
      group.append(opt);
    }
    if (group.children.length) select.append(group);
  }
  const preferred = profiles.find((p) => p.id === "sol-medium" && p.ready) || profiles.find((p) => p.ready && p.id !== "static");
  if (preferred) select.value = preferred.id;
  updateHint();
}
function updateHint() {
  const p = profiles.find((x) => x.id === $("#profile").value);
  $("#profile-hint").textContent = !p
    ? ""
    : p.id === "static"
      ? "Static rules only: fast and free, but misses most wording-only and two-step attacks."
      : p.provider === "ollama"
        ? `Static rules + ${p.label} (${p.model}) semantic review on this machine. Free, private, but much weaker than frontier models.`
        : `Static rules + ${p.label} (${p.model}) semantic review. Uses API credit.`;
}
$("#profile").addEventListener("change", updateHint);

// ---- scanning ----
let pollTimer = null;
let uploadFile = null;
$("#upload").addEventListener("change", (e) => {
  uploadFile = e.target.files[0] || null;
  if (uploadFile) $("#source").value = `upload: ${uploadFile.name}`;
});
$("#source").addEventListener("input", () => { uploadFile = null; });

$("#scan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const profile = $("#profile").value;
  const source = $("#source").value.trim();
  if (!source && !uploadFile) return;
  setBusy(true);
  try {
    let job;
    if (uploadFile) {
      const form = new FormData();
      form.append("file", uploadFile);
      form.append("profile", profile);
      form.append("strict", $("#strict").checked ? "true" : "false");
      job = await api("/api/scan/upload", { method: "POST", body: form });
    } else {
      job = await api("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, profile, strict: $("#strict").checked }),
      });
    }
    watchJob(job.id);
  } catch (err) {
    showStatus(`Error: ${err.message}`, false);
    setBusy(false);
  }
});

function setBusy(busy) { $("#scan-btn").disabled = busy; }

function showStatus(text, spinning) {
  const box = $("#job-status");
  box.classList.remove("hidden");
  box.replaceChildren(spinning ? el("span", { class: "spinner" }) : "", el("span", { text }));
}

function watchJob(id) {
  clearInterval(pollTimer);
  $("#result").classList.add("hidden");
  const t0 = Date.now();
  const tick = async () => {
    let job;
    try { job = await api(`/api/jobs/${id}`); } catch (err) { showStatus(`Error: ${err.message}`, false); clearInterval(pollTimer); setBusy(false); return; }
    const secs = Math.round((Date.now() - t0) / 1000);
    if (job.status === "queued" || job.status === "running") {
      showStatus(`Scanning ${job.source} with ${job.profile} … ${secs}s`, true);
      return;
    }
    clearInterval(pollTimer);
    setBusy(false);
    if (job.status === "error" || job.status === "interrupted") { showStatus(`Scan ${job.status}: ${job.error}`, false); return; }
    $("#job-status").classList.add("hidden");
    renderReport(job);
  };
  pollTimer = setInterval(tick, 1500);
  tick();
}

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
    `${job.source} · ${job.strict ? "strict verdict" : "upstream verdict"} · severity ${verdict.severity || "?"} · ` +
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
      el("button", { class: "primary", text: "Scan", onclick: () => {
        $("#source").value = r.html_url; uploadFile = null; showTab("scan"); $("#scan-form").requestSubmit();
      } }))
  );
  return row;
}

// ---- history ----
async function loadHistory() {
  const box = $("#history");
  const jobs = await api("/api/jobs");
  if (!jobs.length) { box.replaceChildren(el("div", { class: "empty", text: "No scans yet." })); return; }
  const tbody = el("tbody");
  for (const j of jobs) {
    tbody.append(el("tr", {},
      el("td", { text: new Date(j.started * 1000).toLocaleString() }),
      el("td", { text: j.source }),
      el("td", { text: j.profile + (j.strict ? " · strict" : "") }),
      el("td", { text: j.status === "done" ? `${j.score} · ${(j.recommendation || "").replaceAll("_", " ")}` : j.status }),
      el("td", {},
        j.status === "done" ? el("button", { text: "Open", onclick: () => { showTab("scan"); watchJob(j.id); } }) : "",
        " ",
        el("button", { text: "Delete", onclick: async () => { await api(`/api/jobs/${j.id}`, { method: "DELETE" }); loadHistory(); } }))));
  }
  box.replaceChildren(el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { text: "Time" }), el("th", { text: "Source" }), el("th", { text: "Model" }), el("th", { text: "Result" }), el("th", { text: "" }))),
    tbody)));
}

loadProfiles().catch((err) => { $("#profile-hint").textContent = `Could not load profiles: ${err.message}`; });

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
