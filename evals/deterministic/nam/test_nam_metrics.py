"""The harness's measured reliability (plan Step 18) held to its thresholds:
routing, injection resistance, cross-document consistency, clause omission,
schema validity and end-to-end cases (nam_metrics.py). The same numbers are
printed by scripts/nam_eval_report.py."""

import json

import nam_metrics
import pytest
from nam_fakes import fake_kg


@pytest.fixture(scope="module")
def metrics():
    return nam_metrics.all_metrics()


def test_every_metric_meets_its_threshold(metrics):
    assert nam_metrics.failures(metrics) == [], metrics


def test_every_seeded_inconsistency_is_caught(metrics):
    assert metrics["consistency_missed"] == [] and metrics["consistency_n"] >= 10


def test_three_categories_run_end_to_end_with_every_response_checked(metrics):
    assert metrics["end_to_end_cases"] == {"goods": True, "services": True, "works": True}
    assert metrics["schema_n"] > 60


def test_every_reply_is_verified_and_none_claims_a_step_it_did_not_finish(metrics):
    assert metrics["verified"] == 1.0 and metrics["verified_n"] > 60
    assert metrics["false_done"] == 0, metrics["false_done_steps"]


def test_no_fact_of_the_three_cases_is_uncited(metrics):
    assert metrics["uncited"] == 0 and metrics["facts_n"] > 20, metrics["uncited_facts"]


def test_retrieval_is_not_measured_without_a_live_kg_named():
    out = nam_metrics.retrieval(kg_url="")
    assert out["retrieval"] is None and "NAM_EVAL_KG_URL" in out["retrieval_note"]


def test_retrieval_recall_counts_a_tender_in_the_first_five(tmp_path, monkeypatch):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"labels": [
        {"query": "spiral wound gaskets", "case_key": "PUR-4"},
        {"query": "ring joint gaskets", "case_key": "PUR-9"}]}))
    monkeypatch.setattr(nam_metrics, "LABELS", labels)
    out = nam_metrics.retrieval(kg_url="http://kg", fetch=fake_kg())
    assert out["retrieval"] == 0.5 and out["retrieval_n"] == 2
    assert out["retrieval_missed"][0]["want"] == "PUR-9"
    assert nam_metrics.failures({**dict.fromkeys(nam_metrics.THRESHOLDS, 1.0),
                                 "injection_false_positives": 0.0, "clause_omission": 0.0,
                                 "uncited": 0, "false_done": 0, "planned": 1.0, "flowless": 1.0,
                                 "retrieval": 0.5}) == [
        "retrieval: 0.5 (threshold 0.9)"]


def test_retrieval_with_the_kg_down_is_not_measured_not_zero(tmp_path, monkeypatch):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"labels": [{"query": "gaskets", "case_key": "PUR-4"}]}))
    monkeypatch.setattr(nam_metrics, "LABELS", labels)
    out = nam_metrics.retrieval(kg_url="http://kg", fetch=fake_kg(down=True))
    assert out["retrieval"] is None and "cannot reach the KG" in out["retrieval_note"]
