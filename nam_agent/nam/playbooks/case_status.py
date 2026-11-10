"""case.status: where a case stands, for any role that may see it.

Reads the Need Record (nam_agent/nam/store.py) and writes nothing: the stage, what
is selected, the question still open, the approvals given and pending, what
the stage still needs, and the journey (nam_agent/nam/journey.py): what was done,
the step now, what the user still has to give and what comes next.
"""

from __future__ import annotations

from nam_agent.nam import journey, record, store
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    row = store.case_row(ctx.conn, case_id)
    if row is None:
        raise NamError("not_found", f"no case {case_id}")
    need = row["record"]
    pending = (need.get("followups") or {}).get("pending")
    result = {
        "stage": row["stage"],
        "need_stage": str(need.get("stage") or ""),
        "requirement": str(need.get("raw_text") or ""),
        "category": (need.get("category") or {}).get("value"),
        "selected_cases": list(need.get("selected_cases") or []),
        "related_cases": len(need.get("related_cases") or []),
        "open_question": ({"id": str(pending.get("id") or ""),
                           "text": str(pending.get("text") or "")} if pending else None),
        **record.progress(need),
        "updated": {"at": row["updated_at"], "by": row["updated_by"]},
    }
    rec = need_store.load(ctx.conn, case_id)
    if rec is not None:
        j = journey.build(rec)
        result["journey"] = {k: j[k] for k in ("phase", "previous_step", "step", "next_step",
                                               "next_stage", "to_give")}
        result["journey"]["text"] = journey.text(j)
    return Outcome(case_id=case_id, status="ok", result=result)
