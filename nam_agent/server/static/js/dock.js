// The chats: "New chat", the history menu and loading a chat into the page.
// Classic <script>, shared global scope; load order: static/README.md.
//
// Each browser keeps its own chats: the current chat's id (the harness makes
// one on the first message when the page has none) is stored here and sent
// with every message, and the history asks the server only for the ids this
// browser started, so two people on one server never see each other's chats.
const store = {
  get(k){ try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v){ try { localStorage.setItem(k, v); } catch (e) {} },
};
const myChats = () => { try { return JSON.parse(store.get("nam-chats") || "[]"); } catch (e) { return []; } };
let SESSION = store.get("nam-chat") || "";
function rememberChat(id){
  if (!id) return;
  SESSION = id;
  store.set("nam-chat", id);
  const mine = myChats().filter(x => x !== id);
  mine.unshift(id);
  store.set("nam-chats", JSON.stringify(mine.slice(0, 200)));
}
async function newChat(){
  const r = await postJSON("/api/session", {action: "new"});
  if (r.session_id){ rememberChat(r.session_id); CHAT.length = 0; syncChatLogs(); }
  closeMenu();
}
// Load one chat's messages into the page.
async function loadChat(id){
  const r = await postJSON("/api/session", {action: "history", id});
  if (!r.ok) return null;
  const fresh = (r.history || []).map(histItem);
  // a question in the history is answered, unless it is the chat's last word
  fresh.forEach((m, i) => { if (m.ask && i < fresh.length - 1) m.ask.answered = true; });
  rememberChat(r.session_id);
  CHAT.length = 0; fresh.forEach(m => CHAT.push(m)); syncChatLogs();
  return fresh;
}
async function openChat(id){
  closeMenu();
  await loadChat(id);
}
async function toggleSessMenu(ev){
  ev.stopPropagation();
  const trigger = ev.currentTarget;
  if (document.getElementById("ui-menu") && _menuTrigger === trigger){ closeMenu(); return; }
  const r = await postJSON("/api/session", {action: "list", ids: myChats()});
  const chats = (r && r.chats) || [];
  const rows = chats.length
    ? chats.map(s => uiMenuItem(esc(s.title || s.id),
        {sub: sessionMeta(s), on: s.id === SESSION, onclick: `openChat('${esc(s.id)}')`})).join("")
    : `<div class="menu-empty">no past chats yet</div>`;
  openMenu(trigger, uiMenuLabel("Your chats") + rows, {width: "400px"});
}

// --- per-turn stats (gate / seconds / iterations / tools): on by default,
// the choice kept in localStorage; hides the .tele blocks via a body class.
function applyTele(){
  const off = store.get("nam_tele") === "0";
  document.body.classList.toggle("no-tele", off);
  const b = document.getElementById("teletoggle");
  if (b) b.classList.toggle("on", !off);
}
function toggleTele(){
  store.set("nam_tele", store.get("nam_tele") === "0" ? "1" : "0");
  applyTele();
}
