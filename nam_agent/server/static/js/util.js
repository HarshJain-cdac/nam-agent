// Escaping, markdown, postJSON and the shared row atoms. Classic <script>,
// shared global scope; load order: static/README.md.

// Escapes quotes too, not just &<>. Text nodes never needed it, and for a long
// time nothing put model output inside an ATTRIBUTE — so the gap was invisible.
// The copy buttons (#58) are the first place that happens, and a reply
// containing one double quote was enough to close data-text="..." and attach
// its own event handler. The model's output is not fully ours (it quotes SAP
// and tender text), so escape at the helper, once, for every caller.
const esc = s => (s??"").toString().replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// --- tiny markdown renderer for chat replies (no dependency, XSS-safe: we
// escape first, then apply a small set of transforms the LLM actually uses:
// bold/italic/code, links, ordered/unordered lists, and tables).
function mdInline(s){   // s is already HTML-escaped
  return s
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|message:\/\/[^\s)]+)\)/g,
             (m, text, url) => `<a href="${url}" target="_blank" rel="noopener">${text}</a>`)
    .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*_`])[*_]([^*_`\s][^*_`]*?)[*_](?![\w*])/g, "$1<em>$2</em>")
    .replace(/`([^`]+?)`/g, "<code>$1</code>");
}
function renderMarkdown(text){
  const lines = esc(text).split(/\r?\n/);
  const row = l => /^\s*\|.*\|\s*$/.test(l);
  const sep = l => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(l);
  const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
  const out = [];
  let i = 0;
  while (i < lines.length){
    const l = lines[i];
    if (row(l) && i+1 < lines.length && sep(lines[i+1])){          // table
      const head = cells(l); i += 2; const body = [];
      while (i < lines.length && row(lines[i])){ body.push(cells(lines[i])); i++; }
      out.push(`<table class="mdtable"><thead><tr>${head.map(h=>`<th>${mdInline(h)}</th>`).join("")}</tr></thead><tbody>${
        body.map(r=>`<tr>${r.map(c=>`<td>${mdInline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    const h = l.match(/^\s*#{1,6}\s+(.*)$/);
    if (h){ out.push(`<div class="mdh">${mdInline(h[1])}</div>`); i++; continue; }
    if (/^\s*[-*]\s+/.test(l)){                                     // unordered list
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])){ items.push(mdInline(lines[i].replace(/^\s*[-*]\s+/,""))); i++; }
      out.push(`<ul class="mdlist">${items.map(x=>`<li>${x}</li>`).join("")}</ul>`); continue;
    }
    if (/^\s*\d+\.\s+/.test(l)){                                    // ordered list
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])){ items.push(mdInline(lines[i].replace(/^\s*\d+\.\s+/,""))); i++; }
      out.push(`<ol class="mdlist">${items.map(x=>`<li>${x}</li>`).join("")}</ol>`); continue;
    }
    if (/^\s*`{3,}/.test(l)){                                       // fenced code block
      const lang = l.replace(/^\s*`{3,}/, "").trim();
      i++;
      const codeLines = [];
      while (i < lines.length && !/^\s*`{3,}\s*$/.test(lines[i])){ codeLines.push(lines[i]); i++; }
      if (i < lines.length) i++;   // skip closing ```
      const langLabel = lang ? `<span class="mdcode-lang">${lang}</span>` : "";
      out.push(`<div class="mdcode"><div class="mdcode-head">${langLabel}${uiButton("Copy", {level: "tertiary", size: "sm", cls: "mdcode-copy", onclick: "copyCode(this)"})}</div><pre><code>${codeLines.join("\n")}</code></pre></div>`);
      continue;
    }
    if (/^\s*[-*_]{3,}\s*$/.test(l)){ out.push("<hr class='mdhr'>"); i++; continue; } // hr
    if (/^\s*$/.test(l)){ i++; continue; }
    const para = [];                                                // paragraph
    while (i < lines.length && lines[i].trim() && !/^\s*[-*]\s|^\s*\d+\.\s|^\s*#{1,6}\s/.test(lines[i])
           && !(row(lines[i]) && i+1<lines.length && sep(lines[i+1]))){
      para.push(mdInline(lines[i])); i++;
    }
    out.push(`<div class="mdp">${para.join("<br>")}</div>`);
  }
  return out.join("");
}
function copyCode(btn){
  const code = btn.closest(".mdcode").querySelector("pre code");
  navigator.clipboard.writeText(code.textContent).then(() => {
    const orig = btn.textContent;
    btn.textContent = "Copied!"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 2000);
  });
}
function copyMsg(btn){
  const text = btn.getAttribute("data-text") || "";
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = "Copied!"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 2000);
  });
}
async function postJSON(url, body){ return (await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json(); }

// "12 msg · 2026-07-26 21:56": a chat's size and when it last moved.
const sessionMeta = s =>
  `${s.messages} msg · ${esc((s.last_at||"").slice(0,16).replace("T"," "))}`;

// One tool in a stage strip. Tool names run long (nam_memory_memory_search), and in a narrow chat dock the
// badge is cut to fit, so the full name rides along as the hover title.
const toolChip = name => uiBadge("tool · " + esc(name), "ok", "tool · " + name);

// --- data-slot: how shared memory's controls.css finds a control.
//
// controls.css styles controls by data-slot, not by class: the shape, the
// focus ring, pressed and disabled. The views build their controls as HTML
// strings, so rather than adding the attribute to every string (and every
// future one), stamp it on native controls as they appear. An element that
// already has a data-slot is left alone.
const SLOT_FOR = [
  ["button", "button"],
  ["textarea", "textarea"],
  ["select", "select-trigger"],
  ['input[type="checkbox"]', "checkbox"],
  ['input[type="radio"]', "radio-group-item"],
  ['input:not([type]), input[type="text"], input[type="search"], input[type="password"], input[type="email"], input[type="url"], input[type="number"]', "input"],
  [".tab", "tabs-trigger"],
  [".badge, .pill, .chip, .chip-c, .stage", "badge"],
];
function stampSlots(root){
  for (const [sel, slot] of SLOT_FOR){
    if (root.matches && root.matches(sel) && !root.dataset.slot) root.dataset.slot = slot;
    if (root.querySelectorAll) root.querySelectorAll(sel).forEach(el => { if (!el.dataset.slot) el.dataset.slot = slot; });
  }
}
// The chat repaints and menus are appended to <body>, so watch the whole
// body rather than stamping once.
function watchSlots(){
  stampSlots(document.body);
  new MutationObserver(records => {
    for (const r of records) for (const n of r.addedNodes) if (n.nodeType === 1) stampSlots(n);
  }).observe(document.body, {childList: true, subtree: true});
}
