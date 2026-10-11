# -*- coding: utf-8 -*-
"""Manufacturing task loop: a pending work order is one work item;
completing it reports production natively and publishes the next operation."""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMfgTaskLoop(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")

    def _workorder(self, qty=5):
        """Minimal MO + work order in a reportable state."""
        uom = self.env.ref("uom.product_uom_unit")
        product = self.env["product.product"].create({
            "name": "MMUX WO Product", "is_storable": True})
        bom = self.env["mrp.bom"].create({
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_qty": 1, "type": "normal",
            "bom_line_ids": [(0, 0, {"product_id": product.id,
                                     "product_qty": 1})],
        })
        mo = self.env["mrp.production"].create({
            "product_id": product.id, "product_qty": qty,
            "product_uom_id": uom.id, "bom_id": bom.id,
        })
        mo.action_confirm()
        wo = mo.workorder_ids[:1]
        if wo:
            wo.state = "ready"
        return mo, wo

    def test_source_registered_for_manufacturing(self):
        ids = [s["source_id"]
               for s in self.service.task_sources("manufacturing")]
        self.assertIn("mfg_workorder", ids)

    def test_completion_capability_is_L2(self):
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        cap = CAPABILITY_REGISTRY.require("wo_task_complete")
        self.assertEqual(cap.risk, "L2")
        self.assertTrue(cap.require_confirmation)
        self.assertEqual(cap.target_model, "mrp.workorder")

    def test_loop_reports_production_and_publishes_next(self):
        mo, wo = self._workorder()
        if not wo:
            self.skipTest("no work order generated for this BOM")
        self.Param.set_param("mmux.llm.mock_response", json.dumps({
            "normalized_text": "the work order task is done",
            "capability_id": "wo_task_complete",
            "params": {"qty_production": 5}}))
        self.Param.set_param("mmux.llm.mock_error", False)

        res = self.service.task_next("manufacturing",
                                     source_id="mfg_workorder")
        self.assertIsNotNone(res["task"])
        out = self.service.task_report(res["loop_key"], text="报工完成 5 件",
                                       confirmed=True)
        self.assertEqual(out["state"], "completed")
        wo.invalidate_recordset()
        self.assertGreater(wo.qty_produced, 0)

    def test_precondition_rejects_unreportable_workorder(self):
        from odoo.exceptions import UserError
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        mo, wo = self._workorder()
        if not wo:
            self.skipTest("no work order generated for this BOM")
        cap = CAPABILITY_REGISTRY.require("wo_task_complete")
        wo.state = "done"
        with self.assertRaises(UserError):
            cap.precondition(wo, {})

    def test_guided_flow_available_for_the_task(self):
        mo, wo = self._workorder()
        if not wo:
            self.skipTest("no work order generated for this BOM")
        res = self.service.task_next("manufacturing", source_id="mfg_workorder")
        view = self.service.task_guide(res["loop_key"])
        self.assertEqual(view["flow_id"], "mfg_guided_workorder")
        self.assertFalse(view["finished"])
