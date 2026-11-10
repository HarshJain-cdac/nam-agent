# The officer's chat page

Plain static files served as they are by `nam_agent/server/http.py` at `/`.
No build step, no framework, no dependencies. It is the reference client of
the chat API: everything it does, a backend's own frontend can do with the
same calls (docs/backend-integration.md).

- `index.html`: the page (the chat, and the NIT beside it when one is open)
  and the ordered `<script>` tags.
- `style.css`: the rules; every value comes from the tokens in `base.css` and
  `theme.css` (docs/context/design-system.md).
- `base.css`, `fonts/`: sizes, spacing, type and motion tokens, and the fonts.
- `theme.css`: the colours (light and dark), as tokens.

## The scripts (`js/`), in load order

Classic scripts sharing one global scope: a `function`/`let`/`const` in one
file is visible to the others. `main.js` starts the page and loads last.

| file        | what it holds |
|-------------|---------------|
| `util.js`   | `esc`, the markdown renderer, `postJSON`, `sessionMeta`, `toolChip`, `stampSlots` |
| `theme.js`  | the system / light / dark toggle (`cycleTheme`) |
| `ui.js`     | the primitives: `uiCard`, `uiBadge`, `uiButton`, the menu (`openMenu`, `uiMenuItem`) |
| `render.js` | the reply card: the NAM cards (`namCard`, `cardsRow`), the question and its buttons (`askRow`), "How this answer was made" (`turnDetails`), Undo (`undoRow`); the chat log, streaming (`applyStreamEvent`) and `sendChat`; the NIT sheet (`openNit`) |
| `dock.js`   | the chats: `newChat`, the history menu (only this browser's chats), `loadChat`, the stats toggle |
| `main.js`   | the start: theme, controls, and this browser's last chat (loads last) |

## Rules

- **Inline handlers need global names.** Buttons use `onclick="fn()"` in the
  HTML strings the scripts build, so `fn` must stay a top-level name.
  `evals/deterministic/test_static_assets.py` checks this.
- **No build step, no framework, no new dependencies.**
- **No emojis in the UI.**

## Checking a change

Run `nam-agent serve`, hard-reload `http://127.0.0.1:7777`, and check the
browser console shows no errors. The static files are read from disk on every
request; Python changes need the server restarted.
