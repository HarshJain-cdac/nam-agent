"""Stage 1, steps 1d to 1j: the records that match an item, narrowed to one.

  find()      1d: SAP's PR, PO and service lines (sap_search) for the item,
              by meaning and by keyword. Each record is one candidate: a
              material (a material code, every SAP line of it), or a service
              line or line with no material code (by its description).
              Numbers come from SAP; a PUR case's own data that SAP does not
              show is the knowledge graph's (the kg_* tools).
  count()     how many of each the records are, in the words SAP's data
              gives them: "12 materials, 3 service lines".
  check()     1f: the agent's question, its options placed by the data. The
              agent gives each option a label and the words its records
              carry, as the records write them ("0-200", "U320"); the harness
              puts each record under the first option whose words it carries,
              so every count is the data's. Refused, with the reason, when an
              option's words are in no record or in every record, when a
              number its label states is missing from a record it got, or
              when fewer than two options get records of their own; the
              records no option reaches go under "Something else".
  answer()    1f: the officer's answer, an option or their own words; words
              every remaining record does not carry narrow to those that do.
  found_by()  where SAP's record carries the material's words: its short
              text, its long text only, or neither (found by meaning).
  tenders()   the past tenders behind one item's records, latest first:
              ordered and delivered, then ordered, then raised.
  coverage()  1j: the PUR cases ranked by how many of the need's materials
              each holds (the cart's picked codes, and the materials not
              taken forward too). The PUR holding the most is 100%; one
              holding them all is 100% only when it holds them all.

Nothing here decides what to ask: the agent does, and this checks it.
"""

from __future__ import annotations

import re

from nam_agent.nam import guard

TOP_K = 50                 # the KG server's most per search
MAX_OPTIONS = 8
OTHER = "Something else"
_WORD = re.compile(r"[a-z0-9][a-z0-9.#/-]*")
_NUM = re.compile(r"\d+(?:\.\d+)?")
# the count an option's label carries: "(12 materials, 3 service lines)"
_COUNT = re.compile(r"\s*\((?:\d+\s+[a-z ]+?(?:,\s*)?)+\)\s*$", re.IGNORECASE)
_VALUE = re.compile(r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?")


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    return [_stem(w.strip(".-/")) for w in _WORD.findall(guard.plain(text or "").lower())
            if w.strip(".-/")]


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s.#/-]", " ", _COUNT.sub("", text or "").lower()).split())


def _numbers(text: str) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n.lstrip("0") or "0"
            for n in _NUM.findall(guard.plain(text or ""))}


# -- 1d: the records -------------------------------------------------------------

def _sap(data: dict) -> list[dict]:
    out: dict[str, dict] = {}
    for res in (data or {}).get("results") or []:
        codes = res.get("codes") or {}
        for ln in res.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            code = str(ln.get("material") or "").strip()
            desc = " ".join(str(codes.get(code) or ln.get("material_name")
                                or res.get("description") or "").split())
            key = code or "SAP:" + desc.lower()[:60]
            c = out.setdefault(key, {"key": key, "source": "sap", "code": code,
                                     "description": desc, "text": "", "lines": []})
            # the record's own long text; the tender's title (job) is the PUR
            # case's, kept on the line for the cards, never the material's words
            extra = str(ln.get("long_text") or "")
            if extra.strip() and extra not in c["text"]:
                c["text"] = (c["text"] + " " + " ".join(extra.split()))[:800]
            c["lines"].append({k: ln.get(k) for k in (
                "kind", "doc_no", "date", "quantity", "unit", "rate_inr", "pr_no", "po_no",
                "pur_case_id", "status", "delivery_date", "job")})
    for c in out.values():
        c["lines"].sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return list(out.values())


def find(kg_url: str, query: str, call=None) -> tuple[list[dict], list[str]]:
    """(candidates, problems) for the item from SAP; an outage is a problem
    named, never "no record"."""
    from nam_agent.nam import kg_mcp

    call = call or kg_mcp.call
    try:
        data = call(kg_url, "sap_search", query=guard.plain(query).strip(), top_k=TOP_K)
    except Exception as exc:  # noqa: BLE001 - said, never hidden
        return [], [f"SAP could not be read ({type(exc).__name__})"]
    return _sap(data if isinstance(data, dict) else {}), []


def found_by(c: dict, query: str) -> str:
    """'short text' when the record's short text (its description or code)
    carries the material's words, 'long text' when only its long text does,
    'meaning' when neither (SAP's search found it by meaning: BiMet is
    Bimetallic)."""
    ws = words(query)
    if not ws:
        return "meaning"
    if carries({"description": c.get("description") or "", "code": c.get("code") or "",
                "text": ""}, ws):
        return "short text"
    if carries({"description": "", "code": "", "text": c.get("text") or ""}, ws):
        return "long text"
    return "meaning"


def found(cands: list[dict], query: str) -> str:
    """'9 by short text, 3 by long text only, 1 by meaning'."""
    by: dict[str, int] = {}
    for c in cands:
        f = found_by(c, query)
        by[f] = by.get(f, 0) + 1
    said = {"short text": "by short text", "long text": "by long text only",
            "meaning": "by meaning"}
    return ", ".join(f"{by[k]} {v}" for k, v in said.items() if by.get(k))


def unit(c: dict) -> str:
    """What one record is: a material (it has a material code), a service
    line, or a line with no material code."""
    if c.get("code"):
        return "material"
    kinds = {str(ln.get("kind") or "").upper() for ln in c.get("lines") or []}
    return "service line" if "SERVICE" in kinds else "line"


_PLURAL = {"material": "materials", "service line": "service lines", "line": "lines"}


def count(cands: list[dict]) -> str:
    """'12 materials, 3 service lines': the records counted by what they are."""
    by: dict[str, int] = {}
    for c in cands:
        by[unit(c)] = by.get(unit(c), 0) + 1
    return ", ".join(f"{n} {w if n == 1 else _PLURAL[w]}"
                     for w, n in sorted(by.items(), key=lambda wn: list(_PLURAL).index(wn[0])))


# -- matching words ---------------------------------------------------------------

def _haystack(c: dict) -> list[str]:
    """The record's words, each also by its parts and its numbers: "0-700cel"
    is "0700cel", "0", "700cel" and "700", so "0-700", "700" and "700cel" all
    find it; "u320t50" holds "320"."""
    out = []
    # a material code also by its tail: "I-TW-03TE1321-00" is found by
    # "03TE1321-00" and "03te1321", as an officer types part of it
    parts = [p for p in re.split(r"[-/ ]", str(c.get("code") or "").lower()) if p]
    out += ["".join(parts[i:]) for i in range(1, len(parts))]
    for h in words(" ".join(str(x or "") for x in (c["description"], c["code"], c["text"]))):
        out.append(h.replace("-", ""))
        out += [part for part in re.split(r"[-/]", h) if part]
        # and its numbers on their own: "u320t50" and "u320mm" both hold "320"
        out += re.findall(r"\d+(?:\.\d+)?", h)
    return out


def carries(c: dict, ws: list[str]) -> bool:
    """True when the record has every word: a word may begin one ("u320" in
    "u320mm"), but "0-100" never begins "0-1000"."""
    have = _haystack(c)

    def has(w: str) -> bool:
        w = w.replace("-", "")
        return any(h == w or (h.startswith(w) and not (w[-1:].isdigit() and h[len(w)].isdigit()))
                   for h in have)
    return bool(ws) and all(has(w) for w in ws)


# -- 1f: the question ---------------------------------------------------------------

class Refused(Exception):
    """The question does not stand on the records: the reason says why."""


def check(question: str, options: list, cands: list[dict]) -> dict:
    """The question with each option's records, placed by the data."""
    n = len(cands)
    question = " ".join(str(question or "").split())
    raw = options if isinstance(options, list) else []
    if not question or not 2 <= len(raw) <= MAX_OPTIONS:
        raise Refused(f"give one question and 2 to {MAX_OPTIONS} options")
    out, seen, placed, notes = [], set(), set(), []
    for o in raw:
        o = o if isinstance(o, dict) else {"label": str(o), "words": [str(o)]}
        label = " ".join(str(o.get("label") or "").split())[:70]
        said = o.get("words") if isinstance(o.get("words"), list) else [o.get("words") or label]
        ws = [w for x in said for w in words(str(x or ""))]
        if not label or norm(label) in seen:
            raise Refused("every option needs its own label")
        # words only a material code carries, and no short or long text: the
        # officer does not know codes (a tag in the short text is fine)
        said_text = [{"description": c["description"], "code": "", "text": c.get("text") or ""}
                     for c in cands]
        in_code = ws and all(any(carries(c, [w]) for c in cands) for w in ws)
        if in_code and not any(carries(c, ws) for c in said_text):
            raise Refused(f"option '{label}' is a material code: the officer does not know the "
                          "codes. Ask about what tells the records apart in their short or long "
                          "text (a size, a material, a rating, the use)")
        hit, ws, dropped = _relaxed(ws, cands, label)
        if not hit:
            raise Refused(f"no record carries {ws} (option '{label}'): use words as the "
                          f"records write them{_samples(ws, cands)}")
        if dropped:
            notes.append(f"'{label}': matched on {ws}, {dropped} set aside")
        if len(hit) == n:
            raise Refused(f"every record carries {ws}: option '{label}' tells none apart")
        items = [i for i in hit if i not in placed]
        for i in items:
            c = cands[i]
            missing = _numbers(label) - _numbers(f"{c['description']} {c['code']} {c['text']}")
            if missing:
                raise Refused(f"option '{label}' gets '{c['description'][:60]}', which does not "
                              f"carry {sorted(missing)[0]}: fix its words or its label")
        seen.add(norm(label))
        placed.update(items)
        if items:
            out.append({"label": label, "keys": [cands[i]["key"] for i in items],
                        "count": count([cands[i] for i in items])})
    lost = [i for i in range(n) if i not in placed]
    if len(out) < 2:
        got = ", ".join(f"'{o['label']}' {len(o['keys'])}" for o in out) or "none"
        raise Refused(f"the options must split the records: at least two options need "
                      f"records of their own (records placed: {got} of {n}; a record goes "
                      f"under the first option it carries). Words that split these records: "
                      f"{splitters(cands)}")
    if lost:
        out.append({"label": OTHER, "keys": [cands[i]["key"] for i in lost],
                    "count": count([cands[i] for i in lost])})
    if len(out) < 2:
        raise Refused("the options must split the records")
    return {"question": question, "options": out, "notes": notes}


def _relaxed(ws: list[str], cands: list[dict],
             label: str = "") -> tuple[list[int], list[str], list[str]]:
    """(the records hit, the words used, the words dropped). Every word, when
    some record carries them all; else the largest set of them some record
    carries ("0-200" alone when SAP writes "0-200Cel" and "0-200 DegC" and the
    option said both "0-200" and "DegC"). When the label states a number,
    the option is the words carrying it and the words the label names:
    "0-200 °C" with words "0-200" and "DegC" is every record of 0-200,
    whichever way it writes the unit. A word whose number the label states
    is never dropped."""
    from itertools import combinations

    stated = _numbers(label)
    key = [w for w in ws if _numbers(w) and _numbers(w) <= stated]
    if key:
        # the label states its range or size: the option is those words, and
        # the words it does not state (a unit SAP writes two ways) only narrow
        # it where the label says them too
        said = {w.replace("-", "") for w in words(label)}
        use = key + [w for w in ws if w not in key and w.replace("-", "") in said]
        if len(use) < len(ws):
            hit = [i for i, c in enumerate(cands) if carries(c, use)]
            if hit:
                return hit, use, [w for w in ws if w not in use]
    hit = [i for i, c in enumerate(cands) if carries(c, ws)]
    if hit or len(ws) < 2:
        return hit, ws, []
    rest = [w for w in ws if w not in key]
    for size in range(len(rest) - 1, -1, -1):
        best: tuple[list[int], list[str]] = ([], [])
        for sub in combinations(rest, size):
            use = key + list(sub)
            if not use:
                continue
            h = [i for i, c in enumerate(cands) if carries(c, use)]
            if len(h) > len(best[0]):
                best = (h, use)
        if best[0]:
            return best[0], best[1], [w for w in ws if w not in best[1]]
    return [], ws, []


def _samples(ws: list[str], cands: list[dict]) -> str:
    """How the records write what the option was after: descriptions that
    carry one of its words, to copy the words from."""
    seen = []
    for w in ws:
        for c in cands:
            if carries(c, [w]) and c["description"] not in seen:
                seen.append(c["description"])
                break
    return (". Records carrying some of them: " + "; ".join(f"'{d[:60]}'" for d in seen[:3])
            if seen else ". No record carries any of these words.")


def splitters(cands: list[dict], k: int = 8) -> str:
    """The words and values some of the records carry and others do not,
    with how many carry each, best splitting first: what a question's
    options can be made of."""
    n, seen = len(cands), {}
    for c in cands:
        text = guard.plain(f"{c['description']} {c['text']}")
        for w in ({w for w in words(text) if len(w) > 1}
                  | {re.sub(r"\s+", "", v) for v in _VALUE.findall(text)}):
            seen[w] = seen.get(w, 0) + 1
    best = sorted(((w, m) for w, m in seen.items() if 1 <= m < n),
                  key=lambda wm: (abs(wm[1] - n / 2), -wm[1], wm[0]))[:k]
    return ", ".join(f"{w} ({m})" for w, m in best) or "none"


# English function words: never the type of a record ("TW for DCU ...")
_FILLER = {"for", "of", "the", "and", "with", "to", "in", "on", "a", "an", "at", "by", "from"}


def type_of(c: dict, every: set[str]) -> str:
    own = [w for w in words(c["description"])
           if w not in every and len(w) > 1 and not _numbers(w) and w not in _FILLER]
    return own[0] if own else "-"


def initials(name: str) -> str:
    """'temperature gauge' -> 'tg': how SAP often shortens an item's name."""
    ws = words(name)
    return "".join(w[0] for w in ws) if len(ws) > 1 else ""


def by_meaning(cands: list[dict], name: str) -> set[str]:
    """The types every record of which SAP found by meaning only (its text
    does not name the item): other materials the search found, not types
    of the item ("thermocouple" in a search for "temperature gauge")."""
    if not cands or not words(name):
        return set()
    every = set.intersection(*(set(words(c["description"])) for c in cands)) | set(words(name))
    found: dict[str, set[str]] = {}
    for c in cands:
        found.setdefault(type_of(c, every), set()).add(found_by(c, name))
    return {t for t, how in found.items() if how == {"meaning"} and t != initials(name)}


def type_label(cands: list[dict], kind: str, name: str = "") -> str:
    """A type as the records write it: from its word, the words every record
    of that type shares after it, up to four ("Floating roof rim seal", not
    "FLOATING"). An abbreviation (TG) or a lone word stays as it is."""
    every = set.intersection(*(set(words(c["description"])) for c in cands)) | set(words(name))
    texts = []
    for c in cands:
        if type_of(c, every) != kind:
            continue
        toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9./#-]*", guard.plain(c["description"]))
        at = next((i for i, w in enumerate(toks) if _stem(w.lower().strip(".-/")) == kind), None)
        if at is not None:
            texts.append(toks[at:at + 4])
    if not texts:
        return kind.upper()
    common = texts[0]
    for t in texts[1:]:
        n = 0
        while n < min(len(common), len(t)) and common[n].lower() == t[n].lower():
            n += 1
        common = common[:n]
    # not a word every record shares ("GATE VALVE 2IN" is "GATE"), nor a filler
    # word at the end ("Thermowell for" is "Thermowell")
    while len(common) > 1 and (common[-1].lower() in _FILLER
                               or _stem(common[-1].lower().strip(".-/")) in every):
        common = common[:-1]
    label = " ".join(common) if common else texts[0][0]
    return label[:1].upper() + label[1:]


def suggest(cands: list[dict], name: str = "") -> dict | None:
    """A question the records themselves bear out, for when the agent asked
    none: the types SAP holds for the item (narrow.types) as the options,
    each placed and counted by check(). None when the types do not split."""
    kinds = [(t, m) for t, m in types(cands, name) if t != "-" and m < len(cands)][:MAX_OPTIONS - 1]
    if len(kinds) < 2:
        return _by_values(cands, name)
    # when some kinds are other materials the search found by meaning, the
    # question says what they are: kinds of material, not types of the item
    question = (f"SAP's search for '{name}' found these kinds of material: which one do you "
                "need?" if by_meaning(cands, name) else f"Which type of {name or 'it'} do you need?")
    try:
        return check(question, [{"label": type_label(cands, t, name), "words": [t]}
                                for t, _ in kinds], cands)
    except Refused:
        return _by_values(cands, name)      # the types do not split: the values may


def _by_values(cands: list[dict], name: str) -> dict | None:
    """A question from the values that split the records best, as their short
    text writes them (a size "U-240", a material "SS316", a rating "300#"):
    a token with a number and a letter or a sign, for when the types do not
    split the records."""
    n, seen, shown = len(cands), {}, {}
    for c in cands:
        toks = set()
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9.#/-]*", guard.plain(c["description"])):
            tok = tok.strip(".-/")
            if len(tok) >= 3 and re.search(r"\d", tok) and re.search(r"[A-Za-z#]", tok):
                toks.add(tok.lower())
                shown.setdefault(tok.lower(), tok)
        for v in toks:
            seen[v] = seen.get(v, 0) + 1
    vals = [v for v, m in sorted(seen.items(), key=lambda vm: (abs(vm[1] - n / 2), vm[0]))
            if 1 <= m < n][:MAX_OPTIONS - 1]
    if len(vals) < 2:
        return None
    try:
        return check(f"Which of these does the {name or 'item'} you need have?",
                     [{"label": shown[v], "words": [v]} for v in vals], cands)
    except Refused:
        return None


def labels(pending: dict) -> list[str]:
    """Each option with the number beside it, counted from SAP and named by
    what it counts: "0-200 °C (7 materials)"."""
    return [f"{o['label']} ({o.get('count') or len(o['keys'])})" for o in pending["options"]]


def answer(pending: dict | None, cands: list[dict], text: str) -> tuple[list[str], str] | None:
    """(the keys the answer keeps, how it was read), or None when it fits no
    option and no remaining record carries its words."""
    said = norm(text)
    options = (pending or {}).get("options") or []
    for o in options:
        if norm(o["label"]) == said:
            return o["keys"], f"option '{o['label']}'"
    # several options chosen at once ("0-200 °C; 0-80 °C"): every record of each
    parts = [norm(p) for p in re.split(r"\s*;\s*", text or "") if p.strip()]
    if len(parts) > 1:
        chosen = [o for o in options if norm(o["label"]) in parts]
        if len(chosen) == len(parts):
            keys = [k for o in chosen for k in o["keys"]]
            return keys, "options " + ", ".join(f"'{o['label']}'" for o in chosen)
    ws = words(text)
    full = [c["key"] for c in cands if carries(c, ws)]
    # the numbers typed are what the officer means ("320 mm" is every 320,
    # "U320mm" and "U320T50" alike); the other words narrow only when no
    # record is lost by the numbers alone
    nums = [w for w in ws if _numbers(w) and not re.search(r"[a-z]", w)]
    by_num = [c["key"] for c in cands if carries(c, nums)] if nums and nums != ws else []
    if len(by_num) > len(full):
        return by_num, f"the numbers {nums}"
    if full:
        return full, f"the words {ws}"
    # words no remaining record carries ("please", "the one with") are set
    # aside; the words some record carries decide
    kept = [w for w in ws if any(carries(c, [w]) for c in cands)]
    if kept and kept != ws:
        some = [c["key"] for c in cands if carries(c, kept)]
        if some:
            return some, f"the words {kept} ({[w for w in ws if w not in kept]} set aside)"
    return None


# -- 1i: the tenders ---------------------------------------------------------------

_ORDER = {"delivered": 0, "ordered": 1, "raised": 2, "cancelled": 3}
# what the officer reads on a card, and its rank after the year: a PR with
# its PO first, then a PR still going, then a PR whose PO was cancelled
LABEL = {"delivered": ("PR + PO, delivered", 0), "ordered": ("PR + PO", 0),
         "raised": ("PR ongoing", 1), "cancelled": ("PR ongoing, PO cancelled", 2)}


def doc_no(value) -> str:
    """An SAP document number as SAP writes it: ten digits, zero-padded
    (PR 10082938 is 0010082938). Anything not a plain number stays as it is."""
    text = str(value or "").strip()
    return text.zfill(10) if text.isdigit() and len(text) < 10 else text


def prs_of(c: dict) -> list[dict]:
    """The PRs a record's SAP lines belong to, latest first: each with its
    lines, its POs, its PUR case, its last rate and its quantities."""
    by: dict[str, list[dict]] = {}
    for ln in c.get("lines") or []:
        if ln.get("pr_no"):
            by.setdefault(str(ln["pr_no"]), []).append(ln)
    out = []
    for pr_no, lines in by.items():
        st = status_of(lines)
        priced = [ln for ln in lines if ln.get("rate_inr")]
        last = next((ln for ln in priced if ln.get("kind") in ("PO", "SERVICE")),
                    priced[0] if priced else lines[0])
        out.append({"pr_no": pr_no, "lines": lines, **st,
                    "po": sorted({str(ln["po_no"]) for ln in lines if ln.get("po_no")}),
                    "pur": next((ln["pur_case_id"] for ln in lines if ln.get("pur_case_id")), ""),
                    "job": _job(lines), "last": last})
    out.sort(key=lambda p: p["latest"], reverse=True)
    return out


def status_of(lines: list[dict]) -> dict:
    """The status of a record or a PUR case from its SAP lines: the label,
    its rank and the year of the latest line."""
    st = _status(lines)
    dates = [str(ln.get("date")) for ln in lines if ln.get("date")]
    latest = max(dates) if dates else ""
    return {"status": st, "label": LABEL[st][0], "rank": LABEL[st][1],
            "latest": latest, "year": latest[:4]}


def _job(lines: list[dict]) -> str:
    """The tender's title, as most of its SAP lines name it."""
    jobs = [str(ln["job"]) for ln in lines if ln.get("job")]
    return max(set(jobs), key=jobs.count) if jobs else ""


def by_year_then_status(rows: list[dict]) -> list[dict]:
    """The order the cards show: the latest year first, then PR + PO, PR
    ongoing, PR ongoing with its PO cancelled; the latest date within."""
    rows = sorted(rows, key=lambda r: r.get("latest") or "", reverse=True)
    return sorted(rows, key=lambda r: (-(int(r["year"]) if str(r.get("year")).isdigit() else 0),
                                       r["rank"]))


def _status(lines: list[dict]) -> str:
    if any("cancel" in str(ln.get("status") or "").lower()
           or "delet" in str(ln.get("status") or "").lower() for ln in lines):
        return "cancelled"
    if any(ln.get("delivery_date") for ln in lines if ln.get("kind") in ("PO", "SERVICE")):
        return "delivered"
    if any(ln.get("po_no") or ln.get("kind") in ("PO", "SERVICE") for ln in lines):
        return "ordered"
    return "raised"


def tenders(cands: list[dict]) -> list[dict]:
    """The PUR cases behind the records, the latest year first, then by
    status: PR + PO, PR ongoing, PR ongoing with its PO cancelled."""
    by: dict[str, dict] = {}
    for c in cands:
        for ln in c["lines"]:
            pur = ln.get("pur_case_id")
            if not pur:
                continue
            t = by.setdefault(pur, {"pur": pur, "lines": [], "records": set()})
            t["lines"].append(ln)
            t["records"].add(c.get("code") or c["description"][:50])
    out = []
    for t in by.values():
        out.append({"pur": t["pur"], **status_of(t["lines"]),
                    "po": sorted({str(ln["po_no"]) for ln in t["lines"] if ln.get("po_no")}),
                    "pr": sorted({str(ln["pr_no"]) for ln in t["lines"] if ln.get("pr_no")}),
                    "records": sorted(t["records"]), "tender_no": t.get("tender_no"),
                    "job_name": _job(t["lines"])})
    return by_year_then_status(out)


def coverage(items: list[dict]) -> dict:
    """1j: the PUR cases ranked by how many of the need's materials each
    holds. items: {name, chosen, picked (the cart's record or None), records
    (the records that stand for it: the picked one, else those still in, or
    every record found for a material not taken forward)}.

    A PUR holds a material when one of its SAP lines is one of the material's
    records; for a material in the cart, that is the very code picked, and
    the PUR's PR and PO for it are named. The PUR holding the most materials
    is 100%; the rest are their share of that most ("2 of 3 materials" is
    67% when one PUR holds all three, 100% when none holds more than two)."""
    by: dict[str, dict] = {}
    for it in items:
        for c in it["records"]:
            for ln in c.get("lines") or []:
                pur = ln.get("pur_case_id")
                if not pur:
                    continue
                t = by.setdefault(pur, {"pur": pur, "lines": [], "items": {}})
                t["lines"].append(ln)
                t["items"].setdefault(it["name"], set()).add(c.get("code") or c["description"][:50])
    names = [it["name"] for it in items]
    cart = {it["name"] for it in items if it.get("picked")}
    out = []
    for t in by.values():
        out.append({"pur": t["pur"], **status_of(t["lines"]), "job_name": _job(t["lines"]),
                    "items": [n for n in names if n in t["items"]],
                    "codes": {n: sorted(t["items"][n]) for n in names if n in t["items"]},
                    "cart": len(cart & set(t["items"])),
                    "pr": sorted({str(ln["pr_no"]) for ln in t["lines"] if ln.get("pr_no")}),
                    "po": sorted({str(ln["po_no"]) for ln in t["lines"] if ln.get("po_no")})})
    most = max((len(t["items"]) for t in out), default=0)
    for t in out:
        t["match"] = round(100 * len(t["items"]) / most) if most else 0
        t["covers"] = f"{len(t['items'])} of {len(names)} materials"
    out.sort(key=lambda t: t["latest"], reverse=True)
    # the most materials first (that is the match); then the latest year and
    # the status, as every card list shows them
    out = by_year_then_status(out)
    out.sort(key=lambda t: (-len(t["items"]), -t["cart"]))
    return {"total": len(names), "most": most, "cart": sorted(cart), "purs": out,
            "all": bool(out) and most == len(names)}


# -- what the agent reads to choose its question ---------------------------------

def types(cands: list[dict], name: str = "") -> list[tuple[str, int]]:
    """The types SAP holds among the records, most first: each record's type
    is the first word of its short text that not every record shares and
    that is not the item's own name ("GASKET SPW 2IN" and "GASKET RTJ R24"
    are spw and rtj)."""
    if not cands:
        return []
    every = set.intersection(*(set(words(c["description"])) for c in cands)) | set(words(name))
    kinds: dict[str, int] = {}
    for c in cands:
        first = type_of(c, every)
        kinds[first] = kinds.get(first, 0) + 1
    short = initials(name)
    # the item's own initials first (TG for a temperature gauge), then the most
    return sorted(kinds.items(), key=lambda kv: (kv[0] != short, -kv[1], kv[0]))


def digest(cands: list[dict], limit: int = 40, name: str = "") -> str:
    """The records as the agent reads them to choose its next question: the
    types (each short text's first word not every record shares), the words that split them (in some
    records but not all, with how many carry each) and the records in short.
    Everything is counted from the records; nothing names a detail to ask."""
    n = len(cands)
    kinds = dict(types(cands, name))
    meaning = by_meaning(cands, name)
    seen: dict[str, int] = {}
    for c in cands:
        text = guard.plain(f"{c['description']} {c['text']}")
        # the same text a question's words are matched on (the description and
        # SAP's long text); its words, and its numbers and ranges on their own: "0-200Cel" and
        # "0-200 DegC" are both the value 0-200, "U320mm" and "U320T50" both 320
        values = {w for w in words(text) if len(w) > 1}
        values |= {re.sub(r"\s+", "", v) for v in _VALUE.findall(text)}
        for w in values:
            seen[w] = seen.get(w, 0) + 1
    split = sorted(((w, k) for w, k in seen.items() if 1 < k < n),
                   key=lambda wk: (abs(wk[1] - n / 2), -wk[1], wk[0]))[:30]
    out = [f"{count(cands)}.",
           "Types (the first word of the short text that not every record shares): "
           + ", ".join(f"{k} {v}" + (" (found by meaning only: another material, not a type "
                                     "of the item)" if k in meaning else "")
                       for k, v in list(kinds.items())[:12])
           + ".",
           "Words that split them (records carrying each): "
           + (", ".join(f"{w} {k}" for w, k in split) or "none") + ".",
           "Records (key: description):"]
    out += [f"- {c['key']}: {guard.sanitise(c['description'], 90)}" for c in cands[:limit]]
    if n > limit:
        out.append(f"(+{n - limit} more)")
    same = _same_short_text(cands)
    if same:
        out.append("Same short text, different material codes (only the long text tells "
                   "them apart):")
        out += same
    return "\n".join(out)


def _same_short_text(cands: list[dict], limit: int = 6) -> list[str]:
    """For each short text several codes share, the words of each code's long
    text the others' lack: what a question must ask to tell them apart."""
    groups: dict[str, list[dict]] = {}
    for c in cands:
        groups.setdefault(norm(c["description"]), []).append(c)
    out = []
    for group in [g for g in groups.values() if len(g) > 1][:limit]:
        own = [set(words(c.get("text") or "")) for c in group]
        parts = []
        for c, ws in zip(group, own, strict=True):
            others = set().union(*(o for o in own if o is not ws)) if len(own) > 1 else set()
            diff = sorted(w for w in ws - others if len(w) > 1)[:8]
            parts.append(f"{c['key']}: {', '.join(diff) if diff else 'no long text of its own'}")
        out.append(f"- '{guard.sanitise(group[0]['description'], 70)}' ({len(group)} codes): "
                   + "; ".join(parts))
    return out


def on_pr(kg_url: str, pr_no: str, call=None) -> tuple[list[dict], list[str]]:
    """(the records SAP holds on one PR, problems): every material bought on
    it, found by the PR's own number."""
    from nam_agent.nam import kg_mcp

    call = call or kg_mcp.call
    want = str(pr_no).lstrip("0")
    try:
        data = call(kg_url, "sap_search", query=want, top_k=50)
    except Exception as exc:  # noqa: BLE001 - said, never hidden
        return [], [f"PR {doc_no(pr_no)}: SAP could not be read ({type(exc).__name__})"]
    recs = _sap(data if isinstance(data, dict) else {})
    return [r for r in recs if any(str(ln.get("pr_no") or "").lstrip("0") == want
                                   for ln in r.get("lines") or [])], []


def _lead(description: str) -> str:
    """A description's first word, as SAP writes the kind of thing first."""
    m = re.match(r"\W*([A-Za-z][A-Za-z/&.]*)", description or "")
    return m.group(1).lower().rstrip(".") if m else ""


def co_bought(kg_url: str, items: list[dict], call=None, per_item: int = 3,
              most: int = 6) -> dict:
    """What NRL bought together with the need's materials before: the latest
    past PRs holding each material (per_item each, most in all), and every
    other material on those PRs with how many of them it was on. Only SAP's
    records; which of them belong to this need is the agent's judgement."""
    prs: list[str] = []
    for it in items:
        latest: dict[str, str] = {}
        for c in it.get("candidates") or []:
            for p in prs_of(c):
                latest[p["pr_no"]] = max(latest.get(p["pr_no"], ""), p["latest"])
        for pr_no in sorted(latest, key=lambda n: latest[n], reverse=True)[:per_item]:
            if pr_no not in prs:
                prs.append(pr_no)
    prs = prs[:most]
    own = {c["key"] for it in items for c in it.get("candidates") or []}
    # a record SAP names the way it names one of the need's own ("TG
    # Capillary ..." beside the need's "TG Bimetallic ...") may be another size
    # of the same material: it is marked so, for the agent to judge
    leads: dict[str, str] = {}
    for it in items:
        for c in it.get("candidates") or []:
            lead = _lead(c.get("description") or "")
            if lead:
                leads.setdefault(lead, it["name"])
    rows: dict[str, dict] = {}
    problems: list[str] = []
    for pr_no in prs:
        recs, bad = on_pr(kg_url, pr_no, call)
        problems += bad
        for r in recs:
            if r["key"] in own:
                continue
            row = rows.setdefault(r["key"], {"key": r["key"], "code": r.get("code") or "",
                                             "description": r.get("description") or "",
                                             "unit": unit(r), "prs": [], "latest": "",
                                             "like": leads.get(_lead(r.get("description") or ""), "")})
            if pr_no not in row["prs"]:
                row["prs"].append(pr_no)
            dates = [str(ln.get("date") or "") for ln in r.get("lines") or []]
            row["latest"] = max([row["latest"], *dates])
    ranked = sorted(rows.values(), key=lambda r: (len(r["prs"]), r["latest"]), reverse=True)
    return {"prs": prs, "rows": ranked, "problems": problems}
