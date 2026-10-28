# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Sales visit BDD tests (PRD §10.2 销售拜访验收)."""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSalesVisitScenario(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.partner = self.env["res.partner"].create({"name": "MMUX 客户A"})
        self.lead = self.env["crm.lead"].create({
            "name": "MMUX 拜访线索",
            "partner_id": self.partner.id,
        })

    def test_query_customer_history(self):
        self.env["sale.order"].create({"partner_id": self.partner.id})
        res = self.service.execute_capability(
            "partner_query_history", {}, record_id=self.partner.id,
            idem_key="mmux:sales-q1")
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["result"]["recent_orders"]), 1)

    def test_visit_note_is_reviewable_draft(self):
        """口述纪要 → chatter 草稿，用户可检查修正（可删除该消息）。"""
        res = self.service.execute_capability(
            "visit_record_note",
            {"summary": "客户关注交期，下周需提供样品"},
            record_id=self.lead.id, idem_key="mmux:sales-n1")
        self.assertEqual(res["status"], "success")
        msg = self.env["mail.message"].browse(res["result"]["message_id"])
        self.assertIn("交期", msg.body)
        # user may edit/correct: native chatter still owns the message
        msg.unlink()

    def test_lead_fields_whitelist(self):
        res = self.service.execute_capability(
            "lead_update_fields",
            {"lead_name": "MMUX 新需求", "expected_revenue": 50000},
            record_id=self.lead.id, idem_key="mmux:sales-u1")
        self.assertEqual(res["status"], "success")
        self.lead.invalidate_recordset()
        self.assertEqual(self.lead.name, "MMUX 新需求")
        self.assertEqual(self.lead.expected_revenue, 50000)

    def test_followup_activity_created(self):
        res = self.service.execute_capability(
            "schedule_followup",
            {"summary": "发送样品", "date_deadline": "2026-01-31"},
            record_id=self.lead.id, idem_key="mmux:sales-f1")
        self.assertEqual(res["status"], "success")
        act = self.env["mail.activity"].browse(res["result"]["activity_id"])
        self.assertEqual(act.date_deadline.strftime("%Y-%m-%d"), "2026-01-31")

    def test_quote_draft_requires_confirmation_and_stays_draft(self):
        """报价草稿：强制确认；生成后仍为 draft，绝不自动发送/确认。"""
        res = self.service.execute_capability(
            "create_quote_draft", {"reference": "MMUX-REF"},
            record_id=self.partner.id, idem_key="mmux:sales-c1")
        self.assertEqual(res["status"], "awaiting_confirmation")
        res = self.service.execute_capability(
            "create_quote_draft", {"reference": "MMUX-REF"},
            record_id=self.partner.id, idem_key="mmux:sales-c1", confirmed=True)
        self.assertEqual(res["status"], "success")
        order = self.env["sale.order"].browse(res["result"]["order_id"])
        self.assertEqual(order.state, "draft")
