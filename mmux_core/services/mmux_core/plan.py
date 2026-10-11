# -*- coding: utf-8 -*-
"""Constrained server-side plan orchestration (agent loop).

PRD boundary: no generic agent runtime, no arbitrary ORM. What IS allowed
and implemented here:

- A **Plan** is an ordered list of steps; every step references a
  *registered* capability (whitelist) and a parameter binding map whose
  values are literals, references to earlier step results, or user
  prompts (declared missing inputs).
- The plan is proposed either by code or by the LLM (which sees only the
  capability whitelist) and is **validated server-side** before any
  execution: unknown capability, unknown reference, cyclic binding or a
  parameter the schema cannot accept -> plan rejected.
- Execution is a strict loop: each step goes through the SAME
  ExecutionGuard pipeline (schema, ACL, state, risk confirmation,
  idempotency). The loop stops on first failure or awaiting-confirmation;
  it never "self-heals" by inventing new steps.
- Missing inputs surface as `input_required` with a precise list of what
  to provide — the agent loop resumes when the user answers (FR-005:
  ask, don't guess).
- Plan runs carry their own idempotency key: a rerun of a finished or
  in-flight plan with the same key is refused.
"""
from typing import Any, Dict, List, Optional

from .capability import CapabilityRegistry
from .guard import ExecutionGuard, ExecStatus, GuardError
from .schema import ValidationError


class PlanError(ValueError):
    pass


class PlanStep:
    __slots__ = ("step_id", "capability_id", "bindings", "record_ref", "confirm")

    def __init__(self, step_id, capability_id, bindings=None, record_ref=None,
                 confirm=False):
        self.step_id = step_id
        self.capability_id = capability_id
        self.bindings = dict(bindings or {})
        self.record_ref = record_ref  # literal record id, context ref, or prior-step ref
        self.confirm = confirm

    def to_dict(self):
        return {"step_id": self.step_id, "capability_id": self.capability_id,
                "bindings": self.bindings, "record_ref": self.record_ref,
                "confirm": self.confirm}


class Plan:
    def __init__(self, plan_id, name, steps: List[PlanStep], description=""):
        self.plan_id = plan_id
        self.name = name
        self.steps = list(steps)
        self.description = description

    def to_dict(self):
        return {"plan_id": self.plan_id, "name": self.name,
                "description": self.description,
                "steps": [s.to_dict() for s in self.steps]}


class PlanOrchestrator:
    """Pure engine. The backend additionally implements:
      - resolve_binding(value, step_results, context) -> concrete value
        ("$step2.result.product_id", "$context.res_id", literal passthrough)
      - guard() -> ExecutionGuard (for per-step validation/execution)
      - plan_status(key) / save_plan_run(...) for resumable audit.
    """

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    # -------------------------------------------------------------- #
    # validation: plan must be structurally sound before execution
    # -------------------------------------------------------------- #
    def validate_plan(self, plan: Plan) -> Dict[str, Any]:
        """Returns {'required_inputs': [...]} or raises PlanError."""
        if not plan.steps:
            raise PlanError("plan has no steps")
        step_ids = set()
        required_inputs: List[Dict[str, str]] = []
        for step in plan.steps:
            if step.step_id in step_ids:
                raise PlanError(f"duplicate step_id {step.step_id}")
            step_ids.add(step.step_id)
            cap = self.registry.get(step.capability_id)
            if cap is None:
                raise PlanError(
                    f"step {step.step_id}: capability {step.capability_id!r} "
                    f"is not registered (whitelist)")
            if not cap.enabled:
                raise PlanError(
                    f"step {step.step_id}: capability {step.capability_id!r} is disabled")
            # bindings must reference declared schema fields only
            for key in step.bindings:
                if key not in cap.schema.fields:
                    raise PlanError(
                        f"step {step.step_id}: {cap.capability_id} has no "
                        f"parameter {key!r}")
        # reference integrity: $stepN.result... must point to an EARLIER step
        seen: set = set()
        for step in plan.steps:
            for value in list(step.bindings.values()) + ([step.record_ref] if step.record_ref else []):
                for ref in self._refs(value):
                    if ref.startswith("$step"):
                        target = ref[len("$step"):].split(".", 1)[0]
                        if target not in seen:
                            raise PlanError(
                                f"step {step.step_id}: reference {ref} points to a "
                                f"non-existent or later step")
                    elif ref.startswith("$input"):
                        name = ref[len("$input:"):]
                        required_inputs.append({"input": name, "step": step.step_id})
            seen.add(step.step_id)
        return {"required_inputs": required_inputs}

    @staticmethod
    def _refs(value) -> List[str]:
        """Collect $-references inside a binding (str, or dict/list of them)."""
        out: List[str] = []
        if isinstance(value, str) and value.startswith("$"):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                out.extend(PlanOrchestrator._refs(v))
        elif isinstance(value, (list, tuple)):
            for v in value:
                out.extend(PlanOrchestrator._refs(v))
        return out

    # -------------------------------------------------------------- #
    # execution: strict sequential loop, each step guarded
    # -------------------------------------------------------------- #
    def run(self, plan: Plan, guard: ExecutionGuard, backend,
            inputs: Optional[Dict[str, Any]] = None,
            idempotency_key: Optional[str] = None,
            confirmed_steps: Optional[set] = None) -> Dict[str, Any]:
        inputs = inputs or {}
        confirmed_steps = confirmed_steps or set()
        step_results: Dict[str, Any] = {}
        trace: List[Dict[str, Any]] = []
        state = "running"

        for step in plan.steps:
            # resolve pending user inputs BEFORE binding (pause, don't fail)
            missing = [name for name in self._input_names(step)
                       if inputs.get(name) in (None, "")]
            if missing:
                trace.append({"step": step.step_id, "status": "input_required",
                              "missing": missing})
                state = "input_required"
                break

            # bind parameters
            try:
                params = {k: backend.resolve_binding(v, step_results, inputs)
                          for k, v in step.bindings.items()}
                record_id = None
                if step.record_ref:
                    record_id = backend.resolve_binding(step.record_ref, step_results, inputs)
            except PlanError as exc:
                trace.append({"step": step.step_id, "status": "failed",
                              "error": str(exc)})
                state = "failed"
                break

            try:
                plan_ret = guard.validate(
                    step.capability_id, params, record_id,
                    idempotency_key=f"{idempotency_key}:{step.step_id}"
                    if idempotency_key else None,
                    confirmed=step.step_id in confirmed_steps)
                if plan_ret["status"] == ExecStatus.AWAITING_CONFIRMATION:
                    trace.append({"step": step.step_id, "status": "awaiting_confirmation",
                                  "confirmation": plan_ret["confirmation"]})
                    state = "awaiting_confirmation"
                    break
                res = guard.execute(
                    plan_ret,
                    idempotency_key=f"{idempotency_key}:{step.step_id}"
                    if idempotency_key else None,
                    confirmed=step.step_id in confirmed_steps)
                step_results[step.step_id] = res.get("result") or {}
                trace.append({"step": step.step_id, "capability": step.capability_id,
                              "status": res["status"], "result": res.get("result")})
            except GuardError as exc:
                trace.append({"step": step.step_id, "capability": step.capability_id,
                              "status": exc.status.value, "error": str(exc)})
                state = "failed" if exc.status != ExecStatus.UNKNOWN else "unknown"
                break

        if state == "running":
            state = "success"
        return {
            "plan_id": plan.plan_id,
            "state": state,           # success | failed | unknown |
                                      # input_required | awaiting_confirmation
            "steps": trace,
            "results": step_results,
        }

    def _input_names(self, step: PlanStep) -> List[str]:
        names = []
        for value in list(step.bindings.values()) + ([step.record_ref] if step.record_ref else []):
            for ref in self._refs(value):
                if ref.startswith("$input:"):
                    names.append(ref[len("$input:"):])
        return names


# ------------------------------------------------------------------ #
# Plan library: code-defined named plans (like capabilities, code first)
# ------------------------------------------------------------------ #
PLAN_LIBRARY: Dict[str, Plan] = {}


def register_plan(plan: Plan) -> Plan:
    if plan.plan_id in PLAN_LIBRARY:
        raise ValueError(f"duplicate plan id {plan.plan_id}")
    PLAN_LIBRARY[plan.plan_id] = plan
    return plan


def validate_llm_plan(data: Any, registry: CapabilityRegistry) -> Plan:
    """Validate an LLM-proposed plan. The model may only emit:
    {plan_id, name, steps: [{step_id, capability_id, bindings,
    record_ref}]} with registered capability ids; everything else is
    rejected. $-references are allowed but integrity-checked by
    validate_plan at run time — no capability outside the whitelist can
    ever enter a plan (FR-015 preserved)."""
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise PlanError("plan must be an object with a steps list")
    plan_id = data.get("plan_id") or f"llm_plan_{id(data) % 100000}"
    steps = []
    for raw in data["steps"]:
        if not isinstance(raw, dict):
            raise PlanError("each step must be an object")
        cap = registry.get(raw.get("capability_id", ""))
        if cap is None:
            raise PlanError(f"capability {raw.get('capability_id')!r} not registered")
        steps.append(PlanStep(
            step_id=raw.get("step_id") or f"s{len(steps) + 1}",
            capability_id=cap.capability_id,
            bindings=raw.get("bindings") or {},
            record_ref=raw.get("record_ref"),
        ))
    plan = Plan(plan_id, data.get("name") or "LLM proposed plan", steps,
                data.get("description", ""))
    PlanOrchestrator(registry).validate_plan(plan)
    return plan
