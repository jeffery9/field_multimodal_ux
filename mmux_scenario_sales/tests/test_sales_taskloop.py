# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Sales-visit task loop: a lead I own is one visit work item; completing it
records the visit and NEVER touches the sales pipeline."""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSalesTaskLoop(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.partner = self.env["res.partner"].create({
            "name": "MMUX Visit Customer"})

    def _lead(self):
        return self.env["crm.lead"].create({
            "name": "MMUX Visit Lead",
            "partner_id": self.partner.id,
            "user_id": self.env.user.id,
        })

    def test_source_registered_for_sales(self):
        ids = [s["source_id"] for s in self.service.task_sources("sales_visit")]
        self.assertIn("sales_visit", ids)

    def test_completion_capability_is_L2(self):
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        cap = CAPABILITY_REGISTRY.require("visit_task_complete")
        self.assertEqual(cap.risk, "L2")
        self.assertTrue(cap.require_confirmation)
        self.assertEqual(cap.target_model, "crm.lead")

    def test_guided_visit_flow_listed(self):
        flows = {f["flow_id"] for f in self.service.guide_flows()}
        self.assertIn("sales_guided_visit", flows)

    def test_loop_records_visit_without_touching_pipeline(self):
        lead = self._lead()
        stage_before = lead.stage_id
        self.Param.set_param("mmux.llm.mock_response", json.dumps({
            "normalized_text": "the visit is done, customer wants a quote",
            "capability_id": "visit_task_complete",
            "params": {"summary": "客户关心交期，要求下周一给报价",
                       "next_action": "下周一前发出报价"}}))
        self.Param.set_param("mmux.llm.mock_error", False)

        res = self.service.task_next("sales_visit", source_id="sales_visit")
        self.assertEqual(res["task"]["res_id"], lead.id)
        out = self.service.task_report(res["loop_key"],
                                       text="拜访完成，客户要报价",
                                       confirmed=True)
        self.assertEqual(out["state"], "completed")
        lead.invalidate_recordset()
        # the visit summary landed on the lead, and the next action is set
        self.assertTrue(lead.message_ids)
        self.assertIn("报价", lead.next_action or "")
        # ...but the pipeline stage is untouched (native sales flow owns it)
        self.assertEqual(lead.stage_id, stage_before)
        self.assertTrue(lead.active)

    def test_guided_flow_binds_to_the_task_lead(self):
        lead = self._lead()
        res = self.service.task_next("sales_visit", source_id="sales_visit")
        view = self.service.task_guide(res["loop_key"])
        self.assertEqual(view["flow_id"], "sales_guided_visit")
        self.assertIn(str(lead.id), view["current"]["step_id"])

    def test_novice_more_steps_than_expert(self):
        self._lead()
        self.service.set_skill_level("novice")
        novice = self.service.guide_start("sales_guided_visit",
                                          {"lead_id": self._lead().id})
        self.service.set_skill_level("expert")
        expert = self.service.guide_start("sales_guided_visit",
                                          {"lead_id": self._lead().id})
        self.assertGreater(len(novice["steps"]), len(expert["steps"]))
        self.service.set_skill_level("novice")
