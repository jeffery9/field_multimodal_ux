# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Capability access control and execution: the permission gate plus the
single choke point every capability invocation flows through.
"""
import logging

from odoo.exceptions import AccessError

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
    # check_access
    # -------------------------------------------------------------- #
    @api.model
    def check_access(self, cap, record_id):
        Model = self.env[cap.target_model]
        record = Model.browse(int(record_id)).exists()
        if not record:
            raise GuardError(ExecStatus.FAILED,
                             _("The target record no longer exists."),
                             {"record_id": record_id})
        try:
            record.check_access_rights("read")
            record.check_access_rule("read")
        except AccessError as exc:
            raise GuardError(ExecStatus.FAILED,
                             _("You do not have access to this record."),
                             {"record_id": record_id, "detail": str(exc)})
        return record

    # -------------------------------------------------------------- #
    # precondition
    # -------------------------------------------------------------- #
    @api.model
    def precondition(self, cap, record, params):
        """Native state check hook. Scenario modules may register a
        `precondition` callable on the Capability; it receives (record,
        params) and must raise GuardError/UserError on violation."""
        if cap.precondition:
            cap.precondition(record, params)

    # -------------------------------------------------------------- #
    # execute
    # -------------------------------------------------------------- #
    @api.model
    def execute(self, cap, record, params):
        if cap.executor is None:
            raise GuardError(ExecStatus.FAILED,
                             _("capability %s has no executor") % cap.capability_id)
        try:
            return cap.executor(self.env, record, params)
        except (UserError, OdooValidationError, AccessError) as exc:
            # business rule rejection -> clean failure, transaction rolls back
            raise GuardError(ExecStatus.FAILED, str(exc))

    # -------------------------------------------------------------- #
    # verify_result
    # -------------------------------------------------------------- #
    @api.model
    def verify_result(self, cap, record, result):
        if cap.result_check:
            return cap.result_check(self.env, record, result)
        return result if isinstance(result, dict) else {"result": result}

    # -------------------------------------------------------------- #
    # execute_capability
    # -------------------------------------------------------------- #
    @api.model
    def execute_capability(self, capability_id, params, record_id=None,
                           idem_key=None, confirmed=False):
        """The single funnel used by /mmux/execute. Audits every attempt."""
        guard = self._guard()
        Log = self.env["mmux.interaction.log"].sudo()
        log_vals = {
            "user_id": self.env.user.id,
            "capability_id_name": capability_id,
            "input_type": "nl",
            "idempotency_key": idem_key or "",
        }
        try:
            plan = guard.validate(capability_id, params or {}, record_id,
                                  idem_key, confirmed=confirmed)
            if plan["status"] == ExecStatus.AWAITING_CONFIRMATION:
                log_vals.update(exec_status=ExecStatus.AWAITING_CONFIRMATION.value,
                                detail=_("awaiting user confirmation"))
                Log.create(log_vals)
                return {"status": ExecStatus.AWAITING_CONFIRMATION.value,
                        "confirmation": plan["confirmation"]}
            result = guard.execute(plan, idempotency_key=idem_key, confirmed=confirmed)
            log_vals.update(exec_status=result["status"],
                            detail=str(result.get("result", ""))[:4000])
            Log.create(log_vals)
            return result
        except GuardError as exc:
            log_vals.update(exec_status=exc.status.value, detail=str(exc)[:4000])
            Log.create(log_vals)
            return {"status": exc.status.value, "error": str(exc),
                    "details": exc.details}
        except Exception as exc:  # defensive: never leak a raw success
            _logger.exception("mmux execute_capability crash")
            log_vals.update(exec_status=ExecStatus.FAILED.value, detail=str(exc)[:4000])
            Log.create(log_vals)
            return {"status": ExecStatus.FAILED.value, "error": _("unexpected server error")}

    # -------------------------------------------------------------- #
    # resolve_barcode
    # -------------------------------------------------------------- #
    @api.model
    def resolve_barcode(self, barcode, models_hint=()):
        """Resolve a scanned barcode to candidate Odoo records (FR-003/008).
        Returns candidates with distinguishing info; never auto-picks a
        high-impact object for the caller."""
        candidates = []
        hints = models_hint or ["product.product", "stock.lot", "stock.picking",
                                "stock.location"]
        for model_name in hints:
            Model = self.env[model_name]
            if not Model._fields.get("barcode"):
                continue
            try:
                recs = Model.search([("barcode", "=", barcode)], limit=5)
            except Exception:
                continue
            for rec in recs:
                candidates.append({
                    "model": model_name,
                    "id": rec.id,
                    "display_name": rec.display_name,
                })
        # product by default_code as fallback
        Product = self.env["product.product"]
        for rec in Product.search([("default_code", "=", barcode)], limit=5):
            candidates.append({"model": "product.product", "id": rec.id,
                               "display_name": rec.display_name})
        return candidates

    # -------------------------------------------------------------- #
    # capabilities_for_user
    # -------------------------------------------------------------- #
    @api.model
    def capabilities_for_user(self, scenario=""):
        user = self.env.user
        out = []
        for cap in CAPABILITY_REGISTRY.for_scenario(scenario):
            if not cap.enabled:
                continue
            if cap.groups and not all(user.has_group(g) if hasattr(user, "has_group")
                                      else self.has_user_group(g) for g in cap.groups):
                continue
            out.append(cap.to_public_dict())
        return out
