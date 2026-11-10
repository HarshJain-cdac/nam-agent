// The chat: formatters, the reply card (cards, buttons, undo), the chat log,
// streaming and send. Classic <script>, shared global scope; load order:
// static/README.md.

const secs = ms => ms==null ? "—" : (ms/1000).toFixed(1)+"s";

const gateBadge = g => !g ? "" :
  uiBadge("gate · " + esc(g.decision), g.decision === "retrieve" ? "live" : "neutral")
  + `<span class="meta" style="margin:0">${esc(g.reason||"")}</span>`;

// A tool call renders as a status row (dot + one-line summary); the raw output
// hides behind a disclosure so an ugly osascript error never floods the page.
const toolRow = x => `<div class="tool ${x.status||"ok"}">
  <div class="tool-head"><span class="dot ${x.status||"ok"}"></span><code>${esc(x.tool)}</code>
    ${x.summary?`<span style="color:var(--text-muted)">${esc(x.summary)}</span>`:""}</div>
  ${x.output!==undefined?`<details><summary>args &amp; raw output</summary>
    <pre>${esc(x.tool)}(${esc(JSON.stringify(x.args,null,1))})\n\n${esc(x.output)}</pre>
  </details>`:""}
</div>`;

// A stored history row -> a CHAT item. Assistant rows with saved telemetry
// (meta: cards, question, tools) render as the full turn card, so a reopened
// chat looks as it did live. Rows without meta fall back to a plain card.
function histItem(m){
  if (m.role === "user") return {role:"user", text:m.content};
  if (m.meta) return {role:"agent", reply:stripTools(m.content), gate:m.meta.gate,
                      tools:m.meta.tools, iterations:m.meta.iterations,
                      latency_ms:m.meta.latency_ms, model:m.meta.model,
                      // the NAM cards and the question shown with this turn
                      cards:m.meta.cards || [],
                      ask:m.meta.ask ? askOf(m.meta.ask) : null};
  return {role:"agent", reply:stripTools(m.content), historical:true};
}

// The reply's copy button. It sits inside the card, and CSS shows it only
// while the card is hovered (.card:hover .msg-copy).
const msgCopy = text => uiButton("Copy", {level: "tertiary", size: "sm", cls: "msg-copy",
  onclick: "copyMsg(this)", title: "Copy reply", attrs: `data-text="${esc(text)}"`});

// --- The chat (its messages, kept in memory)
const CHAT = [];
// The gate → tools → reply stage strip, shared by the live card and the
// completed/replayed card so the markup can't drift. `live` lights stages up
// (gate flips to done once decided, reply "on" once text streams); otherwise
// every stage is done and the strip carries the .tele class (hidden by the
// stats toggle). (.stages is flexbox, so inter-span whitespace is irrelevant.)
function stagesRow(t, live){
  // A stage that is running is "live", a finished one "ok", one not reached yet "neutral".
  const gateVariant = live && !t.gate ? "live" : "ok";
  const replyVariant = live ? (t.stream ? "live" : "neutral") : "ok";
  const tools = (t.tools||[]).map(x => toolChip(x.tool)).join("");
  const gate = uiBadge(`gate${t.gate?` · ${esc(t.gate.decision)}`:""}`, gateVariant);
  return `<div class="stages${live?"":" tele"}">` + gate + tools + uiBadge("reply", replyVariant) + `</div>`;
}
// The per-turn telemetry footer: seconds · iterations · model · consolidation.
const teleFooter = t => `<div class="meta tele">${secs(t.latency_ms)} · ${t.iterations??"?"} iter${
  t.model?` · ${esc(t.model)}`:""}${t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>`;

// The agent's question with its choices (the ask_options tool): one button per
// choice, and "Other" for an answer in your own words. A pick is sent as your
// reply; once you answer anything, the buttons give way to the plain question.
const askOf = ev => ({question: ev.question || "", options: ev.options || [], multi: !!ev.multi,
                      progress: ev.progress || null});
const askRow = t => {
  if (!t.ask || !t.ask.options.length) return "";
  // the reply already ends with the question (the harness writes it there):
  // show the choices under it, not the question a second time
  const said = (t.reply || "").trim().endsWith((t.ask.question || "").trim());
  const q = said ? "" : `<div class="ask-q">${esc(t.ask.question)}</div>`;
  if (t.ask.answered) return `<div class="ask">${q}</div>`;
  // several may be chosen: each choice toggles, and Send sends them all
  const multi = t.ask.multi;
  // which of the chosen materials this question narrows: "Material 1 of 3"
  const pg = t.ask.progress;
  const step = pg ? `<div class="ask-step">Material ${pg.n} of ${pg.of} &middot; <b>${esc(pg.item)}</b></div>` : "";
  return `<div class="ask${multi ? " ask-multi" : ""}">${step}${q}${multi
      ? `<div class="ask-hint">Choose one or more, then send.</div>` : ""}<div class="ask-opts">`
    + t.ask.options.map(o => uiButton(esc(o), {onclick: multi ? "toggleOption(this)" : "pickOption(this)",
        attrs: `data-v="${esc(o)}"${multi ? ' aria-pressed="false"' : ""}`})).join("")
    + (multi ? uiButton("Send", {level: "primary", onclick: "sendOptions(this)", title: "Send the choices"}) : "")
    + uiButton("Other&hellip;", {level: "tertiary", onclick: "pickOption(null)", title: "Type your own answer"})
    + `</div></div>`;
};
// The NAM cards a turn showed (nam_agent/nam/cards.py): the records still in,
// the PUR cases, a PR. Each card is the case's own data; "Use this" sends the
// record as the officer's choice.
const statusVariant = st => /cancel/i.test(st) ? "bad"
  : /^PR \+ PO|Cleared|ready/i.test(st) ? "ok"
  : /ongoing|AHR|review/i.test(st) ? "warn" : "neutral";
let NC_SEQ = 0;   // a name per card for its radio group
const ncItem = (it, name, first) => `<label class="nc-item${it.full ? " nc-full" : ""}">${it.send
    ? `<input type="radio" name="${name}" value="${esc(it.send)}"${first ? " checked" : ""}>` : ""}
  <span class="nc-item-body"><span class="nc-item-head"><span class="nc-code">${esc(it.code || "")}</span>${
    it.match ? `<span class="nc-match">${esc(it.match)}</span>` : ""}</span>${
    it.title ? `<span class="nc-item-title">${esc(it.title)}</span>` : ""}${
    it.detail ? `<span class="nc-item-detail">${esc(it.detail)}</span>` : ""}</span></label>`;
const namCard = c => {
  const name = `nc${++NC_SEQ}`;
  const items = c.items || [];
  const head = `<div class="nc-head"><div class="nc-title">${esc(c.title)}</div>${
    c.badge ? `<span class="nc-badge">${esc(c.badge)}</span>` : ""}</div>`;
  const sub = c.subtitle ? `<div class="nc-sub">${esc(c.subtitle)}</div>` : "";
  const chips = (c.chips || []).length ? `<div class="nc-chips">${c.chips.map(ch =>
    `<span class="${ch.strong ? "on" : ""}">${esc(ch.text)}</span>`).join("")}</div>` : "";
  const status = c.status ? `<div class="nc-status"><span class="nc-pill nc-${statusVariant(c.status)}">${
    esc(c.status)}</span></div>` : "";
  const fields = (c.fields || []).length ? `<dl class="nc-fields">${c.fields.map(([k, v, st]) =>
    `<dt>${esc(k)}</dt><dd class="${st ? "nc-" + esc(st) : ""}">${esc(v)}</dd>`).join("")}</dl>` : "";
  const list = items.length ? `<div class="nc-items">${items.map((it, i) =>
    `<div class="${i >= 3 ? "nc-more" : ""}">${ncItem(it, name, i === 0)}</div>`).join("")}</div>` : "";
  const foot = c.foot ? `<div class="nc-foot">${esc(c.foot)}</div>` : "";
  const note = c.note ? `<div class="nc-note">${esc(c.note)}</div>` : "";
  const acts = (c.action || items.length > 3) ? `<div class="nc-act">${
    c.action && c.action.pick ? uiButton(esc(c.action.label), {level: "primary", onclick: "startWith(this)"}) : ""}${
    c.action && c.action.other ? uiButton(esc(c.action.other), {onclick: "pickOption(this)",
      attrs: `data-v="${esc(c.action.other)}"`, title: "The closest other match, then the details again"}) : ""}${
    c.action && c.action.send ? uiButton(esc(c.action.label), {level: "primary", onclick: "pickOption(this)",
      attrs: `data-v="${esc(c.action.send)}"`}) : ""}${
    items.length > 3 ? uiButton(`View all ${items.length} items`, {level: "tertiary", onclick: "ncMore(this)"}) : ""}</div>` : "";
  return `<div class="nc">${head}${sub}${chips}${status}${fields}${list}${foot}${note}${acts}</div>`;
};
// "Start with this": the chosen record of the card (its radio), sent as the
// officer's choice.
function startWith(btn){
  const on = btn.closest(".nc").querySelector('input[type="radio"]:checked');
  if (!on) return;
  const input = document.getElementById("dmsg") || document.getElementById("msg");
  if (!input) return;
  input.value = on.value;
  sendChat(input);
}
function ncMore(btn){
  btn.closest(".nc").classList.toggle("nc-open");
}
// One group per title: a turn that narrowed twice shows its last records only.
const lastGroups = cards => {
  const by = new Map();
  (cards || []).forEach(g => { by.delete(g.title); by.set(g.title, g); });
  return [...by.values()];
};
const cardsRow = t => lastGroups(t.cards).map(group => {
  const cards = group.cards || [];
  // a group by year shows a heading for each year, as the cards come
  let body = "";
  if (group.by_year) {
    const years = [...new Set(cards.map(c => c.year || ""))];
    body = years.map(y => `${y ? `<div class="nc-yearhead">${esc(y)}</div>` : ""}<div class="nc-grid">${
      cards.filter(c => (c.year || "") === y).map(namCard).join("")}</div>`).join("");
  } else body = `<div class="nc-grid">${cards.map(namCard).join("")}</div>`;
  return `<div class="nam-cards"><div class="nc-group">${esc(group.title || "")}<span>${cards.length}</span></div>${body}</div>`;
}).join("");
// The NIT the chat generated (nam_agent/nam/nit.py), shown as the filled template
// beside the chat, NIT 65 : chat 35 (#nitpane, style.css). It opens the moment
// it is generated and refreshes after every answer or change (each "nit"
// event); the card's View NIT opens it again. Missing entries are asked for
// in the chat; Download PDF turns on once every entry is filled.
let NIT_OPEN = "", NIT_LABELS = {};
const nitUrl = id => `/api/nit/${encodeURIComponent(id)}`;
async function openNit(id){
  if (!/^NAM-\d{4}-\d{4,}$/.test(id || "")) return;
  NIT_OPEN = id;
  document.getElementById("nit-title").textContent = `NIT ${id}`;
  document.body.classList.remove("dock-closed", "dock-full");
  document.body.classList.add("nit-open");
  const sheet = document.getElementById("nit-sheet");
  if (!sheet.innerHTML) sheet.innerHTML = `<div class="empty">Loading the NIT&hellip;</div>`;
  try {
    const res = await fetch(`${nitUrl(id)}.json`);
    if (!res.ok) throw new Error(res.status);
    renderNit(await res.json());
  } catch (e) { sheet.innerHTML = `<div class="empty">Could not load the NIT (${esc(String(e.message || e))}).</div>`; }
}
function closeNit(){
  NIT_OPEN = "";
  document.body.classList.remove("nit-open");
  document.body.classList.add("dock-full");
  document.getElementById("nit-sheet").innerHTML = "";
}
function downloadNit(id, draft){
  if ((id = id || NIT_OPEN)) location.href = `${nitUrl(id)}.pdf?download=1${draft ? "&draft=1" : ""}`;
}
// A suggestion taken as it is, from the sheet.
async function acceptNit(rowId){
  const res = await fetch(`${nitUrl(NIT_OPEN)}/field`, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({field: rowId, value: "accept"})});
  if (res.ok) renderNit(await res.json());
}
// A change or a missing entry goes through the chat, where the agent saves it.
function askNit(rowId, verb){
  const input = document.getElementById("dmsg");
  if (!input || !NIT_LABELS[rowId]) return;
  input.value = `${verb} ${NIT_LABELS[rowId]} to `;
  input.focus();
}
const nitCell = r => {
  const id = esc(r.id);
  if (r.kind === "fixed") return esc(r.value);
  if (r.kind === "missing")
    return `<span class="nit-gap">[TO BE FILLED]</span><div class="nit-acts">${
      uiButton("Fill", {size: "sm", onclick: `askNit('${id}', 'Set')`})}</div>`;
  if (r.kind === "suggested")
    return `<span class="nit-sugg">${esc(r.value.text)}</span><span class="nit-src">Suggested from ${esc(r.value.source)} in the KG</span>
      <div class="nit-acts">${uiButton("Accept", {size: "sm", level: "primary", onclick: `acceptNit('${id}')`})}${
        uiButton("Change", {size: "sm", onclick: `askNit('${id}', 'Change')`})}</div>`;
  // a filled entry reads as plain text; an accepted suggestion keeps its
  // source tender on the record (nam_nit_fields), not on the sheet
  return esc(r.value);
};
const nitRows = rows => rows.map(r => `<tr><th>${esc(r.label)}</th><td>${nitCell(r)}</td></tr>`).join("");
function renderNit(d){
  NIT_LABELS = {};
  [...d.header, ...d.sections.flatMap(s => s.rows), ...d.contacts.flatMap(c => c.rows)]
    .forEach(r => { NIT_LABELS[r.id] = r.full_label || r.label; });
  const head = Object.fromEntries(d.header.map(r => [r.id, r]));
  const ref = r => r && r.kind === "filled" ? esc(r.value) : `<span class="nit-gap">[TO BE FILLED]</span>`;
  const qty = q => q === null || q === undefined ? "" : esc(String(q));
  document.getElementById("nit-sheet").innerHTML = `<article class="nit-sheet">
    ${d.ready ? "" : `<div class="nit-stamp"><span>Draft &mdash; not for issue</span></div>`}
    <header class="nit-letter">
      <p class="nit-org">Numaligarh Refinery Limited</p>
      <p class="nit-sub">A Government of India Enterprise</p>
      <p class="nit-sub">Materials &amp; Contracts Department &middot; Numaligarh, Golaghat, Assam 785699</p>
    </header>
    <h2 class="nit-doc-title">${esc(d.title)}</h2>
    <div class="nit-refs"><span>Tender No: ${ref(head.tender_no)}</span><span>E-File Ref: ${ref(head.aan_file_number)}</span><span>Case: ${esc(d.case_id)}</span></div>
    <table class="nit-tbl" style="margin-top:var(--space-6)">${nitRows(d.header)}</table>
    <p class="nit-intro">${esc(d.intro)}</p>
    ${d.sections.map(s => `<h3 class="nit-sec">${esc(s.title)}</h3><table class="nit-tbl">${nitRows(s.rows)}</table>`).join("")}
    <h3 class="nit-sec">Contacts</h3>
    ${d.contacts.map(c => `<table class="nit-tbl"><tr><th colspan="2">${esc(c.title)}</th></tr>${nitRows(c.rows)}</table>`).join("")}
    <h3 class="nit-sec">Annexure &mdash; Schedule of items</h3>
    <table class="nit-tbl nit-items"><tr><th>#</th><th>Item</th><th>Qty</th><th>Unit</th></tr>${
      d.items.map((x, i) => `<tr><td>${i + 1}</td><td>${esc(x.item)}</td><td>${qty(x.qty)}</td><td>${esc(x.uom || "")}</td></tr>`).join("")}</table>
    <p class="nit-prov">${esc(d.provenance)}</p>
  </article>`;
  document.getElementById("nit-status").textContent = d.ready ? "Ready to download"
    : `${d.missing.length} missing · ${d.suggested} to confirm`;
  const dl = document.getElementById("nit-download");
  dl.disabled = !d.ready;
  dl.title = d.ready ? "Download the NIT as PDF" : "Active once every entry is filled and every suggestion confirmed";
}
const nitRow = t => {
  if (!t.nit || !t.nit.case_id) return "";
  const n = t.nit, id = esc(n.case_id);
  return `<div class="ask nit"><div class="ask-q">NIT ${esc(n.case_id)}${n.ready ? " (ready)" : ""}</div>
    <div class="meta" style="margin:0 0 var(--space-2)">${n.filled} of ${n.total} entries filled, ${n.suggested || 0} suggestions to confirm, ${n.missing || 0} missing.</div>
    <div class="ask-opts">${uiButton("View NIT", {onclick: `openNit('${id}')`})}</div></div>`;
};
// A choice of several: toggles one, and Send sends every one on, as "a; b".
function toggleOption(btn){
  const on = btn.getAttribute("aria-pressed") !== "true";
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.classList.toggle("on", on);
}
function sendOptions(btn){
  const box = btn.closest(".ask-opts");
  const chosen = [...box.querySelectorAll('[aria-pressed="true"]')].map(b => b.dataset.v);
  if (!chosen.length) return;
  const input = document.getElementById("dmsg") || document.getElementById("msg");
  if (!input) return;
  input.value = chosen.join("; ");
  sendChat(input);
}
function pickOption(btn){
  const input = document.getElementById("dmsg") || document.getElementById("msg");
  if (!input) return;
  if (!btn){ input.placeholder = "Type your answer…"; input.focus(); return; }
  input.value = btn.dataset.v || btn.textContent;
  sendChat(input);
}

// How the turn was made (its stages, the tools and their raw output), in a
// dropdown that starts closed; a reader who opens one keeps it open while the
// chat repaints.
let TURN_SEQ = 0;
const TURN_OPEN = new Set();
function turnToggle(el){
  if (el.open) TURN_OPEN.add(el.dataset.turn); else TURN_OPEN.delete(el.dataset.turn);
}
const turnDetails = (t, body, summary) => {
  if (!body.trim()) return "";
  t._turn = t._turn || String(++TURN_SEQ);
  return `<details class="turn-details" data-turn="${t._turn}" ontoggle="turnToggle(this)"${
    TURN_OPEN.has(t._turn) ? " open" : ""}><summary>${summary}</summary>${body}</details>`;
};
const toolCount = t => (t.tools || []).length;
// The words the Undo button sends: stage_tools.UNDO_LABEL, which the harness
// reads as the officer's undo (evals/deterministic/nam/test_cart_flow.py).
const UNDO_LABEL = "Undo the last step";
const undoRow = (t, latest) => latest && (t.tools || []).some(x => /^nam_/.test(x.tool || ""))
  ? `<div class="turn-undo">${uiButton("Undo last step", {size: "sm", level: "tertiary",
      onclick: "pickOption(this)", attrs: `data-v="${UNDO_LABEL}"`,
      title: "Take back the last answer, choice or pick"})}</div>` : "";
const chatTurnCard = (t, latest = false) => uiCard(`
  ${msgCopy(t.reply)}
  ${turnDetails(t, `${t.gate?`${stagesRow(t, false)}
    <div class="meta tele" style="margin:0 0 calc(var(--spacing) * 1.5)">${esc(t.gate.reason||"")}</div>`:""}
  ${toolCount(t)?`<div class="tele">${(t.tools||[]).map(toolRow).join("")}</div>`:""}`,
    `How this answer was made${toolCount(t) ? ` &middot; ${toolCount(t)} step${toolCount(t) > 1 ? "s" : ""}` : ""}`)}
  <div class="r" style="margin-top:var(--space-2)">${renderMarkdown(t.reply)}</div>
  ${cardsRow(t)}
  ${askRow(t)}
  ${nitRow(t)}
  ${undoRow(t, latest)}
  ${teleFooter(t)}`, {cls: "reply"});

// While a turn runs it streams: the stages light up as the harness reaches
// them, and the reply text appears token by token (with a blinking caret).
const streamingCard = m => uiCard(`
  ${turnDetails(m, `${stagesRow(m, true)}
  ${m.gate&&m.gate.reason?`<div class="meta" style="margin:0 0 calc(var(--spacing) * 1.5)">${esc(m.gate.reason)}</div>`:""}
  ${(m.tools||[]).map(toolRow).join("")}`, "How this answer is being made")}
  ${m.stream
     ? `<div class="r" style="margin-top:var(--space-2)">${renderMarkdown(m.stream)}<span class="caret"></span></div>`
     : `<div class="meta" style="margin:0">${liveStatus(m)}&hellip;${m.started?` ${Math.round((Date.now()-m.started)/1000)}s`:""}</div>`}`, {cls: "reply"});

// What the turn is really doing right now. "Reasoning" only when the model
// streams its reasoning; otherwise the tool that is running, or the wait.
const liveStatus = m =>
    m.phase === "reasoning" ? "Reasoning"
  : m.phase === "tool" ? toolStatus(m.running)
  : "Waiting for the model";
const toolStatus = name =>
    /^kg_/.test(name || "") ? "Searching past tenders"
  : /^nam_/.test(name || "") ? "Working on the procurement need"
  : `Running ${esc((name || "a tool").replace(/_/g, " "))}`;

// Messages loaded from history (a switched/opened conversation) have no live
// latency/iteration data, and their stored form carries an internal
// "[tools used: ...]" annotation — strip both so the thread reads cleanly.
const stripTools = t => (t || "").replace(/\s*\[tools used:[\s\S]*\]\s*$/, "").trim();
const historicalCard = m => uiCard(`
  ${msgCopy(stripTools(m.reply))}
  <div class="r">${renderMarkdown(stripTools(m.reply))}</div>`, {cls: "reply"});

function renderChatLog(){
  if (!CHAT.length)
    return `<div class="empty" style="padding:calc(var(--spacing) * 1.5) calc(var(--spacing) * 0.5)">Say what is to be procured, for example: \"we need gaskets for the crude unit pumps\".</div>`;
  // the latest reply of a NAM turn carries the Undo button
  const last = CHAT.length - 1;
  return CHAT.map((m, i) => m.role==="user"
      ? `<div class="bubble">${esc(m.text)}</div>`
      : m.pending ? streamingCard(m)
      : m.historical ? historicalCard(m)
      : chatTurnCard(m, i === last)).join("");
}

function syncChatLogs(){
  document.querySelectorAll(".chatlog").forEach(el => {
    el.innerHTML = renderChatLog();
    el.scrollTop = el.scrollHeight;      // dock scrolls its own container
  });
}

// One streamed harness event updates the live card in place.
function applyStreamEvent(pending, ev){
  if (ev.kind === "gate") pending.gate = {decision: ev.decision, reason: ev.reason};
  else if (ev.kind === "text") pending.stream = (pending.stream || "") + (ev.delta || "");
  else if (ev.kind === "reasoning") pending.phase = "reasoning";
  else if (ev.kind === "tool_start"){ pending.phase = "tool"; pending.running = ev.tool; }
  else if (ev.kind === "ask_options")
    pending.ask = askOf(ev);
  else if (ev.kind === "nam_cards")
    (pending.cards = pending.cards || []).push(ev);
  else if (ev.kind === "nit"){ pending.nit = ev; openNit(ev.case_id); }
  else if (ev.kind === "tool"){
    (pending.tools = pending.tools || []).push({
      tool: ev.tool, args: ev.args, output: ev.output,
      status: (ev.output||"").toLowerCase().startsWith("error") ? "error" : "ok",
      summary: (ev.output || "").split(". ")[0].slice(0,120)});
    pending.stream = "";   // a new assistant turn begins after the tool result
    pending.phase = "";    // and waits for the model again
  } else if (ev.kind === "done"){
    pending.pending = false; pending.stream = "";
    if (ev.session_id) rememberChat(ev.session_id);   // the chat the harness kept this in
    if (ev.error) pending.reply = "Error: " + ev.error;
    else Object.assign(pending, ev);   // reply, tools, gate, iterations, latency_ms, consolidation
    if (ev.ask_options && !pending.ask) pending.ask = askOf(ev.ask_options);
  }
}

async function sendChat(fromInput){
  const input = fromInput || document.getElementById("msg") || document.getElementById("dmsg");
  const text = (input && input.value || "").trim();
  if (!text) return;
  input.value = "";
  CHAT.forEach(m => { if (m.ask) m.ask.answered = true; });   // a question is answered by the next message
  CHAT.push({role:"user", text});
  const pending = {role:"agent", pending:true, stream:"", started: Date.now()};
  CHAT.push(pending);
  syncChatLogs();
  // tick the elapsed counter while we wait for the first token
  const ticker = setInterval(() => { if (pending.pending && !pending.stream) syncChatLogs(); }, 1000);
  try {
    const res = await fetch("/api/chat/stream", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(SESSION ? {message:text, session_id:SESSION, source:"page"}
                                  : {message:text, source:"page"})});
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;){
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0){
        const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!line.startsWith("data:")) continue;
        try { applyStreamEvent(pending, JSON.parse(line.slice(5).trim())); } catch(e){}
        syncChatLogs();
      }
    }
  } catch(e){ Object.assign(pending, {pending:false, reply:"Error: "+e}); }
  clearInterval(ticker);
  if (pending.pending) pending.pending = false;   // stream ended without a 'done'
  syncChatLogs();
  input.focus();
}
function wireDock(){
  const b = document.getElementById("dsend"), i = document.getElementById("dmsg");
  if (b) b.onclick = () => sendChat(i);
  if (i) i.onkeydown = e => { if (e.key==="Enter") sendChat(i); };
  syncChatLogs();
}
