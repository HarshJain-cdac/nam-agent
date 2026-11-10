"""One NAM harness turn: config -> db -> tools -> memory -> session -> loop.

The server (nam_agent/server/) calls `respond()` once per chat message.
"""

from __future__ import annotations

from nam_agent.config import Settings, load_settings
from nam_agent.db import connect
from nam_agent.loop.agent import LoopResult, Observer, run_loop
from nam_agent.loop.models import get_client
from nam_agent.runtime.tracing import Tracer, compose
from nam_agent.runtime.session import Session
from nam_agent.tools import build_registry
from nam_agent.tools import intent as intent_rules


class NamAgent:
    def __init__(self, settings: Settings | None = None, client=None, conn=None):
        # `client` and `conn` are injectable: evals swap in a scripted model,
        # the server injects a cross-thread connection.
        self.settings = settings or load_settings()
        self.settings.ensure_home()
        self.conn = conn or connect(self.settings.home)
        self.client = client or get_client(self.settings)

        # Memory first: the memory-management tools need it.
        from nam_agent.memory import Memory

        self.memory = Memory(self.conn, self.settings, self.client)
        self.tools = build_registry(self.conn, self.settings, self.memory)
        self.session = Session(self.settings, memory=self.memory)
        self.tracer = Tracer(self.settings)
        # the domain check's model (nam_agent/nam/domain.py), built on first use,
        # and the last turn's system prompt, for the check's second reply
        self._nam_domain = None
        # the earlier NAM chat driver (NAM_CHAT=driver, nam_agent/nam/drive.py):
        # its follow-up writer and intent reader, built on first use, and the
        # question each chat has open, kept in state.db (nam_agent/nam/chatstate.py)
        from nam_agent.nam import chatstate as _nam_chatstate

        self._nam_ask = None
        self._nam_intent = None
        self._nam_awaiting = _nam_chatstate.Awaiting(self.conn)
        self._last_system = ""

    def close(self) -> None:
        """Release the agent's database connection."""
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - already closed
            pass

    def respond(self, user_message: str, observer: Observer | None = None,
                source: str = "cli", stream: bool = False, actor=None) -> LoopResult:
        """One full turn: assemble working memory → run the loop → persist.
        `source` tags where the message came from (api, or dashboard for the
        server's own chat page).
        `stream=True` streams the reply text token by token to the observer.
        Everything that happens is both shown (observer) and recorded (tracer).
        `actor` is the trusted identity context the caller was given
        (nam_agent/nam/identity.py); None is this machine's own officer."""
        # what the turn decided, kept with it so a reopened chat shows it again
        import time
        captured: dict = {}

        def _capture(kind, ev):
            if kind == "gate":
                captured["gate"] = {"decision": ev.get("decision"), "reason": ev.get("reason")}
            if kind == "domain":
                captured["domain"] = {"domain": ev.get("domain"), "reason": ev.get("reason")}
            # what the page shows beside the reply, kept with the turn so a
            # reopened chat shows it again: the NAM cards and the question
            if kind == "nam_cards":
                captured.setdefault("cards", []).append(ev)
            if kind == "ask_options":
                captured["ask"] = ev
        notify = compose(observer, self.tracer.event, _capture)
        t0 = time.perf_counter()
        # the chat this turn runs in, and who acts in it, for the NAM tools
        from contextlib import nullcontext

        from nam_agent.nam import case, chatstate

        chat = chatstate.enter(self.session.session_id, user_message)
        emitter = chatstate.set_emitter(notify)
        try:
            with case.acting(actor) if actor is not None else nullcontext():
                return self._respond(user_message, source, stream, notify, captured, t0)
        finally:
            chatstate.reset_emitter(emitter)
            chatstate.leave(chat)

    def _respond(self, user_message: str, source: str, stream: bool, notify, captured: dict,
                 t0: float) -> LoopResult:
        import time

        with self.tracer.turn(user_message):
            # THE INTENT NODE — what kind of message is this, decided before
            # anything reaches the network. It costs no model call (see
            # nam_agent/tools/intent.py for why that is the point on this
            # deployment), and it is what the harness diagram's Intent box and
            # KG block light on.
            #
            # Only a confident greeting short-circuits. Everything else is
            # LABELLED and falls through untouched, so this node can remove
            # latency and can never remove capability.
            result = None
            in_conversation = any(m.get("role") == "assistant"
                                  for m in self.session.history[-2 * self.settings.history_turns:])
            intent, intent_reason = intent_rules.classify(user_message, in_conversation)
            notify("intent", {"intent": intent, "reason": intent_reason})
            captured["intent"] = {"intent": intent, "reason": intent_reason}

            if intent == intent_rules.GREETING and self.settings.fast_greetings:
                reply_text = intent_rules.greeting_reply(user_message)
                if stream:
                    # Same event shape the loop streams, so every caller
                    # renders it identically — it simply arrives all at once.
                    notify("text", {"delta": reply_text})
                result = LoopResult(reply=reply_text, tool_calls=[], iterations=0)

            # THE EARLIER NAM DRIVER, when this chat is set to it
            # (NAM_CHAT=driver): a stated need or an answer to need
            # capture's open question runs in code (nam_agent/nam/drive.py). The
            # default is the PR Creation Flow, which the agent works itself.
            if result is None and self._nam_driver_on():
                result = self._drive_nam(user_message, notify, stream)

            if result is None:
                result = self._run_full_turn(user_message, notify, stream)
                # the agent stays on the work (nam_agent/nam/domain.py): a reply that
                # answered something else from general knowledge is written again
                result = self._domain_review(user_message, result, notify)
                # a side question mid-case: its answer, then the case's open
                # question again with its checked options (nam_agent/nam/stage_tools.py)
                self._nam_reminder(result, notify, stream)
                # the buttons show the options: a copy the model typed goes; and a
                # list of material codes offered as choices never reaches the officer
                if self.session.nam_guide and not self._nam_driver_on():
                    from nam_agent.nam import case as nam_case
                    from nam_agent.nam import stage_tools

                    if captured.get("ask"):
                        result.reply = stage_tools.drop_typed_options(result.reply,
                                                                      captured["ask"])
                    try:
                        data = nam_case.for_session(self.conn, self.session.session_id)
                    except Exception:  # noqa: BLE001 - the reply stands as written
                        data = None
                    now = nam_case.current_item(data) if data else None
                    if now is not None:
                        codes = {c.get("code") or "" for c in now.get("candidates") or []}
                        result.reply = stage_tools.drop_code_lists(result.reply, codes)


            def _status(out: str) -> str:
                low = (out or "").lower()
                return "error" if ("failed" in low or "timed out" in low
                                   or low.startswith("error")) else "ok"
            meta = {
                "gate": captured.get("gate"),
                "domain": captured.get("domain"),
                "iterations": result.iterations,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "tools": [{"tool": c["tool"], "status": _status(c["output"])}
                          for c in result.tool_calls],
                # which model answered this turn, shown again in a reopened chat
                "model": self.settings.model,
                "provider": self.settings.provider,
                "cards": captured.get("cards"),
                "ask": captured.get("ask"),
            }
            self.session.add_exchange(user_message, result.reply, tool_calls=result.tool_calls,
                                      source=source, meta=meta)
            if self.memory is not None:
                from nam_agent.nam import chatstate as _cs

                # one officer's chat never feeds the shared memory where NAM
                # runs (NAM_CHAT_MEMORY): its own history and case are its memory
                if _cs.shared_memory(self.settings):
                    self.memory.maybe_consolidate(notify=notify)
                    self.memory.export_markdown()   # keep MEMORY.md in sync

        self.tracer.end_turn(result.reply, result.iterations)
        return result

    def _domain_review(self, user_message: str, result: LoopResult, notify) -> LoopResult:
        """The turn's reply, or the agent's second reply when the check found
        the first answered a request that is not procurement work. The agent
        writes both; the harness only checks (nam_agent/nam/domain.py)."""
        from nam_agent.nam import domain

        if not domain.enabled(getattr(self.settings, "kg_url", ""),
                              getattr(self.settings, "domain_only", None)):
            return result
        try:
            if self._nam_domain is None:
                from nam_agent.nam import llm as nam_llm

                self._nam_domain = nam_llm.client_asker(
                    getattr(self.memory, "client", None) or self.client, self.settings,
                    self.conn, "domain") or False
            verdict, why = domain.review(user_message, result.reply, result.tool_calls,
                                         self._nam_domain or None)
            notify("domain", {"domain": verdict, "reason": why})
            if verdict != domain.STRAYED or not self._last_system:
                return result
            window = self.settings.history_turns * 2
            messages = self.session.history[-window:] + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": result.reply},
                {"role": "user", "content": domain.REDO}]
            # not streamed: the screens replace the streamed text with the
            # turn's final reply
            again = run_loop(client=self.client, model=self.settings.model,
                             system=self._last_system, messages=messages, tools=self.tools,
                             max_iterations=self.settings.max_iterations,
                             max_tokens=self.settings.max_tokens, observer=notify,
                             stream=False)
        except Exception as exc:  # noqa: BLE001 - the check never loses a reply
            notify("domain", {"domain": "error", "reason": repr(exc)[:160]})
            return result
        if not (again.reply or "").strip():
            return result
        from nam_agent.nam import answer_data

        again.reply = answer_data.complete(again.reply, again.tool_calls)
        again.iterations += result.iterations
        return again

    def _nam_reminder(self, result: LoopResult, notify, stream: bool) -> None:
        """Ask the case's open question again after a turn that asked nothing."""
        if self._nam_driver_on():
            self._nam_driver_reminder(result, notify, stream)
            return
        from nam_agent.nam import chatstate

        # a question with its buttons was shown this turn: nothing to repeat.
        # A turn whose question was refused, or that only typed one, gets the
        # case's open question again with its buttons
        if chatstate.asked() or "nam_ask" not in {t["name"] for t in self.tools.schemas()}:
            return
        from nam_agent.nam import stage_tools

        try:
            back = stage_tools.reminder(self.conn, self.session.session_id)
        except Exception:  # noqa: BLE001 - a reminder is a courtesy, the reply stands
            return
        if back is None:
            return
        text, ask = back
        tail = [ln for ln in (result.reply or "").splitlines() if ln.strip()][-8:]
        asks_itself = any("?" in ln for ln in tail) and not (result.reply or "").startswith(
            "(I hit my iteration limit")
        # a reply that asks the question itself gets only its buttons, not the
        # question a second time
        extra = "" if asks_itself else f"\n\nBack to the need: {text}"
        if (result.reply or "").startswith("(I hit my iteration limit"):
            # the agent ran out of steps on the need: the officer gets its open
            # question with its buttons, not the loop's limit
            result.reply, extra = "", f"Let me ask that again: {text}"
        result.reply = (result.reply or "") + extra
        if asks_itself:
            from nam_agent.nam import stage_tools as _st

            result.reply = _st.drop_typed_options(result.reply, ask)
        result.tool_calls = [*result.tool_calls, {
            "tool": "ask_options", "args": ask,
            "output": f"Asked the user again: \"{text}\". Waiting for the answer."}]
        if stream and extra:
            notify("text", {"delta": extra})
        notify("ask_options", ask)

    def _nam_driver_reminder(self, result: LoopResult, notify, stream: bool) -> None:
        """The earlier driver's reminder: its open question (drive.reminder)."""
        if not getattr(self.settings, "kg_url", "") or any(
                c.get("tool") == "ask_options" for c in result.tool_calls):
            return              # no harness, or the model asked something itself
        from nam_agent.nam import drive, sap_need

        focus = getattr(self.session, "focus", None) or {}
        try:
            if sap_need.source(getattr(self.settings, "nam_source", None)) == "sap":
                back = sap_need.reminder(self.conn, self.session.session_id)
            else:
                back = drive.reminder(self.conn, self.session.session_id,
                                      str(focus.get("case_id") or ""), self._nam_awaiting)
        except Exception:  # noqa: BLE001 - a reminder is a courtesy, the reply stands
            return
        if back is None:
            return
        text, ask = back
        extra = (f"\n\nBack to the need: {text}" if ask.get("field") == "sap"
                 else f"\n\nBack to the case: {text}")
        result.reply = (result.reply or "") + extra
        result.tool_calls = [*result.tool_calls, {
            "tool": "ask_options", "args": ask,
            "output": f"Asked the user again: \"{ask['question']}\". Waiting for the answer."}]
        if stream:
            notify("text", {"delta": extra})
        notify("ask_options", ask)

    def _drive_nam(self, user_message: str, notify, stream: bool) -> LoopResult | None:
        """One NAM capture step run by the harness, or None for the model."""
        if not getattr(self.settings, "kg_url", ""):
            return None
        from nam_agent.nam import kg_tools, sap_need

        sap = sap_need.source(getattr(self.settings, "nam_source", None)) == "sap"
        only = kg_tools.allowed(getattr(self.settings, "kg_tools", ""))
        if not sap and only is not None and "nam_capture_need" not in only:
            return None             # the KG tools under test do not include NAM's
        from nam_agent.nam import drive
        from nam_agent.nam import llm as nam_llm

        try:
            client = getattr(self.memory, "client", None)
            if self._nam_ask is None:
                self._nam_ask = nam_llm.client_asker(client, self.settings, self.conn,
                                                     "followup") or False
            if sap:
                # need capture on SAP's lines: the material, not the tender
                out = sap_need.turn(self.conn, self.session.session_id,
                                    self.settings.kg_url, user_message,
                                    ask_model=self._nam_ask or None)
                # a question of the officer's own mid-way: the agent answers
                # it knowing which materials are still in
                self.session.sap_open = (sap_need.context(self.conn, self.session.session_id)
                                         if out is None else None)
                return self._nam_out(out, notify, stream)
            if self._nam_intent is None:
                # what an in-flow message means, read with the case's context
                self._nam_intent = nam_llm.client_asker(client, self.settings, self.conn,
                                                        "intent") or False
            focus = getattr(self.session, "focus", None) or {}
            from nam_agent.runtime.session import strip_tools_note

            recent = [(m["role"], strip_tools_note(m.get("content") or ""))
                      for m in self.session.history[-12:]]
            out = drive.turn(self.conn, self.settings.home, self.settings.kg_url,
                             self.session.session_id, user_message,
                             ask_model=self._nam_ask or None, awaiting=self._nam_awaiting,
                             case_id=str(focus.get("case_id") or ""),
                             intent_ask=self._nam_intent or None, history=recent)
        except Exception as exc:  # noqa: BLE001 - the driver must never break a turn
            notify("nam_driver", {"error": repr(exc)})
            return None
        return self._nam_out(out, notify, stream)

    def _nam_out(self, out: dict | None, notify, stream: bool) -> LoopResult | None:
        """A harness step's result as the turn's, its tool calls and question shown."""
        if out is None:
            return None
        # the diagram lights the NAM harness: this turn was the harness's
        notify("nam", {"by": "driver", "step": out["tool_calls"][0]["tool"]
                       if out["tool_calls"] else ""})
        for call in out["tool_calls"]:
            notify("tool_start", {"tool": call["tool"]})
            notify("tool", call)
        if out["ask"]:
            notify("ask_options", out["ask"])
        if stream:
            notify("text", {"delta": out["reply"]})
        return LoopResult(reply=out["reply"], tool_calls=out["tool_calls"], iterations=0)

    def _case_focus(self) -> dict | None:
        """The NAM case this conversation opened last, as Session.focus, or
        None. Never breaks a turn: no case, or no NAM tables, is just None."""
        if getattr(self.session, "focus", None):
            return None
        from nam_agent.nam import drive, journey

        try:
            case_id = drive.session_case(self.conn, self.session.session_id)
            return journey.focus(self.conn, case_id) if case_id else None
        except Exception:  # noqa: BLE001 - context is a courtesy, the turn runs without it
            return None

    def _nam_click(self, user_message: str, notify) -> dict | None:
        """The officer's click on a choice the harness showed, read with its
        tool (stage_tools.read_click), or None. Never breaks a turn."""
        if self._nam_driver_on() or "nam_answer" not in {t["name"] for t in self.tools.schemas()}:
            return None
        from nam_agent.nam import chatstate, stage_tools

        try:
            found = stage_tools.read_click(self.conn, self.session.session_id, user_message)
        except Exception:  # noqa: BLE001 - the agent reads the message itself
            return None
        if found is None:
            return None
        name, args = found
        notify("tool_start", {"tool": name})
        token = chatstate.reading_set(name)        # the officer's own choice, read by the harness
        try:
            output = self.tools.execute(name, args, notify)
        finally:
            chatstate.reading_reset(token)
        chatstate.mark_clicked(name)
        call = {"tool": name, "args": args, "output": output}
        notify("tool", call)
        return call

    def _nam_driver_on(self) -> bool:
        """True when this the NAM harness's chat runs the earlier NAM driver
        (NAM_CHAT=driver) instead of the PR Creation Flow."""
        return (getattr(self.settings, "nam_chat", "") == "driver"
                and bool(getattr(self.settings, "kg_url", "")))

    def _nam_guide(self) -> str | None:
        """The PR Creation Flow and this chat's case, for the agent's
        instructions (nam_agent/nam/flow.py), or None where NAM is not set up.
        Never breaks a turn: an unreadable case is just the guide."""
        if not getattr(self.settings, "kg_url", "") or "nam_need" not in {
                t["name"] for t in self.tools.schemas()}:
            return None
        from nam_agent.nam import case, chatstate, flow

        try:
            data = case.for_session(self.conn, self.session.session_id)
        except Exception:  # noqa: BLE001 - the guide stands without the case
            data = None
        # an open case's questions carry the records' options, never made-up
        # ones: the generic ask_options steps aside (nam_agent/tools/clarify.py)
        # ...while the records are being narrowed; at the PR, a question of the
        # officer's own choosing (one PR or several) may take buttons
        chatstate.set_case_open(data is not None and flow.stage_of(data)[0] in
                                ("1a", "1b", "1c", "1d", "1e", "1f"))
        return flow.guide(data, kg="kg_tender_record" in {t["name"] for t in self.tools.schemas()})

    def _run_full_turn(self, user_message: str, notify, stream: bool) -> LoopResult:
        """The turn: assemble working memory, run the loop."""
        # the PR Creation Flow and this chat's case are in view every turn: the
        # chat's memory of where the need stands (nam_agent/nam/flow.py)
        # a click on one of the harness's own choices is read before the
        # model's turn, so it never depends on the model calling the tool
        clicked = self._nam_click(user_message, notify)
        self.session.nam_guide = None if self._nam_driver_on() else self._nam_guide()
        if clicked and self.session.nam_guide:
            self.session.nam_guide += (
                f"\nThe officer clicked a choice, and the harness has already read it with "
                f"{clicked['tool']}:\n{clicked['output'][:2500]}\nGo on from there (the next "
                f"question, the pick or the next item); do not read the same choice again.")
        # with the earlier driver, the harness case this chat opened is in view
        # for the turn, unless a Procurix screen already named one
        own = self._case_focus() if self._nam_driver_on() else None
        if own:
            self.session.focus = own
        try:
            system = self.session.build_system(user_message, notify=notify)
        finally:
            if own:
                self.session.focus = None
        self._last_system = system
        # Working memory is a bounded window: only the last N turns (2 rows
        # each) enter the prompt, so context/cost/latency stay flat no matter
        # how long the conversation runs. Older turns live in state.db and
        # come back via the retrieval gate + episodic memory when relevant.
        window = self.settings.history_turns * 2
        messages = self.session.history[-window:] + [{"role": "user", "content": user_message}]

        result = run_loop(
            client=self.client,
            model=self.settings.model,
            system=system,
            messages=messages,
            tools=self.tools,
            max_iterations=self.settings.max_iterations,
            max_tokens=self.settings.max_tokens,
            observer=notify,
            stream=stream,
        )
        # the KG data in the answer must not depend on which model wrote it
        # (nam_agent/nam/answer_data.py): what the reply left out is added in code
        from nam_agent.nam import answer_data

        if clicked:
            result.tool_calls = [clicked, *result.tool_calls]
        if self.session.nam_guide:
            # a reply that says a PR was confirmed or a PO made, when the case
            # does not bear it out, gets the case's own word under it
            from nam_agent.nam import stage_tools

            try:
                result.reply = stage_tools.check_reply(self.conn, self.session.session_id,
                                                       result.reply, result.tool_calls)
            except Exception:  # noqa: BLE001 - the check is a courtesy, the reply stands
                pass
        result.reply = answer_data.complete(result.reply, result.tool_calls,
                                            case_open=bool(self.session.nam_guide)
                                            and "Open case in this chat" in self.session.nam_guide)
        return result
