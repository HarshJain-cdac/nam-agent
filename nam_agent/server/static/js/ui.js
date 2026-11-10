// The page's primitives (card, badge, button, menu), as functions that
// return HTML strings, with a "ui" prefix because js/ shares one global
// scope. Each one's look lives in style.css under its class.

// Card — paper ground, a hairline, 16px padding (12px when size is "sm").
function uiCard(body, {title = "", action = "", footer = "", size = "", cls = ""} = {}){
  const head = (title || action)
    ? `<div class="card-head">${title ? `<div class="card-title">${title}</div>` : ""}${action ? `<div class="card-action">${action}</div>` : ""}</div>`
    : "";
  return `<div class="card${size === "sm" ? " card-sm" : ""}${cls ? " " + cls : ""}">${head}<div class="card-body">${body}</div>${footer ? `<div class="card-foot">${footer}</div>` : ""}</div>`;
}

// Badge — the 20px chip. variant: neutral | ok | warn | bad | live | miss | value.
// "value" is for data (a grade, a cost, a model id): normal case, no tracking.
function uiBadge(text, variant = "neutral", title = ""){
  // .badge-t is the text's own box: it is what a capped badge ellipsizes.
  return `<span class="badge badge-${variant}"${title ? ` title="${esc(title)}"` : ""}><span class="badge-t">${text}</span></span>`;
}

// Button — Memory's four levels: primary (the main action), secondary,
// tertiary (text only) and destructive; size "sm" for a row of small ones.
// danger colours a tertiary action red (delete in a table row). cls adds a
// class that other code finds the button by. onclick is an inline handler,
// so it must not contain a double quote.
function uiButton(label, {level = "secondary", size = "", onclick = "", title = "", danger = false, cls = "", attrs = ""} = {}){
  const classes = `btn btn-${level}${size === "sm" ? " btn-sm" : ""}${danger ? " btn-danger" : ""}${cls ? " " + cls : ""}`;
  return `<button type="button" class="${classes}"${onclick ? ` onclick="${onclick}"` : ""}${title ? ` title="${esc(title)}"` : ""}${attrs ? " " + attrs : ""}>${label}</button>`;
}

// Menu — one floating menu at a time, under the control that opened it. A
// click outside or Escape closes it and gives focus back to that control;
// the arrow keys move between items. Build the contents with uiMenuLabel,
// uiMenuItem and uiMenuSep.
let _menuTrigger = null;
function openMenu(trigger, html, {width = "", align = "right"} = {}){
  closeMenu();
  const m = document.createElement("div");
  m.className = "menu"; m.id = "ui-menu"; m.setAttribute("role", "menu");
  if (width) m.style.width = width;
  m.innerHTML = html;
  document.body.appendChild(m);
  const r = trigger.getBoundingClientRect();
  const gap = 6;
  const left = align === "right" ? r.right - m.offsetWidth : r.left;
  m.style.top = (r.bottom + gap) + "px";
  m.style.left = Math.max(gap, Math.min(left, innerWidth - m.offsetWidth - gap)) + "px";
  _menuTrigger = trigger;
  const first = m.querySelector(".menu-item");
  if (first) first.focus();
  setTimeout(() => document.addEventListener("click", _menuOutside), 0);
  document.addEventListener("keydown", _menuKey);
  return m;
}
function closeMenu(){
  const m = document.getElementById("ui-menu");
  if (!m) return;
  m.remove();
  document.removeEventListener("click", _menuOutside);
  document.removeEventListener("keydown", _menuKey);
  if (_menuTrigger && document.contains(_menuTrigger)) _menuTrigger.focus();
  _menuTrigger = null;
}
function _menuOutside(e){
  const m = document.getElementById("ui-menu");
  if (m && !m.contains(e.target) && !(_menuTrigger && _menuTrigger.contains(e.target))) closeMenu();
}
function _menuKey(e){
  const m = document.getElementById("ui-menu");
  if (!m) return;
  if (e.key === "Escape"){ e.preventDefault(); closeMenu(); return; }
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const items = [...m.querySelectorAll(".menu-item")];
  if (!items.length) return;
  const i = items.indexOf(document.activeElement);
  const next = e.key === "ArrowDown" ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
  items[next].focus();
  e.preventDefault();
}
function uiMenuLabel(text){ return `<div class="menu-label">${text}</div>`; }
function uiMenuItem(html, {onclick = "", on = false, sub = "", danger = false, title = ""} = {}){
  return `<button type="button" class="menu-item${on ? " on" : ""}${danger ? " danger" : ""}" role="menuitem"${onclick ? ` onclick="${onclick}"` : ""}${title ? ` title="${esc(title)}"` : ""}>${html}${sub ? `<span class="menu-sub">${sub}</span>` : ""}</button>`;
}
function uiMenuSep(){ return `<div class="menu-sep" role="separator"></div>`; }
