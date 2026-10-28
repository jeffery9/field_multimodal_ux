# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Plan proposal and execution (the multi-step orchestrator path).
"""
import json
import logging

from odoo.exceptions import ValidationError as OdooValidationError

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
    # plan_run
    # -------------------------------------------------------------- #
    @api.model
    def plan_run(self, plan_id=None, llm_plan=None, inputs=None,
                 idempotency_key=None, confirmed_steps=None,
                 context_record=None):
        """Execute a named (code-defined) plan or an LLM-proposed plan.

        The loop is strict: each step passes the same ExecutionGuard; the
        run pauses on input_required / awaiting_confirmation / failed /
        unknown. Resuming re-supplies inputs or confirmed step ids with
        the same idempotency key (already-done steps are skipped by the
        per-step guard dedup)."""
        orch = self._orchestrator()
        if context_record:
            # bind $context.res_id / res_model for this run (before guard)
            self = self.with_context(mmux_field_context=context_record)
        guard = self._guard()
        try:
            if plan_id:
                plan = PLAN_LIBRARY.get(plan_id)
                if plan is None:
                    return {"state": "failed",
                            "error": _("plan %s is not registered") % plan_id}
            elif isinstance(llm_plan, dict):
                plan = validate_llm_plan(llm_plan, CAPABILITY_REGISTRY)
            else:
                return {"state": "failed", "error": _("need plan_id or llm_plan")}
            # structural validation (whitelist refs, input inventory)
            meta = orch.validate_plan(plan)
        except PlanError as exc:
            return {"state": "failed", "error": str(exc)}

        # plan-level idempotency: a finished/in-flight run refuses re-entry
        if idempotency_key and self._plan_is_running(idempotency_key):
            return {"state": "failed",
                    "error": _("a plan run with this idempotency key is "
                               "already finished or in flight")}
        result = orch.run(
            plan, guard, self, inputs=inputs or {},
            idempotency_key=idempotency_key,
            confirmed_steps=set(confirmed_steps or ()))
        self.env["mmux.interaction.log"].sudo().create({
            "user_id": self.env.user.id,
            "input_type": "combo",
            "capability_id_name": f"plan:{plan.plan_id}",
            "exec_status": result["state"],
            "idempotency_key": idempotency_key or "",
            "detail": str(result["steps"])[:4000],
        })
        return result

    # -------------------------------------------------------------- #
    # _plan_is_running
    # -------------------------------------------------------------- #
    @api.model
    def _plan_is_running(self, key):
        Log = self.env["mmux.interaction.log"].sudo()
        return bool(Log.search_count([
            ("idempotency_key", "=", key),
            ("capability_id_name", "=like", "plan:%"),
            ("exec_status", "in", ["success", "running"]),
        ]))

    # -------------------------------------------------------------- #
    # propose_plan
    # -------------------------------------------------------------- #
    @api.model
    def propose_plan(self, text, context=None):
        """LLM-only path for plans (consistent with intent parsing): there
        is no offline keyword match. The model sees the capability
        whitelist and the plan schema; its proposal is validated
        (validate_llm_plan) before it is ever runnable. A proposal never
        executes anything by itself."""
        context = context or {}
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
        caps = self._caps_payload()
        sys = (
            "You are a strict plan composer for an Odoo field-operations agent. "
            "The user may write in ANY language or MIX several languages. "
            "Respond ONLY with JSON: "
            '{"plan_id": "snake_case_id", "name": "...", "steps": ['
            '{"step_id": "s1", "capability_id": "<listed id>", '
            '"bindings": {"<param>": "<literal | $input:name | '
            '$stepN.result.field | $context:res_id>"}, "record_ref": "<ref>"}]}. '
            "Rules: only listed capability ids; only declared parameters; "
            "step references must point to earlier steps; do not invent "
            "capabilities, models or methods; keep plans short (2-6 steps).\n"
            "Available capabilities:\n" + json.dumps(caps, ensure_ascii=False)
        )
        try:
            raw = PROVIDER_REGISTRY.call(
                "llm", llm_provider,
                {"messages": [
                    {"role": "system", "content": sys},
                    {"role": "user", "content": json.dumps(
                        {"text": text, "context": context}, ensure_ascii=False)},
                ]}, config)
            data = raw.get("data")
            if isinstance(data, str):
                data = json.loads(data)
            plan = validate_llm_plan(data, CAPABILITY_REGISTRY)
            return {"source": "llm", "plan": plan.to_dict()}
        except ProviderError as exc:
            retryable = bool(getattr(exc, "retryable", False)
                             or getattr(exc, "timeout", False))
            subtag = "timeout" if getattr(exc, "timeout", False) else "provider_error"
            return self.llm_unavailable(exc, retryable=retryable, subtag=subtag)
        except (json.JSONDecodeError, ValueError):
            return self.llm_unavailable(_("LLM returned invalid JSON"),
                                        retryable=True, subtag="invalid_json")
        except PlanError as exc:
            return {"error": _("plan proposal rejected: %s") % exc}

    # -------------------------------------------------------------- #
    # resolve_binding
    # -------------------------------------------------------------- #
    @api.model
    def resolve_binding(self, value, step_results, inputs):
        """Backend hook for the orchestrator: $stepN.result.x / $input:x /
        $context.res_id / literal passthrough."""
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        if value.startswith("$step"):
            path = value[len("$step"):].strip(".")
            parts = path.split(".")
            step_id = parts[0]
            node = step_results.get(step_id)
            for attr in parts[1:]:
                if isinstance(node, dict):
                    node = node.get(attr)
                else:
                    node = getattr(node, attr, None)
                if node is None:
                    raise PlanError(f"reference {value} resolved to nothing")
            return node
        if value.startswith("$input:"):
            name = value[len("$input:"):]
            val = (inputs or {}).get(name)
            if val in (None, ""):
                raise PlanError(f"missing input {name!r}")
            return val
        if value.startswith("$context:"):
            key = value[len("$context:"):]
            return (self.env.context.get("mmux_field_context") or {}).get(key)
        raise PlanError(f"unsupported reference {value!r}")
