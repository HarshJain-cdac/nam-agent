"""The agent's tools for the PR Creation Flow: each does one step and keeps
the case; each gate the manual sets is checked here, in code.

  nam_need      1a-1c  open the chat's case for the job description, with the
                       materials it names: each is searched in SAP at once
                       and counted (by short text, by long text)
  nam_choose    1d     ask which materials go forward, or record the choice
  nam_search           search one item again, with other words
  nam_ask       1f     a question whose options the records bear out
  nam_answer    1f-1g  the officer's answer: narrows the records, or records
                       a detail (default=true: NRL's standard value, with
                       its source)
  nam_pick      1h     the one material for the item, put in the cart (or
                       taken out); then the next chosen item
  nam_tenders   1j     the PURs holding the most of the need's materials
                       (no item), or one item's past tenders
  nam_estimate  2a-2f  the cart's estimate, only when the officer asks
  nam_pr        3a-3b  each cart item's own PR: fill, set, confirm
  nam_undo             take back the officer's last act(s)
  nam_case             the chat's case as it stands, or another case resumed

The agent decides when to call which (flow.py is its guide). A tool that
refuses says why in its reply, so the agent can put it right. Every call is
one audit row; every write is a new version of the case (case.py).
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

from nam_agent.nam import audit, case, chatstate, estimate, guard, narrow, permissions, pr
from nam_agent.nam import pr as pr_mod
from nam_agent.tools.registry import Tool

CATEGORIES = ("goods", "services", "works")
SHOW = 25             # records listed in a reply; the rest are counted
ALL = "All of them"   # the choice of every material that has records
UNDO_WORDS = ("undo", "go back", "back", "take it back", "undo the last step")
# the words of the page's own buttons, read by the harness (read_click)
OTHER_LABEL = "Show me something else"
YES_LABEL = "Yes, add it"
NO_LABEL = "No, show the others"
UNDO_LABEL = "Undo the last step"
# suggestions from what NRL bought with the need's materials before
RELATED_ROWS = 40
MAX_SUGGEST = 4
SUGGESTED_TAG = "often bought with these"
# the cart review, once every chosen item is in the cart
DONE_LABEL = "That's everything"
MORE_LABEL = "Add more items"


def category_question(guess: str = "", why: str = "") -> dict:
    """The category question: plain, or the agent's guess for the officer to
    confirm or change. Its options are the categories themselves, the guess
    first; "values" maps each option back to its category."""
    if guess not in CATEGORIES:
        values = {c.capitalize(): c for c in CATEGORIES}
        return {"question": "Is this goods, services or works?", "options": list(values),
                "values": values}
    why = guard.sanitise(" ".join((why or "").split()), 200).rstrip(" .")
    values = {f"Yes, go with {guess.capitalize()}": guess}
    values.update({f"Change to {c.capitalize()}": c for c in CATEGORIES if c != guess})
    return {"question": (f"This looks like {guess}" + (f": {why}" if why else "") + ". Shall we "
                         f"go ahead with {guess}, or change it?"),
            "options": list(values), "values": values, "guess": guess}


REVIEW_QUESTION = "Is that everything, or do you want to add more to this cart?"


def review_question() -> dict:
    return {"question": REVIEW_QUESTION, "options": [DONE_LABEL, MORE_LABEL]}


def _date_said(iso: str, heard: str) -> bool:
    """The officer said this date in their own way: its day, and its month by
    number or by name ("1st December", "01/12")."""
    import calendar

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if not m:
        return False
    month, day = int(m.group(2)), int(m.group(3))
    nums = {int(n) for n in re.findall(r"\d+", heard)}
    named = calendar.month_name[month].lower() in heard or (
        calendar.month_abbr[month].lower() + " ") in heard + " "
    return day in nums and (named or month in nums - {day} or month == day)


def _shown(q: dict) -> dict:
    """A stored question as the page shows it (without what only the harness reads)."""
    return {k: v for k, v in q.items() if k not in ("values", "guess")}


class Stop(Exception):
    """The step cannot go on: the message goes back to the agent."""


def _rs(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def records_table(cands: list[dict], limit: int = SHOW) -> str:
    rows = ["| Key | Is | Description | Last order (date, rate INR) | PUR |",
            "|---|---|---|---|---|"]
    for c in cands[:limit]:
        last = next((ln for ln in c["lines"] if ln.get("kind") in ("PO", "SERVICE")
                     and ln.get("rate_inr")), None) or (c["lines"][0] if c["lines"] else {})
        doc = narrow.doc_no(last.get("po_no") or last.get("doc_no")) or "-"
        pur = last.get("pur_case_id") or "-"
        rows.append(f"| {c['key']} | {narrow.unit(c)} | "
                    f"{guard.sanitise(c['description'], 80).replace('|', '/')} | "
                    f"{doc} ({last.get('date') or '-'}, {_rs(last.get('rate_inr'))}) | {pur} |")
    if len(cands) > limit:
        rows.append(f"(+{len(cands) - limit} more)")
    return "\n".join(rows)


PICK_FROM = 3          # records few enough to show the officer as a table to pick from


def _num(v) -> str:
    """10.0 -> '10', 2.5 -> '2.5'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else str(f)


def _quantities(rec: dict, chosen: dict | None) -> list[float]:
    """The quantities SAP shows for a record, the chosen PR's first, then the
    most recent, each once."""
    out: list[float] = []
    for ln in ((chosen or {}).get("lines") or []) + list(rec.get("lines") or []):
        try:
            q = float(ln.get("quantity"))
        except (TypeError, ValueError):
            continue
        if q > 0 and q not in out:
            out.append(q)
    return out[:5]


def _show(item: str, left: list[dict], answers: list[dict] | None) -> None:
    """The item's matches, shown to the officer as cards, the best first."""
    from nam_agent.nam import cards

    chatstate.emit("nam_cards", cards.records(item, left[:SHOW_MATCHES], answers))
    chatstate.mark_done("cards")


SHOW_MATCHES = 8       # the most matches shown as cards at once


def _view(left: list[dict], item: str = "", answers: list[dict] | None = None) -> str:
    """What the agent gets back after a search or an answer. The questions
    come first: while the records still differ in what they say, the agent
    gets the digest to choose its next question from (not for the officer's
    eyes). The matches are shown as cards once the item is described: one
    record left, or nothing left in the records to tell them apart; or when
    the agent judges the description complete and calls nam_matches."""
    splits = len(left) > 1 and narrow.splitters(left) != "none"
    if len(left) == 1 or (left and len(left) <= SHOW_MATCHES and not splits):
        _show(item, left, answers)
        if len(left) == 1:
            return (records_table(left) + "\nThe item is described: its one match is shown to the "
                    "officer as a card. Say what it is in words; when the officer continues with "
                    "it, put it in the cart with nam_pick (key, pr). Ask nothing more about it.")
        return (records_table(left) + "\nNothing in these records tells them apart any more: "
                "they are shown to the officer as cards. Let the officer choose one by what it "
                "says (they do not know codes), then put it in the cart with nam_pick.")
    return ("FOR YOU, NOT TO SHOW: the records still in, to choose the next question from.\n"
            + narrow.digest(left, name=item) + "\nDo not paste these records. Read the need back in one "
            "line, say how many records are in, and ask ONE question about the one detail that "
            "best splits them, with nam_ask. When the officer's need is fully described and "
            "the details left do not matter to them, show the matches with nam_matches.")


class Steps:
    """The tools, bound to the database, the home folder and the KG."""

    def __init__(self, conn, home: Path, kg_url: str, call=None, kg: bool = True):
        self.conn, self.home, self.kg_url, self.call = conn, Path(home), kg_url, call
        self.kg = kg            # the tender knowledge graph's kg_* tools are given
        self.refusals: dict[str, int] = {}     # refusals in a row, per chat

    # -- plumbing -----------------------------------------------------------------

    def _session(self) -> str:
        return chatstate.current()

    def _case(self) -> dict:
        data = case.for_session(self.conn, self._session())
        if data is None:
            raise Stop("No case is open in this chat: call nam_need with the officer's need "
                       "first.")
        return data

    def _item(self, data: dict, name: str, working: bool = True) -> dict:
        """The item named; '' is the item being worked now. working: it must
        be one the officer takes forward."""
        name = narrow.norm(name) if name else ""
        it = case.item(data, name) if name else (case.current_item(data)
                                                 or case.item(data, ""))
        if it is None:
            raise Stop(f"No item '{name}' in {data['case_id']}; its items are: "
                       + ", ".join(i["name"] for i in data["items"]) + ".")
        undecided = not any("selected" in i for i in data["items"])
        if working and undecided and sum(1 for i in data["items"] if i.get("candidates")) > 1:
            raise Stop("The officer has not chosen which materials go forward yet: ask with "
                       "nam_choose(question=...), or read their choice with "
                       "nam_choose(items=[...]).")
        if working and it.get("selected") is False:
            raise Stop(f"'{it['name']}' was not taken forward. To work it, add it to the "
                       "chosen items with nam_choose.")
        return it

    def _save(self, data: dict, act: str = "", did: str = "") -> dict:
        return case.save(self.conn, data, self._session(), act, did)

    def _run(self, name: str, fn, **args) -> str:
        """One tool call: the step, its audit row, and a refusal as text."""
        t0, status, out, cid = time.perf_counter(), "ok", "", ""
        # an argument the step does not take (a model sends case_id= to a step
        # that reads the chat's own case) is left out, not an error
        import inspect

        takes = inspect.signature(fn).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in takes.values()):
            args = {k: v for k, v in args.items() if k in takes}
        try:
            out, cid = fn(**args)
        except (Stop, narrow.Refused, ValueError, permissions.Forbidden, case.NotYours,
                case.Stale) as exc:
            status = "refused"
            out = (f"REFUSED: {exc}" if not isinstance(exc, case.Stale) else
                   "REFUSED: the case changed meanwhile; read it with nam_case and try again.")
        chat = self._session()
        self.refusals[chat] = self.refusals.get(chat, 0) + 1 if status == "refused" else 0
        if self.refusals[chat] >= 3:
            out += (f" This is refusal {self.refusals[chat]} in a row: stop retrying. Tell the "
                    "officer in plain words what you found and ask them directly.")
        try:
            audit.record(self.conn, kind="tool", name=name, status=status, case_id=cid,
                         ms=int((time.perf_counter() - t0) * 1000),
                         detail={"args": {k: str(v)[:80] for k, v in args.items()
                                          if k != "_notify"}})
        except Exception:  # noqa: BLE001 - the audit row is best effort in a chat turn
            pass
        return out

    def _may(self, action: str) -> None:
        permissions.require(case.current().roles, action)

    def _not_asked(self) -> None:
        if chatstate.asked():
            raise Stop(f"You asked \"{chatstate.asked()}\" in this turn: the officer has not "
                       "answered yet. Stop and wait for their reply.")

    def _lookup(self, it: dict, query: str = "") -> list[str]:
        """Search SAP for the item; its records replace the old ones. The
        problems named (an outage is never "no record")."""
        q = (query or it["name"]).strip()
        cands, problems = narrow.find(self.kg_url, q, self.call)
        if not cands and problems:
            return problems
        # the types SAP holds for the item, kept as found: the cart node shows
        # them after the records are narrowed (cart.py)
        it.update(query=q, searched=True, candidates=cands, keys=[c["key"] for c in cands],
                  trail=[], pending=None, picked=None, types=narrow.types(cands, q)[:12])
        return problems

    # -- 1a-1c ---------------------------------------------------------------------

    def need(self, need: str = "", category: str = "", why: str = "", items=None,
             new: bool = False, add: bool = False, _notify=None):
        self._may("need")
        category = (category or "").strip().lower()
        if category and category not in CATEGORIES:
            raise Stop("category is goods, services or works")
        names = [" ".join(str(i).split()) for i in (items or []) if str(i).strip()]
        # the need in the officer's words; a need of one word ("gasket") is
        # often given only as its material
        need = (need or "").strip() or ", ".join(names)
        if new and chatstate.done("need"):
            new = False          # a case was opened or set this turn: it is the one
        data = None if new else case.for_session(self.conn, self._session())
        said = narrow.words(chatstate.message())
        guess = ""
        if category and data is None and said and not any(
                w.startswith(category[:4]) for w in said):
            # the officer did not say goods, services or works: the agent's
            # category is its guess, for the officer to confirm or change
            guess, category = category, ""
        if data is None:
            if not need:
                raise Stop("Give the officer's need, in their words, as need= (or the "
                           "materials it names as items=).")
            data = case.open_case(self.conn, self._session(), need, category)
        elif category and data.get("asking_category"):
            self._not_asked()
        elif category and chatstate.clicked() == "nam_need" and data.get("category") == category:
            raise Stop("The harness already read the category this turn: go on from there.")
        added = []
        if add and data.get("items"):
            # more materials for this same case and cart: the ones already in
            # stay as they are, the new ones are searched and worked after them
            if not names:
                raise Stop("Name the materials to add as items=[...], one short name each.")
            have = {i["name"].lower(): i for i in data["items"]}
            chosen_made = any("selected" in i for i in data["items"])
            for n in names:
                old = have.get(n.lower())
                if old is not None:
                    if chosen_made and not old.get("selected"):
                        # in the need but not taken forward before: it is now
                        old["selected"] = True
                        data["items"].remove(old)
                        data["items"].append(old)
                        added.append(old["name"])
                    continue
                have[n.lower()] = None
                added.append(n)
                data["items"].append({"name": n, "searched": False,
                                      **({"selected": True} if chosen_made else {})})
            if not added:
                raise Stop("Every one of these is already in the case: "
                           + ", ".join(names) + ".")
            # the cart is reviewed again once the new items are in, and the
            # PURs are ranked again over every material
            for key in ("review", "matched", "common_asked"):
                data.pop(key, None)
            names = []
        elif need.strip():
            data["need"] = need.strip()
        if category:
            data["category"] = category
        if not added and (names or not data["items"]):
            have = {i["name"].lower(): i for i in data["items"]}
            before = set(have)
            data["items"] = [have.get(n.lower()) or {"name": n, "searched": False}
                             for n in (names or [data["need"]])]
            if {i["name"].lower() for i in data["items"]} != before:
                # other materials: the choice of what goes forward is made again
                for i in data["items"]:
                    i.pop("selected", None)
                data["choosing"] = None
        # 1a: goods, services or works is settled with the officer before
        # anything is searched: the agent's guess, confirmed or changed by
        # them, or asked plainly when the agent gave none
        if not data.get("category"):
            q = category_question(guess, why)
            data["asking_category"] = q
            data = self._save(data, "need", "set the need: "
                              + ", ".join(i["name"] for i in data["items"])
                              + (f" (looks like {guess})" if guess else ""))
            chatstate.set_case_open(True)
            chatstate.mark_done("need")
            if _notify:
                _notify("ask_options", {**_shown(q), "field": "nam"})
            chatstate.mark_asked(q["question"])
            return (f"{data['case_id']} opened for '{data['need']}' (materials: "
                    + ", ".join(i["name"] for i in data["items"]) + f"). Asked: \"{q['question']}\" "
                    "Options: " + " | ".join(q["options"]) + ". Stop here and wait: nothing is "
                    "searched until the officer confirms or changes it; their answer goes to "
                    "nam_need(category=...).", data["case_id"])
        data["asking_category"] = None
        # 1c: each material is searched in SAP at once, so the officer sees
        # how many SAP holds of each before choosing what goes forward
        problems = []
        for it in data["items"]:
            if not it.get("searched"):
                problems += [f"'{it['name']}': {p}" for p in self._lookup(it)]
        offer = [it for it in data["items"] if it.get("candidates")]
        if len(data["items"]) == 1 and offer:
            data["items"][0]["selected"] = True
        data = self._save(data, "need", ("added to the cart's need: " + ", ".join(added)) if added
                          else f"set the need and its {len(data['items'])} material(s)")
        chatstate.set_case_open(True)       # from now in this turn too, not only the next
        chatstate.mark_done("need")
        rows = ["| # | Material | In SAP | Found by |", "|---|---|---|---|"]
        for n, it in enumerate(data["items"], 1):
            cands = it.get("candidates") or []
            rows.append(f"| {n} | {it['name']} | "
                        f"{narrow.count(cands) or ('not searched' if not it.get('searched') else 'none')}"
                        f" | {narrow.found(cands, it.get('query') or it['name']) or '-'} |")
        capped = [it["name"] for it in data["items"]
                  if len(it.get("candidates") or []) >= narrow.TOP_K]
        notes = ""
        if capped:
            notes += (f"\nSAP's search gives at most {narrow.TOP_K} records: "
                      f"{', '.join(capped)} may have more.")
        if problems:
            notes += "\nNot searched: " + "; ".join(problems) + "."
        want = {"goods": "material", "services": "service line"}.get(data.get("category") or "")
        other = [it for it in data["items"] if want and it.get("candidates")
                 and all(narrow.unit(c) != want for c in it["candidates"])]
        for it in other:
            kinds = sorted({narrow.unit(c) for c in it["candidates"]})
            notes += (f"\nThe need is {data['category']}, but for '{it['name']}' SAP holds only "
                      f"{' and '.join(narrow._PLURAL.get(k, k) for k in kinds)}, such as "
                      f"'{it['candidates'][0]['description']}'. Tell the officer plainly, and ask "
                      "whether they mean that, or to describe what they need another way (a "
                      "rating, a make, a code) to search again.")
        none = [it["name"] for it in data["items"] if it.get("searched") and not it.get("candidates")]
        if none:
            notes += ("\nSAP holds no record of: " + ", ".join(none) + ". Tell the officer so "
                      "plainly, and that they can describe it another way to search again.")
        if added:
            now = case.current_item(data)
            chosen = case.chosen(data)
            nxt = (f"added to the same case and cart: {', '.join(added)}. The cart holds "
                   f"{sum(1 for i in chosen if i.get('picked'))} of {len(chosen)} items now"
                   + (f"; work '{now['name']}' next, as before.\n"
                      + _view(case.remaining(now), now["name"], now.get("answers"))
                      if now is not None and now.get("candidates") else "."))
        elif any("selected" in it for it in data["items"]) and len(data["items"]) > 1:
            nxt = "the officer's choice stands; nam_choose changes it."
        elif len(offer) > 1:
            nxt = ("call nam_related to see what NRL bought together with these before; then "
                   "show this table and ask which materials go forward with nam_choose "
                   "(question=..., no items, suggest=[...] for only what belongs to this "
                   "need): the harness puts the options and counts.")
        elif data["items"][0].get("selected"):
            it = data["items"][0]
            nxt = (("call nam_related once: if something NRL bought with it belongs to this "
                    "need, ask with nam_choose(question=..., suggest=[...]); if nothing does, "
                    if not data.get("related") else "")
                   + f"work '{it['name']}'.\n"
                   + _view(case.remaining(it), it['name'], it.get('answers'))
                   if it.get("candidates") else "")
        else:
            nxt = ("SAP holds no record for these materials: ask the officer to describe them "
                   "another way (their usual names, a size, a material code).")
        return (f"{data['case_id']}: the need holds {len(data['items'])} material(s).\n"
                + "\n".join(rows) + notes + f"\nNext: {nxt}", data["case_id"])

    # -- 1d: which items go forward -------------------------------------------------

    def related(self):
        """What NRL bought together with the need's materials on their past PRs,
        for the agent to judge which belong to this need."""
        self._may("need")
        data = self._case()
        items = [it for it in data["items"] if it.get("candidates")]
        if not items:
            raise Stop("No material of the need has records in SAP yet: search them first.")
        if any(it.get("picked") for it in items):
            raise Stop("Items are already in the cart: suggestions come with the choice of "
                       "items. If the officer wants more, that is the cart review's 'Add more'.")
        found = narrow.co_bought(self.kg_url, items, self.call)
        # the same kind as the need: materials for goods, service lines for services
        kind = {"goods": "material", "services": "service line"}.get(data.get("category") or "")
        if kind:
            found["rows"] = [r for r in found["rows"] if r.get("unit") == kind]
        rows = found["rows"][:RELATED_ROWS]
        data["related"] = {"prs": found["prs"],
                           "codes": {r["key"]: r["description"] for r in found["rows"]},
                           "like": {r["key"]: r["like"] for r in found["rows"] if r.get("like")}}
        data = self._save(data, "related", "read what was bought with these on "
                          f"{len(found['prs'])} past PR(s)")
        own = ", ".join(it["name"] for it in data["items"])
        if not rows:
            return (f"The need's past PRs ({', '.join(narrow.doc_no(p) for p in found['prs']) or 'none'})"
                    " hold nothing else" + (": " + "; ".join(found["problems"]) if
                                            found["problems"] else "") + ". Suggest nothing; go "
                    "on with the choice of items.", data["case_id"])
        table = ["| Key | Material | On how many of the need's PRs | PRs | Named like |",
                 "|---|---|---|---|---|"]
        for r in rows:
            table.append(f"| {r['key']} | {guard.sanitise(r['description'], 70)} | "
                         f"{len(r['prs'])} of {len(found['prs'])} | "
                         + ", ".join(narrow.doc_no(p) for p in r["prs"][:3])
                         + f" | {r.get('like') or '-'} |")
        return (f"FOR YOU, NOT TO SHOW. What NRL bought together with this need's materials "
                f"({own}) on their latest past PRs ({len(found['prs'])}): "
                f"{len(found['rows'])} other material(s); the most often first.\n"
                + "\n".join(table)
                + ("\nNot read: " + "; ".join(found["problems"]) if found["problems"] else "")
                + "\nJudge which of these belong to THIS need: what is fitted or used with the "
                  "officer's materials on the same equipment or job, by what the records say. A "
                  "material only bought on the same PR does not belong; another size or variant "
                  "of a material already in the need is not a new item ('Named like' says when "
                  "SAP names a record the way it names one of the need's own: read what it is). Suggest only those that "
                  "belong (none is a good answer), each with a short name as SAP would write it, "
                  "why= in one plain line, and the keys= above it rests on, with "
                  "nam_choose(question=..., suggest=[...]). Never show this table.",
                data["case_id"])

    def _suggestions(self, data: dict, suggest) -> list[dict]:
        """The agent's suggestions, held to SAP: each rests on materials of the
        need's past PRs, is not one of the need's materials, and SAP holds it."""
        rel = data.get("related") or {}
        if not rel:
            raise Stop("Call nam_related first: a suggestion rests on what NRL bought with "
                       "these materials before.")
        have = {i["name"].lower() for i in data["items"]}
        out, problems = [], []
        for s in (suggest or [])[:MAX_SUGGEST]:
            if not isinstance(s, dict):
                raise Stop("Each suggestion is {name, why, keys}.")
            name = " ".join(str(s.get("name") or "").split())
            why = guard.sanitise(" ".join(str(s.get("why") or "").split()), 160)
            keys = [str(k).strip() for k in s.get("keys") or [] if str(k).strip()]
            if not name or not why or not keys:
                raise Stop("Each suggestion needs name=, why= and keys= (from nam_related).")
            bad = [k for k in keys if k not in rel["codes"]]
            if bad:
                raise Stop(f"{', '.join(bad)} is not on the need's past PRs (nam_related): "
                           "suggest only what NRL bought with these before.")
            if name.lower() in have:
                raise Stop(f"'{name}' is already one of the need's materials.")
            if name in rel["codes"] or any(name.lower() == str(k).lower() for k in rel["codes"]) \
                    or not re.search(r"[A-Za-z]{3,}", name):
                raise Stop(f"'{name}' is a material code: name= is what the material is, in "
                           "words, as SAP would write it (keys= carries the codes).")
            like = {(rel.get("like") or {}).get(k) for k in keys}
            if keys and all(like) and len(like) == 1:
                raise Stop(f"{', '.join(keys)} {'is' if len(keys) == 1 else 'are'} named like "
                           f"'{like.pop()}', already in the need: another size or variant of "
                           "it is not a new item. Suggest only a different material, or none.")
            it = {"name": name, "searched": False, "suggested": why, "evidence": keys}
            problems += self._lookup(it)
            if it.get("candidates"):
                out.append(it)
                have.add(name.lower())
        if problems:
            raise Stop("SAP could not be read for a suggestion: " + "; ".join(problems))
        return out

    def choose(self, question: str = "", items=None, suggest=None, _notify=None):
        self._may("need")
        data = self._case()
        offer = [it for it in data["items"] if it.get("candidates")]
        if not items:
            if not (question or "").strip():
                raise Stop("Give the question in your words (which materials go forward), or "
                           "items= with the officer's choice.")
            extra = self._suggestions(data, suggest) if suggest else []
            if len(offer) + len(extra) < 2:
                raise Stop("There is no choice to ask: "
                           + (f"only '{offer[0]['name']}' has records." if offer else
                              "no material has records.")
                           + (" No suggestion has records in SAP either." if suggest else ""))
            opts = [f"{it['name']} ({narrow.count(it['candidates'])})" for it in offer]
            opts += [f"{it['name']} ({narrow.count(it['candidates'])}), {SUGGESTED_TAG}"
                     for it in extra]
            opts.append(ALL)
            data["suggested"] = extra
            data["choosing"] = {"question": " ".join(question.split()), "options": opts,
                                "names": {narrow.norm(o): (it["name"]) for o, it in zip(
                                    opts, offer + extra)}}
            data = self._save(data, "ask", "asked which materials go forward")
            shown = {"question": data["choosing"]["question"], "options": opts, "field": "nam",
                 "multi": True}
            if _notify:
                _notify("ask_options", shown)
            chatstate.mark_asked(shown["question"])
            none = [it["name"] for it in data["items"] if it.get("searched")
                    and not it.get("candidates")]
            return (f"Asked: \"{shown['question']}\" Options: {' | '.join(opts)}. The officer "
                    "may choose several."
                    + (" Suggested, from what NRL bought with these before: "
                       + "; ".join(f"{it['name']} ({it['suggested']})" for it in extra)
                       + ". Say in one plain line each why it is suggested." if extra else "")
                    + (f" SAP holds no record of {', '.join(none)}: say so in one line, it is not "
                       "among the options." if none else "")
                    + " Stop here and wait.", data["case_id"])
        self._not_asked()
        if chatstate.clicked() == "nam_choose":
            raise Stop("The harness already read the officer's choice of items this turn: go on "
                       "with the first item.")
        # the page sends several choices as "a; b"
        said = [narrow.norm(part) for x in (items if isinstance(items, list) else [items])
                for part in str(x).split(";") if part.strip()]
        extra = data.get("suggested") or []
        names = ((data.get("choosing") or {}).get("names") or {})
        if any(x in (narrow.norm(ALL), "all") for x in said):
            picked = offer + extra
        else:
            picked, unknown = [], []
            for x in said:
                name = names.get(x) or x
                it = (data["items"][int(x) - 1] if x.isdigit() and 0 < int(x) <= len(
                    data["items"]) else case.item(data, name)
                    or next((s for s in extra if s["name"].lower() == name.lower()), None))
                if it is None:
                    unknown.append(x)
                elif it not in picked:
                    picked.append(it)
            if unknown:
                raise Stop(f"No material {unknown} in the need; they are: "
                           + ", ".join(it["name"] for it in data["items"]) + ".")
        if not picked:
            raise Stop("Choose at least one material.")
        locked = case.confirmed(data) - {it["name"] for it in picked}
        if locked:
            raise Stop(f"{', '.join(sorted(locked))} has a confirmed PR: it stays chosen.")
        # a suggestion the officer took is one of the need's materials now; one
        # they left leaves no trace in the case
        order = picked + [it for it in data["items"] if it not in picked]
        data["suggested"] = None
        for it in order:
            it["selected"] = it in picked
            if not it["selected"]:
                it["picked"], it["pending"], it["pr"] = None, None, None
        data["items"], data["choosing"], data["matched"] = order, None, None
        data = self._save(data, "choose", "chose " + ", ".join(it["name"] for it in picked))
        now = case.current_item(data)
        dropped = [it["name"] for it in data["items"] if not it["selected"]]
        out = (f"Taken forward, in this order: {', '.join(it['name'] for it in picked)}."
               + (f" Not taken: {', '.join(dropped)}." if dropped else ""))
        if now is not None:
            out += (f"\nNow '{now['name']}' (one item at a time): SAP has "
                    f"{narrow.count(case.remaining(now)) or 'nothing'} still in.\n"
                    + _view(case.remaining(now), now['name'], now.get('answers')))
        return out, data["case_id"]

    # -- searching again --------------------------------------------------------------

    def search(self, item: str = "", query: str = "", restart: bool = False):
        self._may("need")
        data = self._case()
        it = self._item(data, item, working=False)
        if it.get("pending"):
            raise Stop(f"'{it['name']}' has the open question \"{it['pending']['question']}\": "
                       "the officer's message is most likely its answer, so read it with "
                       "nam_answer first. If the officer wants another item instead, add it "
                       "with nam_need.")
        if not restart and it.get("answers"):
            raise Stop(f"'{it['name']}' is already narrowed by {len(it['answers'])} answer(s): "
                       "call nam_answer or nam_ask. To search again anyway (the answers still "
                       "apply to the new records), pass restart=true.")
        answers = list(it.get("answers") or [])
        problems = self._lookup(it, query)
        if not it.get("candidates") and problems:
            raise Stop("; ".join(problems) + ". SAP is unavailable: say so, and offer to try "
                       "again.")
        q, cands = it["query"], it["candidates"]
        # the officer's answers so far still hold: they narrow the new records
        keys, kept = [c["key"] for c in cands], []
        for a in answers:
            read = narrow.answer(None, [c for c in cands if c["key"] in set(keys)], a["answer"])
            if read is not None:
                keys = read[0]
                kept.append(a["answer"])
        it.update(keys=keys, answers=[a for a in answers if a["answer"] in kept])
        data = self._save(data, "search", f"searched '{q}' for '{it['name']}'")
        warn = (" (" + "; ".join(problems) + ")") if problems else ""
        if kept:
            warn += (f". The earlier answers {kept} still apply: {len(keys)} of {len(cands)} "
                     "records are in")
            cands = case.remaining(it)
        if not cands:
            return (f"NO RECORD: SAP holds no PR, PO or service line for '{q}'{warn}. "
                    "Ask the officer to describe it another way (its usual name, a size, a "
                    "material code), then search again.", data["case_id"])
        return (f"SAP has {narrow.count(it['candidates'])} for '{q}' "
                f"({narrow.found(it['candidates'], q)}){warn}.\n{_view(cands, it['name'], it.get('answers'))}",
                data["case_id"])

    # -- 1f ------------------------------------------------------------------------

    def ask(self, item: str = "", question: str = "", options=None, _notify=None):
        data = self._case()
        it = self._item(data, item)
        said = chatstate.message().strip()
        if it.get("pending") and not chatstate.asked() and said:
            # the officer answered the open question in this turn's message:
            # it narrows the records before anything new is asked
            raise Stop(f"'{it['name']}' has the open question \"{it['pending']['question']}\" "
                       f"and the officer has replied \"{guard.sanitise(said, 120)}\": read it "
                       f"first with nam_answer(item='{it['name']}', answer=<their words>). "
                       "If it answers nothing, ask ONE short question to clarify.")
        left = case.remaining(it)
        if len(left) < 2:
            raise Stop(f"{len(left)} record left for '{it['name']}': nothing to tell apart. "
                       "Show it and put it in the cart with nam_pick, or search again.")
        q = narrow.check(question, options or [], left)
        # an option that is another material SAP found by meaning is not a type
        # of the item: a question that calls it one says something untrue
        name = it.get("query") or it["name"]
        other = narrow.by_meaning(left, name)
        named = [o["label"] for o in options or [] if isinstance(o, dict)
                 and {w for x in (o.get("words") or []) for w in narrow.words(str(x))} & other]
        if named and set(narrow.words(name)) <= set(narrow.words(question)):
            raise Stop(f"{', '.join(named)} {'is' if len(named) == 1 else 'are'} another material "
                       f"SAP found by meaning, not a type of '{it['name']}': ask which kind of "
                       "material the officer needs (without calling them types of the item), or "
                       "leave those options out.")
        it["pending"] = q
        data = self._save(data, "ask", f"asked \"{q['question']}\" for '{it['name']}'")
        shown = {"question": q["question"], "options": narrow.labels(q), "field": "nam",
                 "multi": True, "progress": case.progress(data, it)}
        if _notify:
            _notify("ask_options", shown)
        chatstate.mark_asked(q["question"])
        notes = (" Note: " + "; ".join(q["notes"]) + ".") if q.get("notes") else ""
        return (f"Asked: \"{q['question']}\" Options: {' | '.join(shown['options'])}.{notes} "
                "The officer has not answered yet: stop here and wait.", data["case_id"])

    def answer(self, item: str = "", answer: str = "", field: str = "", value: str = "",
               default: bool = False, source: str = ""):
        self._not_asked()
        if chatstate.clicked() == "nam_answer" and not field and narrow.norm(answer) == narrow.norm(
                chatstate.message()):
            raise Stop("The harness already read the officer's choice this turn (see the case "
                       "above): go on from there, with the next question or the pick.")
        if narrow.norm(answer) in UNDO_WORDS:
            return self.undo()
        data = self._case()
        it = self._item(data, item)
        if field:
            if not str(value).strip():
                raise Stop("Give the detail's value.")
            if default and not source.strip():
                raise Stop("A default value needs its source (the NRL specification clause).")
            it.setdefault("details", {})[field.strip().lower()] = {
                "value": " ".join(str(value).split()), "default": bool(default),
                "source": source.strip()[:200]}
            # a value some of the records carry and some do not tells them apart
            # too: it narrows them, as an answer would
            left, note = case.remaining(it), ""
            plain = re.fullmatch(r"[\d.,]+|\d{4}-\d{2}-\d{2}", str(value).strip())
            read = (None if default or not left or plain  # a count or a date names no record
                    else narrow.answer(None, left, str(value)))
            if read is not None and 0 < len(read[0]) < len(left):
                it["keys"] = [k for k in it["keys"] if k in set(read[0])]
                it.setdefault("answers", []).append({"question": field, "answer": str(value)})
                note = f" It also narrows the records: {len(it['keys'])} still in.\n"
                note += _view(case.remaining(it), it['name'], it.get('answers'))
            data = self._save(data, "detail", f"recorded {field} = {value} for '{it['name']}'")
            return (f"Recorded {field} = {value}" + (" (NRL default)" if default else "")
                    + f" for '{it['name']}'." + note, data["case_id"])
        left = case.remaining(it)
        read = narrow.answer(it.get("pending"), left, answer)
        if read is None:
            raise Stop(f"'{answer}' fits no option and no remaining record carries its words. "
                       "Ask ONE short question to clarify; do not restart.")
        keys, how = read
        keep = [k for k in it["keys"] if k in set(keys)]
        question = (it.get("pending") or {}).get("question") or "(the officer's own words)"
        it.setdefault("answers", []).append({"question": question, "answer": answer})
        it["keys"], it["pending"], it["asked_other"] = keep, None, False
        chatstate.mark_done("answer")
        data = self._save(data, "answer", f"the answer '{answer}' to \"{question}\" for "
                                          f"'{it['name']}'")
        left = case.remaining(it)
        return (f"Read as {how}: {narrow.count(left) or 'nothing'} still in for "
                f"'{it['name']}'.\n"
                f"{_view(left, it['name'], it.get('answers'))}", data["case_id"])

    # -- 1h: the cart ----------------------------------------------------------------

    def pick(self, item: str = "", key: str = "", quantity=None, unit: str = "",
             remove: bool = False, pr: str = "", _notify=None):
        self._may("need")
        self._not_asked()
        data = self._case()
        if not key.strip() and not remove:
            it = self._item(data, item, working=False)
            if it.get("picked"):
                return self._quantity(data, it, quantity, unit)
        if remove:
            it = self._item(data, item, working=False)
            if not it.get("picked"):
                raise Stop(f"'{it['name']}' is not in the cart.")
            if it["name"] in case.confirmed(data):
                raise Stop(f"'{it['name']}' has a confirmed PR: it stays in the cart.")
            it["picked"], it["pr"], data["matched"] = None, None, None
            data = self._save(data, "unpick", f"took '{it['name']}' out of the cart")
            return (f"Took '{it['name']}' out of the cart; its records still in: "
                    f"{narrow.count(case.remaining(it)) or 'none'}.", data["case_id"])
        if chatstate.clicked() == "nam_pick" and chatstate.reading() != "nam_pick":
            # the harness read the officer's click and put it in the cart: a
            # second pick would ask them to confirm what they already chose
            raise Stop("The officer's click already put their choice in the cart this turn: "
                       "do not pick again. Go on from the tool's result (the next item's "
                       "question, or the cart question).")
        it = self._item(data, item)
        if (it.get("picked") or {}).get("key") == key.strip() and chatstate.reading() != "nam_pick":
            raise Stop(f"'{it['picked'].get('code') or key}' is already in the cart for "
                       f"'{it['name']}': do not ask the officer about it again.")
        if chatstate.done("answer") and chatstate.message() and chatstate.clicked() != "nam_pick":
            raise Stop("The officer has just answered: the matches are shown to them as cards. "
                       "Wait for them to continue with one (their click, or their words); "
                       "do not pick for them.")
        left = case.remaining(it)
        rec = next((c for c in left if c["key"] == key.strip()), None)
        if rec is None:
            found = any(c["key"] == key.strip() for c in it.get("candidates") or [])
            raise Stop(f"'{key}' is "
                       + ("not among the records still in after the officer's answers"
                          if found else "not one of the records found")
                       + f" for '{it['name']}'. Pick a Key from the records still in, or take "
                       "an answer back with nam_undo.")
        if len(left) > PICK_FROM and rec["key"] not in (it.get("others") or []):
            raise Stop(f"{len(left)} records are still in for '{it['name']}': ask what tells "
                       "them apart (nam_ask) until a few are left. If the officer named this "
                       "code themselves, read it with nam_answer(answer=<code>).")
        # an item is finished on one PR: the record's PR the officer chose
        prs = narrow.prs_of(rec)
        want = str(pr or "").strip().lstrip("0")
        chosen = next((p for p in prs if p["pr_no"].lstrip("0") == want), None) if want else (
            prs[0] if len(prs) == 1 else None)
        if want and chosen is None:
            raise Stop(f"'{rec['description']}' is not in PR {pr}; its PRs are: "
                       + ", ".join(narrow.doc_no(p["pr_no"]) for p in prs) + ".")
        if chosen is None and len(prs) > 1:
            from nam_agent.nam import cards

            group = cards.records(it["name"], [rec], it.get("answers"))
            group["title"] = f"The PRs of '{rec['description'][:60]}'"
            chatstate.emit("nam_cards", group)
            return (f"'{rec['description']}' is in {len(prs)} PRs (shown to the officer as "
                    "cards, the latest first): the item is finished on one. Ask which PR to "
                    "continue with; their choice is nam_pick(key, pr=...).", data["case_id"])
        # the officer's own words named it (not a card click): the record the
        # agent understood is shown, and the officer confirms before it is added
        if chatstate.message() and chatstate.reading() != "nam_pick":
            return self._confirm_pick(data, it, rec, chosen, _notify)
        lines = chosen["lines"] if chosen else rec["lines"]
        line = lines[0] if lines else {}
        units = [ln.get("unit") for ln in lines if ln.get("unit")]
        it["picked"] = {"key": rec["key"], "code": rec.get("code") or "",
                        "description": rec["description"], "source": rec["source"],
                        "pr_no": chosen["pr_no"] if chosen else "",
                        "pur": (chosen or {}).get("pur") or line.get("pur_case_id")
                        or (rec.get("tender") or {}).get("pur"),
                        "quantity": float(quantity) if quantity not in (None, "") else None,
                        "unit": unit or (units[0] if units else ""), "lines": lines,
                        "past_quantities": _quantities(rec, chosen)}
        it["pending"], it["pending_pick"], data["matched"] = None, None, None
        # the item's PR is drafted from what was found and answered; it is
        # shown when the officer comes to the PRs
        self._may("pr.fill")
        it["pr"] = pr_mod.fill(data, it)
        data = self._save(data, "pick", f"put {rec.get('code') or rec['key']} in the cart for "
                                        f"'{it['name']}'"
                          + (f" from PR {narrow.doc_no(chosen['pr_no'])}" if chosen else ""))
        it = case.item(data, it["name"])
        # the questions were to find the PR that matches: once it is chosen,
        # nothing more is asked; its quantity is the PR's own, unless the
        # officer gave one
        if not it["picked"]["quantity"]:
            q = next((x for x in it["picked"].get("past_quantities") or []), None)
            if q:
                it["picked"]["quantity"], it["picked"]["quantity_from"] = q, "the chosen PR"
                it["pr"] = pr_mod.fill(data, it)
                data = self._save(data, "pick", f"quantity {_num(q)} {it['picked']['unit']} for "
                                                f"'{it['name']}', as in its PR")
                it = case.item(data, it["name"])
        return self._after_pick(data, it)

    def _confirm_pick(self, data: dict, it: dict, rec: dict, chosen: dict | None, _notify=None):
        from nam_agent.nam import cards

        it["pending_pick"] = {"key": rec["key"], "pr": (chosen or {}).get("pr_no", "")}
        q = {"question": f"Add this {it['name']} to the cart?", "options": [YES_LABEL, NO_LABEL]}
        data = self._save(data, "ask", f"asked the officer to confirm {rec.get('code') or rec['key']}"
                                       f" for '{it['name']}'")
        group = cards.records(it["name"], [rec], it.get("answers"))
        if chosen:
            group["cards"] = [c for c in group["cards"]
                              if c["title"] == f"PR {narrow.doc_no(chosen['pr_no'])}"] or group["cards"]
        for c in group["cards"]:
            c.pop("action", None)              # the confirmation is the question below
        group["title"] = f"Is this the {it['name']} you mean?"
        chatstate.emit("nam_cards", group)
        shown = {**q, "field": "nam", "progress": case.progress(data, case.item(data, it["name"]))}
        if _notify:
            _notify("ask_options", shown)
        chatstate.mark_asked(q["question"])
        return (f"The officer's words were read as {rec.get('code') or rec['key']} "
                f"{rec['description']}"
                + (f" from PR {narrow.doc_no(chosen['pr_no'])}" if chosen else "")
                + f": it is shown to them as a card, and they are asked \"{q['question']}\" "
                "(Yes, add it / No, show the others). It is not in the cart until they say yes. "
                "Stop here and wait.", data["case_id"])

    def _quantity(self, data: dict, it: dict, quantity, unit: str):
        """The officer's quantity for an item in the cart."""
        if quantity in (None, ""):
            raise Stop("Give the quantity (a number) and its unit.")
        try:
            n = float(str(quantity).replace(",", ""))
        except ValueError as exc:
            raise Stop(f"'{quantity}' is not a quantity: give a number.") from exc
        if n <= 0:
            raise Stop("The quantity is more than nothing.")
        it["picked"]["quantity"], it["picked"]["quantity_from"] = n, ""
        if unit.strip():
            it["picked"]["unit"] = unit.strip()
        it["pr"] = pr_mod.fill(data, it)
        data = self._save(data, "pick", f"quantity {_num(n)} {it['picked']['unit']} for "
                                        f"'{it['name']}'")
        return self._after_pick(data, case.item(data, it["name"]))

    def _after_pick(self, data: dict, it: dict):
        p = it["picked"]
        cart = [i["name"] for i in case.chosen(data) if i.get("picked")]
        from nam_agent.nam import cards

        chatstate.emit("nam_cards", cards.added(it["name"], p, len(cart), len(case.chosen(data))))
        out = (f"In the cart for '{it['name']}': {p.get('code') or p['key']} {p['description']}"
               + (f" from PR {narrow.doc_no(p['pr_no'])}" if p.get("pr_no") else "")
               + f", quantity {_num(p['quantity']) if p.get('quantity') else '-'} {p['unit']}"
               + (" (as in that PR)" if p.get("quantity_from") else "")
               + f". Cart: {len(cart)} of "
               f"{len(case.chosen(data))} chosen item(s).")
        now = case.current_item(data)
        if now is not None:
            out += (f"\nNext item: '{now['name']}': SAP has "
                    f"{narrow.count(case.remaining(now)) or 'nothing'} still in.\n"
                    + _view(case.remaining(now), now['name'], now.get('answers')))
        elif case.review(data) == "asked":
            out += (f"\nEvery chosen item is in the cart; \"{REVIEW_QUESTION}\" is already "
                    "asked: do not ask it again. Wait for the officer's answer (nam_cart).")
        elif case.review(data) == "due":
            data = self._ask_review(data)
            from nam_agent.nam import cards as _cards

            for i in case.chosen(data):
                q = i["picked"]
                out += (f"\n- {i['name']}: {q['description']}, "
                        f"{_num(q['quantity']) if q.get('quantity') else '-'} {q['unit']}"
                        + (f", past PR {narrow.doc_no(q['pr_no'])}" if q.get("pr_no") else "")
                        + (f", its PO {_cards._orders(q)}" if _cards._orders(q) else "")
                        + (f", PUR {q['pur']}" if q.get("pur") else ""))
            out += ("\nEvery chosen item is in the cart, and the harness showed the whole cart "
                    f"and asked \"{REVIEW_QUESTION}\" with buttons. Write the officer a short "
                    "summary in plain words of what the cart holds: each item, what it is, how "
                    "many, and the past PR and PO it follows (by number, as the card shows "
                    "them); no material codes and no table. Do not ask the question again. Then stop and wait: their "
                    "answer goes to nam_cart.")
        else:
            out += ("\nEvery chosen item is in the cart. Next: the PURs that hold the need's "
                    "materials (nam_tenders with no item), then each item's PR (nam_pr "
                    "item=..., action='fill'). An estimate for the cart only if the officer asks.")
        return out, data["case_id"]

    def _ask_review(self, data: dict) -> dict:
        """The whole cart as cards, and the question whether that is everything."""
        from nam_agent.nam import cards

        data["review"] = {"asked": True}
        data = self._save(data, "ask", "asked whether the cart is complete")
        chatstate.emit("nam_cards", cards.cart(data))
        q = review_question()
        chatstate.emit("ask_options", {**q, "field": "nam"})
        chatstate.mark_asked(q["question"])
        return data

    def _officer_words(self) -> str:
        """Everything the officer said in this chat, this message too."""
        said = [chatstate.message()]
        try:
            said += [r[0] for r in self.conn.execute(
                "SELECT content FROM chat_log WHERE session_id = ? AND role = 'user'",
                (self._session(),)).fetchall()]
        except Exception:  # noqa: BLE001 - no chat log (a direct call): this message alone
            pass
        return "\n".join(s for s in said if s)

    def _unsaid(self, given: dict, prd: dict) -> list[str]:
        """The PR fields given that the officer never said and the harness did not
        carry over: a turn from the officer's chat only (a direct call is the
        caller's own word)."""
        if not given or not chatstate.message():
            return []
        heard = _plain(self._officer_words()).lower()
        heard_words = set(narrow.norm(heard).split())
        header = prd.get("header") or {}
        out = []
        for k, v in given.items():
            v = str(v).strip()
            if not v or str(header.get(k) or "").strip() == v:
                continue                                   # carried over, or unchanged
            if v.lower() in heard or narrow.norm(v) in narrow.norm(heard):
                continue
            if k == "delivery_date" and _date_said(v, heard):
                continue                                   # "1st December" for 2026-12-01
            ws = [w for w in narrow.norm(v).split() if len(w) > 2]
            if k == "justification" and ws and sum(w in heard_words for w in ws) >= 0.6 * len(ws):
                continue                                   # their reason, in other words
            out.append(k)
        return out

    def _review_gate(self, data: dict, what: str) -> None:
        """PURs and PRs wait until the officer said the cart is complete."""
        state = case.review(data)
        if state in ("due", "asked"):
            raise Stop(f"Not yet {what}: the officer has not said whether the cart is complete. "
                       f"Summarise the cart in plain words; the harness asks \"{REVIEW_QUESTION}"
                       "\" and their answer goes to nam_cart.")
        if state == "adding":
            raise Stop(f"Not yet {what}: the officer is adding items to this cart. Ask which "
                       "materials to add, then nam_need(items=[...], add=true); the new items "
                       "go into the cart first.")

    # -- 1h: is the cart complete ---------------------------------------------------

    def cart_review(self, answer: str = ""):
        """The officer's answer to the cart question: done (that is everything)
        or more (they want to add items to this cart)."""
        self._may("need")
        data = self._case()
        answer = (answer or "").strip().lower()
        if answer not in ("done", "more"):
            raise Stop("answer is 'done' (that is everything) or 'more' (add items).")
        if case.review(data) == "adding" and answer == "more":
            raise Stop("The officer is already adding items: ask which materials, then "
                       "nam_need(items=[...], add=true).")
        if not case.cart_full(data):
            now = case.current_item(data)
            raise Stop("Not every chosen item is in the cart yet"
                       + (f": '{now['name']}' is still being worked." if now else "."))
        if answer == "more":
            data["review"] = {"adding": True}
            self._save(data, "review", "the officer wants to add more items to the cart")
            return ("The officer wants to add more to this cart. Ask them, in plain words and "
                    "without buttons, which materials to add. Their answer goes to "
                    "nam_need(items=[...], add=true): the items already in the cart stay, and "
                    "the new ones are searched and narrowed one at a time.", data["case_id"])
        data["review"] = {"done": True}
        data = self._save(data, "review", "the officer said the cart is complete")
        chosen = case.chosen(data)
        nxt = ("the PURs that hold the need's materials (nam_tenders with no item), then each "
               "item's PR (nam_pr item=..., action='fill')" if len(data["items"]) > 1 else
               "the item's PR (nam_pr action='fill')")
        return (f"The cart is complete: {len(chosen)} item(s), "
                + ", ".join(i["name"] for i in chosen) + f". Next: {nxt}. An estimate for the "
                "cart only if the officer asks.", data["case_id"])

    # -- 1j: the matching PURs ----------------------------------------------------------

    def tenders(self, item: str = ""):
        data = self._case()
        if item or len(data["items"]) == 1:
            return self._item_tenders(data, item)
        self._review_gate(data, "the PURs")
        pool = []
        for it in data["items"]:
            if not it.get("candidates"):
                continue
            recs = ([c for c in it["candidates"] if c["key"] == it["picked"]["key"]]
                    if it.get("picked") else
                    case.remaining(it) if it.get("selected") else it["candidates"])
            pool.append({"name": it["name"], "picked": it.get("picked"), "records": recs})
        m = narrow.coverage(pool)
        if not m["purs"]:
            raise Stop("These records name no PUR case.")
        data["matched"] = [{"pur": t["pur"], "match": t["match"], "covers": t["covers"]}
                           for t in m["purs"][:15]]
        data = self._save(data, "match", "ranked the matching PURs")
        from nam_agent.nam import cards

        chatstate.emit("nam_cards", cards.purs("PUR cases holding the need's materials",
                                               m["purs"][:12], coverage=True))
        rows = ["| Match | PUR | Holds | Materials (codes) | Status | Latest | PR | PO |",
                "|---:|---|---|---|---|---|---|---|"]
        for t in m["purs"][:15]:
            mats = "; ".join(f"{n}: {', '.join(c[:2])}" for n, c in t["codes"].items())
            rows.append(f"| {t['match']}% | {t['pur']} | {t['covers']} | "
                        f"{guard.sanitise(mats, 160).replace('|', '/')} | {t['label']} | "
                        f"{t['latest'] or '-'} | {', '.join(narrow.doc_no(x) for x in t['pr'][:3]) or '-'} | "
                        f"{', '.join(narrow.doc_no(x) for x in t['po'][:3]) or '-'} |")
        cart = m["cart"]
        head = (f"The need names {m['total']} material(s); in the cart: "
                f"{', '.join(cart) or 'none yet'}. ")
        head += (f"A PUR holds all {m['total']}: 100%." if m["all"] else
                 f"No PUR holds all {m['total']}: the most any holds is {m['most']}, so a PUR "
                 f"holding {m['most']} is the 100% match.")
        gaps = [t["pur"] for t in m["purs"][:15] if not t["po"] or not t["pr"]]
        tail = ("" if not gaps or not self.kg else "\nSAP shows no PR or PO for " + ", ".join(gaps[:6])
                + ": its tender record in the knowledge graph (kg_tender_record) may.")
        return (head + " The officer sees these as cards: say in a line or two what they show, "
                "do not repeat the table.\n" + "\n".join(rows) + tail, data["case_id"])

    def _item_tenders(self, data: dict, item: str):
        it = self._item(data, item, working=False)
        ranked = narrow.tenders([c for c in it["candidates"] if c["key"] == it["picked"]["key"]]
                                if it.get("picked") else case.remaining(it))
        if not ranked:
            raise Stop("These records name no PUR case.")
        from nam_agent.nam import cards

        chatstate.emit("nam_cards", cards.purs(f"Past tenders for '{it['name']}'", ranked[:12]))
        rows = ["| PUR | Status | Latest | PR | PO | Materials |",
                "|---|---|---|---|---|---|"]
        rows += [f"| {t['pur']} | {t['label']} | {t['latest'] or '-'} | "
                 f"{', '.join(narrow.doc_no(x) for x in t['pr'][:3]) or '-'} | {', '.join(narrow.doc_no(x) for x in t['po'][:3]) or '-'} | "
                 f"{', '.join(t['records'][:4])} |" for t in ranked[:15]]
        gaps = [t["pur"] for t in ranked[:15] if not t["po"] or not t["pr"]]
        tail = ("" if not gaps or not self.kg else "\nSAP shows no PR or PO for " + ", ".join(gaps)
                + ": its tender record in the knowledge graph (kg_tender_record) may.")
        return (f"Past tenders for '{it['name']}' from SAP, the latest year first, then PR + PO, "
                "PR ongoing, PR ongoing with its PO cancelled (shown to the officer as cards; do "
                "not repeat the table):\n"
                + "\n".join(rows) + tail, data["case_id"])

    # -- undo ---------------------------------------------------------------------------

    def undo(self, steps: int = 1):
        self._may("need")
        self._not_asked()
        if chatstate.clicked() == "nam_undo":
            raise Stop("The officer's undo was already done this turn (see the case above).")
        data = self._case()
        data, taken = case.undo(self.conn, data, self._session(), steps)
        now = case.current_item(data)
        out = f"Took back: {'; '.join(taken)}.\n{case.summary(data)}"
        if data.get("choosing"):
            out += "\nThe question of which materials go forward is open again."
        elif now is not None and now.get("pending"):
            out += f"\nThe question \"{now['pending']['question']}\" is open again."
        elif now is not None and now.get("searched"):
            out += "\n" + _view(case.remaining(now), now['name'], now.get('answers'))
        return out, data["case_id"]

    # -- the matches, once the item is described ------------------------------------

    def matches(self, item: str = ""):
        """The records still in for the item, shown as cards, the best first:
        for when the agent judges the officer's description complete."""
        if chatstate.asked():
            raise Stop(f"You asked \"{chatstate.asked()}\" this turn: the matches come once the "
                       "officer has answered.")
        data = self._case()
        it = self._item(data, item)
        left = case.remaining(it)
        if not left:
            raise Stop(f"No record is left for '{it['name']}': take the last answer back "
                       "(nam_undo) or search again.")
        if it.get("pending_pick"):
            it["pending_pick"] = None          # the officer said no: the matches again
            data = self._save(data, "answer", f"the officer did not take the record shown for "
                                              f"'{it['name']}'")
        _show(it["name"], left, it.get("answers"))
        more = (f" (the best {SHOW_MATCHES} of {len(left)})" if len(left) > SHOW_MATCHES else "")
        return (f"The matches for '{it['name']}' are shown to the officer as cards{more}:\n"
                f"{records_table(left[:SHOW_MATCHES])}\nLet them choose one by what it says, "
                "then put it in the cart with nam_pick.", data["case_id"])

    # -- "show me something else" ---------------------------------------------------

    def other(self, item: str = ""):
        """The officer wants something other than the records shown: the
        first time, the closest other record SAP holds for the item (the one
        that carries the most of their answers, the latest first); the second
        time, the last answer is taken back, so the specification is asked
        again with a new follow-up."""
        self._may("need")
        self._not_asked()
        if chatstate.clicked() == "nam_other":
            raise Stop("The officer's request was already read this turn (see the case above).")
        data = self._case()
        it = self._item(data, item)
        if it.get("picked") and it["name"] in case.confirmed(data):
            raise Stop(f"'{it['name']}' has a confirmed PR: it stays.")
        it["picked"], it["pr"], it["pending"] = None, None, None
        from nam_agent.nam import cards

        left = {c["key"] for c in case.remaining(it)}
        seen = set(it.get("others") or [])
        pool = [c for c in it.get("candidates") or [] if c["key"] not in left | seen]
        if not it.get("asked_other") and pool:
            answers = it.get("answers") or []

            # the closest: a record of the item itself (its short text names
            # it), then the most of the officer's answers, then the latest
            found = {"short text": 0, "long text": 1, "meaning": 2}

            def rank(c):
                got, _ = cards._held(c, answers)
                return (found[narrow.found_by(c, it.get("query") or it["name"])], -got,
                        str((c.get("lines") or [{}])[0].get("date") or ""))
            pool.sort(key=lambda c: rank(c)[2], reverse=True)
            pool.sort(key=lambda c: rank(c)[:2])
            best = pool[0]
            it["keys"] = list(it["keys"]) + [best["key"]]
            it.setdefault("others", []).append(best["key"])
            it["asked_other"] = True
            data = self._save(data, "other", f"asked for something else for '{it['name']}'")
            group = cards.records(it["name"], [best], answers)
            group["title"] = f"The closest other match for '{it['name']}'"
            chatstate.emit("nam_cards", group)
            got, of = cards._held(best, answers)
            return (f"The officer wants something other than what was shown. The closest other "
                    f"record SAP holds for '{it['name']}' (shown as a card; it carries {got} of "
                    f"{of} of their answers):\n{records_table([best])}\nSay in one line how it "
                    "differs from what they asked, and let them continue with it or ask again.",
                    data["case_id"])
        # the second time (or nothing else is left): open the specification again
        answers = list(it.get("answers") or [])
        dropped = answers.pop() if answers else None
        keys = [c["key"] for c in it.get("candidates") or []]
        for a in answers:
            read = narrow.answer(None, [c for c in it["candidates"] if c["key"] in set(keys)],
                                 a["answer"])
            if read is not None:
                keys = [k for k in keys if k in set(read[0])]
        it.update(keys=keys, answers=answers, asked_other=False, others=[])
        data = self._save(data, "other", f"opened the specification of '{it['name']}' again")
        left = case.remaining(it)
        took = (f"took back the answer '{dropped['answer']}' to \"{dropped['question']}\""
                if dropped else "no answer to take back")
        return (f"The officer still wants something else: the specification of '{it['name']}' "
                f"is open again ({took}); {narrow.count(left) or 'nothing'} in. Ask ONE new "
                "follow-up about the detail that matters to them (it may be a different detail "
                f"from before), with nam_ask.\n{_view(left, it['name'], answers)}",
                data["case_id"])

    # -- stage 2 (optional) -------------------------------------------------------------

    def estimate_step(self, action: str = "show", line: int = 0, quantity=None, rate=None,
                      decision: str = "", note: str = "", evaluation_date: str = ""):
        data = self._case()
        est = data.get("estimate") or {}
        if case.confirmed(data) and action != "show":
            raise Stop("A PR is confirmed: the estimate can no longer change.")
        if action == "build":
            self._may("estimate.build")
            open_items = [i["name"] for i in case.chosen(data) if not i.get("picked")]
            if open_items:
                raise Stop("Put a material in the cart for every chosen item first: "
                           + ", ".join(open_items))
            until = date.fromisoformat(evaluation_date) if evaluation_date else date.today()
            est = estimate.build([{"name": i["name"], "picked": i["picked"]}
                                  for i in case.chosen(data)], until)
        elif action in ("set", "decide", "lock"):
            if not est.get("lines"):
                raise Stop("No estimate yet: nam_estimate(action='build').")
            if est.get("locked") and action != "lock":
                raise Stop("The estimate is locked; build it again to change it.")
            if action == "set":
                estimate.set_line(est, line, quantity, rate)
            elif action == "decide":
                if decision == "revise" and rate is not None:
                    estimate.set_line(est, line, None, rate)
                else:
                    estimate.decide(est, line, decision, note)
            else:
                self._may("estimate.lock")
                left = estimate.blockers(est)
                if left:
                    raise Stop("The estimate cannot be locked yet: " + "; ".join(left) + ".")
                estimate.lock(est)
                self._sheet(data["case_id"], "estimate.xlsx", "Estimate", [
                    ["S.No", "Code", "Description", "Unit", "Quantity", "Unit price", "Total",
                     "Basis", "Source"]] + [[ln["line"], ln["code"], ln["description"],
                                            ln["unit"], ln["quantity"], ln["rate"],
                                            ln["amount"], ln["basis"], ln["source"]]
                                           for ln in est["lines"]])
        elif action != "show":
            raise Stop("action is build, set, decide, lock or show")
        if not est.get("lines"):
            raise Stop("No estimate yet: nam_estimate(action='build').")
        data["estimate"] = est
        data = self._save(data, "estimate" if action != "show" else "",
                          f"estimate: {action}" + (f" line {line}" if line else ""))
        left = estimate.blockers(est)
        tail = ("LOCKED: each item's PR takes its price from it." if est.get("locked") else
                ("Before the lock: " + "; ".join(left) + "." if left else
                 "Every line is settled: the officer may lock it (action='lock')."))
        return f"Estimate for {data['case_id']}:\n{estimate.table(est)}\n{tail}", data["case_id"]

    # -- stage 3: one PR per cart item -------------------------------------------------

    def pr_step(self, item: str = "", action: str = "show", delivery_date: str = "",
                plant: str = "", storage_location: str = "", justification: str = "",
                rate=None, accept_price: bool = False, items=None, common: bool = False):
        data = self._case()
        if action != "show":
            self._review_gate(data, "the PR")
        if action in ("combine", "split") or common or (data.get("common_pr")
                                                        and not item.strip()):
            return self._common(data, action, items, delivery_date, plant, storage_location,
                                justification)
        if not item.strip():
            ready = [i for i in case.chosen(data) if i.get("picked")
                     and (i.get("pr") or {}).get("status") not in pr.DONE]
            cart = [i for i in case.chosen(data) if i.get("picked")]
            if not ready and len(cart) == 1:
                ready = cart
            if not ready:
                raise Stop("Name the item whose PR this is; the cart holds: "
                           + (", ".join(i["name"] for i in case.chosen(data)
                                        if i.get("picked")) or "nothing yet") + ".")
            it = ready[0]
        else:
            it = self._item(data, item)
        if not it.get("picked"):
            raise Stop(f"'{it['name']}' is not in the cart yet: its material comes first "
                       "(nam_pick).")
        done = (it.get("pr") or {}).get("status") in pr.DONE
        if action in ("fill", "set", "confirm") or not it.get("pr"):
            self._may("pr.fill")
            if not done:
                it["pr"] = pr.fill(data, it)
        given = {k: v for k, v in (("delivery_date", delivery_date), ("plant", plant),
                                   ("storage_location", storage_location),
                                   ("justification", justification)) if v}
        if (given or rate is not None or accept_price) and done:
            raise Stop(f"The PR for '{it['name']}' is confirmed: it can no longer change.")
        unsaid = self._unsaid(given, it.get("pr") or {})
        if unsaid:
            raise Stop("The officer has not given " + ", ".join(pr.OFFICER[k] for k in unsaid)
                       + ": a PR field is the officer's, never filled for them. Ask for "
                       + ("it" if len(unsaid) == 1 else "them") + " in plain words, without "
                       "options, and set only what they say.")
        if given:
            pr.set_fields(it["pr"], **given)
        if rate is not None or accept_price:
            pr.set_price(it["pr"], rate, accept_price)
        cart = [i["name"] for i in case.chosen(data) if i.get("picked")]
        if action == "confirm" and not done and len(cart) >= 2 and not data.get("common_asked"):
            # several items: the officer says first whether they want one PR
            left = [i["name"] for i in data["items"] if i.get("selected") is False]
            raise Stop("Several items are in the cart (" + ", ".join(cart) + "): before any PR "
                       "is confirmed, ask the officer whether they want one common PR for them"
                       + (f", and if so whether to add the items not taken forward "
                          f"({', '.join(left)}) or finish with these" if left else "")
                       + ". Their answer: nam_pr(action='combine', items=[...]) for one PR, or "
                       "nam_pr(action='split') to keep each item's own.")
        if action == "confirm" and not done:
            self._may("pr.confirm")
            it["pr"]["missing"] = pr.missing(it["pr"])
            if it["pr"]["missing"]:
                raise Stop(f"The PR for '{it['name']}' cannot be confirmed yet; missing: "
                           + "; ".join(it["pr"]["missing"]) + ".")
            it["pr"]["status"] = "ready_for_sap"
        elif action not in ("show", "fill", "set", "confirm"):
            raise Stop("action is fill, set, confirm or show")
        data = self._save(data, "pr" if action != "show" else "",
                          f"PR for '{it['name']}': {action}")
        if action == "confirm":
            self._sheet(data["case_id"], "pr.xlsx", "PR", pr.sheet_rows(data))
        it = case.item(data, it["name"])
        from nam_agent.nam import cards

        chatstate.emit("nam_cards", cards.pr(it["name"], it["pr"]))
        left = [i["name"] for i in case.chosen(data) if (i.get("pr") or {}).get("status")
                not in pr.DONE]
        if it["pr"]["status"] in pr.DONE:
            tail = ("CONFIRMED. SAP's PR interface is not connected yet: the PR is ready for "
                    "SAP and written to the bulk-upload sheet; it has no SAP PR number until "
                    "SAP returns one. Never give a PR number. "
                    + (f"PRs still to do: {', '.join(left)}." if left else
                       "Every chosen item has its PR: the need is done."))
        else:
            tail = ("Show this PR to the officer; ask in plain words for what is missing, and "
                    "on their confirmation, action='confirm'.")
        return f"{pr.table(it['pr'])}\n{tail}", data["case_id"]

    def _common(self, data: dict, action: str, items, delivery_date: str, plant: str,
                storage_location: str, justification: str):
        """One PR for several cart items (the officer asked for it): combine,
        set its fields, confirm, show, or split it back into the items' own."""
        self._may("pr.fill")
        cp = data.get("common_pr")
        done = (cp or {}).get("status") in pr.DONE
        if action == "combine":
            if done:
                raise Stop("The common PR is confirmed: it can no longer change.")
            names = [str(x) for x in items or []] or [
                i["name"] for i in case.chosen(data) if i.get("picked")]
            cp = pr.combine(data, names)
            data["common_asked"] = True
        elif action == "split":
            if done:
                raise Stop("The common PR is confirmed: it stays.")
            data["common_asked"] = True       # the officer wants each item's own PR
            for it in data["items"]:
                if (it.get("pr") or {}).get("status") == "merged":
                    it["pr"]["status"] = "draft"
            data["common_pr"] = None
            data = self._save(data, "pr", "each item keeps its own PR")
            return ("Each item keeps its own PR: confirm them one by one (nam_pr item=...).",
                    data["case_id"])
        elif not cp:
            raise Stop("There is no common PR yet: nam_pr(action='combine', items=[...]) makes "
                       "one when the officer asks for it.")
        given = {k: v for k, v in (("delivery_date", delivery_date), ("plant", plant),
                                   ("storage_location", storage_location),
                                   ("justification", justification)) if v}
        if given:
            if done:
                raise Stop("The common PR is confirmed: it can no longer change.")
            pr.set_fields(cp, **given)
        if action == "confirm" and not done:
            self._may("pr.confirm")
            cp["missing"] = pr.missing(cp)
            if cp["missing"]:
                raise Stop("The common PR cannot be confirmed yet; missing: "
                           + "; ".join(cp["missing"]) + ".")
            cp["status"] = "ready_for_sap"
        elif action not in ("show", "set", "combine", "confirm", "fill"):
            raise Stop("action is combine, set, confirm, show or split")
        for it in data["items"]:
            if it["name"] in cp["items"] and it.get("pr"):
                it["pr"]["status"] = ("ready_for_sap" if cp["status"] in pr.DONE else "merged")
        data["common_pr"] = cp
        data = self._save(data, "pr" if action != "show" else "", f"common PR: {action}")
        if action == "confirm":
            self._sheet(data["case_id"], "pr.xlsx", "PR", pr.sheet_rows(data))
        from nam_agent.nam import cards

        chatstate.emit("nam_cards", cards.pr("Common PR", data["common_pr"]))
        cp = data["common_pr"]
        tail = ("CONFIRMED. SAP's PR interface is not connected yet: the common PR is ready for "
                "SAP and written to the bulk-upload sheet; it has no SAP PR number until SAP "
                "returns one. Never give a PR number." if cp["status"] in pr.DONE else
                "It is shown to the officer as a card: ask in plain words for what is missing, "
                "and on their confirmation, nam_pr(common=true, action='confirm').")
        return (f"One common PR for {', '.join(cp['items'])}:\n{pr.table(cp)}\n{tail}",
                data["case_id"])

    def show(self, case_id: str = ""):
        self._may("case.read")
        if case_id.strip():
            data = case.load(self.conn, guard.plain_ids(case_id).strip().upper())
            if data is None:
                raise Stop(f"No case {case_id}.")
            data = self._save(data, "case", "resumed in this chat")
        else:
            data = self._case()
        from nam_agent.nam import flow

        from nam_agent.nam import cart

        now, nxt = flow.stage_of(data)
        return (f"{cart.node(data)}\n{case.summary(data)}\nStep now {now}; next {nxt}.",
                data["case_id"])

    def _sheet(self, case_id: str, name: str, title: str, rows: list[list]) -> Path:
        from nam_agent.nam.xlsx import xlsx

        folder = self.home / "nam" / case_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(xlsx([(title, rows)]))
        return path


def _tool(name, description, props, required, fn, notify=False) -> Tool:
    return Tool(name=name, description=description,
                input_schema={"type": "object", "properties": props, "required": required},
                fn=fn, wants_notify=notify)


ITEM = {"type": "string", "description": "The item's name as nam_need set it ('' for the item "
                                         "being worked now)"}


def make_tools(conn, home: Path, kg_url: str, call=None, kg: bool = True) -> list[Tool]:
    s = Steps(conn, home, kg_url, call, kg)
    return [
        _tool("nam_need", "PR Creation Flow 1a-1c: open the chat's NAM case for the officer's "
              "need or job description (need= in their words, even one word like 'gasket'), "
              "with the materials it names (items: one short name per material, as SAP would "
              "write it). Give category= as your judgement of what the need is (goods: things "
              "to be supplied; services: work done for NRL; works: construction or "
              "installation) and why= the reason in one plain line (what is being supplied or "
              "done, not the need repeated): unless the "
              "officer said it themselves, the harness asks them to confirm or change it, and "
              "their answer comes back as nam_need(category=...). Then each material is "
              "searched in SAP and counted, by short text and long text. add=true with items= "
              "puts more materials into this same case and cart (the officer wants to add "
              "more); new=true starts another case.",
              {"need": {"type": "string", "description": "The need in the officer's words"},
               "category": {"type": "string", "enum": list(CATEGORIES)},
               "why": {"type": "string", "description": "Why the need is that category, in "
                                                        "one plain line: what is supplied or "
                                                        "done (not the need repeated)"},
               "items": {"type": "array", "items": {"type": "string"},
                         "description": "The materials the need names, one short name each"},
               "add": {"type": "boolean", "description": "Add items= to this case's cart"},
               "new": {"type": "boolean"}}, [],
              lambda **a: s._run("nam_need", s.need, **a), notify=True),
        _tool("nam_related", "PR Creation Flow 1d: before the choice of items, what NRL "
              "bought together with the need's materials on their latest past PRs, from SAP, "
              "with how often. For you to judge which belong to this need; suggest only those, "
              "with nam_choose(suggest=...).", {}, [],
              lambda **a: s._run("nam_related", s.related, **a)),
        _tool("nam_choose", "PR Creation Flow 1d: which materials go forward. With question= "
              "and no items: asks the officer, the harness putting each material with its SAP "
              "count as an option; suggest= adds the materials you judged belong to this need "
              "from nam_related (each {name, why, keys}), as options marked as often bought "
              "with these (a single-material need may be asked this way too). With items= (the "
              "officer's choice: names, numbers, or 'All of them'): records it; the items are "
              "then worked one at a time, in that order.",
              {"question": {"type": "string"},
               "items": {"type": "array", "items": {"type": "string"}},
               "suggest": {"type": "array", "items": {
                   "type": "object",
                   "properties": {"name": {"type": "string"}, "why": {"type": "string"},
                                  "keys": {"type": "array", "items": {"type": "string"}}},
                   "required": ["name", "why", "keys"]}}}, [],
              lambda **a: s._run("nam_choose", s.choose, **a), notify=True),
        _tool("nam_search", "Search SAP again for one item, with other words (its usual name, "
              "a size, a code). Replaces the item's records: once the item is narrowed it "
              "needs restart=true.",
              {"item": ITEM, "query": {"type": "string", "description":
                                       "Search words when they differ from the item's name"},
               "restart": {"type": "boolean", "description":
                           "Search again although the item is already narrowed"}},
              [], lambda **a: s._run("nam_search", s.search, **a)),
        _tool("nam_ask", "PR Creation Flow 1f: ask ONE question that tells the item's remaining "
              "records apart. Each option has a label and the words its records carry, as the "
              "records write them (short or long text); the harness places the records and "
              "counts them, and refuses an option the records do not bear out (fix it and call "
              "again). The officer sees the options as buttons.",
              {"item": ITEM, "question": {"type": "string"},
               "options": {"type": "array", "items": {
                   "type": "object", "properties": {
                       "label": {"type": "string"},
                       "words": {"type": "array", "items": {"type": "string"}}},
                   "required": ["label", "words"]}}},
              ["question", "options"],
              lambda **a: s._run("nam_ask", s.ask, **a), notify=True),
        _tool("nam_answer", "PR Creation Flow 1f-1g: the officer's answer. answer= narrows the "
              "records (an option's label, words such as '0-200', or a material code). field= "
              "and value= record a detail the records do not split on (date needed, location, "
              "use, rating, certificate); default=true with source= when NRL's standard value "
              "is used because the officer does not know.",
              {"item": ITEM, "answer": {"type": "string"}, "field": {"type": "string"},
               "value": {"type": "string"}, "default": {"type": "boolean"},
               "source": {"type": "string"}}, [],
              lambda **a: s._run("nam_answer", s.answer, **a)),
        _tool("nam_pick", "PR Creation Flow 1h: put the item's one material in the cart: its "
              "Key from the records still in, and the PR (pr=) the officer chose when the "
              "record is in several PRs. Its quantity is that PR's own unless the officer gave "
              "one; nothing more is asked. With no key, the officer's quantity (and unit) for "
              "the item already in the cart. remove=true takes the item out of the cart.",
              {"item": ITEM, "key": {"type": "string"}, "pr": {"type": "string"},
               "quantity": {"type": "number"}, "unit": {"type": "string"},
               "remove": {"type": "boolean"}}, [],
              lambda **a: s._run("nam_pick", s.pick, **a), notify=True),
        _tool("nam_cart", "PR Creation Flow 1h: the officer's answer once every chosen item is "
              "in the cart and the harness asked whether that is everything. answer='done': "
              "that is everything (then the PURs and the PRs). answer='more': they want to add "
              "materials to this same cart (then ask which, and nam_need(items=[...], "
              "add=true)).",
              {"answer": {"type": "string", "enum": ["done", "more"]}}, ["answer"],
              lambda **a: s._run("nam_cart", s.cart_review, **a)),
        _tool("nam_tenders", "PR Creation Flow 1j. No item: the PUR cases ranked by how many "
              "of the need's materials each holds (the cart's codes, and the materials not "
              "taken forward), each with its match: the PUR holding the most is 100%. With "
              "item=: that item's past tenders."
              + (" A PUR's data SAP does not show is kg_tender_record's." if kg else ""),
              {"item": ITEM}, [],
              lambda **a: s._run("nam_tenders", s.tenders, **a)),
        _tool("nam_matches", "Show the officer the records still in for an item as cards, the "
              "best first, once their description of it is complete (the questions come first; "
              "the harness shows them by itself when one record is left or nothing tells the "
              "rest apart).", {"item": ITEM}, [],
              lambda **a: s._run("nam_matches", s.matches, **a)),
        _tool("nam_other", "The officer wants something other than the records shown for an "
              "item ('Show me something else'): the first time, the closest other record SAP "
              "holds (shown as a card); the second time, the last answer is taken back and the "
              "specification is asked again.", {"item": ITEM}, [],
              lambda **a: s._run("nam_other", s.other, **a)),
        _tool("nam_undo", "Take back the officer's last act (an answer, a detail, the choice "
              "of items, a material put in the cart, a PR field), or the last steps= of them. "
              "Each undo goes one further back; a confirmed PR is never taken back.",
              {"steps": {"type": "integer"}}, [],
              lambda **a: s._run("nam_undo", s.undo, **a)),
        _tool("nam_estimate", "Stage 2, only when the officer asks: the cart's estimate. "
              "action: 'build' (last price, 7% a year to the evaluation date, results), 'set' "
              "(a line's quantity or rate), 'decide' (accept or revise a flagged line; revise "
              "with rate=), 'lock' (only when every flag is decided), 'show'.",
              {"action": {"type": "string", "enum": ["build", "set", "decide", "lock", "show"]},
               "line": {"type": "integer"}, "quantity": {"type": "number"},
               "rate": {"type": "number"},
               "decision": {"type": "string", "enum": ["accept", "revise"]},
               "note": {"type": "string"},
               "evaluation_date": {"type": "string", "description": "YYYY-MM-DD; today if "
                                                                    "not given"}}, ["action"],
              lambda **a: s._run("nam_estimate", s.estimate_step, **a)),
        _tool("nam_pr", "Stage 3: one cart item's own PR. action: 'fill' (from stage 1 and "
              "its price), 'set' (the officer's delivery_date, plant, storage_location, "
              "justification; rate= revises the price, accept_price=true accepts a flagged "
              "one), 'confirm' (only on the officer's word, with every mandatory field), "
              "'show'. Then the next item's PR. When the officer wants ONE PR for several "
              "cart items: action='combine' with items=[...]; then common=true to set, confirm "
              "or show it; 'split' when the officer wants each item's own PR (it also undoes a "
              "common one).",
              {"item": ITEM,
               "action": {"type": "string",
                          "enum": ["fill", "set", "confirm", "show", "combine", "split"]},
               "items": {"type": "array", "items": {"type": "string"},
                         "description": "combine: the cart items the common PR holds"},
               "common": {"type": "boolean", "description": "set, confirm or show the "
                                                           "common PR"},
               "delivery_date": {"type": "string"}, "plant": {"type": "string"},
               "storage_location": {"type": "string"}, "justification": {"type": "string"},
               "rate": {"type": "number"}, "accept_price": {"type": "boolean"}},
              ["action"], lambda **a: s._run("nam_pr", s.pr_step, **a)),
        _tool("nam_case", "The chat's NAM case as it stands (materials, chosen items, cart, "
              "records left, answers, PURs, PRs), or case_id= to resume another case here.",
              {"case_id": {"type": "string"}}, [],
              lambda **a: s._run("nam_case", s.show, **a)),
    ]


def reminder(conn, session_id: str) -> tuple[str, dict] | None:
    """(the open question, its options as shown) of the chat's case, for the
    harness to ask again after a turn that asked nothing, or None."""
    data = case.for_session(conn, session_id)
    for i in (case.chosen(data) if data else []):
        if i.get("pending_pick") and not i.get("picked"):
            q = f"Add this {i['name']} to the cart?"
            return q, {"question": q, "options": [YES_LABEL, NO_LABEL], "field": "nam",
                       "progress": case.progress(data, i)}
    if data and case.review(data) in ("due", "asked"):
        if case.review(data) == "due":
            # every item is in the cart and nobody asked yet: the whole cart first
            from nam_agent.nam import cards

            data["review"] = {"asked": True}
            case.save(conn, data, session_id, "ask", "asked whether the cart is complete")
            chatstate.emit("nam_cards", cards.cart(data))
        q = review_question()
        return q["question"], {**q, "field": "nam"}
    if data and data.get("asking_category"):
        q = data["asking_category"]
        return q["question"], {**_shown(q), "field": "nam"}
    if data and data.get("choosing"):
        q = data["choosing"]
        return q["question"], {"question": q["question"], "options": q["options"],
                               "field": "nam"}
    it = case.current_item(data) if data else None
    if it is not None and it.get("pending"):
        q = it["pending"]
        return q["question"], {"question": q["question"], "options": narrow.labels(q),
                               "field": "nam", "multi": True,
                               "progress": case.progress(data, it)}
    # few records are in, no question could split them (the agent's were
    # refused) and none was shown this turn: the harness shows them as cards
    left = case.remaining(it) if it is not None and it.get("searched") else []
    if (it is not None and left and len(left) <= SHOW_MATCHES and not it.get("pending")
            and not chatstate.done("cards")
            and (len(left) <= PICK_FROM
                 or narrow.suggest(left, it.get("query") or it["name"]) is None)):
        _show(it["name"], left, it.get("answers"))
        return None
    # several records are in and no question is open (the agent's were
    # refused, or it typed one): the harness asks one the records bear out
    if it is not None and it.get("searched") and len(case.remaining(it)) > PICK_FROM:
        q = narrow.suggest(case.remaining(it), it.get("query") or it["name"])
        if q is not None:
            it["pending"] = q
            case.save(conn, data, session_id, "ask", f"asked \"{q['question']}\" for "
                                                      f"'{it['name']}'")
            return q["question"], {"question": q["question"], "options": narrow.labels(q),
                                   "field": "nam", "multi": True,
                                   "progress": case.progress(data, it)}
    return None


def read_click(conn, session_id: str, message: str) -> tuple[str, dict] | None:
    """(the tool, its arguments) that read the officer's message when it is
    exactly a choice the harness itself showed: an option of the open
    question, or several joined as "a; b". Anything else (their own words, a
    side question) is the agent's to read, so None."""
    data = case.for_session(conn, session_id) if session_id else None
    if not data or not (message or "").strip():
        return None
    said = narrow.norm(message)
    if said == narrow.norm(UNDO_LABEL):
        return "nam_undo", {}
    if said.startswith(narrow.norm(OTHER_LABEL)):
        named = said[len(narrow.norm(OTHER_LABEL)):].strip()
        named = named[4:].strip() if named.startswith("for ") else named
        it = case.item(data, named) if named else None
        if it is not None:
            return "nam_other", {"item": it["name"]}
        it = case.current_item(data) or next(
            (i for i in reversed(case.chosen(data)) if i.get("picked")
             and i["name"] not in case.confirmed(data)), None)
        return ("nam_other", {"item": it["name"]}) if it is not None else None
    # the officer's yes or no to the record the agent understood
    for i in case.chosen(data):
        pp = i.get("pending_pick")
        if pp and said == narrow.norm(YES_LABEL):
            return "nam_pick", {"item": i["name"], "key": pp["key"], "pr": pp["pr"]}
        if pp and said == narrow.norm(NO_LABEL):
            return "nam_matches", {"item": i["name"]}
    # the officer's answer to whether the cart is complete
    if case.review(data) in ("due", "asked"):
        if said == narrow.norm(DONE_LABEL):
            return "nam_cart", {"answer": "done"}
        if said == narrow.norm(MORE_LABEL):
            return "nam_cart", {"answer": "more"}
    # a card's "Continue with this": the record and the PR it stands for
    it = case.current_item(data)
    if it is not None and it.get("searched"):
        from nam_agent.nam import cards

        pool = case.remaining(it)
        choice = cards.records(it["name"], pool[:SHOW_MATCHES], it.get("answers"))["choices"].get(
            message.strip())
        if choice is None:
            # a record shown on its own (its PRs), still among those in
            for c in pool:
                choice = cards.records(it["name"], [c], it.get("answers"))["choices"].get(
                    message.strip())
                if choice:
                    break
        if choice:
            return "nam_pick", {"item": it["name"], "key": choice["key"], "pr": choice["pr"]}
    parts = [narrow.norm(p) for p in message.split(";") if p.strip()]

    def all_in(options: list[str]) -> bool:
        labels = {narrow.norm(o) for o in options}
        return bool(parts) and all(p in labels for p in parts)

    if data.get("asking_category"):
        q = data["asking_category"]
        values = q.get("values") or {o: o.lower() for o in q["options"]}
        if len(parts) == 1:
            picked = next((c for label, c in values.items() if narrow.norm(label) == parts[0]),
                          None)
            if picked in CATEGORIES:
                return "nam_need", {"category": picked}
        return None
    if data.get("choosing"):
        return ("nam_choose", {"items": [message]}) if all_in(data["choosing"]["options"]) else None
    it = case.current_item(data)
    if it is not None and it.get("pending"):
        if all_in([o["label"] for o in it["pending"]["options"]]):
            return "nam_answer", {"item": it["name"], "answer": message}
    return None


_CLAIM = re.compile(r"\b(confirmed|finali[sz]ed|ready for sap|sent to sap|submitted to sap|"
                    r"purchase orders?|PO numbers?|POs? (?:is |are |has been |have been )?"
                    r"(?:created|raised|issued)|PR number)\b", re.IGNORECASE)
_NEG = re.compile(r"\b(not|no|never|yet to|cannot|can't|before|once|until|when|if|would|will)\b",
                  re.IGNORECASE)


def claims(conn, session_id: str, reply: str, tool_calls: list[dict]) -> str | None:
    """The case's own word when a reply says a PR was confirmed, sent, or a
    purchase order made, and the case does not bear it out: what was done is
    the tools' (a confirmation is nam_pr's, on the officer's word), and NAM
    raises no purchase order. None when the reply claims nothing, or the
    case agrees."""
    data = case.for_session(conn, session_id) if session_id else None
    if not data or not reply:
        return None
    said = [x for x in re.split(r"(?<=[.!?])\s+|\n+", reply)
            if _CLAIM.search(x) and not _NEG.search(x)]
    if not said:
        return None
    confirmed_now = any(c.get("tool") == "nam_pr" and "CONFIRMED" in str(c.get("output") or "")
                        for c in tool_calls or [])
    names = [i["name"] for i in case.chosen(data) if i.get("picked")]
    done = [i["name"] for i in case.chosen(data)
            if (i.get("pr") or {}).get("status") in pr.DONE]
    po = any(re.search(r"purchase orders?|\bPOs?\b", x, re.IGNORECASE) for x in said)
    if not po and (confirmed_now or (names and set(done) >= set(names))):
        return None                                  # the case bears it out
    state = (f"confirmed (ready for SAP): {', '.join(done)}" if done else "no PR is confirmed yet")
    todo = [n for n in names if n not in done]
    return ("**From the case** (as NAM holds it): " + state
            + (f"; still drafts: {', '.join(todo)}" if todo else "") + ". A PR is confirmed "
            "only by the officer's word, and NAM raises no purchase order: SAP does, from the "
            "confirmed PR.")


_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_DASHES = re.compile("[\u2010-\u2015\u2212]")


def _plain(text: str) -> str:
    """Text with every dash a plain "-" (a model writes U+2011 in codes)."""
    return _DASHES.sub("-", guard.plain_ids(text or ""))


def _words(text: str) -> list[str]:
    return [w for w in narrow.norm(_plain(re.sub(r"[*_`]+", "", text or ""))).split() if len(w) > 2]


def _asks_the_same(sentence: str, question: str) -> bool:
    """A sentence of the reply says the open question again, in any words:
    most of the question's words are in it, and it is not much longer."""
    q, s = set(_words(question)), _words(sentence)
    if not q or not s:
        return False
    return len(q & set(s)) >= 0.75 * len(q) and len(s) <= 2.5 * len(q) + 3


# a short instruction about the buttons ("Please select one.", "Choose the ones you
# need below."): the buttons say it themselves
_BUTTON_HINT = re.compile(r"^\W*(?:please\s+)?(?:select|choose|pick|tap|click)\b[^.?!\d]{0,50}"
                          r"\b(?:one|ones|option|options|below|above|apply|any|items?|"
                          r"materials?)\b[^.?!\d]{0,30}"
                          r"[.!]?\W*$",
                          re.IGNORECASE)


def _without_question(sentences: list[str], question: str) -> list[str]:
    """The sentences, without those that say the question again: one sentence,
    or two or three in a row that are each mostly the question's own words."""
    q = set(_words(question))
    out, i = [], 0
    while i < len(sentences):
        for size in (3, 2, 1):
            window = sentences[i:i + size]
            if len(window) < size:
                continue
            mostly = all(len(set(_words(x)) & q) >= 0.6 * max(1, len(set(_words(x))))
                         for x in window)
            if (size == 1 or mostly) and _asks_the_same(" ".join(window), question):
                i += size
                break
        else:
            out.append(sentences[i])
            i += 1
    return out


def drop_typed_options(reply: str, ask: dict | None) -> str:
    """The reply with the open question said once, at its end, right above
    its buttons: the options the model typed out again go (in whatever
    spelling: "0-200 °C" for "0-200 DegC (3 materials)" is the same option by
    its numbers), and so does every sentence that asks the question again and
    the bracketed hint about the buttons beside them. Everything else stays as
    the model wrote it."""
    if not reply or not ask or not ask.get("options"):
        return reply
    question = " ".join(str(ask.get("question") or "").split())
    labels = {narrow.norm(o) for o in ask["options"]} | {"something else", "other",
                                                        "other (please specify)"}
    numbered = [narrow._numbers(o) for o in ask["options"]]

    def is_option(text: str) -> bool:
        t = narrow.norm(_plain(re.sub(r"\*+", "", text)))
        if not t:
            return False
        if t in labels or any(t == lab.split(" (")[0] for lab in labels):
            return True
        nums = narrow._numbers(_plain(text))
        # the same option written another way: the same numbers, few other words
        return bool(nums) and nums in numbered and len(t.split()) <= 6

    lines = reply.splitlines()
    if question:
        # the question said again across two or three lines ("This looks like
        # goods: ..." then "Shall we go ahead ...?"): those lines go together,
        # each of them being mostly the question's own words
        q = set(_words(question))
        i = 0
        while i < len(lines):
            for size in (3, 2):
                window = [x for x in lines[i:i + size] if x.strip()]
                if (len(window) == size and not any(_BULLET.match(x) for x in window)
                        and all(len(set(_words(x)) & q) >= 0.6 * max(1, len(set(_words(x))))
                                for x in window)
                        and _asks_the_same(" ".join(window), question)):
                    del lines[i:i + size]
                    break
            else:
                i += 1
    out, after_question, dropped = [], False, False
    for line in lines:
        body = _BULLET.sub("", line)
        if re.fullmatch(r"\s*[\[(]\s*\**options?\b[^\])]*[\])]\s*", line, re.IGNORECASE):
            dropped = True                     # "[options: Yes, No]": the buttons show them
            continue
        # a list typed straight under the question is the options again, as
        # the model wrote them: the buttons show the real ones
        if _BULLET.match(line) and (is_option(body) or after_question):
            dropped = True
            continue
        m = re.search(r"\**options?(?:\s*\([^)]*\))?\**\s*:\**", line, re.IGNORECASE)
        if m and not line[m.end():].strip():
            # an "Options:" heading over the typed list: the list goes, and so does it
            after_question, dropped = True, True
            if not line[:m.start()].strip():
                continue
            line = line[:m.start()].rstrip()
        if m:
            parts = [p for p in re.split(r"[|,]", line[m.end():]) if p.strip()]
            if parts and sum(is_option(p) for p in parts) >= max(2, len(parts) - 1):
                line, dropped = line[:m.start()].rstrip(), True
        # every sentence asking the open question again goes: it is said once, last
        if question and line.strip() and _asks_the_same(line, question) and len(
                set(_words(line)) - set(_words(question))) <= 2:
            after_question, dropped = True, True          # the whole line is the question
            continue
        if question and line.strip():
            sentences = re.split(r"(?<=[.?!])\s+", line.strip())
            kept = [x for x in _without_question(sentences, question)
                    if not _BUTTON_HINT.match(x)]
            if len(kept) < len(sentences):
                after_question, dropped = True, True
                line = " ".join(kept)
                if not line.strip():
                    continue
            elif after_question and line.strip() and not _BULLET.match(line):
                after_question = False
        # a bracketed hint about the buttons, beside what was dropped
        if dropped and re.fullmatch(r"\s*\*?\([^()]{1,90}\)\*?\s*", line or ""):
            continue
        if not line.strip() and out and not out[-1].strip():
            continue                  # one blank line where the list was, not two
        out.append(line)
    text = "\n".join(out).strip()
    if question:
        text = (text + "\n\n" if text else "") + question
    return text


def drop_code_lists(reply: str, codes: set[str]) -> str:
    """The reply without a list of material codes offered as choices: the
    officer does not know the codes, and the records are theirs to choose by
    what they say (cards, or the next question)."""
    if not reply or not codes:
        return reply
    norm = {narrow.norm(_plain(c)) for c in codes if c}
    out = []
    for line in reply.splitlines():
        body = narrow.norm(_plain(re.sub(r"\*+", "", _BULLET.sub("", line))))
        if _BULLET.match(line) and body.split(" ")[0] in norm:
            continue
        if re.search(r"\b(keys?|codes?)\b", line, re.IGNORECASE) and re.search(
                r"options?|select|choose|pick", line, re.IGNORECASE) and len(line) < 160:
            continue
        out.append(line)
    return "\n".join(out).rstrip()


_DOCNO = re.compile(r"(?<![\d.,])\d{8,10}(?![\d.,]*\d)")
_CART = re.compile(r"\b(added|put|placed|is in|are in|now in|went into)\b[^.]*\bcart\b|\bcart\b[^.]*"
                   r"\b(now (?:contains|has|holds))\b", re.IGNORECASE)
_NONE = re.compile(r"\b(no (?:sap )?(?:line|record)|not (?:exist|found|present|in sap)|does not exist|"
                   r"doesn't exist|neither)\b", re.IGNORECASE)


def _known_numbers(data: dict, tool_calls: list[dict]) -> set[str]:
    """Every PR, PO and document number the case's SAP records hold, and any
    this turn's tools returned: the numbers a reply may name."""
    out = set()
    for it in data.get("items") or []:
        for c in it.get("candidates") or []:
            for ln in c.get("lines") or []:
                for k in ("pr_no", "po_no", "doc_no"):
                    if ln.get(k):
                        out.add(str(ln[k]).lstrip("0"))
    for c in tool_calls or []:
        out |= {n.lstrip("0") for n in _DOCNO.findall(str(c.get("output") or ""))}
    return out


_TOOL_WORD = re.compile(r"`?\b(?:nam_[a-z_]+|sap_[a-z_]+|kg_[a-z_]+|ask_options)\b(?:\([^)]*\))?`?")
_TABLE_ROW = re.compile(r"^\s*\|")


def plain_words(reply: str, data: dict, tool_calls: list[dict] | None = None) -> str:
    """The reply in the officer's words, not the harness's:

      a sentence naming a tool ("recorded with nam_need(category=...)") goes;
      a record's internal key ("SAP:motor dismantling & erection:") is
      written as the record's own description;
      the materials nam_related read for the agent alone are not listed to
      the officer (a table or list of them goes): what belongs is offered as
      suggestions, with buttons.
    """
    if not reply:
        return reply
    names: dict[str, str] = {}
    for it in data.get("items") or []:
        for c in it.get("candidates") or []:
            if c.get("key") and not c.get("code") and c["key"] != c.get("description"):
                names[c["key"]] = c.get("description") or ""
    for key, desc in sorted(names.items(), key=lambda kv: -len(kv[0])):
        reply = reply.replace(f"**{key}**", desc).replace(key, desc)
    rel_codes = (data.get("related") or {}).get("codes") or {}
    related = {narrow.norm(d)[:40] for d in rel_codes.values() if narrow.norm(d)} | {
        narrow.norm(k) for k in rel_codes if narrow.norm(k)}
    read_related = any(c.get("tool") == "nam_related" for c in tool_calls or [])

    def lists_related(line: str) -> bool:
        t = narrow.norm(_plain(line))
        return bool(related) and any(d and d in t for d in related)

    lines = reply.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if read_related and _TABLE_ROW.match(line):
            # a whole table at once: it goes when it lists what nam_related read
            j = i
            while j < len(lines) and _TABLE_ROW.match(lines[j]):
                j += 1
            block = lines[i:j]
            if sum(lists_related(x) for x in block) >= 2:
                i = j
                continue
            out += block
            i = j
            continue
        if read_related and _BULLET.match(line) and lists_related(line):
            i += 1
            continue
        if _TOOL_WORD.search(line):
            kept = [s for s in re.split(r"(?<=[.?!])\s+", line) if not _TOOL_WORD.search(s)]
            line = " ".join(kept)
            if not line.strip() or re.fullmatch(r"\s*\(?\s*\)?\s*", line):
                i += 1
                continue
        out.append(line)
        i += 1
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def check_reply(conn, session_id: str, reply: str, tool_calls: list[dict]) -> str:
    """The reply held to the case and to SAP, in code:

      a PR or PO number SAP holds nowhere for this case (nor this turn's
      tools returned) is not the officer's to see: its line goes;
      "SAP has no line for PR X" when the case holds PR X is corrected;
      an item said to be in the cart that is not, is corrected;
      a PR said confirmed, or a purchase order said made, that the case does
      not bear out, is corrected (claims).
    """
    data = case.for_session(conn, session_id) if session_id else None
    if not data or not reply:
        return reply
    reply = plain_words(reply, data, tool_calls)
    known = _known_numbers(data, tool_calls)
    kept, dropped = [], 0
    for line in reply.splitlines():
        made_up = [n for n in _DOCNO.findall(_plain(line)) if n.lstrip("0") not in known]
        if made_up:
            dropped += 1
            continue
        kept.append(line)
    out = "\n".join(kept).rstrip()
    notes = []
    if dropped:
        notes.append("a PR or PO number SAP does not hold for this case was taken out of the reply")
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", _plain(out)):
        if not _NONE.search(sentence):
            continue
        for n in _DOCNO.findall(sentence):
            if n.lstrip("0") in {x for x in known}:
                notes.append(f"PR/PO {narrow.doc_no(n)} is in SAP's records for this case: it "
                             "exists (SAP's search finds records by what they say, not by their "
                             "number)")
                break
    picked = {i["name"].lower() for i in data.get("items") or [] if i.get("picked")}
    for i in data.get("items") or []:
        if i["name"].lower() in picked:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", out):
            if i["name"].lower() in sentence.lower() and _CART.search(sentence) and not _NEG.search(
                    sentence):
                notes.append(f"'{i['name']}' is not in the cart yet: it goes in when the officer "
                             "continues with one of its records")
                break
    fix = claims(conn, session_id, out, tool_calls)
    if notes:
        out += "\n\n**From the case** (as NAM holds it): " + "; ".join(dict.fromkeys(notes)) + "."
    if fix:
        out += "\n\n" + fix
    return out
