# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Warehouse task loop: an assigned picking is one work item; completing it
validates the transfer natively and publishes the next picking."""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestWarehouseTaskLoop(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.product = self.env["product.product"].create({
            "name": "MMUX WH Loop", "is_storable": True, "barcode": "MMUX-WL1"})

    def _picking(self, qty=2):
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": qty, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def _mock_complete(self):
        self.Param.set_param("mmux.llm.mock_response", json.dumps({
            "normalized_text": "the picking task is done",
            "capability_id": "picking_task_complete", "params": {}}))
        self.Param.set_param("mmux.llm.mock_error", False)

    def test_source_registered_for_warehouse(self):
        ids = [s["source_id"] for s in self.service.task_sources("warehouse")]
        self.assertIn("wh_picking", ids)

    def test_completion_capability_is_L2(self):
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        cap = CAPABILITY_REGISTRY.require("picking_task_complete")
        self.assertEqual(cap.risk, "L2")
        self.assertTrue(cap.require_confirmation)
        self.assertEqual(cap.target_model, "stock.picking")
        self.assertEqual(cap.scenario, "warehouse")

    def test_full_loop_marks_picking_done_and_publishes_next(self):
        p1 = self._picking()
        p2 = self._picking()
        p1.move_ids.move_line_ids.write({"quantity": 2})
        p2.move_ids.move_line_ids.write({"quantity": 2})
        self._mock_complete()

        res = self.service.task_next("warehouse", source_id="wh_picking")
        out = self.service.task_report(res["loop_key"], text="拣货完成",
                                       confirmed=True)
        self.assertEqual(out["state"], "completed")
        self.assertEqual(p1.state, "done")
        self.assertEqual(out["task"]["res_id"], p2.id)

    def test_guided_flow_available_for_the_task(self):
        picking = self._picking()
        res = self.service.task_next("warehouse", source_id="wh_picking")
        view = self.service.task_guide(res["loop_key"])
        self.assertEqual(view["flow_id"], "wh_guided_picking")
        self.assertIn(str(picking.id), view["current"]["step_id"])

    def test_precondition_rejects_already_done_picking(self):
        """已完成单据不能再次走完成能力（原生状态约束）。"""
        picking = self._picking()
        picking.move_ids.move_line_ids.write({"quantity": 2})
        from odoo.exceptions import UserError
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        cap = CAPABILITY_REGISTRY.require("picking_task_complete")
        picking.button_validate()
        self.assertEqual(picking.state, "done")
        with self.assertRaises(UserError):
            cap.precondition(picking, {})
