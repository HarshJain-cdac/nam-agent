"""The officer's edits to a list of lines, in one plain format shared by
quantity.finalise and the estimate flows. Parsed in code, never by a model.

One edit per ';' or new line:

    3: qty=20, uom=Nos             change line 3 (numbered from 1)
    3: remove                      drop line 3
    3-40, 52: uom=Nos              the same edit on several lines (ranges and lists)
    3-40: remove                   drop lines 3 to 40
    add: item=gate valve 2in, qty=10, uom=Nos, code=MC-GV-2
    all: insurance=esi             a setting for the whole list

Keys are lower case letters and underscores; a value runs to the next comma
(a value with a comma in it goes in double quotes). Anything else is an
error naming the edit, and no edit of the answer is applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_NUMS = r"\d{1,4}(?:\s*-\s*\d{1,4})?(?:\s*,\s*\d{1,4}(?:\s*-\s*\d{1,4})?)*"
_EDIT = re.compile(rf"^\s*({_NUMS}|add|all)\s*:\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL)
_PAIR = re.compile(r'\s*([a-z_]{1,30})\s*=\s*("([^"]*)"|[^,]*)\s*(?:,|$)')
MAX_EDITS = 500
MAX_TARGETS = 2000                   # lines one answer may name, ranges counted out


@dataclass
class Edit:
    target: str                      # "3" (1-based line), "add" or "all"
    fields: dict = field(default_factory=dict)
    remove: bool = False

    @property
    def index(self) -> int | None:
        return int(self.target) - 1 if self.target.isdigit() else None


class LineError(ValueError):
    pass


def parse(text: str, allowed: set[str]) -> list[Edit]:
    """The edits in `text`, each field checked against `allowed`."""
    parts = [p for p in re.split(r"[;\n]", text or "") if p.strip()]
    if not parts:
        raise LineError("no edits: write them as '3: qty=20, uom=Nos; add: item=...'")
    if len(parts) > MAX_EDITS:
        raise LineError(f"at most {MAX_EDITS} edits in one answer")
    out = []
    for part in parts:
        m = _EDIT.match(part)
        if not m:
            raise LineError(f"'{part.strip()[:60]}': start with a line number, add: or all:")
        target, rest = m.group(1).lower(), m.group(2)
        targets = _targets(target, part) if target[0].isdigit() else [target]
        if rest.lower() == "remove":
            if not targets[0].isdigit():
                raise LineError(f"'{part.strip()[:60]}': remove needs a line number")
            out += [Edit(t, remove=True) for t in targets]
            continue
        fields, pos = {}, 0
        while pos < len(rest):
            pair = _PAIR.match(rest, pos)
            if not pair or pair.end() == pos:
                raise LineError(f"'{part.strip()[:60]}': write key=value, key=value")
            key = pair.group(1)
            value = pair.group(3) if pair.group(3) is not None else pair.group(2).strip()
            if key not in allowed:
                raise LineError(f"'{key}' is not a field here: one of "
                                f"{', '.join(sorted(allowed))}")
            fields[key] = value
            pos = pair.end()
        if not fields:
            raise LineError(f"'{part.strip()[:60]}': nothing to set")
        out += [Edit(t, dict(fields)) for t in targets]
    if len(out) > MAX_TARGETS:
        raise LineError(f"at most {MAX_TARGETS} lines in one answer")
    return out


def _targets(text: str, part: str) -> list[str]:
    """'3-5, 9' -> ['3', '4', '5', '9']: each line once, in order."""
    out: list[str] = []
    seen: set[int] = set()
    for chunk in text.split(","):
        lo, _, hi = chunk.partition("-")
        lo, hi = int(lo), int(hi or lo)
        if lo < 1 or hi < lo:
            raise LineError(f"'{part.strip()[:60]}': lines are numbered from 1, low to high")
        if hi - lo >= MAX_TARGETS:
            raise LineError(f"at most {MAX_TARGETS} lines in one answer")
        out += [str(n) for n in range(lo, hi + 1) if n not in seen]
        seen.update(range(lo, hi + 1))
    return out


def number(value: str, what: str, *, zero: bool = False) -> Decimal:
    """A positive (or, with zero, non-negative) number, commas allowed."""
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        d = None
    if d is None or not d.is_finite() or d < 0 or (d == 0 and not zero):
        raise LineError(f"{what}: '{value}' is not a {'non-negative' if zero else 'positive'} "
                        "number")
    return d


def plain(d: Decimal) -> float | int:
    """A JSON number for a Decimal: an int when whole."""
    d = d.normalize()
    return int(d) if d == d.to_integral_value() else float(d)
