# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Canonical-language normalisation contract.

The product decision: whatever the input language (mixed or single), the
LLM produces a canonical ENGLISH rewrite used for audit and parameter
extraction. Execution still happens on capability_id + params.

These tests drive the deterministic MockLLMProvider with a canned answer,
mirroring exactly what the service layer does — asserting the
normalized_text contract and that business entities survive verbatim.
"""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.capability import Capability, CapabilityRegistry  # noqa: E402
from mmux_core.schema import ParamSchema, Field  # noqa: E402
from mmux_core.intent import IntentParser  # noqa: E402
from mmux_core.provider import REGISTRY as PREG  # noqa: E402
from mmux_core.i18n import detect_languages  # noqa: E402


def _registry():
    reg = CapabilityRegistry()
    reg.register(Capability(
        "picking_report_qty", "Report qty", "", "stock.move.line", "write",
        schema=ParamSchema([
            Field("quantity", "float", required=True, min_val=0),
            Field("barcode", "str", required=False)]), risk="L2"))
    return reg


# Simulated LLM answers per input class: the canonical English rewrite.
CASES = [
    ("把 MMUX-0001 的 stock 数量改成 5 个",
     {"normalized_text": "set the stock quantity of MMUX-0001 to 5 pieces",
      "capability_id": "picking_report_qty",
      "params": {"quantity": 5, "barcode": "MMUX-0001"}},
     "MMUX-0001"),
    ("帮我看看 stock 够不够",
     {"normalized_text": "check whether the stock is sufficient",
      "capability_id": None, "params": {}},
     None),
    ("报工 120 件",
     {"normalized_text": "report 120 units",
      "capability_id": "picking_report_qty",
      "params": {"quantity": 120}},
     None),
    ("picked 8 pieces",
     {"normalized_text": "picked 8 pieces",
      "capability_id": "picking_report_qty",
      "params": {"quantity": 8}},
     None),
]


class TestCanonicalNormalization(unittest.TestCase):
    def setUp(self):
        self.parser = IntentParser(_registry())

    def test_mock_provider_drives_full_path(self):
        """Through the Mock provider the whole validate step is exercised."""
        canned = CASES[0][1]
        out = PREG.call("llm", "mock", {"messages": []},
                        {"mmux.llm.mock_response": canned})
        intent = self.parser.validate_llm_output(out["data"], text=CASES[0][0])
        self.assertEqual(intent.capability_id, "picking_report_qty")
        self.assertEqual(intent.params["quantity"], 5.0)

    def test_entities_preserved_verbatim(self):
        """型号编码/数量在归一化文本与参数中都不被翻译或改写。"""
        raw, canned, must_keep = CASES[0]
        intent = self.parser.validate_llm_output(canned, text=raw)
        self.assertIn(must_keep, intent.normalized_text)
        self.assertEqual(intent.params["barcode"], must_keep)

    def test_all_input_classes_yield_english_normalized_text(self):
        for raw, canned, _ in CASES:
            with self.subTest(raw=raw):
                intent = self.parser.validate_llm_output(canned, text=raw)
                self.assertTrue(intent.normalized_text)
                # canonical form must be Latin script (English)
                self.assertTrue(
                    all(c.isascii() for c in intent.normalized_text),
                    f"not english: {intent.normalized_text!r}")

    def test_unmatched_input_is_empty_intent_not_error(self):
        raw, canned, _ = CASES[1]
        intent = self.parser.validate_llm_output(canned, text=raw)
        self.assertTrue(intent.is_empty)
        # the canonical English is still recorded for audit
        self.assertEqual(intent.normalized_text,
                         "check whether the stock is sufficient")

    def test_detected_scripts_recorded_for_audit(self):
        raw, canned, _ = CASES[0]
        intent = self.parser.validate_llm_output(canned, text=raw)
        self.assertEqual(set(intent.languages), {"zh", "en"})
        self.assertEqual(detect_languages(raw), intent.languages)

    def test_pure_language_inputs_normalise_too(self):
        for raw, canned, _ in CASES[2:]:
            with self.subTest(raw=raw):
                intent = self.parser.validate_llm_output(canned, text=raw)
                self.assertTrue(intent.normalized_text.isascii())


if __name__ == "__main__":
    unittest.main()
