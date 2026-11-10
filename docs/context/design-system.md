# Design system

The chat page (`nam_agent/server/static/`) has its own design system. Read this
file before changing how anything looks.

## Where it lives

- `base.css` holds the fonts (loaded from `fonts/`, nothing fetched from the
  network), the type scale, the two weights, the line heights, the spacing
  steps, the corners, the layout widths, the stacking order and the one
  duration.
- `theme.css` holds the colours: a white light theme and a neutral dark one
  (#18181B), with a blue accent, under three selectors (light, dark by the
  system, dark by choice). `evals/deterministic/test_page_theme.py` keeps
  it colour tokens only, both themes complete, and every text colour at 4.5:1
  or more on each surface.
- The order in `index.html` is `base.css`, `theme.css`, `style.css`. The
  page carries no product name or mark.

## Rules for CSS and inline styles

These apply to `style.css` and to any inline style written in `js/`:

- **Every value comes from a token.** Colour (`--text-*`, `--surface-*`,
  `--rule*`, `--accent*`, `--ok`/`--warn`/`--bad`, `--chart-1…5`), size
  (`--text-xs|sm|base|lg|xl|2xl`), weight (400 or 500), face
  (`--face-sans|mono|display`), corner (`--radius`, or `--shape-chip` for a
  badge, `--shape-circle` for a dot, `--shape-bubble` for your chat message)
  and duration (`--motion-fast`).
- **No shadows.** Separate things with a 1px `--rule`. The one exception is a
  floating menu (`openMenu`, and a select's open list), which takes
  `--shadow-md`. Use a native `<select>` for a choice in a form: `style.css`
  draws its open list as the menu, so it needs no JavaScript.
- **The accent is a surface, not a text colour.** Accent text uses
  `--accent-fg`, and text on an accent fill uses `--accent-ink`.
- **Buttons never fill.** There are four levels, all from `uiButton`:
  *primary* for the main action (paper ground, `--rule-hard` border, ink label:
  Save, Send), *secondary* (transparent, `--rule` border, muted label),
  *tertiary* (text only) and *destructive* (`--bad` label). Hover moves the
  border, and `controls.css` adds the focus ring, pressed and disabled states.
- **Labels** are uppercase `--face-mono` at `--tracking-label`.
- **Controls get `data-slot` automatically** (`stampSlots` in `util.js`). Use a
  native `<button>`, `<input>` or `<select>`, and never fake one with a `<div>`.
- **Use the token names.** The old short names (`--ink2`, `--line` and so on)
  are gone, and a test fails if one comes back.
- **Spacing** is `--space-2|3|4|6|8` (8–32px) between things, and
  `calc(var(--spacing) * 0.5|1|1.5)` (2–6px) inside one control.

`test_design_system.py` enforces all of this except which button level you pick.

## Primitives (`js/ui.js`)

Build screens from these rather than writing the markup. Each one returns an
HTML string and is named after the matching component in the shared memory
console.

| Function | Use it for |
|---|---|
| `uiCard(body, {title, action, footer, size})` | a block of related content |
| `uiBadge(text, variant)` | a status or a value: `neutral`, `ok`, `warn`, `bad`, `live` (running), `miss`, `value` (data, normal case) |
| `uiTable(columns, rows, {caption, empty})` | anything with rows and columns |
| `uiTabs(items)` | switching between parts of one view |
| `uiNotice(level, html, action)` | an explanation or a status message: `note`, `ok`, `warn`, `failed` |
| `uiStatBand(items)` | a view's headline numbers |
| `uiRow(lead, title, meta, {onclick})` | a list of things you can open |
| `openDialog(html, {wide, onClose})` / `closeDialog()` | a task that needs its own space; Escape and the scrim close it |
| `uiButton(label, {level, size, onclick, danger, cls, attrs})` | any action: `primary`, `secondary`, `tertiary` (text only, for row actions such as edit), `destructive`; `danger` makes a tertiary delete red |
| `uiLink(label, href)` | going to another tab, never an action |
| `openMenu(trigger, html)` / `closeMenu()` with `uiMenuLabel`, `uiMenuItem`, `uiMenuSep` | a short list of choices under a button; an outside click and Escape close it |

If none of them fits, open an issue before adding one: a new primitive is a
design decision.

## Checking your change

Verify the result in a browser, as "Verifying a change" in
[nam_agent/server/static/README.md](../../nam_agent/server/static/README.md) describes, in both
the light and the dark theme.
