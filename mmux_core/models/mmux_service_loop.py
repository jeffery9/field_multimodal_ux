# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""The agent loop: scratchpad persistence, observation/step/apply/confirm
and the two public turn entry points.
This is the only cluster with an internal back-reference, which is why
its residents are kept together instead of split further.
"""
import json
import logging

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

from odoo import api, models

from ..services.mmux_core import (
    ExecutionGuard, ExecStatus, GuardError,
    IntentParser, NORMALIZED_LANG,
    PROVIDER_REGISTRY,
    ProviderError,
    Plan, PlanStep, PlanOrchestrator, PlanError,
    PLAN_LIBRARY, validate_llm_plan,
    TaskFlowEngine,
    Task, TASK_ENGINE, build_loop_state, loop_label,
    AgentPick, validate_pick, fallback_pick,
    SOURCE_LLM, SOURCE_FALLBACK, SOURCE_EMPTY,
    AgentAction, AgentObservation, AgentScratchpad, AgentTurn,
    validate_action, build_loop_prompt, step_from_action,
    execution_request, run_loop, fold_turn_progress,
    MAX_PROMPT_TURNS,
    AGENT_ACTIONS, LOOP_STATES,
    LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE, LOOP_SOURCE_NONE,
    ContextSignal, ScenarioResolution, validate_scenario_choice,
    choice_from_answer, resolve_scenario, build_scenario_prompt,
    build_ask_question,
    unresolved_with_all,
    SOURCE_SIGNAL, SOURCE_AMBIGUOUS, SOURCE_NONE, SOURCE_USER,
    CHOICE_SOURCE_LLM,
    idempotency_key,
    normalize_lang, resolve_text, detect_languages,
    mixed_language_hint,
)

#: Bound on the persisted conversation history in a loop blob. The pure
#: core caps the *prompt window* (MAX_PROMPT_TURNS); the blob keeps a
#: little more so a resumed session can rebuild context even after a
#: rejected turn. Both bounds exist because unbounded memory in a
#: prompt is a token leak waiting to happen (docs/CONFIG.md §14.4).
MAX_AGENT_HISTORY = MAX_PROMPT_TURNS * 3


class MmuxService(models.AbstractModel):
    _inherit = "mmux.service"

    # -------------------------------------------------------------- #
    # transcribe
    # -------------------------------------------------------------- #
    # ------------------------------------------------------------------ #
    # high-level entry points (called by controllers)
    # ------------------------------------------------------------------ #
    @api.model
    def transcribe(self, audio_data_b64, filename="", text_hint=None,
                   lang=None):
        config = dict(self._provider_config())
        # ASR language follows the caller's hint, else the user's language;
        # auto-detection is left to the provider when unset (mixed speech).
        config["mmux.asr.language"] = normalize_lang(lang or self.env.user.lang)
        provider = config.get("mmux.asr.provider") or "mock"
        payload = {"audio_b64": audio_data_b64, "filename": filename,
                   "text_hint": text_hint}
        try:
            return PROVIDER_REGISTRY.call("asr", provider, payload, config)
        except ProviderError as exc:
            return {"error": str(exc), "fallback": "text"}

    # -------------------------------------------------------------- #
    # analyze_image
    # -------------------------------------------------------------- #
    @api.model
    def analyze_image(self, image_b64, purpose="ocr", instruction=None):
        config = self._provider_config()
        provider = (config.get("mmux.ocr.provider") if purpose == "ocr"
                    else config.get("mmux.vision.provider")) or "mock"
        payload = {"image_b64": image_b64, "purpose": purpose,
                   "instruction": instruction or "Describe and extract structured data."}
        try:
            return PROVIDER_REGISTRY.call("vision" if provider != "mock" else "llm",
                                          provider, payload, config)
        except ProviderError as exc:
            return {"error": str(exc)}

    # -------------------------------------------------------------- #
    # parse_intent
    # -------------------------------------------------------------- #
    @api.model
    def parse_intent(self, text, context=None, langs=None):
        """LLM-first semantic recognition — the ONLY parsing path.

        Flow: send the whole utterance (however it mixes languages) to the
        LLM, which returns strict JSON with a canonical **English** rewrite
        (`normalized_text`), a whitelisted `capability_id`, and `params`.
        The output is validated server-side; execution still happens on
        capability_id + params only.

        `langs` is accepted for backward compatibility but IGNORED — the
        LLM handles all languages and produces one canonical language.

        Returns either an intent dict or a structured error dict
        ({code: "llm_unavailable", retryable, ...}). A valid LLM answer
        that maps to no capability yields an EMPTY intent (not an error).
        """
        context = context or {}
        if not text or not str(text).strip():
            return {"error": _("empty input")}

        config = self._provider_config()
        llm_provider = config.get("mmux.llm.provider")
        if not llm_provider:
            return self.llm_unavailable(
                _("no LLM provider configured (mmux.llm.provider)"),
                retryable=False, subtag="not_configured")
        if llm_provider != "mock" and not config.get("mmux.llm.api_key"):
            return self.llm_unavailable(
                _("LLM provider %s has no api_key configured") % llm_provider,
                retryable=False, subtag="not_configured")

        detected = detect_languages(text)
        lang_hint = mixed_language_hint(text)
        parser = self._parser()
        prompt = self._build_llm_prompt(text, context, detected)

        try:
            raw = PROVIDER_REGISTRY.call("llm", llm_provider,
                                         {"messages": prompt}, config)
        except ProviderError as exc:
            retryable = bool(getattr(exc, "retryable", False)
                             or getattr(exc, "timeout", False))
            subtag = "timeout" if getattr(exc, "timeout", False) else "provider_error"
            _logger.info("LLM intent parsing failed: %s", exc)
            return self.llm_unavailable(exc, retryable=retryable, subtag=subtag)

        data = raw.get("data") if isinstance(raw, dict) else None
        if data is None and isinstance(raw, dict) and isinstance(raw.get("raw"), str):
            try:
                data = json.loads(raw["raw"])
            except (json.JSONDecodeError, ValueError):
                return self.llm_unavailable(
                    _("LLM returned invalid JSON"), retryable=True,
                    subtag="invalid_json")
        if not isinstance(data, dict):
            return self.llm_unavailable(
                _("LLM output is not a JSON object"), retryable=True,
                subtag="invalid_json")

        intent = parser.validate_llm_output(
            data, text, normalized_lang=NORMALIZED_LANG,
            source_lang_hint=lang_hint)
        intent.languages = list(detected)
        self._log_intent(intent, raw_text=text, lang_hint=lang_hint)
        return intent.to_dict()

    # -------------------------------------------------------------- #
    # _build_llm_prompt
    # -------------------------------------------------------------- #
    @api.model
    def _build_llm_prompt(self, text, context, detected_langs=None):
        """Prompt the model to do three things in one shot:
        ① rewrite the utterance as plain English (fixed canonical language,
           business entities preserved verbatim),
        ② pick a whitelisted capability id,
        ③ extract params from the capability's declared schema."""
        caps = self._caps_payload()
        sys = (
            "You are a strict intent router for an Odoo field-operations "
            "assistant. The user may write in ANY language or MIX SEVERAL "
            "languages in one sentence. Always understand all of them.\n"
            "\n"
            "Do these three things and reply with ONE JSON object:\n"
            "1. normalized_text: rewrite the user's utterance as plain "
            "ENGLISH. This is the canonical form used for audit and for "
            "extracting parameters. PRESERVE business entities VERBATIM — "
            "never translate or alter model/barcode codes (e.g. MMUX-0001), "
            "quantities, lot/serial numbers, document numbers or locations.\n"
            "2. capability_id: choose ONE id from the list below, or null "
            "if nothing fits.\n"
            "3. params: only the parameters declared by the chosen "
            "capability; do not invent fields.\n"
            "\n"
            'Reply ONLY with JSON: {"normalized_text": "...", '
            '"capability_id": "<listed id or null>", "params": {...}, '
            '"missing": [<required info still needed>], '
            '"ambiguous": [<competing readings the user must resolve>], '
            '"confidence": <0.0-1.0>}. '
            "NEVER name an Odoo model, field or method. Never invent "
            "capability ids or parameters. Do not guess business-critical "
            "values (quantities, lots, money) — put them in `missing` "
            "instead. Answer with JSON keys in English.\n"
            "\n"
            "Example:\n"
            'input: "把 MMUX-0001 的 stock 数量改成 5 个"\n'
            'output: {"normalized_text": "set the stock quantity of '
            'MMUX-0001 to 5 pieces", "capability_id": "picking_report_qty", '
            '"params": {"quantity": 5}, "missing": [], "ambiguous": [], '
            '"confidence": 0.9}\n'
            "\n"
            "Available capabilities:\n" + json.dumps(caps, ensure_ascii=False)
        )
        user = json.dumps({
            "text": text,
            "context": context,
            "detected_scripts": list(detected_langs or []),
            "normalize_to": NORMALIZED_LANG,
        }, ensure_ascii=False)
        return [{"role": "system", "content": sys},
                {"role": "user", "content": user}]

    # -------------------------------------------------------------- #
    # _scratch_key
    # -------------------------------------------------------------- #
    # ---- scratchpad persistence (the agent's memory) ------------------ #
    @api.model
    def _scratch_key(self, loop_key):
        """One scratchpad row per loop. Derived from the loop key so a
        resume finds the same memory without the client having to carry
        it (and so a client cannot point at someone else's run — the row
        is additionally filtered by `user_id` on load)."""
        return f"agent:{loop_key}" if loop_key else "agent"

    # -------------------------------------------------------------- #
    # _scratch_save
    # -------------------------------------------------------------- #
    @api.model
    def _scratch_save(self, scratch, loop_key, blob=None, extra=None):
        """Persist the agent's memory *and* the loop's bookkeeping.

        One row per loop carries both halves, and they are deliberately
        different things:

        * the **scratchpad** is what the MODEL may see (`SCRATCHPAD_KEYS`)
          — its memory. Written from `scratch.to_dict()` so the model's
          view can never be widened by adding a key here.
        * the **blob** is what the SERVER needs to resume the loop: the
          recipe, the done-step cursor, the parked confirmation, the last
          outcome. It is never shown to the model.

        Both must be written together. Persisting only the scratchpad
        would silently drop `done_ids` (so the recipe restarts every turn)
        and `pending_confirmation` (so a parked L2 write could never be
        released) — the two failures that matter most in this design.

        `extra` is a final, explicit override for the small bookkeeping
        fields the audit row itself needs (scenario, …).
        """
        key = self._scratch_key(loop_key)
        Log = self.env["mmux.interaction.log"].sudo()
        detail = dict(blob or {})
        detail.update(scratch.to_dict())      # model memory always wins
        detail.pop("_repeats", None)          # runtime-only, never persisted
        if extra:
            detail.update(extra)
        vals = {"detail": json.dumps(detail, ensure_ascii=False),
                "exec_status": "running",
                "input_type": "agent_turn",
                "capability_id_name": f"agent:{scratch.flow_id or ''}",
                "scenario": detail.get("scenario", "")}
        existing = Log.search([("idempotency_key", "=", key)], limit=1)
        if existing:
            existing.write(vals)
        else:
            Log.create(dict(vals, idempotency_key=key,
                            user_id=self.env.user.id))
        return key

    # -------------------------------------------------------------- #
    # _scratch_load
    # -------------------------------------------------------------- #
    @api.model
    def _scratch_load(self, loop_key):
        """Restore the agent's memory, or a fresh one.

        Returns `(scratch, blob)` where `blob` is the raw persisted dict
        (it carries the loop's own bookkeeping — trace, current task — and
        is deliberately separate from the scratchpad, which is *only* the
        model-visible memory).
        """
        Log = self.env["mmux.interaction.log"].sudo()
        rec = Log.search([("idempotency_key", "=", self._scratch_key(loop_key)),
                          ("user_id", "=", self.env.user.id)], limit=1)
        blob = {}
        if rec and rec.detail:
            try:
                blob = json.loads(rec.detail) or {}
            except ValueError:
                blob = {}
        return AgentScratchpad.from_dict(blob), blob

    # -------------------------------------------------------------- #
    # _agent_observe
    # -------------------------------------------------------------- #
    # ---- the observation: what the agent is allowed to see ------------ #
    @api.model
    def _agent_observe(self, blob, loop_key, last_result=None, answers=None):
        """Build the observation for the next turn.

        Deliberately narrow: the goal, the current task, the standard
        operating procedure (the scenario-defined recipe) annotated with
        the server's progress ledger, the bounded world state of the
        record under work, the last outcome and the answers so far. It is
        *not* a database view — the model never receives another user's
        records, other pending tasks, prices or customer details beyond
        the localized title the operator can already see on screen.

        v3.5 reasoning-first: the recipe is handed over WHOLE as the SOP
        reference (`recipe_reference` in the prompt), not as one cursor
        line. The `done_ids` ledger annotates each step with what has
        actually been completed, and `current_step` remains only as the
        deterministic fallback's narration point — the model sees it as a
        per-step `next` marker it may disagree with, never as a command.
        """
        lang = self._user_lang()
        skill = blob.get("skill_level") or self._user_skill_level()
        flow_id = blob.get("flow_id")
        run = None
        steps = []
        current = None
        if flow_id:
            recipe = blob.get("recipe")
            # DB overrides (mmux.flow.override) resolve FIRST here too —
            # but only for the initial render: the recipe is then
            # snapshotted into the blob, so the script the operator
            # hears cannot shift mid-task even if an admin edits the
            # override while the loop runs. New loops pick the edit up.
            if recipe is None:
                flow = self._flow_for(flow_id)
                if flow:
                    recipe = flow.recipe(
                        self.env, blob.get("params") or {}, skill)
                    blob["recipe"] = recipe
            steps = list(recipe or [])
            done = set(blob.get("done_ids") or [])
            for step in steps:
                if step.get("step_id") in done:
                    continue
                current = step
                break
        task = {}
        if blob.get("task_id"):
            task = {"task_id": blob.get("task_id"),
                    "title": blob.get("title"),
                    "res_model": blob.get("res_model")}
        return AgentObservation(
            goal=blob.get("goal") or blob.get("title") or "",
            task=task, flow_id=flow_id, skill_level=skill, lang=lang,
            current_step=current, steps=steps, last_result=last_result,
            answers=answers or {},
            available_capabilities=self._agent_allowed_capabilities(
                blob.get("scenario", "")),
            # the server's authoritative progress ledger, used by the
            # pure core ONLY to annotate recipe_reference with `done`
            # markers (docs/CONFIG.md §14.2). One source, no drift.
            done_ids=blob.get("done_ids") or [],
            world_state=self._agent_world_state(blob),
        )

    # -------------------------------------------------------------- #
    # _agent_world_state
    # -------------------------------------------------------------- #
    @api.model
    def _agent_world_state(self, blob):
        """Bounded view of the record under work, for the observation.

        The reasoning agent needs to know what the world already looks
        like — otherwise it cannot decide whether an SOP step still has
        to run ("quantity already reported -> do not report again").
        This collector deliberately exposes ONLY fields the operator can
        already see in the loop view (task identity, progress ledger,
        outcome trail); it never reads arbitrary columns of the business
        record. A deployment that wants richer world state visible to
        the model must extend this method explicitly — the whitelist
        lives here, at the server edge, never in the model's hands.
        """
        state = {}
        if blob.get("task_id"):
            state["task"] = {
                "task_id": blob.get("task_id"),
                "res_model": blob.get("res_model"),
                "completed": bool(blob.get("task_completed")),
            }
        done = list(blob.get("done_ids") or [])
        if done:
            state["steps_done"] = len(done)
        last = blob.get("last_result")
        if isinstance(last, dict) and last:
            state["last_action"] = {
                k: last.get(k) for k in ("state", "action", "exec_status")
                if last.get(k) is not None}
        parked = blob.get("pending_confirmation")
        if isinstance(parked, dict) and parked.get("capability_id"):
            state["awaiting_confirmation_for"] = parked["capability_id"]
        return state

    # -------------------------------------------------------------- #
    # _agent_run_capability
    # -------------------------------------------------------------- #
    # ---- the capability runner handed to the pure loop ---------------- #
    @api.model
    def _agent_run_capability(self, request):
        """`run_loop`'s execution hook — and the security pinch point.

        `request` comes from `execution_request()`, which already hardwired
        `confirmed=False`. We pass it through unchanged: this method must
        never add a `confirmed` argument of its own, and must never import
        a value from the model. Doing so would silently disable the human
        confirmation gate for every L2 capability the agent proposes.
        """
        return self.execute_capability(
            request["capability_id"], request.get("params") or {},
            record_id=request.get("record_id"),
            idem_key=request.get("idem_key"),
            confirmed=False,          # never read from `request`
        )

    # -------------------------------------------------------------- #
    # _agent_log_turn
    # -------------------------------------------------------------- #
    @api.model
    def _agent_log_turn(self, scratch, blob, turn, loop_key):
        """Audit ONE loop step.

        Every turn is logged, including rejected actions and degraded
        turns — that is what makes the agent's judgement measurable later.
        A `failed` status here therefore means "the model's answer was
        unusable", not "the business call failed"; the capability's own
        outcome is recorded by `execute_capability` on its own row.
        """
        try:
            Log = self.env["mmux.interaction.log"].sudo()
            Log.create({
                "user_id": self.env.user.id,
                "input_type": "agent_turn",
                "exec_status": "success" if turn.source == LOOP_SOURCE_LLM
                               else "failed",
                "capability_id_name": f"agent_turn:{blob.get('flow_id') or ''}",
                "scenario": blob.get("scenario", ""),
                "idempotency_key": idempotency_key("agent_turn"),
                "detail": json.dumps({
                    "loop_key": loop_key,
                    "state": turn.state,
                    "source": turn.source,
                    "action": turn.action.to_dict() if turn.action else None,
                    "rejected_reason": turn.rejected_reason,
                    "turn": scratch.turn,
                    "capability_calls": scratch.capability_calls,
                }, ensure_ascii=False),
            })
        except Exception as exc:                       # never break the loop
            _logger.info("agent turn audit failed: %s", exc)

    # -------------------------------------------------------------- #
    # agent_start
    # -------------------------------------------------------------- #
    # ---- public entry points ----------------------------------------- #
    @api.model
    def agent_start(self, loop_key=None, loop=None, goal="", signal=None):
        """Start (or adopt) an agent loop.

        Three ways in, deliberately:

        * `loop_key` — adopt an existing v2.x task loop: its task, flow
          and scenario become the agent's mission. This is how the
          existing task-loop UI upgrades to the agent without the operator
          doing anything different.
        * `loop` — an explicit dict (scenario / flow_id / res_id / goal),
          for callers that are not driving a task loop.
        * `signal` — where the user is in the web client. When the caller
          does not already know the scenario (the field case: the operator
          just opens a screen and starts talking), the server derives it
          from this and narrows the capability whitelist accordingly.

        An explicit `scenario` in `loop` always wins over the derived one:
        the caller stating the context is authoritative, the derived one
        is an inference. And an unresolved context is never silently
        widened to "everything" — it becomes a question (`ask_user`),
        because quietly handing the agent more capabilities is worse than
        asking the operator where they are.

        Returns the loop key, the first `AgentTurn` and the loop view.
        Returns `{"error": ...}` when the agent is disabled or there is
        nothing to work on.
        """
        if not self._agent_enabled():
            return {"error": _("the agent loop is disabled "
                               "(mmux.agent.enabled)"),
                    "code": "agent_disabled"}

        blob = {}
        if loop_key:
            existing = self._loop_load(loop_key)
            if existing is None:
                return {"error": _("unknown task loop — fetch a task first")}
            blob.update(existing)
        if isinstance(loop, dict):
            blob.update(loop)
        if goal:
            blob["goal"] = goal
        if not blob.get("goal"):
            blob["goal"] = blob.get("title") or ""

        # a loop without a key cannot be resumed, so one is always minted
        # when the caller did not supply (or adopt) one. This is what makes
        # the "page reload" case work: the client only ever has to hold the
        # loop key, never the agent's memory.
        if not loop_key:
            loop_key = idempotency_key("agent")
        blob["loop_key"] = loop_key

        # ---- scenario context: derive it, but never override a stated one
        #
        # "Stated" beats "derived": if the caller already named a scenario
        # (an explicit request from the operator's own UI), that is the
        # authoritative answer and no amount of position signal may overrule
        # it. Deriving is for the common case where nobody said anything.
        #
        # The signal is still probed either way, so a disagreement between
        # where the operator IS and what they ASKED for stays visible
        # instead of being silently resolved — and it is visible in the
        # VIEW, not only in the audit: a mismatch the operator cannot see
        # is a mismatch nobody can act on.
        explicit_scenario = blob.get("scenario")
        derived = None if explicit_scenario else self._scenario_resolve(signal)
        audited = derived
        if explicit_scenario and signal is not None:
            # audit-only probe: never overrides, always recorded
            audited = self._scenario_resolve(signal, allow_llm=False)

        if derived is not None:
            blob["scenario_context"] = derived.to_dict(self._user_lang())
            blob["scenario_candidates"] = list(derived.candidates)
            blob["scenario_source"] = derived.source
            if derived.is_resolved:
                blob["scenario"] = derived.scenario
        if explicit_scenario and signal is not None:
            # The stated scenario IS the context, even though we did not
            # derive it: it is what the whitelist will be built from, so
            # the block must say so rather than showing an empty scenario
            # the UI would render as "unknown".
            #
            # Guarded on `signal is not None` on purpose. A client that
            # sends NO signal at all is an older client for which this
            # whole feature does not apply, and emitting a context block
            # for it would change the v3.0 response shape out from under
            # it. "Not applicable" must stay invisible.
            ctx = dict(blob.get("scenario_context") or {})
            ctx["scenario"] = explicit_scenario
            ctx.setdefault("source", SOURCE_USER)
            ctx.setdefault("needs_asking", False)
            ctx["stated_scenario"] = explicit_scenario
            blob["scenario_context"] = ctx
        if explicit_scenario and audited is not None:
            # the stated scenario stands; the probe is reported alongside it
            ctx = dict(blob.get("scenario_context") or {})
            ctx.setdefault("stated_scenario", explicit_scenario)
            ctx["position_scenario"] = audited.scenario
            ctx["position_mismatch"] = bool(
                audited.is_resolved and audited.scenario != explicit_scenario)
            blob["scenario_context"] = ctx
            if not blob.get("scenario_candidates"):
                blob["scenario_candidates"] = list(audited.candidates)
        if audited is not None:
            self._scenario_log(audited, signal, loop_key)

        blob["skill_level"] = blob.get("skill_level") or \
            self._user_skill_level()
        blob["params"] = blob.get("params") or {}
        if blob.get("res_id") and not blob["params"].get("res_id"):
            blob["params"]["res_id"] = blob["res_id"]
        blob.setdefault("done_ids", [])
        blob.setdefault("trace", [])

        scratch, _scratch_blob = self._scratch_load(loop_key)
        scratch.goal = blob["goal"]
        scratch.task_id = blob.get("task_id")
        scratch.flow_id = blob.get("flow_id")
        scratch.skill_level = blob["skill_level"]
        scratch.lang = self._user_lang()

        # an unresolved context is the FIRST thing the agent must resolve
        # before it can meaningfully work: without a scenario its
        # capability list is ambiguous, so asking is the only honest step.
        # Only a DETACHED context blocks the loop: if the caller stated the
        # scenario, the operator has already told us where they are, so a
        # probe that disagreed must not hold up the work.
        if derived is not None and derived.needs_asking:
            turn = self._scenario_ask_turn(derived, blob, scratch)
        else:
            turn = self._agent_step(scratch, blob, loop_key)

        new_key = self._scratch_save(scratch, loop_key, blob,
                                     {"scenario": blob.get("scenario", "")})
        return self._agent_view(scratch, blob, turn, loop_key, new_key)

    # -------------------------------------------------------------- #
    # agent_turn
    # -------------------------------------------------------------- #
    @api.model
    def agent_turn(self, loop_key, answer=None, confirmed=False):
        """Advance the agent loop by ONE step.

        `answer` supplies the value for a previous `ask_user`. `confirmed`
        is accepted because the client uses one endpoint for the whole
        conversation, but it **only ever releases a capability that the
        guard itself parked on `awaiting_confirmation`** — it is never
        forwarded into the reasoning turn, so it cannot cause the agent to
        propose something different.

        An unknown `loop_key` returns an error rather than silently
        starting a new loop: continuing a *different* run under the key of
        an expired one would give the operator a turn they did not ask
        for, against a task they may no longer hold.
        """
        if not loop_key:
            return {"error": _("missing loop_key")}
        scratch, blob = self._scratch_load(loop_key)
        if not blob:
            return {"error": _("unknown agent loop (expired?) — "
                               "start it again"),
                    "code": "unknown_loop"}

        # An answer to a pending question is bookkeeping, not reasoning:
        # the operator was ASKED something, and the record of their reply
        # must be kept whether or not reasoning is currently switched on.
        # Checking the switch first would discard the answer and leave the
        # loop asking the same question forever.
        answering_pending = answer is not None and blob.get("pending_field")
        if not self._agent_enabled() and not answering_pending:
            return {"error": _("the agent loop is disabled"),
                    "code": "agent_disabled"}

        if answer is not None:
            field = blob.get("pending_field") or "answer"
            if field == "scenario":
                # The operator answered the context question. The answer must
                # select one of the scenarios the SERVER offered — a reply
                # naming anything else is refused and the question stands.
                # Accepting it unchecked would let a client choose its own
                # capability whitelist by typing.
                choice = choice_from_answer(
                    answer, blob.get("scenario_candidates") or [])
                if choice.is_valid:
                    blob["scenario"] = choice.scenario
                    blob["scenario_source"] = SOURCE_USER
                    blob["scenario_context"] = dict(
                        blob.get("scenario_context") or {},
                        scenario=choice.scenario, source=SOURCE_USER,
                        needs_asking=False,
                        confidence=choice.confidence,
                        choice_source=SOURCE_USER)
                    blob.pop("pending_question", None)
                    blob["last_result"] = {"answered": True,
                                           "field": "scenario",
                                           "scenario": choice.scenario}
                else:
                    # unparseable / out-of-set: keep asking rather than
                    # guessing. The pending field stays set so the next
                    # turn asks again.
                    blob["last_result"] = {"answered": False,
                                           "field": "scenario",
                                           "rejected_reason":
                                               choice.rejected_reason}
                    scratch.note_error("scenario_answer_rejected")
                    turn = self._scenario_ask_turn(
                        resolution=self._resolution_from_blob(blob),
                        blob=blob, scratch=scratch)
                    new_key = self._scratch_save(
                        scratch, loop_key, blob,
                        {"scenario": blob.get("scenario", "")})
                    return self._agent_view(scratch, blob, turn, loop_key,
                                            new_key)
            else:
                scratch.answer(field, answer)
            blob.pop("pending_field", None)
            blob.pop("pending_options", None)   # consumed: chips are gone
            if field != "scenario":
                blob["last_result"] = {"answered": True, "field": field}

        if confirmed:
            turn = self._agent_confirm(scratch, blob, loop_key)
        else:
            turn = self._agent_step(scratch, blob, loop_key)

        new_key = self._scratch_save(scratch, loop_key, blob,
                                     {"scenario": blob.get("scenario", "")})
        return self._agent_view(scratch, blob, turn, loop_key, new_key)

    # -------------------------------------------------------------- #
    # _agent_step
    # -------------------------------------------------------------- #
    # ---- one reasoning step ------------------------------------------- #
    @api.model
    def _agent_step(self, scratch, blob, loop_key):
        """Ask the model (or the recipe) for the next action, then apply
        only the *safe* consequences here.

        A `call_capability` action is deliberately NOT executed by this
        method: it is executed through `_agent_run_capability` inside
        `run_loop`, which routes to `execute_capability`. `propose_step`
        only advances the loop cursor.
        """
        observation = self._agent_observe(
            blob, loop_key,
            last_result=blob.get("last_result"),
            answers=scratch.user_answers)
        llm_call, config = self._agent_llm_call()
        # budgets come from configuration (settings page); the pure core
        # falls back to its module defaults on anything invalid, so an
        # empty/absent parameter behaves exactly like before v19.0.6.
        # v3.5 per-turn idempotency: the OLD fixed key `{loop_key}:agent`
        # made every capability call inside one loop look like a replay of
        # the first, so a legitimate corrected retry (wrong quantity ->
        # right quantity) was silently swallowed as `duplicate` by the
        # guard. Turn-scoped keys let retries through while a same-turn
        # double-submit (client retry of one HTTP step) still dedupes —
        # `scratch.turn` only advances inside `run_loop`, once per step.
        turn_no = int(getattr(scratch, "turn", 0) or 0)
        turn = run_loop(
            scratch, observation,
            llm_call=llm_call,
            llm_config=config,
            run_capability=lambda req: self._agent_run_capability(
                dict(req, idem_key=f"{loop_key}:agent:{turn_no}")),
            allowed_capabilities=observation.available_capabilities,
            history=blob.get("history") or [],
            **self._agent_budgets(),
        )
        self._agent_apply(scratch, blob, turn, loop_key)
        self._agent_fold_progress(blob, scratch, turn, observation)
        self._agent_log_turn(scratch, blob, turn, loop_key)
        return turn

    # -------------------------------------------------------------- #
    # _agent_fold_progress
    # -------------------------------------------------------------- #
    @api.model
    def _agent_fold_progress(self, blob, scratch, turn, observation):
        """Fold the finished turn into the v3.5 progress ledger.

        Pure bookkeeping on top of `fold_turn_progress`: it records the
        stalled flag in the blob (server-side audit; never model memory)
        and appends the turn to the bounded conversation history that the
        next observation replays. History is capped so a long-running
        session cannot grow the prompt without limit.
        """
        stalled = fold_turn_progress(scratch, turn.action, observation,
                                     turn.exec_result)
        if stalled:
            blob["stalled"] = True
            blob.setdefault("trace", []).append(
                {"event": "stalled", "turn": scratch.turn})
        action = turn.action
        if action is not None and getattr(action, "ok", False):
            entry = {"role": "assistant",
                     "turn": scratch.turn,
                     "content": json.dumps({
                         "action": action.action,
                         "payload": {k: v for k, v in action.payload.items()
                                     if k in ("step_id", "capability_id",
                                             "question", "field", "why")}},
                         ensure_ascii=False)}
            history = list(blob.get("history") or [])
            history.append(entry)
            blob["history"] = history[-MAX_AGENT_HISTORY:]

    # -------------------------------------------------------------- #
    # _agent_apply
    # -------------------------------------------------------------- #
    @api.model
    def _agent_apply(self, scratch, blob, turn, loop_key):
        """Fold a turn's outcome into the persisted loop state.

        Kept separate from `_agent_step` because the interesting logic —
        what counts as progress, what a park means, what an uncertain
        result must NOT do — deserves to be readable on its own.
        """
        action = turn.action
        blob["last_result"] = {
            "state": turn.state,
            "action": getattr(action, "action", None),
            "exec_status": (turn.exec_result or {}).get("status"),
            "rejected_reason": turn.rejected_reason,
        }
        if action is None:
            return

        # an `ask_user` records what is being asked, so the answer can be
        # routed back into the scratchpad under the right key
        if action.action == "ask_user":
            blob["pending_field"] = action.payload.get("field")
            blob["pending_question"] = action.payload.get("question")
            # v3.5 symmetric read/write: `_agent_view` renders
            # `blob["pending_options"]`, but nothing ever wrote it for
            # agent-driven questions, so the UI option chips were lost.
            # The scenario-context path writes the SAME key with its own
            # candidates; this closes the other half of the channel.
            options = action.payload.get("options") or []
            blob["pending_options"] = [
                {"value": o, "label": o} if isinstance(o, str) else o
                for o in options if isinstance(o, (str, dict))]
            return

        # a proposed step advances the recipe cursor — and ONLY the
        # cursor. No business state is touched by a proposal.
        if action.action == "propose_step":
            step_id = action.payload.get("step_id")
            if step_id and step_id not in blob["done_ids"]:
                blob["done_ids"] = list(blob["done_ids"]) + [step_id]
            return

        if action.action == "call_capability":
            result = turn.exec_result or {}
            status = result.get("status")
            if status == "success":
                # only a VERIFIED success advances the loop. `unknown` and
                # `failed` deliberately leave the cursor where it was, so a
                # retry re-reads the record rather than skipping ahead.
                cap = action.capability_id
                if blob.get("capability_id") == cap:
                    # the task's own completion -> the loop is done and the
                    # next task is published by the caller (task_report
                    # semantics), not invented here.
                    blob["task_completed"] = True
            elif status == "awaiting_confirmation":
                # remember what is parked so a later `confirmed=True` turn
                # releases EXACTLY this request and nothing else.
                blob["pending_confirmation"] = {
                    "capability_id": action.capability_id,
                    "params": action.params,
                    "record_id": action.record_id,
                    "index": len(blob.get("trace") or []),
                }
            return

    # -------------------------------------------------------------- #
    # _agent_confirm
    # -------------------------------------------------------------- #
    @api.model
    def _agent_confirm(self, scratch, blob, loop_key):
        """Release a capability the guard parked on `awaiting_confirmation`.

        Only the request the guard itself parked is re-submitted, from the
        *persisted* blob — never from anything the client sent. A client
        therefore cannot smuggle in a different capability, a different
        record or different params by "confirming" something else.
        """
        pending = blob.get("pending_confirmation")
        if not pending:
            # nothing parked: an unsolicited `confirmed=True` is a no-op,
            # not an error, and must not be reinterpreted as consent for
            # whatever the model proposes next.
            turn = AgentTurn(state="running", source=LOOP_SOURCE_NONE)
            return turn

        res = self.execute_capability(
            pending["capability_id"], pending.get("params") or {},
            record_id=pending.get("record_id"),
            idem_key=f"{loop_key}:agent:confirm",
            confirmed=True,
        )
        status = res.get("status")
        action = AgentAction(action="call_capability", ok=True, payload={
            "capability_id": pending["capability_id"],
            "params": pending.get("params") or {},
            "record_id": pending.get("record_id"),
        })
        if status == "success":
            turn = AgentTurn(action=action, state="running",
                             source=LOOP_SOURCE_NONE, exec_result=res)
            blob.pop("pending_confirmation", None)
            if blob.get("capability_id") == pending["capability_id"]:
                blob["task_completed"] = True
        else:
            turn = AgentTurn(action=action, state="failed",
                             source=LOOP_SOURCE_NONE, exec_result=res,
                             rejected_reason=status)
            blob["last_result"] = {"exec_status": status}
        self._agent_log_turn(scratch, blob, turn, loop_key)
        return turn

    # -------------------------------------------------------------- #
    # _agent_view
    # -------------------------------------------------------------- #
    # ---- the rendered loop view --------------------------------------- #
    @api.model
    def _agent_view(self, scratch, blob, turn, loop_key, scratch_key=None):
        """What the frontend renders: the turn, the recipe position, the
        budgets and the trace so far."""
        lang = self._user_lang()
        turn_dict = turn.to_dict(lang)
        return {
            "loop_key": loop_key,
            "scratch_key": scratch_key,
            "scenario": blob.get("scenario", ""),
            # v3.1: HOW the scenario was established, and what the options
            # were. Shown so the operator can see the agent is guessing
            # correctly — or tell it that it is not. `None` when this loop
            # was started without position signals (older client), which is
            # meaningfully different from "could not determine".
            "scenario_context": self._scenario_context_view(blob, lang),
            "flow_id": blob.get("flow_id"),
            "task": {
                "task_id": blob.get("task_id"),
                "title": resolve_text(blob.get("title"), lang),
                "res_model": blob.get("res_model"),
                "res_id": blob.get("res_id"),
            } if blob.get("task_id") else None,
            "turn": turn_dict,
            "state": turn.state,
            "source": turn.source,
            "speak": turn_dict["speak"],
            "action": turn_dict["action"],
            "exec": turn.exec_result,
            "awaiting_confirmation": turn.state == "awaiting_confirmation",
            "awaiting_user": turn.state == "awaiting_user",
            "pending_field": blob.get("pending_field"),
            "pending_question": resolve_text(blob.get("pending_question"), lang),
            "pending_options": blob.get("pending_options") or [],
            "task_completed": bool(blob.get("task_completed")),
            "budget": {"turns_used": scratch.turn,
                       "turns_left": scratch.budget_left,
                       "capability_calls": scratch.capability_calls,
                       "capability_calls_left": scratch.calls_left},
            "progress": {
                "done_ids": list(blob.get("done_ids") or []),
                "step_count": len(blob.get("recipe") or []),
            },
            "agent_enabled": self._agent_enabled(),
            # Whether the client should ASK THE BROWSER for its position.
            # The server decides, not the client: a browser that starts
            # requesting geolocation because its own code felt like it
            # would make the admin's `geo_collect` setting meaningless.
            # Off by default, so a deployment that never enables it never
            # triggers a permission prompt.
            "geo_collect": self._scenario_geo_settings()[0],
            "lang": lang,
        }

    # -------------------------------------------------------------- #
    # _resolve_record_ref
    # -------------------------------------------------------------- #
    @api.model
    def _resolve_record_ref(self, record_ref, context):
        """Resolve a `record_ref` to a concrete record id.

        Accepts a plain id or a `$context.<key>` reference. Anything the
        agent invents that we cannot resolve returns None, and the guard
        then either uses the context record or fails cleanly — it never
        guesses a target.
        """
        if record_ref in (None, "", False):
            return None
        if isinstance(record_ref, int) and not isinstance(record_ref, bool):
            return record_ref
        if isinstance(record_ref, str):
            if record_ref.isdigit():
                return int(record_ref)
            if record_ref.startswith("$context."):
                return (context or {}).get(record_ref[len("$context."):])
        return None
