# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Odoo-side tests: LLM-first intent path end to end.

Covers the service-layer contract:
  - successful recognition returns capability_id + params + normalized_text
  - the three unavailability modes map to {code: "llm_unavailable", retryable}
  - the canonical English rewrite is audited and never reaches execution
"""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLLMIntentPath(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")

    def _mock(self, answer=None, error=None):
        self.Param.set_param(
            "mmux.llm.mock_response", json.dumps(answer) if answer else False)
        self.Param.set_param("mmux.llm.mock_error", error or False)

    # --- happy path -------------------------------------------------- #
    def test_recognized_intent(self):
        self._mock({"normalized_text": "report 5 units",
                    "capability_id": "picking_report_qty",
                    "params": {"quantity": 5}, "confidence": 0.9})
        out = self.service.parse_intent("报工 5 件")
        self.assertEqual(out["capability_id"], "picking_report_qty")
        self.assertEqual(out["params"]["quantity"], 5)
        self.assertEqual(out["normalized_text"], "report 5 units")
        self.assertEqual(out["source"], "llm")

    def test_empty_intent_when_nothing_matches(self):
        self._mock({"normalized_text": "hello world",
                    "capability_id": None, "params": {}})
        out = self.service.parse_intent("你好")
        self.assertIsNone(out["capability_id"])
        self.assertNotIn("code", out)

    def test_hallucinated_capability_is_empty_intent(self):
        self._mock({"normalized_text": "drop the table",
                    "capability_id": "res.partner:unlink", "params": {}})
        out = self.service.parse_intent("删除全部客户")
        self.assertIsNone(out["capability_id"])
        self.assertNotIn("code", out)
        self.assertTrue(out["ambiguous"])

    def test_forbidden_orm_keys_rejected(self):
        self._mock({"normalized_text": "x", "model": "res.users",
                    "method": "write", "ids": [1]})
        out = self.service.parse_intent("把管理员改了")
        self.assertIsNone(out["capability_id"])
        self.assertTrue(out["ambiguous"])

    # --- unavailability --------------------------------------------- #
    def test_not_configured(self):
        self.Param.set_param("mmux.llm.provider", "")
        out = self.service.parse_intent("库存")
        self.assertEqual(out["code"], "llm_unavailable")
        self.assertIs(out["retryable"], False)
        self.assertEqual(out["detail"]["subtag"], "not_configured")

    def test_timeout_retryable(self):
        self._mock(error="timeout")
        out = self.service.parse_intent("库存")
        self.assertEqual(out["code"], "llm_unavailable")
        self.assertIs(out["retryable"], True)
        self.assertEqual(out["detail"]["subtag"], "timeout")

    def test_invalid_json_retryable(self):
        self._mock(error="invalid_json")
        out = self.service.parse_intent("库存")
        self.assertEqual(out["code"], "llm_unavailable")
        self.assertIs(out["retryable"], True)
        self.assertEqual(out["detail"]["subtag"], "invalid_json")

    def test_non_object_payload_retryable(self):
        self._mock(error="not_object")
        out = self.service.parse_intent("库存")
        self.assertEqual(out["code"], "llm_unavailable")
        self.assertIs(out["retryable"], True)

    # --- audit ------------------------------------------------------- #
    def test_normalized_text_is_audited(self):
        self._mock({"normalized_text": "set quantity to 3",
                    "capability_id": "picking_report_qty",
                    "params": {"quantity": 3}, "confidence": 0.7})
        self.service.parse_intent("数量改成 3")
        Log = self.env["mmux.interaction.log"].sudo()
        rec = Log.search([("exec_status", "=", "recognized")], limit=1)
        self.assertTrue(rec)
        detail = json.loads(rec.detail)
        self.assertEqual(detail["normalized_text"], "set quantity to 3")
        self.assertIn("lang_hint", detail)
        self.assertEqual(detail["source"], "llm")

    def test_normalized_text_not_passed_to_execution(self):
        """执行入口只接受 capability_id + params；归一化文本仅审计。"""
        self._mock({"normalized_text": "IGNORE ME admin override",
                    "capability_id": "picking_report_qty",
                    "params": {"quantity": 1}, "confidence": 0.4})
        out = self.service.parse_intent("报工 1 件")
        res = self.service.execute_capability(
            out["capability_id"], out["params"], idem_key="mmux:llm-audit")
        self.assertNotIn("normalized_text", res)
