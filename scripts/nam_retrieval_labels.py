"""Build the retrieval eval's labelled set (plan Step 13) from the live KG.

    python scripts/nam_retrieval_labels.py --kg http://127.0.0.1:8077 \
        --cases-dir /path/to/nrl_kg_data/Goods --cases-dir /path/to/nrl_kg_data/Service \
        [--n 60] [--write]

Each case folder named PUR-... is a past tender. For a fixed, evenly spaced
sample of them the KG is asked for the tender's job name (/api/kg/case),
and the label is (a short subject phrasing of that name, the case key):
"Supply of Spiral Wound Gaskets for CDU 2024-25" is asked as "spiral wound
gaskets cdu". The metric then asks the KG's tender search for each phrasing
and counts the tenders found in the first five (nam_metrics.retrieval).

This is known-item retrieval: it measures whether a tender is found by its
own subject in other words, not whether the best precedent for a new need
ranks first. A set labelled by officers can replace it in the same file.

Without --write it prints the labels; with it, it writes
evals/deterministic/nam/retrieval_labels.json. Only the KG named by --kg is
called.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "evals" / "deterministic" / "nam" / \
    "retrieval_labels.json"
_CASE = re.compile(r"^PUR-[A-Z0-9-]+$")
# the words around a subject that say nothing about what is bought
_NOISE = {"supply", "supplies", "of", "for", "the", "and", "to", "at", "in", "on", "with",
          "annual", "rate", "contract", "arc", "procurement", "purchase", "providing",
          "hiring", "nrl", "numaligarh", "refinery", "limited", "ltd", "tender", "work",
          "works", "job", "services", "service", "period", "year", "years", "months"}


def subject(job_name: str, words: int = 4) -> str:
    """A short phrasing of a job name: its first content words, lowercased,
    without years, numbers and the words every tender has."""
    tokens = [t for t in re.findall(r"[a-z][a-z-]+", job_name.lower()) if t not in _NOISE]
    return " ".join(tokens[:words])


def case_keys(dirs: list[Path]) -> list[str]:
    keys = set()
    for d in dirs:
        for p in d.glob("*/*"):
            if p.is_dir() and _CASE.match(p.name):
                keys.add(p.name)
        for p in d.glob("*"):
            if p.is_dir() and _CASE.match(p.name):
                keys.add(p.name)
    return sorted(keys)


def _get(kg: str, path: str, params: dict) -> dict:
    url = f"{kg.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read())


def build(kg: str, keys: list[str], n: int) -> tuple[list[dict], list[str]]:
    step = max(1, len(keys) // n)
    labels, skipped = [], []
    for key in keys[::step][:n]:
        try:
            data = _get(kg, "/api/kg/case", {"case_key": key, "clause_limit": 1,
                                             "item_limit": 1})
        except OSError as exc:
            skipped.append(f"{key}: {exc}")
            continue
        if data.get("error"):
            skipped.append(f"{key}: {str(data['error'])[:120]}")
            continue
        phrase = subject(str(data.get("job_name") or ""))
        if len(phrase.split()) < 2:
            skipped.append(f"{key}: job name too short to phrase ({data.get('job_name')!r})")
            continue
        labels.append({"query": phrase, "case_key": key,
                       "job_name": str(data.get("job_name"))[:300]})
    return labels, skipped


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kg", required=True, help="the KG precedent API, e.g. http://127.0.0.1:8077")
    ap.add_argument("--cases-dir", action="append", type=Path, required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    keys = case_keys(args.cases_dir)
    if not keys:
        print("no PUR-... case folders found", file=sys.stderr)
        return 1
    labels, skipped = build(args.kg, keys, args.n)
    for s in skipped:
        print(f"skipped {s}", file=sys.stderr)
    if not labels:
        print("no labels: is the KG's graph reachable?", file=sys.stderr)
        return 1
    body = {"about": json.loads(OUT.read_text(encoding="utf-8"))["about"],
            "built": datetime.now(UTC).isoformat(timespec="seconds"), "labels": labels}
    if args.write:
        OUT.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {len(labels)} labels to {OUT}")
    else:
        print(json.dumps(body, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
