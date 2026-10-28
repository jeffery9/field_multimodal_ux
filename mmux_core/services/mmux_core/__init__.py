# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Pure-Python core for Field Multimodal UX.

This package must not import `odoo`. It contains the framework-agnostic
building blocks (schema validation, capability/provider registries, risk
policy, idempotency keys, result state machine) that the Odoo layer in
`mmux_core` wires together. This keeps the security-critical logic unit
testable outside an Odoo runtime.
"""
from .schema import ParamSchema, Field, ValidationError
from .capability import Capability, CapabilityRegistry, REGISTRY as CAPABILITY_REGISTRY
from .provider import ProviderRegistry, ProviderError, REGISTRY as PROVIDER_REGISTRY
from .guard import Risk, ExecStatus, ExecutionGuard, GuardError
from .intent import Intent, IntentParser, NORMALIZED_LANG, FORBIDDEN_KEYS
from .quantity import extract_quantities, reconcile_with_text
from .idempotency import idempotency_key
from .plan import (Plan, PlanStep, PlanOrchestrator, PlanError,
                   PLAN_LIBRARY, register_plan, validate_llm_plan)
from .flow import (GuidedStep, TaskFlow, TaskFlowEngine,
                   FLOW_ENGINE, register_flow)
from .taskqueue import (TaskSource, Task, TaskQueueEngine,
                        TASK_ENGINE, register_task_source,
                        next_task, build_loop_state, loop_label, LOOP_STATES)
from .agent import (AgentPick, validate_pick, fallback_pick,
                    MAX_ALTERNATIVES, REASON_LANG_KEYS,
                    SOURCE_LLM, SOURCE_FALLBACK, SOURCE_EMPTY)
from .agentloop import (AgentAction, AgentObservation, AgentScratchpad,
                        AgentTurn, validate_action, build_loop_prompt,
                        step_from_action, execution_request, run_loop,
                        AGENT_ACTIONS, ACTION_PAYLOAD_KEYS, SCRATCHPAD_KEYS,
                        LOOP_STATES, LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE,
                        LOOP_SOURCE_NONE, MAX_AGENT_TURNS,
                        MAX_AGENT_CAPABILITY_CALLS, MAX_REPEATED_STEP,
                        MAX_STALLED_TURNS, MAX_RECENT_OUTCOMES,
                        MAX_PROMPT_TURNS, fold_turn_progress,
                        ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER,
                        ACT_ANSWER, ACT_FINISH, ACT_DELEGATE_PICK)
from .i18n import (normalize_lang, language_chain, detect_languages,
                   is_mixed, mixed_language_hint, resolve_text, resolve_dict)
from .scenario import (ContextSignal, ScenarioHint, ScenarioChoice,
                       ScenarioResolution, resolve_scenario, collect_hints,
                       validate_scenario_choice, choice_from_answer,
                       build_scenario_prompt, build_ask_question,
                       unresolved_with_all,
                       RES_MODEL_MAP, ACTION_XMLID_MAP, MENU_XMLID_MAP,
                       AMBIGUOUS_RES_MODEL_MAP,
                       CHOICE_KEYS, SCENARIO_SOURCES, MAX_CANDIDATES,
                       WEIGHT_ACTION_XMLID, WEIGHT_RES_MODEL,
                       WEIGHT_MENU_XMLID, WEIGHT_GEO, MIN_DECISIVE_SCORE,
                       SOURCE_SIGNAL, SOURCE_AMBIGUOUS, SOURCE_NONE,
                       SOURCE_USER, CHOICE_SOURCE_LLM,
                       CHOICE_SOURCE_DETERMINISTIC)

__all__ = [
    "ParamSchema", "Field", "ValidationError",
    "Capability", "CapabilityRegistry", "CAPABILITY_REGISTRY",
    "ProviderRegistry", "ProviderError", "PROVIDER_REGISTRY",
    "Risk", "ExecStatus", "ExecutionGuard", "GuardError",
    "Intent", "IntentParser", "NORMALIZED_LANG", "FORBIDDEN_KEYS",
    "extract_quantities", "reconcile_with_text",
    "idempotency_key",
    "Plan", "PlanStep", "PlanOrchestrator", "PlanError",
    "PLAN_LIBRARY", "register_plan", "validate_llm_plan",
    "GuidedStep", "TaskFlow", "TaskFlowEngine",
    "FLOW_ENGINE", "register_flow",
    "TaskSource", "Task", "TaskQueueEngine",
    "TASK_ENGINE", "register_task_source",
    "next_task", "build_loop_state", "loop_label", "LOOP_STATES",
    "AgentPick", "validate_pick", "fallback_pick",
    "MAX_ALTERNATIVES", "REASON_LANG_KEYS",
    "SOURCE_LLM", "SOURCE_FALLBACK", "SOURCE_EMPTY",
    "AgentAction", "AgentObservation", "AgentScratchpad", "AgentTurn",
    "validate_action", "build_loop_prompt", "step_from_action",
    "execution_request", "run_loop",
    "AGENT_ACTIONS", "ACTION_PAYLOAD_KEYS", "SCRATCHPAD_KEYS", "LOOP_STATES",
    "LOOP_SOURCE_LLM", "LOOP_SOURCE_RECIPE", "LOOP_SOURCE_NONE",
    "MAX_AGENT_TURNS", "MAX_AGENT_CAPABILITY_CALLS", "MAX_REPEATED_STEP",
    "MAX_STALLED_TURNS", "MAX_RECENT_OUTCOMES",
    "MAX_PROMPT_TURNS", "fold_turn_progress",
    "ACT_CALL_CAPABILITY", "ACT_PROPOSE_STEP", "ACT_ASK_USER",
    "ACT_ANSWER", "ACT_FINISH", "ACT_DELEGATE_PICK",
    "normalize_lang", "language_chain", "detect_languages",
    "is_mixed", "mixed_language_hint", "resolve_text", "resolve_dict",
    "ContextSignal", "ScenarioHint", "ScenarioChoice", "ScenarioResolution",
    "resolve_scenario", "collect_hints", "validate_scenario_choice",
    "choice_from_answer", "build_scenario_prompt", "build_ask_question",
    "unresolved_with_all",
    "RES_MODEL_MAP", "ACTION_XMLID_MAP", "MENU_XMLID_MAP",
    "AMBIGUOUS_RES_MODEL_MAP",
    "CHOICE_KEYS", "SCENARIO_SOURCES", "MAX_CANDIDATES",
    "WEIGHT_ACTION_XMLID", "WEIGHT_RES_MODEL", "WEIGHT_MENU_XMLID",
    "WEIGHT_GEO", "MIN_DECISIVE_SCORE",
    "SOURCE_SIGNAL", "SOURCE_AMBIGUOUS", "SOURCE_NONE", "SOURCE_USER",
    "CHOICE_SOURCE_LLM", "CHOICE_SOURCE_DETERMINISTIC",
]
