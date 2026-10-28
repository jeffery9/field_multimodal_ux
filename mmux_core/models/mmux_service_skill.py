# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""User-facing preferences: language resolution and skill level.
Leaf helpers -- they depend on nothing but `res.users`.
"""
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
    # _user_lang
    # -------------------------------------------------------------- #
    @api.model
    def _user_lang(self):
        """The current user's language, normalised (zh_CN / en_US / ...).
        Used for both TTS voice selection and step text localization."""
        return normalize_lang(self.env.user.lang)

    # -------------------------------------------------------------- #
    # _user_skill_level
    # -------------------------------------------------------------- #
    @api.model
    def _user_skill_level(self):
        """Novice (default, detailed guidance) or expert (terse). Stored as
        a user preference so it survives sessions (PRD §9.3).

        Read as the current user, with no elevation: this is the user's own
        preference field, and reading it requires no more than the ordinary
        record rules already grant. An earlier version read it through
        `sudo()`, which granted nothing needed and hid the fact that the
        permission model was never exercised here at all.
        """
        value = self.env.user.mmux_skill_level or "novice"
        return value if value in ("novice", "expert") else "novice"

    # -------------------------------------------------------------- #
    # set_skill_level
    # -------------------------------------------------------------- #
    @api.model
    def set_skill_level(self, level):
        if level not in ("novice", "expert"):
            return {"error": _("invalid skill level")}
        self.env.user.mmux_skill_level = level
        return {"skill_level": level}
