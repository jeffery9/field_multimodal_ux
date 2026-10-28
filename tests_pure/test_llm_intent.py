# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""LLM-first intent path: validation, normalisation, consistency guard.

Simulates what the service layer does after the LLM answers: it validates
the raw JSON through IntentParser and asserts the shape the execution
layer will see (capability_id + params) plus the audit-only fields
(normalized_text, languages, confidence).
"""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.capability import Capability, CapabilityRegistry  # noqa: E402
from mmux_core.schema import ParamSchema, Field  # noqa: E402
from mmux_core.intent import IntentParser, NORMALIZED_LANG, FORBIDDEN_KEYS  # noqa: E402


def make_registry():
    reg = CapabilityRegistry()
    reg.register(Capability(
        "picking_report_qty", "Report qty", "desc", "stock.move.line", "write",
        schema=ParamSchema([
            Field("quantity", "float", required=True, min_val=0),
            Field("barcode", "str", required=False),
        ]), risk="L2"))
    reg.register(Capability(
        "stock_query_product", "Query", "desc", "product.product", "read",
        schema=ParamSchema([Field("barcode", "str", required=False)]), risk="L0"))
    return reg


class TestLLMIntentValidation(unittest.TestCase):
    def setUp(self):
        self.parser = IntentParser(make_registry())

    def test_valid_answer_becomes_intent(self):
        intent = self.parser.validate_llm_output({
            "normalized_text": "set the stock quantity of MMUX-0001 to 5 pieces",
            "capability_id": "picking_report_qty",
            "params": {"quantity": 5, "barcode": "MMUX-0001"},
            "missing": [], "ambiguous": [], "confidence": 0.92,
        }, text="把 MMUX-0001 的 stock 数量改成 5 个")
        self.assertEqual(intent.capability_id, "picking_report_qty")
        self.assertEqual(intent.params["quantity"], 5.0)
        self.assertEqual(intent.params["barcode"], "MMUX-0001")
        self.assertAlmostEqual(intent.confidence, 0.92, places=2)
        self.assertEqual(intent.source, "llm")

    def test_normalized_text_is_preserved(self):
        intent = self.parser.validate_llm_output({
            "normalized_text": "report 3 units",
            "capability_id": "picking_report_qty", "params": {"quantity": 3},
        })
        self.assertEqual(intent.normalized_text, "report 3 units")
        self.assertIn("normalized_text", intent.to_dict())

    def test_null_capability_is_empty_not_error(self):
        intent = self.parser.validate_llm_output({
            "normalized_text": "the weather is nice today",
            "capability_id": None, "params": {}})
        self.assertTrue(intent.is_empty)
        self.assertIn("capability_id", intent.missing)

    def test_hallucinated_id_rejected(self):
        intent = self.parser.validate_llm_output({
            "capability_id": "sale.order.line:create", "params": {}})
        self.assertTrue(intent.is_empty)
        self.assertTrue(any("unknown capability" in a for a in intent.ambiguous))

    def test_forbidden_keys_are_protocol_violation(self):
        for key in sorted(FORBIDDEN_KEYS):
            with self.subTest(key=key):
                intent = self.parser.validate_llm_output({
                    "capability_id": "picking_report_qty",
                    "params": {"quantity": 1}, key: "whatever"})
                self.assertTrue(intent.is_empty)
                self.assertTrue(any("model/method" in a
                                    for a in intent.ambiguous))

    def test_schema_violation_reports_missing(self):
        intent = self.parser.validate_llm_output({
            "capability_id": "picking_report_qty", "params": {}})
        self.assertIn("quantity", intent.missing)

    def test_unknown_param_rejected(self):
        intent = self.parser.validate_llm_output({
            "capability_id": "picking_report_qty",
            "params": {"quantity": 1, "sneaky": "x"}})
        # strict schema: unknown field rejected -> reported, not executed
        self.assertTrue(intent.missing or intent.is_empty)

    def test_confidence_clamped(self):
        high = self.parser.validate_llm_output({
            "capability_id": "picking_report_qty",
            "params": {"quantity": 1}, "confidence": 9.9})
        self.assertLessEqual(high.confidence, 1.0)
        low = self.parser.validate_llm_output({
            "capability_id": "picking_report_qty",
            "params": {"quantity": 1}, "confidence": -3})
        self.assertGreaterEqual(low.confidence, 0.0)

    def test_languages_and_hint_for_audit(self):
        intent = self.parser.validate_llm_output({
            "capability_id": "stock_query_product", "params": {}},
            text="查看 stock 库存")
        self.assertIn("zh", intent.languages)
        self.assertIn("en", intent.languages)
        self.assertEqual(intent.lang_hint, "zh+en")

    def test_consistency_guard_flags_mismatch(self):
        """LLM 说 8，文本明说 5 -> 写入 ambiguous，不静默采信。"""
        intent = self.parser.validate_llm_output({
            "normalized_text": "report 8 units",
            "capability_id": "picking_report_qty",
            "params": {"quantity": 8}}, text="完成 5 件")
        self.assertTrue(any("mismatch" in a for a in intent.ambiguous))

    def test_consistency_guard_no_false_positive(self):
        intent = self.parser.validate_llm_output({
            "normalized_text": "report 5 units",
            "capability_id": "picking_report_qty",
            "params": {"quantity": 5}}, text="完成 5 件")
        self.assertEqual(intent.ambiguous, [])

    def test_normalized_lang_constant_is_english(self):
        self.assertEqual(NORMALIZED_LANG, "en_US")


if __name__ == "__main__":
    unittest.main()
