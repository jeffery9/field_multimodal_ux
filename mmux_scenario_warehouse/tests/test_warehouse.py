# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Warehouse BDD tests (PRD §10.2 仓库验收 / §10.3 安全验收)."""
from odoo.tests import TransactionCase, tagged
from odoo import Command


@tagged("post_install", "-at_install")
class TestWarehouseScenario(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Product = self.env["product.product"]
        self.service = self.env["mmux.service"]
        self.product = self.Product.create({
            "name": "MMUX Test Product",
            "is_storable": True,
            "barcode": "MMUX-0001",
        })

    def _make_picking(self):
        customer_loc = self.env.ref("stock.stock_location_customers")
        stock_loc = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock_loc.id,
            "location_dest_id": customer_loc.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name,
            "product_id": self.product.id,
            "product_uom_qty": 5,
            "picking_id": picking.id,
            "location_id": stock_loc.id,
            "location_dest_id": customer_loc.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def test_query_stock_by_barcode(self):
        res = self.service.execute_capability(
            "stock_query_product", {"barcode": "MMUX-0001"}, idem_key="mmux:wh-q1")
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["result"]["found"])
        self.assertEqual(res["result"]["product_name"], self.product.display_name)

    def test_query_unknown_barcode_is_not_an_error_but_found_false(self):
        res = self.service.execute_capability(
            "stock_query_product", {"barcode": "NOPE"}, idem_key="mmux:wh-q2")
        self.assertEqual(res["status"], "success")
        self.assertFalse(res["result"]["found"])

    def test_report_quantity_on_move_line(self):
        picking = self._make_picking()
        line = picking.move_ids.move_line_ids[0]
        res = self.service.execute_capability(
            "picking_report_qty", {"quantity": 3},
            record_id=line.id, idem_key="mmux:wh-q3", confirmed=True)
        self.assertEqual(res["status"], "success")
        self.assertEqual(line.quantity, 3)

    def test_report_quantity_rejected_on_done_picking(self):
        """Acceptance §10.2: 非法状态变更被拒绝。"""
        picking = self._make_picking()
        picking.move_ids.move_line_ids.write({"quantity": 5})
        picking.button_validate()
        line = picking.move_ids.move_line_ids
        res = self.service.execute_capability(
            "picking_report_qty", {"quantity": 9},
            record_id=line[0].id if line else picking.move_ids[0].id,
            idem_key="mmux:wh-q4", confirmed=True)
        self.assertEqual(res["status"], "failed")

    def test_validate_picking_requires_confirmation_then_native_flow(self):
        picking = self._make_picking()
        picking.move_ids.move_line_ids.write({"quantity": 5})
        res = self.service.execute_capability(
            "picking_validate", {}, record_id=picking.id, idem_key="mmux:wh-q5")
        self.assertEqual(res["status"], "awaiting_confirmation")
        res = self.service.execute_capability(
            "picking_validate", {}, record_id=picking.id,
            idem_key="mmux:wh-q5", confirmed=True)
        self.assertEqual(res["status"], "success")
        self.assertEqual(picking.state, "done")

    def test_duplicate_submit_no_double_side_effect(self):
        """Acceptance §10.1: 重复请求不会造成重复业务副作用。"""
        picking = self._make_picking()
        res1 = self.service.execute_capability(
            "picking_validate", {}, record_id=picking.id, idem_key="mmux:wh-dup")
        res2 = self.service.execute_capability(
            "picking_validate", {}, record_id=picking.id,
            idem_key="mmux:wh-dup", confirmed=True)
        self.assertEqual(res2["status"], "failed")
        self.assertIn("duplicate", res2["error"])

    def test_evidence_posted_to_chatter(self):
        picking = self._make_picking()
        res = self.service.execute_capability(
            "picking_capture_evidence", {"note": "包装破损，拍照已存证"},
            record_id=picking.id, idem_key="mmux:wh-ev1")
        self.assertEqual(res["status"], "success")
        self.assertIn("包装破损", picking.message_ids[0].body)
