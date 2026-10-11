# -*- coding: utf-8 -*-
"""Odoo-side tests: novice vs expert guidance levels."""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSkillLevels(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.product = self.env["product.product"].create({
            "name": "MMUX Skill Product", "is_storable": True,
            "barcode": "MMUX-S1",
        })

    def _picking(self):
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": 2, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def test_default_skill_level_is_novice(self):
        self.env.user.sudo().mmux_skill_level = False
        self.assertEqual(self.service._user_skill_level(), "novice")

    def test_set_skill_level(self):
        res = self.service.set_skill_level("expert")
        self.assertEqual(res["skill_level"], "expert")
        self.assertEqual(self.service._user_skill_level(), "expert")
        self.service.set_skill_level("novice")
        self.assertEqual(self.service._user_skill_level(), "novice")
        self.assertIn("error", self.service.set_skill_level("wizard"))

    def test_novice_produces_more_steps_than_expert(self):
        picking = self._picking()
        params = {"picking_id": picking.id}
        self.service.set_skill_level("novice")
        novice = self.service.guide_start("wh_guided_picking", params)
        n_count = len(novice["steps"])
        self.service.set_skill_level("expert")
        expert = self.service.guide_start("wh_guided_picking", params)
        e_count = len(expert["steps"])
        self.assertGreater(n_count, e_count)

    def test_skill_level_recorded_in_run(self):
        picking = self._picking()
        self.service.set_skill_level("expert")
        view = self.service.guide_start("wh_guided_picking",
                                        {"picking_id": picking.id})
        self.assertEqual(view["skill_level"], "expert")
        reloaded = self.service.guide_current(view["guide_key"])
        self.assertEqual(reloaded["skill_level"], "expert")

    def test_flows_report_skill_support(self):
        flows = {f["flow_id"]: f for f in self.service.guide_flows()}
        self.assertTrue(flows["wh_guided_picking"]["supports_skill_levels"])

    def test_config_changes_do_not_skip_guard(self):
        """技能等级只影响步骤粒度：熟手模式下 L2 验证步骤仍走确认守卫。"""
        picking = self._picking()
        picking.move_ids.move_line_ids.write({"quantity": 2})
        self.service.set_skill_level("expert")
        view = self.service.guide_start("wh_guided_picking",
                                        {"picking_id": picking.id})
        key = view["guide_key"]
        # expert: first step is the merged qty step
        cur = view["current"]
        view = self.service.guide_advance(key, cur["step_id"], {"quantity": 2})
        # then the validate step must still await confirmation
        self.assertIn("validate_", view["current"]["step_id"])
        view = self.service.guide_advance(key, view["current"]["step_id"],
                                          {"confirmed": False})
        self.assertEqual(view["state"], "awaiting_confirmation")
