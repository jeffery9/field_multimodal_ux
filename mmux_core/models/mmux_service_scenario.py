# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Scenario detection and the audit trail.
Scenario is a security boundary: the same source decides what is offered
to the model AND what the answer is validated against, so it is derived
here in one place rather than chosen by the model.
"""
import json
import logging

_logger = logging.getLogger(__name__)

from odoo import api, models

from ..services.mmux_core import (
    CAPABILITY_REGISTRY,
    ExecutionGuard, ExecStatus, GuardError,
    IntentParser, NORMALIZED_LANG,
    PROVIDER_REGISTRY,
    ProviderError,
    Plan, PlanStep, PlanOrchestrator, PlanError,
    PLAN_LIBRARY, validate_llm_plan,
    TaskFlowEngine, FLOW_ENGINE,
    Task, TASK_ENGINE, build_loop_state, loop_label,
    AgentPick, validate_pick, fallback_pick,
    SOURCE_LLM, SOURCE_FALLBACK, SOURCE_EMPTY,
    AgentAction, AgentObservation, AgentScratchpad, AgentTurn,
    validate_action, build_loop_prompt, step_from_action,
    execution_request, run_loop,
    AGENT_ACTIONS, LOOP_STATES,
    LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE, LOOP_SOURCE_NONE,
    MAX_AGENT_TURNS, MAX_AGENT_CAPABILITY_CALLS,
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


class MmuxService(models.AbstractModel):
    _inherit = "mmux.service"

    # -------------------------------------------------------------- #
    # _scenario_known_codes
    # -------------------------------------------------------------- #
    # ================================================================== #
    # Scenario context (v3.1): WHERE is the user working?
    # ================================================================== #
    # The scenario is not cosmetic. `_agent_allowed_capabilities(scenario)`
    # is used TWICE from one source — it is the capability list OFFERED to
    # the model and the whitelist its answer is VALIDATED against. So the
    # scenario is the boundary that makes that whitelist meaningful, and
    # it must never be something the model gets to choose.
    #
    # Hence the split enforced throughout this section:
    #
    #   the SERVER derives candidate scenarios from position signals
    #   (deterministic, auditable); the LLM may only DISAMBIGUATE within
    #   that candidate set; a scenario outside it is rejected outright.
    #
    # The LLM participates in the judgement. It does not set the border.
    # ------------------------------------------------------------------ #
    @api.model
    def _scenario_known_codes(self):
        """The scenario codes the server can actually serve.

        Read from the model the admin edits, so a scenario addon that is
        not installed simply does not appear — and a table entry pointing
        at it is treated as uninformative rather than as a recommendation
        that would leave the operator with an empty toolset.
        """
        try:
            records = self.env["mmux.scenario.config"].sudo().search(
                [("active", "=", True)])
            return [r.code for r in records if r.code]
        except Exception as exc:                      # never break context
            _logger.info("scenario codes unavailable: %s", exc)
            return []

    # -------------------------------------------------------------- #
    # _scenario_labels
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_labels(self):
        """code -> display name, for the `ask_user` options and the UI."""
        try:
            records = self.env["mmux.scenario.config"].sudo().search(
                [("active", "=", True)])
            return {r.code: resolve_text(r.name, self._user_lang())
                    for r in records if r.code}
        except Exception:                                 # defensive
            return {}

    # -------------------------------------------------------------- #
    # _scenario_geo_settings
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_geo_settings(self):
        """The position policy, read from the scenario config.

        Returns `(collect, weight)`. Both are the CONSERVATIVE value when
        the configuration cannot be read: `collect=False` because asking
        a browser for its position is not something to do by accident, and
        `weight=0` because a position must never decide anything unless an
        admin has said so explicitly.

        The per-scenario `geo_weight` is deliberately reduced to a single
        number here: today every scenario ships 0, and the pure core
        applies ONE weight to the signal as a whole. Taking the MAXIMUM
        across active scenarios means enabling geofencing for one scenario
        cannot be silently cancelled by another scenario's default.
        """
        try:
            records = self.env["mmux.scenario.config"].sudo().search(
                [("active", "=", True)])
            collect = any(getattr(r, "geo_collect", False) for r in records)
            weight = max([int(getattr(r, "geo_weight", 0) or 0)
                          for r in records] or [0])
            return bool(collect), max(0, weight)
        except Exception as exc:                      # never break context
            _logger.info("scenario geo settings unavailable: %s", exc)
            return False, 0

    # -------------------------------------------------------------- #
    # _scenario_llm_call
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_llm_call(self):
        """The provider callable for disambiguation, or None.

        Returning None (never raising) is what lets the resolution degrade
        to `ask_user`: with no model we can still derive the candidate set
        deterministically, we simply cannot narrow it, so we ask.
        """
        llm_call, _config = self._agent_llm_call()
        return llm_call

    # -------------------------------------------------------------- #
    # _scenario_disambiguate
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_disambiguate(self, resolution, signal):
        """Let the model choose WITHIN the candidate set, then validate.

        The model's answer is passed through `validate_scenario_choice`
        against the SERVER's candidates. A rejected answer is not a
        failure to be retried or repaired — it falls through to asking the
        operator, which is the honest outcome when we cannot decide.
        """
        llm_call = self._scenario_llm_call()
        if llm_call is None or not resolution.candidates:
            return resolution
        try:
            prompt = build_scenario_prompt(
                signal, resolution.candidates,
                labels=self._scenario_labels(), lang=self._user_lang())
            raw = llm_call({"messages": prompt}, self._provider_config())
            data = self._extract_llm_data(raw)
        except Exception as exc:                      # provider problems
            _logger.info("scenario disambiguation unavailable: %s", exc)
            return resolution

        choice = validate_scenario_choice(data, resolution.candidates)
        if not choice.is_valid:
            # the reason is kept for the audit; the outcome is "ask"
            resolution.rejected_reason = choice.rejected_reason
            return resolution

        resolution.scenario = choice.scenario
        resolution.source = SOURCE_SIGNAL
        resolution.confidence = choice.confidence or resolution.confidence
        resolution.choice_source = CHOICE_SOURCE_LLM
        resolution.candidates = [choice.scenario]
        return resolution

    # -------------------------------------------------------------- #
    # _scenario_resolve
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_resolve(self, signal=None, allow_llm=True):
        """Derive the working scenario from the client's position.

        Returns a `ScenarioResolution`. An unresolved context is a normal
        outcome, not an error: it carries `needs_asking` and the caller
        turns it into an `ask_user`.

        `signal=None` (an older client that does not report position) is
        NOT treated as "no signal" — it is treated as "not applicable",
        and the caller keeps its previous behaviour. That distinction
        matters: conflating the two would make every existing client
        suddenly start asking the operator to pick a scenario.
        """
        if signal is None:
            return None
        if not isinstance(signal, ContextSignal):
            signal = ContextSignal.from_dict(signal)

        # The configured weight, not the built-in constant: an admin who
        # has turned on geofencing raises it, and everyone else gets 0,
        # which is exactly `WEIGHT_GEO`. Reading it here (rather than
        # hardcoding) is what makes the feature configurable at all.
        _collect, geo_weight = self._scenario_geo_settings()

        known = self._scenario_known_codes()
        resolution = resolve_scenario(signal, known, geo_weight=geo_weight)
        # `geo_recorded` rides into the audit even when we cannot decide:
        # the operator should be able to see that their position arrived
        # and that it was deliberately not used.
        if resolution.needs_asking and resolution.candidates and allow_llm:
            resolution = self._scenario_disambiguate(resolution, signal)

        # A context we could not establish becomes an `ask_user`, and an
        # ask with no options is not a question. Widen the *question* to
        # every scenario that exists — never the *whitelist*: `scenario`
        # stays None until a human answers.
        if resolution.needs_asking:
            resolution = unresolved_with_all(resolution, known)
        return resolution

    # -------------------------------------------------------------- #
    # _scenario_log
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_log(self, resolution, signal, loop_key):
        """Audit the context decision — including a decision to ask.

        A context that could not be established is exactly the kind of
        event worth being able to count later, so it is recorded with the
        same weight as a successful one.
        """
        if resolution is None:
            return
        # `signal` arrives as a raw dict from the controller and as a
        # `ContextSignal` from the internal callers; normalise rather than
        # trusting the caller, because a crash here would silently drop the
        # audit row (the except below swallows it).
        if signal is not None and not isinstance(signal, ContextSignal):
            signal = ContextSignal.from_dict(signal)
        try:
            Log = self.env["mmux.interaction.log"].sudo()
            Log.create({
                "user_id": self.env.user.id,
                "input_type": "nl",
                "exec_status": "success" if resolution.is_resolved else "failed",
                "capability_id_name": "scenario_context",
                "scenario": resolution.scenario or "",
                "idempotency_key": idempotency_key("scenario"),
                "detail": json.dumps({
                    "loop_key": loop_key,
                    "scenario": resolution.scenario,
                    "source": resolution.source,
                    "choice_source": resolution.choice_source,
                    "candidates": resolution.candidates,
                    "confidence": resolution.confidence,
                    "rejected_reason": resolution.rejected_reason,
                    "signals": (signal.to_dict() if signal is not None else None),
                }, ensure_ascii=False),
            })
        except Exception as exc:                      # never break the loop
            _logger.info("scenario context audit failed: %s", exc)

    # -------------------------------------------------------------- #
    # _scenario_ask_turn
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_ask_turn(self, resolution, blob, scratch):
        """Turn an unresolved context into the loop's `ask_user` step.

        Reuses the agent loop's EXISTING `ask_user` channel rather than
        inventing a second questioning mechanism: one place that asks,
        one place that records the answer, one place the UI renders. The
        answer comes back through `agent_turn(answer=...)` and is folded
        into the scratchpad exactly like any other answer.

        The question's options are the server's candidates — so an answer
        can only ever select a scenario the server already had in mind.
        """
        question, options = build_ask_question(
            resolution, labels=self._scenario_labels())
        payload = {"question": question, "options": options,
                   "field": "scenario"}
        blob["pending_field"] = "scenario"
        blob["pending_question"] = question
        blob["pending_options"] = options
        scratch.mark_step("scenario_context")
        turn = AgentTurn(
            action=AgentAction(action="ask_user", ok=True, payload=payload),
            state="awaiting_user", source=LOOP_SOURCE_NONE)
        turn.speak = resolve_text(question, self._user_lang())
        return turn

    # -------------------------------------------------------------- #
    # _scenario_context_view
    # -------------------------------------------------------------- #
    @api.model
    def _scenario_context_view(self, blob, lang):
        """The scenario-context block, localized, or None.

        `None` means "this loop was never given position signals" — a
        different thing from a resolution that failed, and the UI must be
        able to tell them apart (one is normal, the other deserves a
        prompt).

        Two things are reported side by side and never conflated:

        * `scenario` — the scenario the loop is actually RUNNING under
          (stated by the operator or derived from the signal), which is
          what the capability whitelist comes from;
        * `position_scenario` — what the position alone suggested. When
          the two disagree, `position_mismatch` is true and the UI should
          say so: an operator working from the wrong screen deserves to
          know before they are told to do the wrong thing.
        """
        ctx = blob.get("scenario_context")
        if not isinstance(ctx, dict):
            return None
        labels = self._scenario_labels()
        return {
            "scenario": ctx.get("scenario") or blob.get("scenario") or "",
            "source": ctx.get("source") or SOURCE_NONE,
            "confidence": ctx.get("confidence") or 0.0,
            "choice_source": ctx.get("choice_source"),
            "rejected_reason": ctx.get("rejected_reason"),
            "needs_asking": bool(ctx.get("needs_asking")),
            "geo_recorded": bool(ctx.get("geo_recorded")),
            "stated_scenario": ctx.get("stated_scenario"),
            "position_scenario": ctx.get("position_scenario"),
            "position_mismatch": bool(ctx.get("position_mismatch")),
            "candidates": [
                {"value": c, "label": labels.get(c, c)}
                for c in (blob.get("scenario_candidates") or [])
            ],
            "evidence": ctx.get("evidence") or [],
        }

    # -------------------------------------------------------------- #
    # _resolution_from_blob
    # -------------------------------------------------------------- #
    @api.model
    def _resolution_from_blob(self, blob):
        """Rebuild a `ScenarioResolution` from persisted bookkeeping.

        Needed to re-ask after a rejected answer: the question must be the
        same one, over the same candidate set, or the operator would be
        choosing from a list that shifted under them.
        """
        resolution = ScenarioResolution(
            scenario=blob.get("scenario"),
            source=blob.get("scenario_source") or SOURCE_NONE,
            candidates=blob.get("scenario_candidates") or [])
        # Defensive: a resolution rebuilt from storage that has no scenario
        # must still be ANSWERABLE. If the stored candidate list came back
        # empty (an older blob, or a write that lost it), fall back to the
        # scenarios that exist — otherwise the re-ask would show no options
        # and the operator would be stuck with an unanswerable question.
        if not resolution.scenario:
            resolution = unresolved_with_all(
                resolution, self._scenario_known_codes())
        return resolution

    # -------------------------------------------------------------- #
    # _log_intent
    # -------------------------------------------------------------- #
    @api.model
    def _log_intent(self, intent, raw_text, lang_hint):
        """Audit the recognition step. The canonical English rewrite is
        recorded here — it is NEVER fed into the execution path."""
        try:
            Log = self.env["mmux.interaction.log"].sudo()
            Log.create({
                "user_id": self.env.user.id,
                "input_type": "nl",
                "capability_id_name": intent.capability_id or "",
                "exec_status": "recognized",
                "detail": json.dumps({
                    "raw_text": raw_text,
                    "normalized_text": intent.normalized_text,
                    "lang_hint": lang_hint,
                    "languages": intent.languages,
                    "confidence": round(intent.confidence, 3),
                    "missing": intent.missing,
                    "ambiguous": intent.ambiguous,
                    "source": intent.source,
                }, ensure_ascii=False),
            })
        except Exception as exc:  # auditing must never break parsing
            _logger.warning("failed to write intent audit log: %s", exc)

    # -------------------------------------------------------------- #
    # _log_agent_pick
    # -------------------------------------------------------------- #
    @api.model
    def _log_agent_pick(self, pick, pool, scenario=""):
        """Audit every decision — including the *rejected* ones.

        Logging failures is what makes the agent's quality measurable
        later, so a rejected pick is recorded with its reason even though
        the loop quietly recovered via the queue. The reason is stored as
        the original multilingual mapping (evidence-complete: resolving it
        here would destroy the other languages).
        """
        try:
            Log = self.env["mmux.interaction.log"].sudo()
            Log.create({
                "user_id": self.env.user.id,
                "input_type": "combo",
                "exec_status": "success" if pick.is_valid else "failed",
                "capability_id_name": f"agent_pick:{scenario}",
                "scenario": scenario,
                "idempotency_key": idempotency_key("agent_pick"),
                "detail": json.dumps({
                    "source": pick.source,
                    "task_id": pick.task_id,
                    "reason": pick.reason,
                    "confidence": pick.confidence,
                    "rejected_reason": pick.rejected_reason,
                    "candidate_count": len(pool or []),
                    "alternatives": [a.get("task_id")
                                     for a in pick.alternatives],
                }, ensure_ascii=False),
            })
        except Exception as exc:                       # never break the loop
            _logger.info("agent pick audit failed: %s", exc)
