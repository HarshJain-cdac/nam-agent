"""DETERMINISTIC EVAL — the chat page's white and dark themes, its unbranded
face, and the chat that pulls out to the full page.

theme.css holds the colour tokens: a pure white light theme and a neutral
dark one. These
checks keep it a token file, keep both themes complete, and hold every text
colour to WCAG AA (4.5:1) on the page ground and on the darkest panel.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "nam_agent" / "server" / "static"
THEME = STATIC / "theme.css"


def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", THEME.read_text(encoding="utf-8"), flags=re.DOTALL)


def _blocks() -> dict[str, dict[str, str]]:
    """selector -> {property: value} for the three theme blocks."""
    out = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _css()):
        sel = " ".join(m.group(1).split())
        out[sel] = {p.strip(): v.strip() for p, v in
                    (d.split(":", 1) for d in m.group(2).split(";") if ":" in d)}
    return out


def _lum(hex_: str) -> float:
    h = hex_.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_theme_loads_after_the_base_and_before_style():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = ["base.css", "theme.css", "style.css"]
    pos = [html.find(f'href="/static/{name}"') for name in order]
    assert -1 not in pos and pos == sorted(pos), dict(zip(order, pos))


def test_theme_declares_colour_tokens_only():
    for sel, decls in _blocks().items():
        for prop in decls:
            assert prop.startswith("--") or prop == "color-scheme", (sel, prop)


def test_three_blocks_with_the_same_tokens():
    blocks = _blocks()
    light = blocks[':root, [data-theme="light"]']
    system_dark = blocks[':root:not([data-theme="light"])']
    dark = blocks['[data-theme="dark"]']
    assert set(light) == set(dark) == set(system_dark)
    assert system_dark == dark, "dark by the system and dark by choice must match"
    assert light["--surface-bg"] == "#FFFFFF" and light["color-scheme"] == "light"
    assert dark["color-scheme"] == "dark"


@pytest.mark.parametrize("which", ["light", "dark"])
def test_text_meets_aa_on_the_page_and_the_darkest_panel(which):
    blocks = _blocks()
    t = blocks[':root, [data-theme="light"]'] if which == "light" else blocks['[data-theme="dark"]']
    for token in ("--text-ink", "--text-muted", "--text-faint", "--accent-fg", "--ok", "--warn", "--bad"):
        for ground in ("--surface-bg", "--surface-paper", "--surface-raised", "--surface-sunk"):
            ratio = _ratio(t[token], t[ground])
            assert ratio >= 4.5, f"{which}: {token} on {ground} is {ratio:.2f}:1"
    assert _ratio(t["--accent-ink"], t["--accent"]) >= 4.5
    assert _ratio(t["--chat-user-fg"], t["--chat-user-bg"]) >= 4.5      # your message
    assert _ratio(t["--text-ink"], t["--chat-reply-bg"]) >= 4.5          # a reply


VISIBLE = re.compile(r"""["'`>][^"'`<>]*\b(?:NamAgent|nam-agent)(?![_\-/.])\b[^"'`<>]*["'`<]""")


def test_the_page_carries_no_product_name():
    """The name is gone from what a person reads: titles, labels, placeholders,
    empty states. Code comments, storage keys (nam_agent-theme), setting names
    (NAM_*) and paths (.nam/) are not visible text and stay."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "わく" not in html and "nam_agent-mark.svg" not in html and 'class="r-name"' not in html
    assert "github.com/HarshJain-cdac/nam_agent" not in html
    for path in [STATIC / "index.html", *sorted((STATIC / "js").glob("*.js"))]:
        src = path.read_text(encoding="utf-8")
        src = re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL)
        src = "\n".join(line for line in src.splitlines() if not line.strip().startswith(("//", "*", "/*")))
        found = VISIBLE.findall(src)
        assert not found, f"{path.name}: {found[:3]}"


def test_light_is_white_and_the_chat_is_blue_on_white():
    light = _blocks()[':root, [data-theme="light"]']
    assert light["--surface-bg"] == light["--surface-paper"] == "#FFFFFF"
    assert light["--chat-user-bg"] == light["--accent"] and light["--chat-user-fg"] == "#FFFFFF"
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".bubble{align-self:flex-end;background:var(--chat-user-bg);color:var(--chat-user-fg)" in css
    assert "#dock .card{background:var(--chat-reply-bg)" in css


def test_the_chat_is_the_whole_page():
    """The page is the chat: full width, and back to full width when the NIT closes."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert '<body class="dock-full">' in html
    assert "body.dock-full:not(.dock-closed) #dock{width:auto;flex:1" in css
    render = (STATIC / "js" / "render.js").read_text(encoding="utf-8")
    close = render[render.index("function closeNit()"):]
    assert 'document.body.classList.add("dock-full");' in close.split("}")[0]
