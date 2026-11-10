"""How far the chat takes a NAM case, for now.

The PR Creation Flow runs from the need to a submitted PR (steps 1a-3b). The
work today is its first part: from what the officer asks for to the past
tender that matches it. Until the matching is right, the later questions
(the tender type, codified or not, quantities, the estimate, the PR) only get
in the way, so the chat stops at the matching tender:

  NAM_STOP_AT=tender   (the default) need capture asks only what tells
                            the past tenders apart, then shows the tender(s)
                            left; no classification, detail, quantity,
                            estimate or PR question is asked
  NAM_STOP_AT=pr       the whole flow, to the submitted PR

Nothing of the later steps is removed: their screens and API calls still
work; the chat just does not lead the officer into them.
"""

from __future__ import annotations

import os

STAGES = ("tender", "pr")

END = ("That is where NAM stops for now: the matching tender. Ask for its PR number or "
       "files, or describe another need.")


def stop_at() -> str:
    """The stage the chat stops at, read at call time ("tender" or "pr")."""
    value = os.environ.get("NAM_STOP_AT", "tender").strip().lower()
    return value if value in STAGES else "tender"


def tender_only() -> bool:
    return stop_at() == "tender"
