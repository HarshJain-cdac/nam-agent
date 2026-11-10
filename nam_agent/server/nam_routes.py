"""The NAM screens' routes: /api/nam/*, /api/nit/* and /api/tender/*.

The Procurix NAM screens (need capture, references, estimate, PR, register)
call these. Each runs its work through nam_agent.nam.run(), the same entry
point as POST /v1/nam/run, so the two cannot drift apart. They act as the
officer of the reserved "local" tenant, with the role the payload names; a
deployment whose users must be kept apart calls /v1/nam/* with the identity
headers instead (docs/backend-integration.md).

handle_get and handle_post take the HTTP handler (nam_agent/server/http.py)
and answer True when the path was theirs.
"""

from __future__ import annotations

import dataclasses
import json
import re
from contextlib import nullcontext
from urllib.parse import parse_qs, unquote, urlparse

from nam_agent.config import load_settings
from nam_agent.server.agent import agent_lock, get_agent
from nam_agent.server.chat import case_focus, chat

def nam_capture_route(payload: dict, conn, home, kg_url: str, models) -> tuple[dict, int]:
    """/api/nam/capture's old payload in, its old body out, with the work done
    by nam_agent.nam.run(). Returns (body, HTTP status)."""
    import nam_agent.nam as nam_harness
    from nam_agent.nam import dossier as nam_dossier

    text = (payload.get("text") or "").strip()
    need_id = (payload.get("need_id") or "").strip()
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else None
    skip = bool(payload.get("skip_followups"))
    undo = str(payload.get("undo") or "").strip()
    category = str(payload.get("category") or "").strip()
    if not text and not (need_id and (answer or skip or undo)):
        return {"error": "text is required: state what is to be procured"}, 400
    request: dict = {"system": "Capture this procurement need.",
                     "messages": [{"role": "user", "content": text}]}
    if need_id:
        request["case_id"] = need_id
    if answer:
        request["answer"] = {"question_id": str(answer.get("question_id") or ""),
                             "value": str(answer.get("value") or "")}
    request["options"] = {k: v for k, v in (("skip_questions", skip), ("undo", undo),
                                            ("category", category)) if v}
    ctx = nam_harness.Context(conn=conn, home=home, kg_url=kg_url, models=models)
    # the dashboard is a local dev tool: it acts as the officer of the
    # reserved "local" tenant, which also owns the cases from before tenants
    caller = nam_harness.Caller(tenant="local", user="dashboard", roles=("U1",))
    out = nam_harness.run(request, ctx, caller)
    if "error" in out:
        err = out["error"]
        body = {"error": err["message"], "code": err["code"]}
        if out.get("case_id"):
            body.update(need_id=out["case_id"], stage="capture", next_step=None)
        return body, nam_harness.status_of(out)
    result = out["result"]
    moving = out["status"] == "ok"
    pending = None
    if out["questions"] and out["questions"][0]["id"] != "detail":
        pending = {"narrows": True, **out["questions"][0]}
    return {
        "need_id": out["case_id"],
        "stage": "references" if moving else "capture",
        "next_step": "references" if moving else None,
        "readiness": result["readiness"],
        "item": result["item"],
        "related": result["related"],
        "incidental": result["incidental"],
        "semantic": result["semantic"],
        "tenders_in_graph": result["tenders_in_graph"],
        "followups": {**result["followups"], "pending": pending,
                      "ignored": next((w for w in out["warnings"] if w.startswith("answer to")),
                                      "")},
        "category": result["classification"]["category"],
        "ask_about": result["ask_about"],
        "error": next((w for w in out["warnings"] if "names an item" in w), None),
        "dossier_path": str(nam_dossier.path_for(home, out["case_id"])),
        "dossier": result["dossier"],
    }, 200


# the roles the dashboard may act as (policy/permissions.json): it is a local
# dev tool, so the officer picks the role an approval needs, as the backend's
# auth component would carry it (plan D4)
DASHBOARD_ROLES = tuple(f"U{i}" for i in range(1, 8))


def nam_run_route(payload: dict, conn, home, kg_url: str, models) -> tuple[dict, int]:
    """/api/nam/run: any NAM flow through nam_agent.nam.run(), the same request as
    POST /v1/nam/run plus the role to act as. Returns (the nam.v1 body, HTTP
    status). The NAM screen's steps after need capture use it."""
    import nam_agent.nam as nam_harness

    role = str(payload.get("role") or "U1")
    if role not in DASHBOARD_ROLES:
        return {"error": f"role is one of {', '.join(DASHBOARD_ROLES)}"}, 400
    request: dict = {"system": str(payload.get("system") or "")[:2000],
                     "messages": [{"role": "user", "content": str(payload.get("text") or "")}]}
    for key in ("case_id", "case_version", "answer", "confirm", "options", "documents"):
        if payload.get(key) not in (None, "", {}, []):
            request[key] = payload[key]
    ctx = nam_harness.Context(conn=conn, home=home, kg_url=kg_url, models=models)
    caller = nam_harness.Caller(tenant="local", user=f"dashboard-{role.lower()}", roles=(role,))
    out = nam_harness.run(request, ctx, caller)
    return out, nam_harness.status_of(out)


def nit_view(case_id: str) -> dict | None:
    """The NIT of one case with the user's answers (nam_agent/nam/nit.py). The KG's
    suggestions are read first, outside the agent lock, so a slow KG never
    holds up the chat; the build under the lock then reads them from cache."""
    from nam_agent.nam import need as nam
    from nam_agent.nam import nit

    if not re.fullmatch(r"NAM-\d{4}-\d{4,}", case_id or ""):
        return None
    with agent_lock:
        agent = get_agent()
        need = nam.load(agent.conn, case_id)
    if need is None:
        return None
    nit.suggestions(need, agent.settings.kg_url)
    with agent_lock:
        return nit.build(need, agent.settings.kg_url, agent.conn)


def nit_set_route(case_id: str, payload: dict) -> tuple[dict, int]:
    """POST /api/nit/<case>/field: the sheet's Accept button. Takes the
    suggestion of one row ({"field": id, "value": "accept"}); a typed change
    goes through the chat, so the agent stays the one place answers come in."""
    from nam_agent.nam import nit

    doc = nit_view(case_id)
    if doc is None:
        return {"error": "unknown case"}, 404
    row = nit.find_row(doc, str(payload.get("field") or ""))
    if row is None or row["kind"] != "suggested" or payload.get("value") != "accept":
        return {"error": "only a suggested entry can be accepted here"}, 400
    with agent_lock:
        nit.save(get_agent().conn, case_id, row["id"], row["value"]["text"],
                 f"accepted from {row['value']['source']}")
    return nit_view(case_id), 200


def nam_workflow_view(conn, need_id: str = "") -> tuple[dict, int]:
    """/api/nam/workflow: the S1-S10 stages with the flows of each, and,
    for a case, where it stands: its stage, what the stage still needs, the
    pending approval and the artifacts it holds. The NAM stepper is drawn
    from it, so the screen follows the harness's own stage machine."""
    from nam_agent.nam import journey, orchestrator, permissions, playbooks
    from nam_agent.nam import need as nam_need
    from nam_agent.nam import outputs as nam_outputs

    flows = playbooks.registry()
    stages = [{"id": st.id, "name": st.name, "gates": list(st.gates), "action": st.action,
               "flows": [{"id": f.id, "title": f.title, "built": f.handler is not None}
                         for f in flows.values() if st.id in f.stages]}
              for st in orchestrator.STAGES]
    # the PR flow's phases over the stages (policy/journey.json), each step
    # with what the officer gives there: the screen groups and explains its
    # steps by them, case or no case
    phases = [{"name": ph["name"], "says": ph.get("says", ""), "stages": ph.get("stages", []),
               "steps": [{"id": st["id"], "stage": st["stage"], "name": st["name"],
                          "user": st.get("user", "")} for st in ph.get("steps", [])]}
              for ph in journey.policy().get("phases", [])]
    body: dict = {"stages": stages, "phases": phases, "roles": list(DASHBOARD_ROLES),
                  "approvers": {k: list(v) for section in ("gates", "actions")
                                for k, v in permissions.policy()[section].items()}}
    if not need_id:
        return body, 200
    rec = nam_need.load(conn, need_id)
    if rec is None:
        return {"error": f"no case {need_id}"}, 404
    wf = orchestrator.workflow(rec)
    order = [st.id for st in orchestrator.STAGES]
    at = order.index(wf["stage"])
    for i, st in enumerate(stages):
        st["state"] = ("done" if i < at or wf.get("closed") else
                       "current" if i == at else "ahead")
    body.update(need_id=need_id, version=rec.version, stage=wf["stage"],
                closed=bool(wf.get("closed")), frozen=bool(wf.get("frozen")),
                category=(rec.category or {}).get("value"),
                missing=orchestrator.missing(rec, wf),
                pending=orchestrator.public(wf.get("pending")),
                artifacts=sorted(wf["artifacts"]),
                journey=journey.build(rec),
                # NAM's outputs (nam_agent/nam/outputs.py): which can be downloaded yet
                outputs=nam_outputs.available(rec))
    return body, 200


def entry_chat_words(payload: dict) -> str:
    """The words a Need Assessment capture call should hand to the NAM harness's chat
    instead, or "": a greeting, a question or a lookup typed as a new need,
    or a greeting or question typed while a follow-up question is open (that
    question stays open). An option's value, "I don't know" included, is
    always the capture's."""
    from nam_agent.nam import drive as nam_drive
    from nam_agent.nam import followup as nam_followup
    from nam_agent.tools import intent as intent_rules

    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else None
    if answer is not None:
        typed = str(answer.get("value") or "").strip()
        if typed and typed != nam_followup.UNKNOWN and (
                intent_rules.is_greeting(typed) or "?" in typed):
            return typed
        return ""
    if payload.get("skip_followups") or payload.get("undo"):
        return ""
    words = str(payload.get("text") or "").strip()
    return words if words and nam_drive.entry_kind(words) == "chat" else ""


def handle_get(self) -> bool:
    """GET /api/tender/*, /api/nit/*, /api/nam/*: True when the path was one of them."""
    if self.path.startswith("/api/tender/"):
        # The tender archive: /api/tender/<case>/nit.pdf serves the notice,
        # /api/tender/<case>/documents lists what the case holds.
        #
        # A reference like PUR-MNTPLN-1005566 opens the scanned notice
        # itself, so a citation can be checked.
        from nam_agent.tools import tender_archive

        parts = [unquote(p) for p in self.path.split("?")[0].split("/") if p]
        # ['api', 'tender', '<case>', '<leaf>']
        case_key = parts[2] if len(parts) > 2 else ""
        leaf = parts[3] if len(parts) > 3 else "documents"
        kg_url = getattr(load_settings(), "kg_url", "")

        if leaf == "documents":
            self._send(json.dumps(tender_archive.documents(case_key, kg_url),
                                   default=str).encode(), "application/json")
            return True

        found = tender_archive.best_notice(case_key, kg_url)
        if not found:
            # A miss is a state the UI renders, not an error it toasts.
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({
                "case_key": case_key,
                "error": "no notice on file for this case",
            }).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        data, content_type, filename = found
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        # inline: the point is to READ it in the preview pane. A download
        # would take the officer out of the conversation they are in.
        self.send_header("Content-Disposition",
                          f'inline; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    elif self.path.startswith("/api/nam/case/"):
        # the NAM case of the PR Creation Flow (nam_agent/nam/case.py) as JSON,
        # or its estimate.xlsx / pr.xlsx; the caller's tenant only
        _nam_case_get(self)
    elif self.path.startswith("/api/nit/"):
        # The NIT the chat generated (nam_agent/nam/nit.py): <case>.json is the
        # filled sheet beside the chat; <case>.pdf the PDF, served only once
        # the sheet is ready unless ?draft=1; ?download=1 saves it.
        url = urlparse(self.path)
        name = url.path[len("/api/nit/"):].strip("/")
        query = parse_qs(url.query)
        nit_doc = nit_view(name.removesuffix(".pdf").removesuffix(".json"))
        if nit_doc is None:
            self._send(json.dumps({"error": "unknown case"}).encode(), "application/json",
                       status=404)
            return True
        if not name.endswith(".pdf"):
            self._send(json.dumps(nit_doc, default=str).encode(), "application/json",
                       no_cache=True)
            return True
        from nam_agent.nam import nit

        if not nit_doc["ready"] and not query.get("draft"):
            self._send(json.dumps({"error": "the NIT is not ready: entries are missing or "
                                   "suggestions are not confirmed", "missing":
                                   nit_doc["missing"]}).encode(), "application/json",
                       status=409)
            return True
        data = nit.pdf(nit_doc)
        how = "attachment" if query.get("download") else "inline"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition",
                         f'{how}; filename="NIT-{nit_doc["case_id"]}'
                         f'{"" if nit_doc["ready"] else "-draft"}.pdf"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    elif self.path.startswith("/api/nam/workflow"):
        # the S1-S10 stages and, with need_id, where the case stands
        need_id = (parse_qs(urlparse(self.path).query).get("need_id", [""])[0] or "").strip()
        with agent_lock:
            body, code = nam_workflow_view(get_agent().conn, need_id)
        self._send(json.dumps(body, default=str).encode(), "application/json", status=code)
    elif self.path.startswith("/api/nam/register"):
        # The Procurix Need Assessment register: one row per case with its
        # stage badge, plus the KPI cards (nam_agent/nam/register.py).
        from nam_agent.nam import register as nam_register
        from nam_agent.nam import store as nam_store

        with agent_lock:
            body = nam_register.register(get_agent().conn, nam_store.current().tenant)
        self._send(json.dumps(body, default=str).encode(), "application/json")
    elif self.path.startswith("/api/nam/norms"):
        # The PQC norms the harness reviews criteria against (DRAFT policy).
        from nam_agent.nam.playbooks import review as nam_review

        body = json.loads(nam_review.NORMS.read_text(encoding="utf-8"))
        self._send(json.dumps(body).encode(), "application/json")
    elif self.path.startswith("/api/nam/output"):
        # NAM's outputs as the PR Creation Flow names them, as .xlsx:
        # ?need_id=...&kind=requirement|estimate|pr (nam_agent/nam/outputs.py)
        from nam_agent.nam import need as nam_need
        from nam_agent.nam import outputs as nam_outputs

        q = parse_qs(urlparse(self.path).query)
        with agent_lock:
            rec = nam_need.load(get_agent().conn, (q.get("need_id") or [""])[0])
            if rec is None:
                self._send(json.dumps({"error": "unknown need_id"}).encode(),
                           "application/json", status=404)
                return True
            try:
                name, data = nam_outputs.build(rec, (q.get("kind") or [""])[0])
            except nam_outputs.OutputError as exc:
                self._send(json.dumps({"error": str(exc)}).encode(), "application/json", status=409)
                return True
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument"
                         ".spreadsheetml.sheet")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    elif self.path.startswith("/api/nam/pr_fields"):
        # The PR's fields, where each comes from and which are mandatory
        # (DRAFT policy): the PR screen shows the source of every value.
        from nam_agent.nam.playbooks import pr as nam_pr

        body = json.loads(nam_pr.POLICY.read_text(encoding="utf-8"))
        self._send(json.dumps(body).encode(), "application/json")
    elif self.path.startswith("/api/nam/drafts"):
        # The officer's unsent edits on the Procurix wizard screens.
        from nam_agent.nam import register as nam_register

        need_id = (parse_qs(urlparse(self.path).query).get("need_id", [""])[0] or "").strip()
        with agent_lock:
            body = {"need_id": need_id,
                    "drafts": nam_register.drafts(get_agent().conn, need_id)}
        self._send(json.dumps(body, default=str).encode(), "application/json")
    elif self.path.startswith("/api/nam/needs"):
        # "Your drafts" on the NAM landing screen.
        from nam_agent.nam import need as nam

        with agent_lock:
            agent = get_agent()
            self._send(json.dumps({"needs": nam.recent(agent.conn)},
                                   default=str).encode(), "application/json")
    elif self.path.startswith("/api/nam/dossier"):
        # The need's runtime dossier, as Markdown. Every NAM step adds its
        # section to it, so this is the whole case so far in one read.
        from nam_agent.nam import dossier as nam_dossier
        from nam_agent.nam import need as nam

        need_id = (parse_qs(urlparse(self.path).query).get("need_id", [""])[0] or "").strip()
        with agent_lock:
            agent = get_agent()
            need = nam.load(agent.conn, need_id)
        body = ({"need_id": need_id, "stage": need.stage,
                 "markdown": nam_dossier.read(agent.settings.home, need_id)}
                if need else {"error": "unknown need_id"})
        self._send(json.dumps(body, default=str).encode(), "application/json")
    elif self.path.startswith("/api/nam/need"):
        from nam_agent.nam import need as nam

        need_id = (parse_qs(urlparse(self.path).query).get("need_id", [""])[0] or "").strip()
        with agent_lock:
            agent = get_agent()
            need = nam.load(agent.conn, need_id)
        body = {"need": dataclasses.asdict(need), "ask_about": need.unresolved} \
            if need else {"error": "unknown need_id"}
        self._send(json.dumps(body, default=str).encode(), "application/json")
    else:
        return False
    return True


def _nam_case_get(self) -> None:
    """GET /api/nam/case/<id>[/estimate.xlsx|/pr.xlsx]: the case of the chat's flow."""
    from nam_agent.nam import case as nam_case

    parts = [p for p in self.path.split("?")[0].split("/") if p][3:]
    case_id = parts[0] if parts else ""
    leaf = parts[1] if len(parts) > 1 else ""
    actor, bad = self._actor()
    if bad:
        self._send(json.dumps({"error": bad}).encode(), "application/json", status=401)
        return
    with nam_case.acting(actor) if actor else nullcontext():
        with agent_lock:
            agent = get_agent()
            try:
                data = nam_case.load(agent.conn, case_id)
            except nam_case.NotYours:
                data = None
            home = agent.settings.home
    if data is None:
        self._send(json.dumps({"error": f"no case {case_id}"}).encode(), "application/json",
                   status=404)
        return
    if leaf in ("estimate.xlsx", "pr.xlsx"):
        path = home / "nam" / case_id / leaf
        if not path.is_file():
            self._send(json.dumps({"error": f"{leaf} is not written yet"}).encode(),
                       "application/json", status=404)
            return
        self._send(path.read_bytes(), "application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet")
        return
    from nam_agent.nam import cart

    data["node"] = cart.node_data(data)        # the cart node, for any client
    self._send(json.dumps(data, default=str).encode(), "application/json", no_cache=True)


def handle_post(self, length: int) -> bool:
    """POST /api/nit/<case>/field and /api/nam/<route>: True when the path was one of them."""
    m = re.fullmatch(r"/api/nit/(NAM-\d{4}-\d{4,})/field", self.path)
    if m:
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        body, code = nit_set_route(m.group(1), payload if isinstance(payload, dict) else {})
        self._send(json.dumps(body, default=str).encode(), "application/json", status=code)
        return True
    # ---- Need Assessment Module ------------------------------------
    # NAM is the source the NIT module draws from: capture a need,
    # classify it, retrieve similar past cases, and record which ones the
    # OFFICER selected. The FRS is explicit that downstream documents are
    # built "based on the tender(s) selected by the user, rather than by AI
    # selection alone" — so /select is a real gate, not a formality.
    if self.path.startswith("/api/nam/"):
        from nam_agent.nam import need as nam

        payload = json.loads(self.rfile.read(length) or "{}")
        route = self.path[len("/api/nam/"):].strip("/")
        with agent_lock:
            agent = get_agent()
        kg_url = getattr(agent.settings, "kg_url", "")

        def reply(body: dict, code: int = 200) -> bool:
            self._send(json.dumps(body, default=str).encode(), "application/json",
                       status=code)
            return True

        if route == "edit":
            # A screen's edits as the flow's own structured answer
            # (nam_agent/nam/edits.py), run through nam_agent.nam.run.
            from nam_agent.nam import edits as nam_edits
            from nam_agent.nam import llm as nam_llm

            try:
                models = (nam_llm.Models.from_settings(agent.client, agent.settings)
                          if agent.client is not None else None)
            except ValueError:
                models = None
            role = str(payload.get("role") or "U1")
            with agent_lock:
                need = nam.load(agent.conn, str(payload.get("need_id") or ""))
                if need is None:
                    return reply({"error": "unknown need_id"}, 404)

                def run_one(request: dict) -> dict:
                    body, _ = nam_run_route({**request, "role": role}, agent.conn,
                                            agent.settings.home, kg_url, models)
                    return body
                try:
                    out = nam_edits.apply(need, str(payload.get("kind") or ""), payload,
                                          run_one)
                except nam_edits.EditError as exc:
                    return reply({"error": str(exc)}, 400)
            return reply(out)
        if route == "drafts":
            from nam_agent.nam import register as nam_register

            with agent_lock:
                err = nam_register.save_draft(agent.conn, str(payload.get("need_id") or ""),
                                              str(payload.get("section") or ""),
                                              payload.get("data"))
            return reply({"error": err}, 400) if err else reply({"ok": True})
        if route == "docx":
            # An edited document (Scope, Technical spec, SCC) as .docx.
            from nam_agent.nam import register as nam_register

            try:
                data = nam_register.docx(payload.get("title") or "", payload.get("blocks"))
            except ValueError as exc:
                return reply({"error": str(exc)}, 400)
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload.get("filename") or "document"))
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document")
            self.send_header("Content-Disposition", f'attachment; filename="{name}.docx"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True
        if route == "need":
            text = (payload.get("text") or "").strip()
            if not text:
                return reply({"error": "text is required — one sentence is enough"})
            need = nam.create(agent.conn, text, payload.get("session_id", ""))
            return reply({
                "need": dataclasses.asdict(need),
                # Exactly the fields the chatbot should ask about, and no
                # others: §3.1.2 asks a follow-up "only for whichever field
                # it could not confidently predict".
                "ask_about": need.unresolved,
                "vocabularies": {
                    "category": list(nam.CATEGORIES),
                    "subtype": {k: list(v) for k, v in nam.SUBTYPES.items()},
                    "procurement_type": list(nam.PROCUREMENT_TYPES),
                    "tender_type": list(nam.TENDER_TYPES),
                },
            })

        if route == "run":
            # any NAM flow after need capture, through nam_agent.nam.run
            from nam_agent.nam import llm as nam_llm

            try:
                models = (nam_llm.Models.from_settings(agent.client, agent.settings)
                          if agent.client is not None else None)
            except ValueError:
                models = None
            with agent_lock:
                body, code = nam_run_route(payload, agent.conn, agent.settings.home,
                                           kg_url, models)
            return reply(body, code)

        if route == "capture":
            # Step 1, Need capture, through the NAM harness's one entry
            # point (nam_agent.nam.run), so this route and POST /v1/nam/run
            # cannot drift apart. Pass need_id to refine a need already
            # captured; answer ({question_id, value}) answers the open
            # follow-up question; skip_followups ends them.
            from nam_agent.nam import llm as nam_llm

            words = entry_chat_words(payload)
            if words:
                # "Hii", a question or a lookup is the NAM harness's to answer, in the
                # case's own conversation: never a KG search for the words
                case_id = str(payload.get("need_id") or "")
                turn = chat(words, session_id=str(payload.get("session_id") or
                                                 f"px-{case_id or 'entry'}"),
                            focus=case_focus({"case_id": case_id, "screen": "Need capture"}))
                return reply({"chat": {"reply": turn.get("reply", ""),
                                       "error": turn.get("error")},
                              "need_id": case_id or None})

            try:    # the models per task (NAM_MODELS) over nam_agent's own client
                models = (nam_llm.Models.from_settings(agent.client, agent.settings)
                          if agent.client is not None else None)
            except ValueError:
                models = None
            with agent_lock:
                body, code = nam_capture_route(
                    payload, agent.conn, agent.settings.home, kg_url, models)
            return reply(body, code)

        if route == "classify":
            return reply({"prediction": nam.classify(payload.get("text") or "")})

        if route == "similar":
            need = nam.load(agent.conn, payload.get("need_id") or "")
            if need is None:
                return reply({"error": "unknown need_id"})
            result = nam.similar_cases(need, kg_url,
                                       limit=int(payload.get("limit") or 5))
            need.stage = "matching"
            nam.save(agent.conn, need, reason="dashboard: similar cases searched")
            return reply({"need_id": need.need_id, **result})

        if route == "update":
            need = nam.load(agent.conn, payload.get("need_id") or "")
            if need is None:
                return reply({"error": "unknown need_id"})
            # The rules live in nam.apply_update, where they can be tested;
            # this only carries the answer in and the verdict out.
            problem = nam.apply_update(need, payload)
            if problem:
                return reply({"error": problem})
            nam.save(agent.conn, need, reason="dashboard: officer set fields")
            return reply({"need": dataclasses.asdict(need),
                          "ask_about": need.unresolved})

        if route == "reference":
            return reply(nam.lookup_reference(payload.get("ref") or "", kg_url))

        if route == "replicate":
            key = (payload.get("case_key") or "").strip()
            if not key:
                return reply({"error": "case_key is required"})
            need = nam.replicate(agent.conn, key, kg_url, payload.get("session_id", ""))
            if need is None:
                return reply({"error": f"could not read tender {key} from the repository"})
            return reply({"need": dataclasses.asdict(need), "ask_about": need.unresolved})

        if route == "case":
            key = (payload.get("case_key") or "").strip()
            if not key:
                return reply({"error": "case_key is required"})
            return reply(nam.case_detail(key, kg_url))

        if route == "compare":
            keys = payload.get("case_keys") or []
            if len(keys) < 2:
                return reply({"error": "compare needs at least two case_keys"})
            return reply(nam.compare([str(k) for k in keys], kg_url))

        if route == "material":
            # What the officer's SELECTED tenders contribute — the only
            # input generation is allowed to read.
            need = nam.load(agent.conn, payload.get("need_id") or "")
            if need is None:
                return reply({"error": "unknown need_id"})
            return reply({"need_id": need.need_id,
                          **nam.selected_material(need, kg_url)})

        if route == "select":
            need = nam.load(agent.conn, payload.get("need_id") or "")
            if need is None:
                return reply({"error": "unknown need_id"})
            chosen = payload.get("case_keys") or []
            if isinstance(chosen, str):
                chosen = [chosen]
            need.selected_cases = [str(c) for c in chosen if c]
            need.stage = "selected" if need.selected_cases else "matching"
            nam.save(agent.conn, need, reason="dashboard: officer selected precedent")
            return reply({
                "need_id": need.need_id,
                "selected_cases": need.selected_cases,
                "stage": need.stage,
                "note": ("Documents will be built from these tender(s) only."
                         if need.selected_cases else
                         "Nothing selected — nothing will be generated from precedent."),
            })

        return reply({"error": f"unknown NAM route '{route}'"})
    return False
