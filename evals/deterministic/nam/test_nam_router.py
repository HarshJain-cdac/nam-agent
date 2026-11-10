"""Routing (plan Step 8): the short system prompt and the case's stage pick
the NAM flow, in code.

Every flow is reached from at least three phrasings, every FRS function is
covered by a flow, prompts outside NAM are refused, ambiguous ones get
needs_input, and the model is asked only with a closed list of flow ids."""

import dataclasses
import re
from pathlib import Path

import pytest
from nam_fakes import ask, classified, ctx, run

import nam_agent.nam as harness
from nam_agent.nam import permissions, playbooks, router, schema
from nam_agent.nam.errors import NamError
from nam_agent.nam.router import CaseState

ROOT = Path(__file__).resolve().parents[3]

PHRASES = {
    "need.capture": ["Assess need for request #123", "Capture this procurement need.",
                     "Start a new requirement for the refinery", "We need 20 gate valves"],
    "need.replicate": ["Replicate tender PUR-MECHMNT-1008357", "Create by copy from PO 4500012345",
                       "Same as the last order for gaskets", "Reuse the previous tender"],
    "need.classify": ["Confirm the classification", "Set the tender type to Open",
                      "Change the category to Works", "Is this CAPEX or OPEX",
                      "Classify the need for request #123"],
    "need.guided_form": ["No similar tenders fit, use the guided form",
                         "None of these match", "Fill the form from scratch"],
    "precedent.search": ["Find similar tenders for request #123",
                         "Search past tenders for this need", "Try another search",
                         "Show precedents"],
    "precedent.select": ["Select tender PUR-4 as the precedent",
                         "Choose the precedent for request #123", "Go with PUR-1 and PUR-2"],
    "need.enrich": ["Enrich the case from SAP", "Fetch enterprise data for request #123",
                    "Fill the missing mandatory fields", "Get the SAP history for this item code"],
    "quantity.finalise": ["Finalise the quantity for request #123", "Confirm qty and UoM",
                          "Set the quantity to 20 nos"],
    "material.suggest": ["Suggest materials for this requirement",
                         "Recommend a grade of steel", "Which material should we use"],
    "case.edit": ["Edit the case details", "Change the rates on line 4",
                  "Open the review screen", "Correct the item description fields"],
    "estimate.goods": ["Compare PO rates for the BOQ items", "Work out VARN and CAGR",
                       "Goods estimate from rate contract rates"],
    "estimate.wage": ["Estimate the manpower cost", "Compute the minimum wage estimate",
                      "Wage rates for 12 labourers"],
    "estimate.works": ["Works estimate from the SOR", "Rate analysis using DSR",
                       "Estimate for the empanelment civil job"],
    "docs.generate": ["Generate the BOQ and SCC", "Draft the tender documents",
                      "Prepare the technical specification", "Build the PQC with a QCBS matrix"],
    "docs.review": ["Review the draft SCC", "Check for conflicting clauses",
                    "Please review the documents", "Proofread the PQC"],
    "compliance.build": ["Build the compliance requirements", "Add MSE reservation",
                         "OEM compliance requirements for this case"],
    "validate": ["Validate the PR and documents", "Check consistency across the package",
                 "Cross-document check", "Verify against the procurement manual and circulars"],
    "package.freeze": ["Freeze the package", "Assemble the package for review",
                       "Lock the package now"],
    "pr.build": ["Prepare the PR", "Build the purchase requisition", "Fill in the PR fields"],
    "pr.submit": ["Submit the PR", "One-click PR creation", "Create the PR in SAP",
                  "Record the SAP PR number"],
    "approval": ["Route the case for approval", "Prepare the ANN",
                 "Create the Aarohan file for approval", "Administrative approval for the case"],
    "document.update": ["Upload the edited SCC", "Here is the corrigendum",
                        "Upload a new version of the BOQ", "The spec was edited outside NAM"],
    "package.publish": ["Push the documents to Aarohan", "Upload the package to Aarohan",
                        "Aarohan upload for the frozen package"],
    "handoff.nit": ["Hand off to the NIT module", "Send the package to the commercial department",
                    "Handoff the case"],
    "case.status": ["What is the status of request #123", "What stage is the case at",
                    "Track the progress of this case", "Show the case history"],
    "case.ask": ["What did we pay for gate valves", "What is a PQC",
                 "Explain the SCC for this case", "Show me past tenders for gaskets"],
    "case.plan": ["Make a to-do list for this request", "Complete the whole procurement",
                  "Take this need up to the PR step by step", "Create a plan and run it",
                  "Process everything end to end"],
}

REFUSED = ["Write me a poem about the sea", "What is the weather in Guwahati",
           "Tell me a joke", "Translate this paragraph into Hindi",
           "Evaluate the bids for tender 42", "Award the contract to the L1 bidder",
           "Create the NIT and publish the tender on CPPP", "Who won the cricket match",
           "Handle this", "Do the next thing"]

AMBIGUOUS = ["Help with request #123", "Help me with NAM for this",
             "Process the case documents"]


def _req(system, user="", **extra):
    return {"system": system, "messages": [{"role": "user", "content": user}], **extra}


def _never(*_):
    raise AssertionError("the model must not be asked when the code decides")


# -- the routing table --------------------------------------------------------

def test_every_flow_has_at_least_three_phrasings():
    assert set(PHRASES) == set(playbooks.registry())
    assert all(len(p) >= 3 for p in PHRASES.values())


@pytest.mark.parametrize("flow, phrase",
                         [(f, p) for f, ps in PHRASES.items() for p in ps],
                         ids=lambda v: v if "." in v and " " not in v else None)
def test_each_phrasing_routes_to_its_flow(flow, phrase):
    chosen = router.route(_req(phrase), None, _never)
    assert (chosen.flow, chosen.by) == (flow, "intent"), chosen


def test_routing_is_deterministic():
    for phrases in PHRASES.values():
        for phrase in phrases:
            assert router.route(_req(phrase), None) == router.route(_req(phrase), None)


# -- refusals and ambiguity ---------------------------------------------------

@pytest.mark.parametrize("phrase", REFUSED)
def test_outside_nam_is_refused(phrase):
    chosen = router.route(_req(phrase), None, _never)
    assert chosen.flow is None and chosen.status == "refused", chosen


def test_a_vague_prompt_on_a_case_is_asked_about_not_refused():
    chosen = router.route(_req("Handle this"), CaseState(stage="S2"))
    assert chosen.status == "needs_input"
    assert "precedent.search" in chosen.candidates and "need.capture" not in chosen.candidates


def test_handing_off_to_nit_is_not_mistaken_for_nit_work():
    assert router.route(_req("Hand off to the NIT module"), None).flow == "handoff.nit"


@pytest.mark.parametrize("phrase", AMBIGUOUS)
def test_ambiguous_prompts_need_input(phrase):
    chosen = router.route(_req(phrase), None)
    assert chosen.flow is None and chosen.status == "needs_input" and chosen.candidates


def test_the_model_is_asked_with_a_closed_list_only():
    asked = []

    def model(system, user):
        asked.append((system, user))
        return "need.replicate"

    chosen = router.route(_req("Help with request #123"), None, model)
    assert (chosen.flow, chosen.by) == ("need.replicate", "model")
    (system, user), = asked
    assert "exactly one flow id from the list" in system
    listed = re.findall(r"^- ([a-z._]+):", user, re.MULTILINE)
    offered = router.route(_req("Help with request #123"), None).candidates
    assert listed == list(offered)


@pytest.mark.parametrize("reply", ["delete_everything", "need.capture; DROP TABLE",
                                   "none", "", "estimate.goods"])
def test_a_model_answer_outside_the_list_is_ignored(reply):
    # at S1 with no case, estimate.goods is not a candidate either
    chosen = router.route(_req("Help with request #123"), None, lambda s, u: reply)
    if reply.startswith("need.capture"):
        assert chosen.flow == "need.capture"          # the first word is the answer
    else:
        assert chosen.flow is None and chosen.status == "needs_input"


def test_a_failing_model_leaves_the_question_to_the_caller():
    def down(system, user):
        raise OSError("model unavailable")

    chosen = router.route(_req("Help with request #123"), None, down)
    assert chosen.flow is None and chosen.status == "needs_input"


# -- stage, category, resume --------------------------------------------------

@pytest.mark.parametrize("category, flow", [("goods", "estimate.goods"),
                                            ("services", "estimate.wage"),
                                            ("works", "estimate.works")])
def test_the_category_picks_the_estimate(category, flow):
    case = CaseState(stage="S4", category=category)
    chosen = router.route(_req("Prepare the estimate"), case, _never)
    assert chosen.flow == flow and chosen.by in ("category", "intent")


def test_an_estimate_without_a_category_is_asked_about():
    chosen = router.route(_req("Prepare the estimate"), None)
    assert chosen.status == "needs_input"
    assert set(chosen.candidates) == {"estimate.goods", "estimate.wage", "estimate.works"}


@pytest.mark.parametrize("stage, flow", [("S1", "need.capture"), ("S2", "precedent.search"),
                                         ("S3", "quantity.finalise"), ("S6", "validate")])
def test_continue_follows_the_stage(stage, flow):
    chosen = router.route(_req("Continue with request #123"), CaseState(stage=stage), _never)
    assert (chosen.flow, chosen.by) == (flow, "continue")


def test_an_answer_resumes_the_flow_that_asked():
    case = CaseState(stage="S1", last_flow="need.capture", open_question="variant")
    chosen = router.route(_req("Help with request #123",
                               answer={"question_id": "variant", "value": "ring joint"}),
                          case, _never)
    assert (chosen.flow, chosen.by) == ("need.capture", "resume")


def test_a_named_flow_id_is_taken_as_is():
    chosen = router.route(_req("run estimate.works for this case"), None, _never)
    assert (chosen.flow, chosen.by) == ("estimate.works", "explicit")


def test_the_last_message_is_read_when_the_prompt_says_nothing():
    chosen = router.route(_req("Help with request #123", "we need 40 spiral wound gaskets"),
                          None, _never)
    assert (chosen.flow, chosen.by) == ("need.capture", "message")


def test_identifiers_are_read_from_the_prompt():
    assert router.refs("Status of NAM-2026-0012 for request #123, tender PUR-MECH-1008357, "
                       "PR 1000234567") == {"case_id": "NAM-2026-0012", "request": "123",
                                            "tender": "PUR-MECH-1008357", "pr": "1000234567"}


def test_answering_the_flow_question_with_something_else_is_refused():
    with pytest.raises(NamError) as exc:
        router.route(_req("x", answer={"question_id": "flow", "value": "rm -rf"}), None)
    assert exc.value.code == "bad_request"


# -- through run() -------------------------------------------------------------

def _valid(body):
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    return body


def test_an_ambiguous_call_asks_which_flow_and_the_answer_routes(tmp_path):
    c = ctx(tmp_path)
    body = _valid(run(ask("about this", system="Help with request #123"), c))
    assert body["status"] == "needs_input" and body["flow"] == "nam.route"
    assert body["route"]["by"] == "none" and body["case_id"] is None
    q, = body["questions"]
    assert q["id"] == "flow" and {o["value"] for o in q["options"]} >= {"need.capture"}
    again = _valid(run(ask("i need to procure gaskets", system="Help with request #123",
                           answer={"question_id": "flow", "value": "need.capture"}), c))
    assert again["flow"] == "need.capture" and again["route"]["by"] == "answer"


def test_a_refused_call_writes_nothing(tmp_path):
    c = ctx(tmp_path)
    body = _valid(run(ask("a poem please", system="Write me a poem about the sea"), c))
    assert body["status"] == "refused" and body["warnings"] and body["case_id"] is None
    assert c.conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'nam_cases'"
                          ).fetchone()[0] == 0


def test_a_flow_not_built_yet_is_a_501_after_its_permission_and_stage(tmp_path, monkeypatch):
    # every flow is built now: need.enrich stands in for one that is not
    table = playbooks.registry()
    table["need.enrich"] = dataclasses.replace(table["need.enrich"], handler=None)
    monkeypatch.setattr("nam_agent.nam.playbooks.registry", lambda: table)
    c = ctx(tmp_path)
    first = run(ask("i need to procure gaskets"), c)
    case_id = first["case_id"]
    # ahead of the case's stage: 409, whether or not the flow is built
    body = _valid(run(ask("", system="Compare PO rates for the BOQ items", case_id=case_id), c))
    assert body["error"]["code"] == "stage_gate" and harness.status_of(body) == 409
    drafter = harness.Caller(tenant="nrl", user="d1", roles=("U4",))
    body = run(ask("", system="Compare PO rates for the BOQ items", case_id=case_id), c, drafter)
    assert body["error"]["code"] == "forbidden_role"
    # at the case's stage (S2 once capture is done) but not built: 501
    run(ask("", case_id=case_id, answer={"question_id": "variant", "value": "ring joint"}), c)
    assert classified(c, case_id)["stage"] == "S2"
    body = _valid(run(ask("", system="Enrich the case from SAP", case_id=case_id), c))
    assert body["error"]["code"] == "flow_not_built" and harness.status_of(body) == 501
    assert "flow: need.enrich" in body["error"]["details"]


def test_case_status_through_the_prompt_alone(tmp_path):
    c = ctx(tmp_path)
    case_id = run(ask("i need to procure gaskets"), c)["case_id"]
    body = _valid(run(ask("", system=f"What is the status of {case_id}"), c))
    assert body["flow"] == "case.status" and body["case_id"] == case_id
    assert body["result"]["open_question"]["id"] == "variant"
    other = harness.Caller(tenant="acme", user="x", roles=("U1",))
    assert run(ask("", system=f"Status of {case_id}"), c, other)["error"]["code"] == \
        "forbidden_tenant"


def test_a_flow_that_needs_a_case_says_so(tmp_path):
    body = _valid(run(ask("", system="What stage is the case at"), ctx(tmp_path)))
    assert body["error"]["code"] == "bad_request" and "case_id" in body["error"]["message"]


def test_the_prompt_and_case_id_must_agree(tmp_path):
    c = ctx(tmp_path)
    case_id = run(ask("i need to procure gaskets"), c)["case_id"]
    body = run(ask("", system="Status of NAM-2020-0001", case_id=case_id), c)
    assert body["error"]["code"] == "bad_request"


# -- the playbooks ------------------------------------------------------------

@pytest.mark.parametrize("flow_id", sorted(playbooks.registry()))
def test_each_flow_has_its_instructions(flow_id):
    flow = playbooks.registry()[flow_id]
    text = playbooks.instructions(flow_id, "goods")
    assert text.startswith(f"# {flow_id}: ")
    assert f"FRS: {', '.join(flow.frs)}\n" in text
    for heading in ("Purpose", "Steps", "Rules", "Tools", "Outputs"):
        assert f"\n## {heading}\n" in text
    tools = re.findall(r"^- `([a-z_]+)`$", text.split("## Tools")[1].split("##")[0],
                       re.MULTILINE)
    assert tools == list(flow.tools) and set(tools) <= playbooks.TOOLS
    assert flow_id in permissions.policy()["flows"]
    if flow.handler is not None:
        assert schema.load(flow.result_schema)


def test_instructions_keep_only_the_cases_category():
    goods = playbooks.instructions("docs.generate", "goods")
    works = playbooks.instructions("docs.generate", "works")
    assert "## Goods" in goods and "## Works" not in goods and "## Services" not in goods
    assert "## Works" in works and "## Goods" not in works
    shared = playbooks.instructions("docs.generate")
    assert "## Steps" in shared and "## Goods" not in shared


def test_every_flow_id_fits_the_response_schema():
    pattern = schema.load("response")["properties"]["flow"]["pattern"]
    for flow_id in [*playbooks.registry(), "nam.route", "nam.confirm"]:
        assert re.fullmatch(pattern, flow_id), flow_id
