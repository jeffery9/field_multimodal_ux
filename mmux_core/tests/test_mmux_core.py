# -*- coding: utf-8 -*-
"""Odoo-side tests: core funnel against a real registry + env.

The pure-pure logic is covered by tests_pure/; these tests verify the
Odoo glue: ACL behavior, record rules, duplicate idempotency keys and
the log trail (PRD §10.3 security acceptance subset).
"""
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged("post_install", "-at_install")
class TestMmuxCoreService(TransactionCase):
    def test_capabilities_for_user_respects_groups(self):
        service = self.env["mmux.service"]
        caps = service.capabilities_for_user("")
        self.assertTrue(any(c["capability_id"] == "stock_query_product" for c in caps))

    def test_execute_unknown_capability_fails(self):
        res = self.env["mmux.service"].execute_capability(
            "no_such_capability", {}, idem_key="mmux:test-1")
        self.assertEqual(res["status"], "failed")

    def test_execute_duplicate_key_rejected(self):
        svc = self.env["mmux.service"]
        res1 = svc.execute_capability(
            "stock_query_product", {"barcode": "XYZ-NONE"}, idem_key="mmux:dup-key")
        res2 = svc.execute_capability(
            "stock_query_product", {"barcode": "XYZ-NONE"}, idem_key="mmux:dup-key")
        self.assertEqual(res1["status"], "success")
        self.assertEqual(res2["status"], "failed")
        self.assertIn("duplicate", res2["error"])

    def test_l2_requires_confirmation(self):
        # stock_adjust_count is L2 with forced confirmation
        res = self.env["mmux.service"].execute_capability(
            "stock_adjust_count", {"counted_qty": 1}, confirmed=False,
            idem_key="mmux:l2-confirm")
        self.assertEqual(res["status"], "awaiting_confirmation")
        self.assertEqual(res["confirmation"]["risk"], "L2")

    def test_llm_intent_warehouse(self):
        """LLM-first: recognition is driven by the LLM (mock in tests).
        The canonical English rewrite is returned for audit; execution
        still keys off capability_id + params."""
        svc = self.env["mmux.service"]
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("mmux.llm.provider", "mock")
        Param.set_param("mmux.llm.mock_response", {
            "normalized_text": "query available stock for product 12345678",
            "capability_id": "stock_query_product",
            "params": {"barcode": "12345678"},
            "missing": [], "ambiguous": [], "confidence": 0.9,
        })
        intent = svc.parse_intent("这个商品 12345678 的可用库存是多少")
        if self.env["ir.module.module"].search_count(
                [("name", "=", "mmux_scenario_warehouse"), ("state", "=", "installed")]):
            self.assertEqual(intent["capability_id"], "stock_query_product")
            self.assertEqual(intent["normalized_text"],
                             "query available stock for product 12345678")
            self.assertIn("zh", intent["languages"])

    def test_llm_unavailable_returns_structured_error(self):
        """未配置 LLM provider 时返回结构化错误，绝不回退规则解析。"""
        svc = self.env["mmux.service"]
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("mmux.llm.provider", "")
        out = svc.parse_intent("任意输入")
        self.assertEqual(out.get("code"), "llm_unavailable")
        self.assertIs(out.get("retryable"), False)

    def test_intent_audit_records_normalized_text(self):
        """归一化英文写入审计 detail，且不进入执行链。"""
        svc = self.env["mmux.service"]
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("mmux.llm.provider", "mock")
        Param.set_param("mmux.llm.mock_response", {
            "normalized_text": "set the stock quantity to 5 pieces",
            "capability_id": "picking_report_qty",
            "params": {"quantity": 5}, "confidence": 0.8})
        svc.parse_intent("把 stock 数量改成 5 个")
        Log = self.env["mmux.interaction.log"].sudo()
        last = Log.search([("exec_status", "=", "recognized")], limit=1)
        self.assertTrue(last)
        self.assertIn("set the stock quantity to 5 pieces", last.detail)

    def test_audit_log_written(self):
        self.env["mmux.service"].execute_capability(
            "stock_query_product", {}, idem_key="mmux:audit-1")
        Log = self.env["mmux.interaction.log"].sudo()
        self.assertTrue(Log.search([("idempotency_key", "=", "mmux:audit-1")]))

    def test_user_without_group_gets_capabilities_hidden(self):
        # capability list only exposes what the current user may use; a
        # non-mmux user must get nothing through this API.
        portal = self.env.ref("base.demo_user0") if "base.demo_user0" in self.env else None
        if portal is None:
            self.skipTest("no demo user available")
        service = self.env["mmux.service"].with_user(portal)
        try:
            caps = service.capabilities_for_user("")
            self.assertEqual(caps, [])
        except AccessError:
            pass  # acceptable: ACL denies the service model entirely
