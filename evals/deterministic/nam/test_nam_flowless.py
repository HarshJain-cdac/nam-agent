"""Flowless work (router rule "words", intent.py, case.ask): words that name
no NAM step, sent under one generic system prompt, take the step they need
and only then; a change in words is applied, or said to be not applied; a
question is answered without changing the case."""

import pytest
from flowless_corpus import CORPUS, check
from nam_fakes import OFFICER, ctx

import nam_agent.nam as harness
from nam_agent.nam import store
from nam_agent.nam.playbooks import ask


@pytest.mark.parametrize("entry", CORPUS, ids=[f"{e[0]}:{e[1][:30]}" for e in CORPUS])
def test_flowless_words_take_the_step_they_need(entry):
    assert check(entry) == ""


def test_a_question_changes_nothing_on_the_case(tmp_path):
    from nam_walk import goods_at_s4

    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    version = store.version_of(c.conn, case_id)
    for words in ("what does the estimate come to?", "approve it", "why was PUR-4 chosen?",
                  "What did we pay for gaskets last time?", "hello"):
        body = harness.run({"system": "Procurix NAM assistant", "case_id": case_id,
                            "messages": [{"role": "user", "content": words}]}, c, OFFICER)
        assert body["flow"] == "case.ask" and body["result"]["answer"], body
        assert store.version_of(c.conn, case_id) == version, words


def test_the_answers_are_cited_and_say_who_may_approve(tmp_path):
    from nam_walk import goods_at_s4

    c = ctx(tmp_path)
    prices = harness.run({"system": "x", "messages": [
        {"role": "user", "content": "What did we pay for gaskets last time?"}]}, c, OFFICER)
    assert prices["result"]["facts"] and all(f["authority"] == "precedent"
                                             for f in prices["result"]["facts"])
    assert "not a current rate" in prices["result"]["answer"]
    term = harness.run({"system": "x", "messages": [{"role": "user", "content": "What is an EMD?"}]},
                       c, OFFICER)
    assert term["result"]["facts"][0]["source"] == "policy/glossary.json"
    skill = harness.run({"system": "x", "messages": [{"role": "user", "content": "What is a PQC?"}]},
                        c, OFFICER)
    assert skill["result"]["facts"][0]["source"] == "skill:nam-documents/pqc"
    case_id = goods_at_s4(c)
    harness.run({"system": "Goods estimate", "case_id": case_id,
                 "messages": [{"role": "user", "content": ""}]}, c, OFFICER)
    said = harness.run({"system": "x", "case_id": case_id,
                        "messages": [{"role": "user", "content": "approve it"}]}, c, OFFICER)
    assert "not given in words" in said["result"]["answer"] and "U1" in said["result"]["answer"]
    assert said["pending_action"]["kind"] == "gate:estimate"


def test_a_knowledge_graph_that_is_down_is_said(tmp_path):
    from nam_fakes import fake_kg

    c = ctx(tmp_path, fetch=fake_kg(down=True))
    body = harness.run({"system": "x", "messages": [
        {"role": "user", "content": "Show me past tenders for gaskets"}]}, c, OFFICER)
    assert "knowledge graph is down" in body["result"]["answer"] and body["warnings"]


def test_every_glossary_term_is_answered():
    for term in ask.glossary():
        assert ask._term(f"what is {term}?")[1], term
