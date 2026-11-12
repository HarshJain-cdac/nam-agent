"""The PR Creation Flow in NAM, as the agent's guide.

NAM raises a Purchase Request for goods in three stages (the manual "PR
Creation Flow in NAM", final version):

  1. Need Identification  the officer says what is needed; NAM finds the
                          matching past purchases, asks until the item is
                          fully described, and the officer picks the items
  2. Estimate creation    the last price of each item, brought up to today
                          at 7% a year, checked, decided and locked
  3. PR Creation          the PR filled from stages 1 and 2, confirmed, and
                          sent to SAP

The main output is the need understood: the materials a job description
names, how many of each SAP holds, the ones the officer takes forward, and
for each of those, one at a time, the one material code (into the cart).
Then the PUR cases that hold the most of the materials, and one PR per cart
item. The estimate is there when the officer asks for it.

The flow is soft: the steps below are what the agent knows and follows, not a
script the harness runs. The agent decides what to do next in the chat, with
its tools; the harness keeps the case (case.py) and enforces only the gates
(the choice before the questions, a material from the records still in, a
flagged price decided and every mandatory field before a PR's confirmation),
each in the tool that passes it (stage_tools.py).

guide(case) is what the agent reads each turn: the chat's cart node first
(cart.py: the items, the types SAP holds, the details given, the cart), then
the manual's steps and, for the open case, where it stands and what comes next.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    id: str
    stage: int
    name: str
    does: str           # what happens, in the manual's words
    tools: str          # how the agent does it


STEPS: tuple[Step, ...] = (
    Step("1a", 1, "Category",
         "Goods, services or works: judge it from the need and give it to nam_need with why= "
         "(one line from the officer's words); unless they said it themselves, the harness "
         "asks them to confirm or change it before anything is searched. Their answer goes "
         "back to nam_need.",
         "nam_need(category=..., why=...)"),
    Step("1b", 1, "The need",
         "The officer types the need or a job description in their own words; read it back "
         "in one line.",
         "nam_need(need=...)"),
    Step("1c", 1, "The materials",
         "Take out every material the need names, one short name each, as SAP would write "
         "it. Each is searched in SAP at once, by short text and long text; show how many SAP "
         "holds of each.",
         "nam_need(items=[...])"),
    Step("1d", 1, "Choose the items",
         "See what NRL bought together with these materials on their past PRs (nam_related) "
         "and judge which of those belong to this need. Then ask which materials go forward, "
         "the ones that belong suggested beside the need's own, each with why (the officer "
         "may choose several); they are then worked one at a time, in that order.",
         "nam_related(); nam_choose(question=..., suggest=[...]) then nam_choose(items=[...])"),
    Step("1e", 1, "Find the missing details",
         "For the item being worked, find what still tells its records apart: size or range, "
         "material, standard, rating, certificate, make. Records that share a short text "
         "differ in their long text: ask about that.",
         "read the records digest"),
    Step("1f", 1, "Follow-up questions",
         "Ask ONE question at a time, its options taken from the records, until one material "
         "code is left (or a few to pick from). The officer clicks or types; say back what "
         "you understood. An answer that fits no option gets one short clarifying question, "
         "never a restart.",
         "nam_ask(item, question, options) then nam_answer(item, answer)"),
    Step("1g", 1, "NRL's standard value",
         "When the officer does not know a required detail, use NRL's specification value "
         "if one exists and mark it default.",
         "nam_answer(item, field=..., value=..., default=true, source=...)"),
    Step("1h", 1, "Into the cart",
         "The officer continues with the one PR that matches (its card); it goes in the cart "
         "with that PR's quantity, and nothing more is asked. Then the next chosen item, the "
         "same way. With every item in, the harness shows the whole cart and asks whether "
         "that is everything: summarise the cart in plain words. 'More' adds materials to "
         "this same case and cart (nam_need add=true), worked the same way; the PURs and the "
         "PRs wait until the officer says that is everything.",
         "nam_pick(item, key, pr); nam_cart(answer); nam_need(items, add=true)"),
    Step("1i", 1, "Undo",
         "Whenever the officer wants a step back: an answer, a choice, a material in the "
         "cart, a PR field.",
         "nam_undo(steps)"),
    Step("1j", 1, "Matching PURs (when asked)",
         "Only when the officer asks for them: the PUR cases that hold the most of the "
         "need's materials, with their PR and PO; the PUR holding the most is the 100% match. "
         "The flow's own end is each item on ONE past PR, then its new PR.",
         "nam_tenders(); kg_tender_record(PUR) for what SAP does not show"),
    Step("2a", 2, "Estimate (only when asked)",
         "The cart's last prices, brought up to today at 7% a year, checked (YRS, CAGR: "
         "Cleared, AHR, Manual review, New item), decided and locked.",
         "nam_estimate(action='build' | 'set' | 'decide' | 'lock')"),
    Step("3a", 3, "Fill each item's PR",
         "One PR for each item in the cart, each field with where it came from; its price is "
         "the last price brought up to today (or the locked estimate's). The officer gives "
         "delivery date, plant, storage location and justification, and accepts or revises a "
         "flagged price.",
         "nam_pr(item, action='fill' | 'set')"),
    Step("3b", 3, "Confirm each PR",
         "Check every mandatory field, show the PR, and on the officer's word confirm it; "
         "then the next item's PR.",
         "nam_pr(item, action='confirm')"),
)

STAGES = {1: "Need Identification", 2: "Estimate (optional)", 3: "PR Creation"}

# where the facts come from: SAP alone, or SAP and the tender knowledge graph
SOURCES_SAP = ("- One source: SAP. Every number, material, rate, PR, PO and PUR comes from SAP's "
               "records (nam_need, nam_search, sap_search). What SAP does not hold, say so.\n")
SOURCES_KG = ("- Two sources, two tools. SAP gives the numbers: how many, which materials, rates, "
              "PR and PO numbers (nam_need, nam_search, sap_search). The knowledge graph gives a "
              "PUR case's own data that SAP's lines do not show (kg_tender_record): ask it only "
              "for what SAP did not give.\n")

RULES = (
    "How you work the PR Creation Flow:\n"
    "- The main output is understanding the need: which materials, and for each chosen one "
    "the exact material code. You decide each next step from the officer's message and the "
    "case below; the steps are a guide, not a script. Skip what the case already has; go "
    "back when the officer changes something.\n"
    "{sources}"
    "- Every fact (item, code, rate, PR, PO, PUR, tender) comes from a tool. Never invent "
    "one, and never offer an option no record carries.\n"
    "- Several codes can share a short text: keep asking, one precise question at a time, "
    "until the officer's need leaves one code. While several records are in, do not show "
    "the list: say how many are in and ask about the one detail that best splits them.\n"
    "- The officer does not know material codes: never ask them to choose a code or a key. "
    "Ask about what the records say (a size, a material, a rating, the use).\n"
    "- For every item, the questions come first; the matching records are shown as cards "
    "once the item is described (nam_matches), not after each question.\n"
    "- When an item goes into the cart its PR is drafted and shown; say what it still needs, "
    "then go on with the next item.\n"
    "- Explain a record only from what SAP holds for it (its short and long text, its "
    "numbers, its PR and PO). Never expand an abbreviation or give a reason SAP does not "
    "state (a size, a grade, why a rate is higher): say SAP does not say. Name only PR and PO "
    "numbers a tool gave you; a PR is never 'missing' because sap_search did not find it by "
    "its number.\n"
    "- Nothing is done unless a tool did it in this turn: never say a PR is confirmed, sent "
    "or a purchase order made from your own words. NAM raises no purchase order.\n"
    "- SAP's search also finds other materials by meaning (a thermocouple in a search for a "
    "temperature gauge); the digest marks them. They are not types of the item: ask which kind "
    "of material the officer needs, never 'which type of <item>' over them.\n"
    "- Options come only from nam_ask and nam_choose. Never list choices in your own text; "
    "a date or a place is asked in plain words.\n"
    "- The questions are to find the PR that best matches the officer's need. Once they have "
    "chosen it, ask nothing more about the item (not the quantity: the PR's own is taken, and "
    "the officer may change it), and go on with the next item.\n"
    "- One question at a time. A tool that refuses tells you why: fix it and call it again "
    "once; if it refuses again, ask the officer in plain words instead.\n"
    "- 'Undo', 'go back', 'not that one' is nam_undo.\n"
    "- 'It', 'them', 'that one' mean the case's items and candidates below.\n"
    "- A side question (a rate, a PO, what a term means) is answered, then the open question "
    "is asked again.")


def stage_of(case: dict | None) -> tuple[str, str]:
    """(the step now, the step next) for a case, from what it holds."""
    if not case:
        return "1b", "1b"
    from nam_agent.nam import case as nam_case

    items = case.get("items") or []
    if case.get("asking_category") or not case.get("category"):
        return "1a", "1c"
    if not items:
        return "1b", "1c"
    if any(not it.get("searched") for it in items):
        return "1c", "1c"
    offer = [it for it in items if it.get("candidates")]
    if len(offer) > 1 and not any("selected" in it for it in items):
        return "1d", "1d"
    now = nam_case.current_item(case)
    if now is not None:
        return ("1f" if now.get("pending") else "1e"), "1h"
    if nam_case.review(case) in ("due", "asked", "adding"):
        return "1h", "3a"
    chosen = nam_case.chosen(case)
    if all((it.get("pr") or {}).get("status") in ("ready_for_sap", "sent") for it in chosen):
        return "3b", "done"
    return "3a", "3b"


def next_hint(case: dict | None) -> str:
    """The next act the case points to, in one line: a hint the agent weighs,
    not an order (the officer's message may ask for something else)."""
    if not case or not case.get("items"):
        return ("open the case with nam_need: the need, and the materials it names "
                "(items=[...]).")
    from nam_agent.nam import case as nam_case

    items = case["items"]
    if case.get("asking_category"):
        guess = case["asking_category"].get("guess")
        return ("the officer's message is most likely their answer to the category question"
                + (f" (the guess was {guess}: a yes keeps it, or they name another)" if guess
                   else "") + ": record the category they confirm or name with "
                "nam_need(category='goods' | 'services' | 'works'); SAP is searched then.")
    if not case.get("category"):
        return ("judge goods, services or works from the need and call nam_need with "
                "category= and why=; the officer confirms or changes it.")
    if any(not it.get("searched") for it in items):
        return "search the materials not searched yet (nam_need again, or nam_search)."
    if case.get("choosing"):
        return ("the officer's message is most likely their choice of materials: record it "
                "with nam_choose(items=[...]).")
    offer = [it for it in items if it.get("candidates")]
    if len(offer) > 1 and not any("selected" in it for it in items):
        if not case.get("related"):
            return ("see what NRL bought together with these materials before (nam_related), "
                    "then ask which go forward with nam_choose(question=...), suggesting only "
                    "what belongs to this need (suggest=[...]).")
        return ("show how many SAP holds of each material and ask which go forward with "
                "nam_choose(question=...), with suggest=[...] for what belongs to this need.")
    if (len(items) == 1 and offer and not case.get("related") and not items[0].get("answers")
            and not items[0].get("pending") and not items[0].get("picked")):
        return ("see what NRL bought together with this material before (nam_related): if "
                "something belongs to this need, ask with nam_choose(question=..., "
                "suggest=[...]); if nothing does, go on with its first question.")
    review = nam_case.review(case)
    if review == "adding" and nam_case.cart_full(case):
        return ("the officer wants to add more to this cart: ask which materials, in plain "
                "words; their answer goes to nam_need(items=[...], add=true).")
    it = nam_case.current_item(case)
    if it is None and review in ("due", "asked"):
        return ("every chosen item is in the cart: summarise it for the officer in plain words "
                "(each item, what it is, how many, the past PR it follows; no codes) and let "
                "the harness's question stand: is that everything, or add more? Their answer "
                "goes to nam_cart.")
    if it is not None:
        name, left = it["name"], len(nam_case.remaining(it))
        if it.get("pending"):
            q = it["pending"]["question"]
            return (f"the officer's message is most likely the answer to \"{q}\" "
                    f"for '{name}': read it with nam_answer.")
        if not it.get("candidates"):
            return (f"SAP holds no record for '{name}': ask the officer to describe it "
                    "another way, then nam_search with those words.")
        if left == 0:
            return (f"no record is left for '{name}': take the last answer back with "
                    "nam_undo, or search again with other words.")
        if left == 1:
            return (f"one record is left for '{name}' (shown as a card): when the officer "
                    "continues with it, put it in the cart with nam_pick (key, pr).")
        return (f"{left} records are still in for '{name}': ask what tells them apart with "
                "nam_ask.")
    chosen = nam_case.chosen(case)
    common = case.get("common_pr") or {}
    if common:
        if common.get("status") in ("ready_for_sap", "sent"):
            return "the common PR is ready for SAP: the need is done."
        if common.get("missing"):
            return ("ask the officer, in plain words, for the common PR: "
                    + ", ".join(common["missing"]) + ".")
        return "show the common PR and confirm it on the officer's word (nam_pr common=true)."
    drafted = [i for i in chosen if i.get("picked") and i.get("pr")]
    done = any((i.get("pr") or {}).get("status") in ("ready_for_sap", "sent") for i in chosen)
    if len(drafted) >= 2 and len(drafted) == len(chosen) and not done \
            and not case.get("common_asked"):
        left = [i["name"] for i in case.get("items") or [] if i.get("selected") is False]
        return ("every chosen item has its PR drafted: ask the officer, with buttons "
                "(ask_options), whether they want one common PR for "
                + ", ".join(i["name"] for i in drafted) + "."
                + (f" If yes, ask whether to add the items not taken forward ({', '.join(left)}"
                   ") or finish with these; added items are taken forward with nam_choose and "
                   "worked first." if left else "")
                + " Then nam_pr(action='combine', items=[...]); if no, each item's PR is "
                  "confirmed on its own.")
    for it in chosen:
        pr = it.get("pr") or {}
        if not pr:
            return f"fill the PR for '{it['name']}' with nam_pr(item=..., action='fill')."
        if pr.get("status") in ("ready_for_sap", "sent"):
            continue
        if pr.get("missing"):
            return (f"ask the officer, in plain words, for the PR of '{it['name']}': "
                    + ", ".join(pr["missing"]) + ".")
        return (f"show the PR for '{it['name']}' and confirm it on the officer's word "
                "(nam_pr action='confirm').")
    return "every chosen item's PR is ready for SAP: the need is done."


def guide(case: dict | None, kg: bool = True) -> str:
    """The manual's steps and the open case's place in them, for the agent.
    kg: the tender knowledge graph's tools are given too (else SAP alone)."""
    now, nxt = stage_of(case)
    lines = []
    if case:
        # the cart node first: the requirements as the case holds them
        from nam_agent.nam import cart

        lines.append("\n" + cart.node(case))
    lines += ["\nNAM — the PR Creation Flow (goods). Stages: "
             + "; ".join(f"{n}. {name}" for n, name in STAGES.items()) + ".",
             RULES.replace("{sources}", SOURCES_KG if kg else SOURCES_SAP), "Steps:"]
    for s in STEPS:
        mark = " <- now" if s.id == now else ""
        tools = s.tools if kg else s.tools.replace("; kg_tender_record(PUR) for what SAP does "
                                                   "not show", "")
        lines.append(f"  {s.id} {s.name}: {s.does} [{tools}]{mark}")
    if case:
        from nam_agent.nam import case as nam_case

        lines.append(f"Open case in this chat: {nam_case.summary(case)}")
        lines.append(f"Where it stands: step {now} now; next {nxt}. Next: {next_hint(case)}")
    else:
        lines.append("No case is open in this chat. A need or job description starts one "
                     "with nam_need, with the materials it names.")
    return "\n".join(lines)
