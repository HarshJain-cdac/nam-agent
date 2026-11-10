// The page's start: theme, controls, and this browser's last chat. Loads last.
// Classic <script>, shared global scope; load order: static/README.md.

applyTheme(currentTheme());
watchSlots();
wireDock();
applyTele();
if (SESSION) loadChat(SESSION).catch(() => {});
