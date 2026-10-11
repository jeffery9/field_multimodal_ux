# -*- coding: utf-8 -*-
"""Framework-agnostic unit tests for the pure core.

Run:  python3 -m unittest discover -s tests_pure -t .
(from the repo root). No Odoo runtime required.
"""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.schema import ParamSchema, Field, ValidationError  # noqa: E402
from mmux_core.capability import Capability, CapabilityRegistry  # noqa: E402
from mmux_core.guard import ExecutionGuard, ExecStatus, GuardError, Risk  # noqa: E402
from mmux_core.intent import IntentParser  # noqa: E402
from mmux_core.quantity import extract_quantities  # noqa: E402
from mmux_core.provider import ProviderRegistry, ProviderError, REGISTRY as PREG  # noqa: E402
from mmux_core.idempotency import idempotency_key  # noqa: E402


def make_registry():
    reg = CapabilityRegistry()
    schema = ParamSchema([
        Field("quantity", "float", required=True, min_val=0),
        Field("note", "str", required=False),
    ])
    reg.register(Capability(
        "test_report", "Report", "desc", "test.move", "write",
        schema=schema, risk="L2", scenario="test"))
    reg.register(Capability(
        "test_high", "High risk", "desc", "test.move", "post",
        schema=ParamSchema([]), risk="L3"))
    reg.register(Capability(
        "test_query", "Query", "desc", "test.product", "read",
        schema=ParamSchema([Field("barcode", "str", required=False)]),
        risk="L0"))
    return reg


class FakeBackend:
    """Minimal BackendOps double implementing the FR-016 pipeline."""

    def __init__(self):
        self.executed = []
        self.keys = {}
        self.no_access_ids = set()
        self.fail_execute = False
        self.groups = set()

    def has_user_group(self, g):
        return g in self.groups

    def check_access(self, cap, record_id):
        if record_id in self.no_access_ids:
            raise GuardError(ExecStatus.FAILED, "no access")
        return {"id": record_id, "display_name": f"rec {record_id}"}

    def precondition(self, cap, record, params):
        if cap.capability_id == "test_report" and record and record["id"] == 99:
            raise GuardError(ExecStatus.FAILED, "invalid state")

    def execute(self, cap, record, params):
        if self.fail_execute:
            raise RuntimeError("backend crashed mid-flight")
        self.executed.append((cap.capability_id, record and record["id"], params))
        return {"ok": True}

    def verify_result(self, cap, record, result):
        assert result == {"ok": True}
        return result

    def is_duplicate(self, key):
        return key in self.keys and self.keys[key] in (
            ExecStatus.SUBMITTED.value, ExecStatus.SUCCESS.value, ExecStatus.UNKNOWN.value)

    def mark_executed(self, key, status):
        if key:
            self.keys[key] = status


class TestSchema(unittest.TestCase):
    def test_ok_and_coercion(self):
        s = ParamSchema([Field("q", "float", required=True, min_val=0),
                         Field("n", "str", required=False)])
        out = s.validate({"q": 5})  # int -> float coercion
        self.assertEqual(out, {"q": 5.0})

    def test_unknown_rejected(self):
        s = ParamSchema([Field("q", "float", required=True)])
        with self.assertRaises(ValidationError):
            s.validate({"q": 1.0, "evil": "x"})

    def test_missing_required(self):
        s = ParamSchema([Field("q", "float", required=True)])
        with self.assertRaises(ValidationError):
            s.validate({})

    def test_enum_and_bounds(self):
        s = ParamSchema([Field("e", "str", enum=["a", "b"]),
                         Field("m", "int", min_val=1, max_val=10)])
        self.assertEqual(s.validate({"e": "a", "m": 3}), {"e": "a", "m": 3})
        with self.assertRaises(ValidationError):
            s.validate({"e": "c", "m": 3})
        with self.assertRaises(ValidationError):
            s.validate({"e": "a", "m": 99})

    def test_bool_strictness(self):
        s = ParamSchema([Field("b", "bool", required=True)])
        with self.assertRaises(ValidationError):
            s.validate({"b": "yes"})


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.reg = make_registry()
        self.backend = FakeBackend()
        self.guard = ExecutionGuard(self.reg, self.backend)

    def test_l0_query_executes_without_confirmation(self):
        plan = self.guard.validate("test_query", {"barcode": "8888"})
        res = self.guard.execute(plan)
        self.assertEqual(res["status"], ExecStatus.SUCCESS)

    def test_l2_requires_confirmation(self):
        plan = self.guard.validate("test_report", {"quantity": 10}, record_id=1)
        self.assertEqual(plan["status"], ExecStatus.AWAITING_CONFIRMATION)
        # confirming in the same request proceeds
        res = self.guard.execute(plan, confirmed=True)
        self.assertEqual(res["status"], ExecStatus.SUCCESS)
        self.assertEqual(self.backend.executed[0][2]["quantity"], 10.0)

    def test_l3_disabled_by_default(self):
        with self.assertRaises(GuardError) as ctx:
            self.guard.validate("test_high", {})
        self.assertEqual(ctx.exception.status, ExecStatus.FAILED)
        self.assertIn("disabled", str(ctx.exception))

    def test_unknown_capability(self):
        with self.assertRaises(GuardError):
            self.guard.validate("hacker_capability", {})

    def test_param_validation(self):
        with self.assertRaises(GuardError) as ctx:
            self.guard.validate("test_report", {"quantity": -5}, record_id=1)
        self.assertIn("invalid parameters", str(ctx.exception))

    def test_access_denied(self):
        self.backend.no_access_ids.add(42)
        with self.assertRaises(GuardError):
            self.guard.validate("test_report", {"quantity": 1}, record_id=42)

    def test_precondition_state_rejection(self):
        with self.assertRaises(GuardError) as ctx:
            self.guard.validate("test_report", {"quantity": 1}, record_id=99)
        self.assertIn("invalid state", str(ctx.exception))

    def test_idempotency_blocks_duplicate(self):
        key = idempotency_key()
        plan = self.guard.validate("test_query", {}, idempotency_key=key)
        self.guard.execute(plan, idempotency_key=key)
        with self.assertRaises(GuardError) as ctx:
            self.guard.validate("test_query", {}, idempotency_key=key)
        self.assertIn("duplicate", str(ctx.exception))

    def test_crash_yields_unknown_not_failed(self):
        key = idempotency_key()
        plan = self.guard.validate("test_query", {})
        self.backend.fail_execute = True
        with self.assertRaises(GuardError) as ctx:
            self.guard.execute(plan, idempotency_key=key)
        self.assertEqual(ctx.exception.status, ExecStatus.UNKNOWN)
        self.assertEqual(self.backend.keys[key], ExecStatus.UNKNOWN.value)
        # a retry with the same key is refused: user must verify first
        with self.assertRaises(GuardError):
            self.guard.validate("test_query", {}, idempotency_key=key)


class TestIntent(unittest.TestCase):
    """LLM-first intent validation (no rule parser any more)."""

    def setUp(self):
        self.reg = make_registry()
        self.parser = IntentParser(self.reg)

    def test_quantity_extraction_zh(self):
        q = extract_quantities("本工序完成120件，其中3件不合格")
        self.assertEqual(q["qty"], 120.0)
        self.assertEqual(q["scrap"], 3.0)

    def test_quantity_extraction_en(self):
        q = extract_quantities("done 50 units, 2 scrap")
        self.assertEqual(q["qty"], 50.0)
        self.assertEqual(q["scrap"], 2.0)

    def test_llm_output_validated(self):
        intent = self.parser.validate_llm_output(
            {"capability_id": "test_report",
             "normalized_text": "report 3 units",
             "params": {"quantity": 3}, "confidence": 0.9})
        self.assertEqual(intent.capability_id, "test_report")
        self.assertEqual(intent.params["quantity"], 3.0)
        self.assertEqual(intent.normalized_text, "report 3 units")
        self.assertAlmostEqual(intent.confidence, 0.9, places=2)

    def test_llm_empty_capability_is_empty_intent(self):
        intent = self.parser.validate_llm_output(
            {"capability_id": None, "normalized_text": "the weather is nice",
             "params": {}})
        self.assertTrue(intent.is_empty)
        self.assertIn("capability_id", intent.missing)

    def test_llm_hallucinated_capability_rejected(self):
        intent = self.parser.validate_llm_output(
            {"capability_id": "stock.picking:write", "params": {"x": 1}})
        self.assertTrue(intent.is_empty)
        self.assertTrue(intent.ambiguous)

    def test_llm_arbitrary_orm_rejected(self):
        """命名模型/方法是协议违规：直接拒绝，不做任何修补。"""
        intent = self.parser.validate_llm_output(
            {"model": "res.partner", "method": "unlink", "ids": [1]})
        self.assertTrue(intent.is_empty)
        self.assertTrue(any("model/method" in a for a in intent.ambiguous))

    def test_llm_bad_params_reported_missing(self):
        intent = self.parser.validate_llm_output(
            {"capability_id": "test_report", "params": {}})
        self.assertIn("quantity", intent.missing)

    def test_llm_missing_normalized_text_is_flagged(self):
        intent = self.parser.validate_llm_output(
            {"capability_id": "test_report", "params": {"quantity": 2}})
        self.assertIn("normalized_text", intent.missing)

    def test_llm_output_not_dict_is_empty(self):
        intent = self.parser.validate_llm_output([1, 2, 3], text="hi")
        self.assertTrue(intent.is_empty)
        self.assertIn("capability_id", intent.missing)

    def test_languages_detected_for_audit(self):
        intent = self.parser.validate_llm_output(
            {"capability_id": "test_report", "params": {"quantity": 1}},
            text="把 stock 改成 5")
        self.assertIn("zh", intent.languages)
        self.assertIn("en", intent.languages)
        self.assertEqual(intent.lang_hint, "zh+en")

    def test_barcode_not_mistaken_for_quantity(self):
        """一致性护栏：型号编码里的数字不得被当成数量。"""
        from mmux_core.quantity import extract_quantities as eq
        q = eq("把 MMUX-0001 的 stock 数量改成 5 个")
        self.assertEqual(q["barcode"], "MMUX-0001")
        self.assertEqual(q["qty"], 5.0)

    def test_reconcile_flags_mismatch(self):
        """LLM 抽取的数量与文本不一致时，写入 ambiguous 而非静默采信。"""
        from mmux_core.quantity import reconcile_with_text
        params, notes = reconcile_with_text({"quantity": 8}, "完成 5 件")
        self.assertTrue(notes)
        self.assertIn("quantity", notes[0])

    def test_reconcile_fills_missing_quantity(self):
        from mmux_core.quantity import reconcile_with_text
        params, notes = reconcile_with_text({"quantity": None}, "完成 7 件")
        self.assertEqual(params["quantity"], 7.0)


class TestProviders(unittest.TestCase):
    def test_mock_asr_fixture(self):
        out = PREG.call("asr", "mock", {}, {"mmux.asr.mock_transcript": "hello"})
        self.assertEqual(out["text"], "hello")

    def test_mock_llm_canned(self):
        out = PREG.call("llm", "mock", {},
                        {"mmux.llm.mock_response": {"capability_id": "x"}})
        self.assertEqual(out["data"]["capability_id"], "x")

    def test_unknown_provider_raises(self):
        with self.assertRaises(ProviderError):
            PREG.call("asr", "nope", {}, {})


class TestIdempotency(unittest.TestCase):
    def test_key_shape(self):
        k = idempotency_key()
        self.assertTrue(k.startswith("mmux:"))
        self.assertNotEqual(k, idempotency_key())


if __name__ == "__main__":
    unittest.main()
