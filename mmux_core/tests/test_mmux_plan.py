# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Odoo-side tests for plan orchestration (agent loop through the service)."""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMmuxPlanRun(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.product = self.env["product.product"].create({
            "name": "MMUX Plan Product", "is_storable": True, "barcode": "MMUX-P1",
        })

    def test_plan_run_success_wh_receipt(self):
        """收货闭环计划：L0 查询直通，L2 步骤逐个确认后完成。"""
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": 2, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        line = picking.move_ids.move_line_ids[0]
        line.quantity = 2

        res = self.service.plan_run(
            plan_id="wh_receipt_roundtrip",
            inputs={"barcode": "MMUX-P1", "quantity": 2,
                    "picking_id": picking.id,
                    "move_line_id": line.id,
                    "damage_note": "外箱轻微压痕"},
            idempotency_key="mmux:plan-t1")
        # L2 validate step pauses for confirmation
        self.assertIn(res["state"],
                      ("awaiting_confirmation", "input_required", "success"))
        if res["state"] == "awaiting_confirmation":
            confirmed = [s["step"] for s in res["steps"]
                         if s["status"] == "awaiting_confirmation"]
            res = self.service.plan_run(
                plan_id="wh_receipt_roundtrip",
                inputs={"barcode": "MMUX-P1", "quantity": 2,
                        "picking_id": picking.id,
                        "move_line_id": line.id,
                        "damage_note": "外箱轻微压痕"},
                idempotency_key="mmux:plan-t1",
                confirmed_steps=confirmed)
            self.assertEqual(res["state"], "success")
        self.assertEqual(picking.state, "done")

    def test_plan_run_unknown_plan(self):
        res = self.service.plan_run(plan_id="no_such_plan",
                                    idempotency_key="mmux:plan-t2")
        self.assertEqual(res["state"], "failed")

    def test_llm_plan_validated_before_run(self):
        """LLM 提案必须先过白名单校验：幻觉能力直接失败，不执行任何步骤。"""
        res = self.service.plan_run(
            llm_plan={"steps": [{"step_id": "s1",
                                 "capability_id": "res.users:unlink",
                                 "bindings": {}}]},
            idempotency_key="mmux:plan-t3")
        self.assertEqual(res["state"], "failed")
        self.assertIn("not registered", res["error"])

    def test_plan_audit_logged(self):
        self.service.plan_run(plan_id="no_such_plan",
                              idempotency_key="mmux:plan-t4")
        Log = self.env["mmux.interaction.log"].sudo()
        self.assertTrue(Log.search([
            ("idempotency_key", "=", "mmux:plan-t4"),
            ("capability_id_name", "=like", "plan:%")]))
