# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Odoo-side tests for the guided voice-navigation flow."""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMmuxGuide(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.product = self.env["product.product"].create({
            "name": "MMUX Guide Product", "is_storable": True,
            "barcode": "MMUX-G1",
        })

    def _make_assigned_picking(self):
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": 3, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def test_guide_flow_listed(self):
        res = self.service.guide_flows()
        ids = [f["flow_id"] for f in res]
        self.assertIn("wh_guided_picking", ids)

    def test_full_guided_picking_loop(self):
        """语音引导闭环：开始 → 到单确认 → 扫码（走守卫）→ 口述数量（走守卫）
        → 验证步骤强制确认 → 完成。"""
        picking = self._make_assigned_picking()
        line = picking.move_ids.move_line_ids[0]
        line.quantity = 3  # pre-fill so validate succeeds

        view = self.service.guide_start("wh_guided_picking",
                                        {"picking_id": picking.id})
        self.assertFalse(view["finished"])
        key = view["guide_key"]

        # step 1: goto (skippable, confirm)
        self.assertIn("goto_", view["current"]["step_id"])
        view = self.service.guide_advance(key, view["current"]["step_id"])
        # step 2: scan (capability-backed; invalid barcode still advances query)
        self.assertIn("scan_", view["current"]["step_id"])
        view = self.service.guide_advance(key, view["current"]["step_id"],
                                          {"barcode": "MMUX-G1"})
        # step 3: qty (L2 write through the guard)
        self.assertIn("qty_", view["current"]["step_id"])
        view = self.service.guide_advance(key, view["current"]["step_id"],
                                          {"quantity": 3})
        self.assertEqual(line.quantity, 3)
        # step 4: validate pauses for confirmation (L2)
        self.assertIn("validate_", view["current"]["step_id"])
        view = self.service.guide_advance(key, view["current"]["step_id"],
                                          {"confirmed": False})
        self.assertEqual(view["state"], "awaiting_confirmation")
        # step id of validate step is a record-id-suffixed key; fetch current
        cur = view["step"]["step_id"]
        view = self.service.guide_advance(key, cur, {"confirmed": True})
        self.assertEqual(view.get("last_exec"), "success")
        self.assertEqual(picking.state, "done")
        self.assertTrue(view["finished"])

    def test_cannot_complete_out_of_order(self):
        picking = self._make_assigned_picking()
        view = self.service.guide_start("wh_guided_picking",
                                        {"picking_id": picking.id})
        key = view["guide_key"]
        # try to complete a later qty step while goto is current
        line = picking.move_ids.move_line_ids[0]
        res = self.service.guide_advance(key, f"qty_{line.id}", {"quantity": 1})
        self.assertIn("error", res)

    def test_guide_cursor_survives_reload(self):
        picking = self._make_assigned_picking()
        view = self.service.guide_start("wh_guided_picking",
                                        {"picking_id": picking.id})
        key = view["guide_key"]
        view = self.service.guide_advance(key, view["current"]["step_id"])
        reloaded = self.service.guide_current(key)
        self.assertNotIn("goto_", reloaded["current"]["step_id"])
