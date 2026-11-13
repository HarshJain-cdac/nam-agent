"""DETERMINISTIC EVAL: the officer leads the case; the agent changes it for them.

From an officer's chat: "i need gasket and capillary", then "no no i want to
hire labour for grass cutting", then "cancel this task". The old materials
stayed under the new need, the category of the old need stuck, nothing could
close the case, and its question was put back after every reply.
"""

from __future__ import annotations

import sqlite3

import pytest

from evals.deterministic.nam.test_cart_flow import JOB, SAP, URL
from evals.deterministic.nam.test_pr_flow import _kg
from nam_agent.nam import case, chatstate, stage_tools


@pytest.fixture
def t(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    tools["_conn"] = conn
    return tools


def _turn(t, message, tool, **args):
    shown = []
    token = chatstate.enter("chat-o", message)
    try:
        out = t[tool].fn(**args, **({"_notify": lambda k, e: shown.append(e)}
                                   if t[tool].wants_notify else {}))
    finally:
        chatstate.leave(token)
    return out, shown


def _data(t):
    return case.for_session(t["_conn"], "chat-o")


def test_a_different_need_is_a_new_case_and_its_category_is_asked_again(t):
    _turn(t, "i need gasket and valve", "nam_need", need="gasket and valve",
          items=["gasket", "valve"], category="goods", why="materials supplied")
    _turn(t, "Yes, go with Goods", "nam_need", category="goods")
    first = _data(t)["case_id"]
    assert _data(t)["category"] == "goods"
    out, shown = _turn(t, "no no, i want a thermowell instead", "nam_need", need="a thermowell",
                       items=["thermowell"], category="goods", why="a thermowell is supplied")
    data = _data(t)
    assert data["case_id"] != first and [i["name"] for i in data["items"]] == ["thermowell"]
    assert not data.get("category")                        # asked again for the new need
    assert shown and shown[-1]["options"][0] == "Yes, go with Goods"
    old = case.load(t["_conn"], first)
    assert old["closed"].startswith("replaced by")


def test_a_new_need_given_without_its_materials_is_refused_not_kept_on_the_old(t):
    _turn(t, "i need gasket and valve", "nam_need", need="gasket and valve",
          items=["gasket", "valve"], category="goods")
    out, _ = _turn(t, "no, labour for grass cutting", "nam_need", need="grass cutting labour",
                   category="services")
    assert out.startswith("REFUSED: The officer gave a different need: name the materials")
    assert [i["name"] for i in _data(t)["items"]] == ["gasket", "valve"]     # untouched


def test_cancel_closes_the_case_and_nothing_of_it_is_asked_again_until_undone(t):
    _turn(t, "i need gasket", "nam_need", need="gasket", items=["gasket"], category="goods",
          why="materials")
    assert stage_tools.reminder(t["_conn"], "chat-o") is not None   # its question is open
    out, _ = _turn(t, "no, cancel this task", "nam_cancel", reason="no need")
    assert "is cancelled (no need)" in out
    assert _data(t) is None and stage_tools.reminder(t["_conn"], "chat-o") is None
    out, _ = _turn(t, "Undo the last step", "nam_undo")
    assert out.startswith("Took back: the officer cancelled the case")
    assert _data(t) is not None


def test_materials_come_out_of_the_case_and_its_cart(t):
    _turn(t, "goods", "nam_need", need=JOB, items=["gasket", "valve", "thermowell"], category="goods")
    _turn(t, "", "nam_choose", items=["valve", "thermowell"])
    _turn(t, "screwed", "nam_answer", item="valve", answer="screwed")
    _turn(t, "", "nam_pick", item="valve", key="V-2", quantity=4)
    out, _ = _turn(t, "drop the valve", "nam_need", remove=["valve"])
    assert out.startswith("Taken out of the need and the cart: valve.")
    data = _data(t)
    assert [i["name"] for i in data["items"]] == ["thermowell", "gasket"]
    assert case.current_item(data)["name"] == "thermowell"
    out, _ = _turn(t, "drop everything", "nam_need", remove=["thermowell", "gasket"])
    assert "nam_cancel" in out


def test_starting_over_closes_the_old_case(t):
    _turn(t, "goods", "nam_need", need="gasket", items=["gasket"], category="goods")
    first = _data(t)["case_id"]
    _turn(t, "start again: a thermowell", "nam_need", need="thermowell", items=["thermowell"],
          category="goods", new=True)
    assert _data(t)["case_id"] != first
    assert case.load(t["_conn"], first)["closed"] == "the officer started over"
