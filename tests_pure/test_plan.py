# -*- coding: utf-8 -*-
"""Unit tests for the constrained plan orchestrator (agent loop)."""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.capability import Capability, CapabilityRegistry  # noqa: E402
from mmux_core.schema import ParamSchema, Field  # noqa: E402
from mmux_core.guard import ExecutionGuard, ExecStatus, GuardError  # noqa: E402
from mmux_core.plan import (  # noqa: E402
    Plan, PlanStep, PlanOrchestrator, PlanError, register_plan, validate_llm_plan,
    PLAN_LIBRARY,
)


def make_registry():
    reg = CapabilityRegistry()
    reg.register(Capability(
        "q_product", "Query", "", "product.product", "read",
        schema=ParamSchema([Field("barcode", "str", required=False)]),
        risk="L0"))
    reg.register(Capability(
        "report_qty", "Report", "", "stock.move.line", "write",
        schema=ParamSchema([Field("quantity", "float", required=True, min_val=0)]),
        risk="L2", require_confirmation=False, scenario="wh"))
    reg.register(Capability(
        "validate", "Validate", "", "stock.picking", "button_validate",
        schema=ParamSchema([]), risk="L2", require_confirmation=True))
    return reg


class FakeBackend:
    def __init__(self):
        self.keys = {}

    def resolve_binding(self, value, step_results, inputs):
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        if value.startswith("$input:"):
            name = value[len("$input:"):]
            if inputs.get(name) in (None, ""):
                raise PlanError(f"missing input {name!r}")
            return inputs[name]
        if value.startswith("$step"):
            parts = value[len("$step"):].strip(".").split(".")
            node = step_results.get(parts[0])
            for attr in parts[1:]:
                node = (node or {}).get(attr) if isinstance(node, dict) else None
            if node is None:
                raise PlanError(f"reference {value} resolved to nothing")
            return node
        if value.startswith("$context:"):
            return None  # simplified for pure tests
        raise PlanError(f"unsupported reference {value!r}")

    # guard BackendOps
    def has_user_group(self, g):
        return True

    def check_access(self, cap, record_id):
        return {"id": record_id, "display_name": f"rec{record_id}"}

    def precondition(self, cap, record, params):
        pass

    def execute(self, cap, record, params):
        if cap.capability_id == "q_product":
            return {"product_id": 42, "found": True}
        return {"ok": True}

    def verify_result(self, cap, record, result):
        return result

    def is_duplicate(self, key):
        return key in self.keys and self.keys[key] in (
            "submitted", "success", "unknown")

    def mark_executed(self, key, status):
        if key:
            self.keys[key] = status


class TestPlanValidation(unittest.TestCase):
    def setUp(self):
        self.reg = make_registry()
        self.orch = PlanOrchestrator(self.reg)

    def test_valid_plan_lists_required_inputs(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "q_product", bindings={"barcode": "$input:bc"}),
            PlanStep("s2", "report_qty",
                     bindings={"quantity": "$input:qty"},
                     record_ref="$context:res_id"),
        ])
        meta = self.orch.validate_plan(plan)
        names = {i["input"] for i in meta["required_inputs"]}
        self.assertEqual(names, {"bc", "qty"})

    def test_unknown_capability_rejected(self):
        plan = Plan("p1", "P", [PlanStep("s1", "evil_orm_write", bindings={})])
        with self.assertRaises(PlanError):
            self.orch.validate_plan(plan)

    def test_forward_reference_rejected(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "report_qty",
                     bindings={"quantity": "$step2.result.x"}),
            PlanStep("s2", "q_product", bindings={}),
        ])
        with self.assertRaises(PlanError):
            self.orch.validate_plan(plan)

    def test_unknown_param_binding_rejected(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "report_qty", bindings={"evil_field": 1}),
        ])
        with self.assertRaises(PlanError):
            self.orch.validate_plan(plan)

    def test_llm_plan_validated(self):
        plan = validate_llm_plan({
            "plan_id": "llm_p", "name": "LLM plan",
            "steps": [{"step_id": "s1", "capability_id": "q_product",
                       "bindings": {"barcode": "$input:bc"}}],
        }, self.reg)
        self.assertEqual(plan.steps[0].capability_id, "q_product")

    def test_llm_plan_hallucination_rejected(self):
        with self.assertRaises(PlanError):
            validate_llm_plan({
                "steps": [{"step_id": "s1",
                           "capability_id": "stock.picking:unlink",
                           "bindings": {}}],
            }, self.reg)


class TestPlanRun(unittest.TestCase):
    def setUp(self):
        self.reg = make_registry()
        self.orch = PlanOrchestrator(self.reg)
        self.backend = FakeBackend()
        self.guard = ExecutionGuard(self.reg, self.backend)

    def test_run_success_with_step_reference(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "q_product", bindings={"barcode": "MMUX-1"}),
            PlanStep("s2", "report_qty",
                     bindings={"quantity": "$input:qty"},
                     record_ref=1),
        ], )
        res = self.orch.run(plan, self.guard, self.backend,
                            inputs={"qty": 5},
                            confirmed_steps={"s2"})
        self.assertEqual(res["state"], "success")
        self.assertEqual(res["steps"][0]["result"]["product_id"], 42)
        self.assertEqual(res["steps"][1]["status"], ExecStatus.SUCCESS.value)

    def test_run_pauses_on_missing_input(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "q_product", bindings={"barcode": "$input:bc"}),
        ])
        res = self.orch.run(plan, self.guard, self.backend, inputs={})
        self.assertEqual(res["state"], "input_required")
        self.assertIn("bc", res["steps"][0]["missing"])

    def test_run_pauses_on_confirmation(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "validate", record_ref=7),
        ])
        res = self.orch.run(plan, self.guard, self.backend, inputs={})
        self.assertEqual(res["state"], "awaiting_confirmation")
        # resume: user confirms the step
        res2 = self.orch.run(plan, self.guard, self.backend, inputs={},
                             confirmed_steps={"s1"})
        self.assertEqual(res2["state"], "success")

    def test_run_stops_on_failure_no_selfheal(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "report_qty", bindings={"quantity": -1},
                     record_ref=1),
            PlanStep("s2", "q_product", bindings={}),
        ])
        res = self.orch.run(plan, self.guard, self.backend,
                            inputs={}, confirmed_steps={"s1", "s2"})
        self.assertEqual(res["state"], "failed")
        self.assertEqual(len(res["steps"]), 1)  # s2 never ran

    def test_per_step_idempotency(self):
        plan = Plan("p1", "P", [
            PlanStep("s1", "q_product", bindings={}),
        ])
        key = "mmux:planrun-1"
        res = self.orch.run(plan, self.guard, self.backend, idempotency_key=key)
        self.assertEqual(res["state"], "success")
        # replaying the same finished plan run is refused by step-level guard
        res2 = self.orch.run(plan, self.guard, self.backend,
                             idempotency_key=key, confirmed_steps={"s1"})
        self.assertEqual(res2["state"], "failed")
        self.assertIn("duplicate", res2["steps"][0].get("error", ""))


if __name__ == "__main__":
    unittest.main()
