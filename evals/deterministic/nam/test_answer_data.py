"""DETERMINISTIC EVAL — a model change does not change the data in an answer.

Two models answered the same KG question with different amounts of data: one
kept the comparison table and both tenders, another one tender and rounded
rates. Now every model call is sampled the same way (temperature 0, a fixed
seed), and after each turn the harness checks the reply against the KG tools'
output, adding what it left out under "Data from the KG"
(nam_agent/nam/answer_data.py). A terse model and a wordy one end with the same
tenders and the same figures.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from nam_agent.loop import models
from nam_agent.nam import answer_data

ROOT = Path(__file__).resolve().parents[3]

ITEM_HISTORY = """ITEM CHECK — YES: 36 line item(s) in 2 tender(s) match every word.

Comparison (one column per tender; every figure read from the graph):
|  | PUR-INSTMNT-1002364 | PUR-INSTMNT-1003188 |
|---|---|---|
| Tender no. | OC03000183/SNJ | OC03000186/BHA |
| Lowest bid per unit (min / median / max) | Rs 1,240 / Rs 1,280 / Rs 1,325 (12 lines) | Rs 1,040 / Rs 1,070 / Rs 2,800 (24 lines) |
| Bidders | 4: ALTOP | 3: Asco |

TO ANSWER A COMPARISON: (1) reproduce the comparison table.

PUR-INSTMNT-1002364 items:
| Item (as written) | Size match | Code | Rates on file |
|---|---|---|---|
| TG BiMet 0-150 DegC | exact | I-TG | bid Rs 51,200.00 for 40 each (= Rs 1,280.00/each) L1 |
"""
FIND = """2 past tender(s) match 'gasket'.
REPRODUCE THE TABLE BELOW VERBATIM in your reply.

| Tender | Items | Subject |
|---|---:|---|
| PUR-MECHMNT-1008357 | 1305 | Annual rate contract for Gaskets |
| PUR-MECHMNT-1003042 | 31 | Procurement of gaskets |
"""
PRICE = """Rows marked * are the items the requirement actually matched.
* GASKET SPIRAL WOUND SS304; bidder's quote; per each; observed unit price: min 802.00 / median 908.00; precedent: PUR-MECHMNT-1008357
  other row; precedent: PUR-SMT-1000001
"""
CALLS = [{"tool": "kg_item_history", "args": {}, "output": ITEM_HISTORY}]


def _pur(text):
    return set(re.findall(r"PUR-[A-Z]+-\d+", text))


def test_a_terse_model_gets_the_missing_data_added():
    terse = "Yes, we bought it in PUR-INSTMNT-1002364."
    out = answer_data.complete(terse, CALLS)
    assert out.startswith(terse) and answer_data.HEADING in out
    assert _pur(out) == {"PUR-INSTMNT-1002364", "PUR-INSTMNT-1003188"}
    assert "| Lowest bid per unit (min / median / max) | Rs 1,240" in out   # the graph's own table
    assert "TO ANSWER" not in out and "ITEM CHECK" not in out              # no instructions


def test_a_reply_that_carries_the_data_is_left_as_written():
    wordy = ("Two tenders: PUR-INSTMNT-1002364 (lowest Rs 1,240 per unit) and "
             "PUR-INSTMNT-1003188 (lowest Rs 1,040 per unit).")
    assert answer_data.complete(wordy, CALLS) == wordy


def test_a_rounded_or_missing_rate_is_caught():
    rounded = "PUR-INSTMNT-1002364 at about Rs 1,200 and PUR-INSTMNT-1003188 at Rs 1,040."
    assert answer_data.HEADING in answer_data.complete(rounded, CALLS)


def test_terse_and_wordy_models_end_with_the_same_data():
    terse = answer_data.complete("We have bought these before.", CALLS)
    wordy = answer_data.complete("Two tenders, PUR-INSTMNT-1002364 and PUR-INSTMNT-1003188, "
                                 "lowest Rs 1,240 and Rs 1,040 per unit; more detail...", CALLS)
    figures = lambda t: {f for f in answer_data._numbers(t) if f in {1240.0, 1040.0}}  # noqa: E731
    assert _pur(terse) == _pur(wordy) and figures(terse) == figures(wordy) == {1240.0, 1040.0}


def test_tender_lists_and_prices_are_covered_and_outages_are_not_padded():
    out = answer_data.complete("Some gasket tenders exist.",
                               [{"tool": "kg_find_tenders", "output": FIND}])
    assert "| PUR-MECHMNT-1003042 | 31 | Procurement of gaskets |" in out
    out = answer_data.complete("Rates exist.", [{"tool": "kg_price_reference", "output": PRICE}])
    assert "* GASKET SPIRAL WOUND SS304" in out and "PUR-SMT-1000001" not in out
    down = [{"tool": "kg_item_history", "output": "KG PRECEDENT UNAVAILABLE: refused"}]
    assert answer_data.complete("The KG is down.", down) == "The KG is down."
    assert answer_data.complete("Hi!", [{"tool": "ask_options", "output": "Asked"}]) == "Hi!"


def test_the_check_runs_after_every_turn():
    src = (ROOT / "nam_agent/app.py").read_text(encoding="utf-8")
    assert "result.reply = answer_data.complete(result.reply, result.tool_calls," in src


# -- fixed sampling -------------------------------------------------------------------

def _client(monkeypatch, refuse=None, **env):
    for key in ("NAM_TEMPERATURE", "NAM_SEED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = models.OpenAICompatClient.__new__(models.OpenAICompatClient)
    client.sampling = models.sampling()
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        bad = [k for k in (refuse or ()) if k in kwargs]
        if bad:
            raise ValueError(f"Unsupported value: '{bad[0]}' does not support 0 with this model")
        return SimpleNamespace(ok=True)

    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, sent


def test_every_call_is_sampled_the_same_way(monkeypatch):
    client, sent = _client(monkeypatch)
    client._call_once({"model": "m", "messages": []})
    assert sent[-1]["temperature"] == 0.0 and sent[-1]["seed"] == 7
    client._call_once({"model": "m", "messages": [], "temperature": 0.5})    # a caller's own wins
    assert sent[-1]["temperature"] == 0.5
    client, sent = _client(monkeypatch, NAM_TEMPERATURE="", NAM_SEED="11")
    client._call_once({"model": "m", "messages": []})
    assert "temperature" not in sent[-1] and sent[-1]["seed"] == 11


def test_a_model_that_refuses_a_setting_runs_without_it(monkeypatch):
    client, sent = _client(monkeypatch, refuse=("temperature",))
    assert client._call_once({"model": "m", "messages": []}).ok
    assert "temperature" not in sent[-1] and sent[-1]["seed"] == 7
    client._call_once({"model": "m", "messages": []})                         # remembered
    assert len(sent) == 3


NOT_AT_SIZE = ("ITEM CHECK — NOT AT THIS SIZE: the item was bought (11 line item(s) in 1 "
               "tender(s)), but no line item has 30 mm, 6 mm. Searched all line items.\n"
               "PUR-MECHMNT-1008357 items:\n| Item | Size match |\n|---|---|\n"
               "| 500 NB 300# SS gasket | other size |")


def test_a_reply_that_claims_the_missing_size_was_bought_is_corrected():
    bad = ("No exact-size line was found in the graph.\nThe closest precedent is the 30 mm x "
           "6 mm PTFE spiral-wound gasket that was procured in tender PUR-MECHMNT-1008357.\n"
           "| Matching lines (exact 30 mm x 6 mm) | 0 / 11 |\n"
           "Draft a new tender for the exact 30 mm x 6 mm size?")
    calls = [{"tool": "kg_item_history", "output": NOT_AT_SIZE}]
    claim = ("The closest precedent is the 30 mm x 6 mm PTFE spiral-wound gasket that was "
             "procured in tender PUR-MECHMNT-1008357.")
    assert answer_data.size_claims(bad, NOT_AT_SIZE) == [claim]
    assert "**Correction from the KG:** no past NRL line item is 30 mm, 6 mm" in \
        answer_data.complete(bad, calls)
    good = "NRL has not bought it at 30 mm x 6 mm; PUR-MECHMNT-1008357 bought 500 NB only."
    assert "Correction" not in answer_data.complete(good, calls)


RECORD = ("TENDER RECORD — PUR-MECHMNT-1008357 · OC01000435 / PRV\nReproduce both tables.\n\n"
          "| Field | On file |\n|---|---|\n| PR no. | 10081673 |\n| PO no. | not recorded |")


def test_a_tender_record_reply_carries_the_pr_number():
    calls = [{"tool": "kg_tender_record", "output": RECORD}]
    out = answer_data.complete("Here is the record of PUR-MECHMNT-1008357.", calls)
    assert answer_data.HEADING in out and "| PR no. | 10081673 |" in out
    kept = "PUR-MECHMNT-1008357 has PR no. 10081673; the graph holds no PO number."
    assert answer_data.complete(kept, calls) == kept
