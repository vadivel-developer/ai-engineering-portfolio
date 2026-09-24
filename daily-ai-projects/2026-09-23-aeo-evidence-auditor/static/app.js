"use strict";
// All untrusted strings (page titles, quotes, model output) are inserted with textContent, never innerHTML.

const AGENTS = {
  crawler: ["Crawler", "Fetches same-site pages, respects robots.txt, quarantines injected instructions"],
  technical_auditor: ["Technical Auditor", "Rule-based checks: titles, meta, H1, noindex, links, JSON-LD"],
  intent_analyst: ["Intent Analyst", "Classifies each query and searches the site for its best page"],
  gap_strategist: ["Gap Strategist", "Proposes content changes backed by quotes or missing terms"],
  evidence_verifier: ["Evidence Verifier", "Re-checks every claim; sends failures back for one revision"],
  human_review: ["Human review", "You approve or reject each verified recommendation"],
};
const STATE_TEXT = { pending: "Waiting", running: "Working", done: "Done", skipped: "Skipped", failed: "Failed" };
const STATUS_TEXT = {
  queued: "Queued", running: "Agents working", awaiting_review: "Waiting for your review",
  completed: "Review complete", failed: "Failed",
};
const TYPE_TEXT = {
  fix_technical: "Technical fix", expand_page: "Expand page", add_faq: "Add Q&A", add_schema: "Structured data",
  new_page: "New page",
};

const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) n.append(kid instanceof Node ? kid : String(kid));
  return n;
}
const shortUrl = (u) => { try { const p = new URL(u); return p.pathname === "/" ? p.host + "/" : p.pathname; } catch { return u; } };

let config = null;
let runId = null;
let pollTimer = null;
let reviewRendered = false;

// ------------------------------------------------------------------ API

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    let msg = body && (body.error || body.detail);
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return body;
}

// ------------------------------------------------------------------ setup

async function init() {
  renderPipeline([]);
  try {
    config = await api("/api/config");
  } catch (e) {
    setMode("Backend unreachable. Start the server and reload.", "down");
    $("run-btn").disabled = true;
    return;
  }
  if (config.provider === "demo") setMode("Demonstration mode: deterministic rules, no language model", "demo");
  else if (config.model_ready) setMode(config.mode, "model");
  else setMode(`${config.mode} unavailable: ${config.model_detail}`, "down");

  const sel = $("sample");
  for (const s of config.samples) sel.append(el("option", { value: s.id, text: s.name }));
  const applySample = () => {
    const s = config.samples.find((x) => x.id === sel.value);
    if (!s) return;
    $("sample-desc").textContent = s.description;
    $("queries").value = s.default_queries.join("\n");
  };
  sel.addEventListener("change", applySample);
  applySample();

  if (!config.live_crawl_enabled) {
    $("source-live").disabled = true;
    $("source-live").parentElement.title = "Live crawling is disabled on this server";
    $("live-hint").textContent = "Live crawling is disabled on this server.";
  }
  document.querySelectorAll("input[name=source]").forEach((r) => r.addEventListener("change", () => {
    const live = document.querySelector("input[name=source]:checked").value === "live";
    $("live-box").hidden = !live;
    $("sample-box").hidden = live;
  }));
  $("max-pages").max = String(config.max_pages);
}

function setMode(text, kind) {
  const m = $("mode");
  m.textContent = text;
  m.className = `mode ${kind}`;
}

// ------------------------------------------------------------------ start run

$("audit-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("form-error").textContent = "";
  const queries = $("queries").value.split("\n").map((q) => q.trim()).filter(Boolean);
  const qErr = !queries.length ? "Add at least one search query."
    : queries.length > 12 ? "Use 12 queries or fewer."
    : queries.some((q) => q.length > 120) ? "Each query must be 120 characters or fewer." : "";
  $("queries-error").textContent = qErr;
  $("queries").setAttribute("aria-invalid", qErr ? "true" : "false");
  if (qErr) { $("queries").focus(); return; }

  const source = document.querySelector("input[name=source]:checked").value;
  const body = { source, queries, max_pages: Number($("max-pages").value) || 15 };
  if (source === "sample") body.sample_id = $("sample").value;
  else body.start_url = $("start-url").value.trim();

  const btn = $("run-btn");
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    const { id } = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
    runId = id;
    reviewRendered = false;
    $("empty").hidden = true;
    $("results").hidden = false;
    $("review-form").hidden = true;
    $("report").hidden = true;
    $("review-loading").hidden = false;
    $("review-loading").textContent = "The agents are still working. Recommendations appear here once the verifier has checked them.";
    selectTab("tab-review");
    // On a stacked (mobile) layout the results are below the form; take the user to them.
    if (window.matchMedia("(max-width: 900px)").matches) {
      $("main").focus({ preventScroll: true });
      $("main").scrollIntoView({ block: "start" });
    }
    poll();
  } catch (e) {
    $("form-error").textContent = e.message;
    btn.disabled = false;
    btn.textContent = "Run audit";
  }
});

async function poll() {
  clearTimeout(pollTimer);
  let run;
  try {
    run = await api(`/api/runs/${runId}`);
  } catch (e) {
    $("run-error").hidden = false;
    $("run-error").textContent = `Lost contact with the server: ${e.message}`;
    resetRunButton();
    return;
  }
  render(run);
  if (run.status === "queued" || run.status === "running") pollTimer = setTimeout(poll, 700);
  else resetRunButton();
}

function resetRunButton() {
  const btn = $("run-btn");
  btn.disabled = false;
  btn.textContent = "Run audit";
}

// ------------------------------------------------------------------ render

function render(run) {
  $("run-title").textContent = run.request.source === "sample" ? "Brightwater Plumbing Co. sample" : shortUrl(run.request.start_url || "");
  $("run-meta").textContent = `${run.request.queries.length} queries, up to ${run.request.max_pages} pages. ${run.mode}.`;
  const pill = $("status-pill");
  pill.textContent = STATUS_TEXT[run.status] || run.status;
  pill.className = `status-pill ${run.status}`;
  $("run-error").hidden = !run.error;
  $("run-error").textContent = run.error ? `The audit stopped: ${run.error}` : "";

  renderPipeline(run.stages);
  const calls = run.stages.reduce((a, s) => a + s.llm_calls, 0);
  const pt = run.stages.reduce((a, s) => a + s.prompt_tokens, 0);
  const ot = run.stages.reduce((a, s) => a + s.output_tokens, 0);
  const total = run.stages.filter((s) => s.agent !== "human_review").reduce((a, s) => a + (s.elapsed_ms || 0), 0);
  $("usage").textContent = run.stages.length
    ? `Agent time ${(total / 1000).toFixed(1)} s · ${calls} model calls · ${pt.toLocaleString()} prompt / ${ot.toLocaleString()} output tokens`
    : "";

  $("c-findings").textContent = run.findings.length || "";
  $("c-intents").textContent = run.intents.length || "";
  $("c-pages").textContent = run.pages.length || "";
  $("c-trace").textContent = run.trace.length || "";
  renderFindings(run);
  renderIntents(run);
  renderPages(run);
  renderTrace(run);

  if (run.status === "failed") {
    $("review-loading").hidden = false;
    $("review-loading").textContent = "No recommendations: the audit stopped before verification. See the Trace tab for details.";
  }
  if ((run.status === "awaiting_review" || run.status === "completed") && !reviewRendered) {
    renderReview(run);
    reviewRendered = true;
    if (window.matchMedia("(max-width: 900px)").matches) $("main").scrollIntoView({ block: "start" });
  }
}

function renderPipeline(stages) {
  const list = $("pipeline");
  list.replaceChildren();
  for (const [key, [name, role]] of Object.entries(AGENTS)) {
    const s = stages.find((x) => x.agent === key) || { status: "pending" };
    const time = s.elapsed_ms != null && key !== "human_review" ? ` · ${(s.elapsed_ms / 1000).toFixed(1)} s` : "";
    list.append(el("li", { class: s.status, "aria-current": s.status === "running" ? "step" : null },
      el("span", { class: "name", text: name }),
      el("span", { class: "state", text: (key === "human_review" && s.status === "running" ? "Your turn" : STATE_TEXT[s.status] || s.status) + time }),
      el("span", { class: "role", text: role })));
  }
}

function evidenceItem(e) {
  const where = el("span", { class: "where", text: shortUrl(e.url) });
  if (e.kind === "quote") return el("li", {}, el("span", { class: "kind", text: "Quote" }), el("span", {}, el("mark", { text: `“${e.text}”` }), where));
  if (e.kind === "absent") return el("li", {}, el("span", { class: "kind", text: "Missing" }), el("span", {}, el("mark", { class: "absent", text: e.text }), " never appears", where));
  return el("li", {}, el("span", { class: "kind", text: e.field }), el("span", {}, el("mark", { text: e.text }), where));
}

function renderReview(run) {
  $("review-loading").hidden = true;
  const verdicts = Object.fromEntries(run.verifications.map((v) => [v.rec_id, v]));
  const verified = run.recommendations.filter((r) => verdicts[r.id]?.verdict === "verified");
  const rejected = run.recommendations.filter((r) => verdicts[r.id]?.verdict === "rejected");
  $("c-review").textContent = verified.length || "";

  if (run.status === "completed") { showReport(run); return; }
  $("review-form").hidden = false;
  $("review-summary").textContent = `${verified.length} verified recommendations need a decision. ${rejected.length} failed verification and are shown for transparency only.`;

  const list = $("rec-list");
  list.replaceChildren();
  const bySource = (src) => verified.filter((r) => r.source_agent === src);
  const group = (title, recs) => { if (recs.length) { list.append(el("h3", { class: "group-title", text: `${title} (${recs.length})` })); recs.forEach((r) => list.append(recCard(r, verdicts[r.id]))); } };
  group("Content and answer-engine gaps", bySource("gap_strategist"));
  group("Technical fixes", bySource("technical_auditor"));
  group("Failed verification, cannot be approved", rejected);
  if (!run.recommendations.length) list.append(el("p", { class: "loading", text: "The agents found nothing to recommend for these queries." }));
}

function recCard(r, v) {
  const ok = v.verdict === "verified";
  const card = el("article", { class: `rec ${v.verdict}`, "aria-labelledby": `h-${r.id}` });
  const tags = el("div", { class: "tags" },
    el("span", { class: `tag ${r.priority}`, text: `${r.priority} priority` }),
    el("span", { class: "tag", text: TYPE_TEXT[r.type] || r.type }),
    r.query ? el("span", { class: "tag", text: `“${r.query}”` }) : null,
    el("span", { class: "tag", text: `${r.source_agent === "gap_strategist" ? "Gap Strategist" : "Technical Auditor"}${r.revision ? ", revised" : ""}` }),
    el("span", { class: "tag", text: ok ? "Evidence verified" : "Evidence rejected" }));
  const top = el("div", { class: "rec-top" }, el("div", {}, el("h4", { id: `h-${r.id}`, text: r.title }), tags));
  if (ok) {
    const name = `d-${r.id}`;
    top.append(el("fieldset", { class: "decision" },
      el("legend", { class: "sr-only", text: `Decision for ${r.title}` }),
      el("label", {}, el("input", { type: "radio", name, value: "approved", checked: true }), "Approve"),
      el("label", {}, el("input", { type: "radio", name, value: "rejected" }), "Reject")));
  }
  card.append(top, el("p", { class: "rec-detail", text: r.detail }),
    el("ul", { class: "evidence", "aria-label": "Evidence" }, r.evidence.map(evidenceItem)));
  if (!ok) card.append(el("ul", { class: "reasons", "aria-label": "Why verification failed" }, v.reasons.map((t) => el("li", { text: t }))));
  if (ok) {
    card.append(el("label", { class: "note-label", for: `n-${r.id}`, text: "Note for the report (optional)" }),
      el("textarea", { id: `n-${r.id}`, rows: 1, maxlength: 500, "data-rec": r.id }));
  }
  return card;
}

function setAll(value) {
  document.querySelectorAll(`#rec-list input[type=radio][value=${value}]`).forEach((i) => { i.checked = true; });
}
$("approve-all").addEventListener("click", () => setAll("approved"));
$("reject-all").addEventListener("click", () => setAll("rejected"));

$("review-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const reviewer = $("reviewer").value.trim();
  $("review-error").textContent = "";
  if (!reviewer) {
    $("review-error").textContent = "Enter your name so the report records who approved it.";
    $("reviewer").setAttribute("aria-invalid", "true");
    $("reviewer").focus();
    return;
  }
  $("reviewer").setAttribute("aria-invalid", "false");
  const decisions = [...document.querySelectorAll("#rec-list input[type=radio]:checked")].map((i) => {
    const id = i.name.slice(2);
    return { rec_id: id, decision: i.value, note: document.querySelector(`textarea[data-rec="${id}"]`)?.value.trim() || "" };
  });
  const btn = $("submit-review");
  btn.disabled = true;
  btn.textContent = "Creating report…";
  try {
    await api(`/api/runs/${runId}/review`, { method: "POST", body: JSON.stringify({ reviewer, decisions }) });
    const run = await api(`/api/runs/${runId}`);
    render(run);
    await showReport(run);
  } catch (e) {
    $("review-error").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Approve selected and create report";
  }
});

async function showReport(run) {
  $("review-form").hidden = true;
  const res = await fetch(`/api/runs/${run.id}/report.md`);
  const text = await res.text();
  $("report-body").textContent = text;
  $("download").href = `/api/runs/${run.id}/report.md`;
  $("download").setAttribute("download", `aeo-audit-${run.id}.md`);
  $("report").hidden = false;
  $("report-title").focus();
}

function renderFindings(run) {
  const body = $("findings-body");
  body.replaceChildren();
  const order = { high: 0, medium: 1, low: 2 };
  [...run.findings].sort((a, b) => order[a.severity] - order[b.severity]).forEach((f) => body.append(el("tr", {},
    el("td", {}, el("span", { class: `sev ${f.severity}`, text: f.severity })),
    el("td", { text: f.check.replaceAll("_", " ") }),
    el("td", { class: "url", text: shortUrl(f.url) }),
    el("td", { text: f.message }))));
  if (!run.findings.length) body.append(el("tr", {}, el("td", { colspan: 4, text: run.status === "running" ? "Checking pages…" : "No technical findings." })));
}

function renderIntents(run) {
  const list = $("intent-list");
  list.replaceChildren();
  if (!run.intents.length) { list.append(el("p", { class: "loading", text: "Intent analysis has not run yet." })); return; }
  run.intents.forEach((i) => list.append(el("article", { class: "intent" },
    el("h3", { text: `“${i.query}”` }),
    el("div", { class: "tags" },
      el("span", { class: "tag", text: i.intent }),
      el("span", { class: `tag ${i.match === "none" ? "high" : i.match === "partial" ? "medium" : ""}`, text: `${i.match} match` }),
      el("span", { class: "tag", text: i.best_url ? shortUrl(i.best_url) : "no page answers it" }),
      el("span", { class: "tag", text: `${i.tool_calls} tool call${i.tool_calls === 1 ? "" : "s"}` })),
    el("p", { text: i.rationale }))));
}

function renderPages(run) {
  const body = $("pages-body");
  body.replaceChildren();
  run.pages.forEach((p) => body.append(el("tr", {},
    el("td", { class: "url", text: shortUrl(p.url) }),
    el("td", { text: p.title || "(no title)" }),
    el("td", { text: p.word_count }),
    el("td", { text: p.jsonld_types.join(", ") || "none" }),
    el("td", {}, p.quarantined.length ? p.quarantined.map((q) => el("div", { class: "quarantine", text: q })) : "none"))));
  const errs = $("crawl-errors");
  errs.replaceChildren();
  run.crawl_errors.forEach((e) => errs.append(el("li", { text: `${shortUrl(e.url)}: ${e.reason}${e.found_on ? ` (linked from ${shortUrl(e.found_on)})` : ""}` })));
  if (!run.crawl_errors.length) errs.append(el("li", { text: "Every discovered page was fetched." }));
}

function renderTrace(run) {
  const list = $("trace");
  list.replaceChildren();
  const t0 = run.trace.length ? run.trace[0].ts : 0;
  run.trace.forEach((t) => list.append(el("li", { class: `k-${t.kind}` },
    el("span", { class: "t", text: `+${(t.ts - t0).toFixed(2)}s` }),
    el("span", { class: "who", text: (AGENTS[t.agent] || ["Orchestrator"])[0] }),
    el("span", { class: "msg", text: `${t.kind === "handoff" ? "Handoff: " : t.kind === "retry" ? "Retry: " : ""}${t.message}` }))));
}

// ------------------------------------------------------------------ tabs (WAI-ARIA pattern)

const tabs = [...document.querySelectorAll("[role=tab]")];
function selectTab(id) {
  for (const t of tabs) {
    const on = t.id === id;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    $(t.getAttribute("aria-controls")).hidden = !on;
  }
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => selectTab(t.id));
  t.addEventListener("keydown", (e) => {
    let j = null;
    if (e.key === "ArrowRight") j = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = tabs.length - 1;
    if (j !== null) { e.preventDefault(); selectTab(tabs[j].id); tabs[j].focus(); }
  });
});

init();
