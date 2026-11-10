"""The NAM harness's eval report (plan Step 18): each metric of
evals/deterministic/nam/nam_metrics.py with its threshold, offline.

    python scripts/nam_eval_report.py               the table
    python scripts/nam_eval_report.py --json out.json   and the numbers as JSON
    NAM_EVAL_KG_URL=http://127.0.0.1:8077 python scripts/nam_eval_report.py
                                                    also retrieval recall, live (opt in)

Exits 1 when a metric misses its threshold, so CI can run it as a step.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "evals" / "deterministic" / "nam"
sys.path.insert(0, str(HERE))

LABELS = {
    "routing": "routing: phrasings to their flow",
    "refused": "routing: outside NAM refused",
    "ambiguous": "routing: vague prompts asked about",
    "injection_found": "injection: hostile texts found",
    "injection_false_positives": "injection: tender wording flagged (lower is better)",
    "consistency": "cross-document inconsistencies caught",
    "clause_omission": "precedent clauses omitted (lower is better)",
    "schema": "responses valid against their schemas",
    "end_to_end": "end-to-end cases from need to NIT",
    "uncited": "facts without a source (lower is better)",
    "verified": "replies carrying a step verification",
    "false_done": "steps reported done that failed verification (lower is better)",
    "planned": "a case completed from one query by its to-do list",
    "flowless": "words naming no step: the step they need, or an answer",
}
COUNTS = {"routing": "routing_n", "injection_found": "injection_n",
          "injection_false_positives": "benign_n", "consistency": "consistency_n",
          "clause_omission": "clauses_n", "schema": "schema_n", "uncited": "facts_n", "verified": "verified_n",
          "planned": "planned_calls", "flowless": "flowless_n"}


def main(argv: list[str]) -> int:
    logging.disable(logging.WARNING)
    import nam_metrics

    m = nam_metrics.all_metrics()
    print("| metric | value | threshold | n |\n|---|---|---|---|")
    for key, label in LABELS.items():
        value = m.get(key)
        shown = f"{value:.1%}" if isinstance(value, float) else str(value)
        print(f"| {label} | {shown} | {nam_metrics.THRESHOLDS[key]:.0%} | "
              f"{m.get(COUNTS.get(key, ''), '')} |")
    shown = f"{m['retrieval']:.1%}" if m["retrieval"] is not None else "not measured"
    print(f"| retrieval: recall@{nam_metrics.RETRIEVAL_K} of past tenders | {shown} | "
          f"{nam_metrics.RETRIEVAL_RECALL:.0%} (DRAFT) | {m.get('retrieval_n', '')} |")
    print(f"\nretrieval: {m['retrieval_note']}")
    print(f"\nend-to-end: {m['end_to_end_cases']}, seconds: {m['end_to_end_s']}")
    if m["consistency_missed"]:
        print(f"missed inconsistencies: {', '.join(m['consistency_missed'])}")
    if "--json" in argv and argv.index("--json") + 1 < len(argv):
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps(m, indent=2, default=str))
    bad = nam_metrics.failures(m)
    for line in bad:
        print(f"BELOW THRESHOLD {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
