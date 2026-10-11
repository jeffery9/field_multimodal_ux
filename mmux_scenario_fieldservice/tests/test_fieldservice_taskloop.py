# -*- coding: utf-8 -*-
"""Field-service task loop: an open project.task assigned to me is one work
item; completing it closes the task natively."""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFieldServiceTaskLoop(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")

    def _task(self, name="MMUX Service Visit"):
        return self.env["project.task"].create({
            "name": name,
            "user_ids": [(4, self.env.user.id)],
        })

    def test_source_registered_for_field_service(self):
        ids = [s["source_id"]
               for s in self.service.task_sources("field_service")]
        self.assertIn("fs_task", ids)

    def test_completion_capability_is_L2(self):
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        cap = CAPABILITY_REGISTRY.require("service_task_complete")
        self.assertEqual(cap.risk, "L2")
        self.assertTrue(cap.require_confirmation)
        self.assertEqual(cap.target_model, "project.task")

    def test_guided_service_flow_listed(self):
        flows = {f["flow_id"] for f in self.service.guide_flows()}
        self.assertIn("fs_guided_service", flows)

    def test_loop_closes_task_and_publishes_next(self):
        t1 = self._task("MMUX Service A")
        t2 = self._task("MMUX Service B")
        self.Param.set_param("mmux.llm.mock_response", json.dumps({
            "normalized_text": "the service task is done, replaced the filter",
            "capability_id": "service_task_complete",
            "params": {"summary": "更换滤芯，用时 1.5 小时"}}))
        self.Param.set_param("mmux.llm.mock_error", False)

        res = self.service.task_next("field_service", source_id="fs_task")
        self.assertIn(res["task"]["res_id"], (t1.id, t2.id))
        first_id = res["task"]["res_id"]
        out = self.service.task_report(res["loop_key"], text="服务完成",
                                       confirmed=True)
        self.assertEqual(out["state"], "completed")
        closed = self.env["project.task"].browse(first_id)
        self.assertEqual(closed.state, "1_done")
        self.assertTrue(closed.message_ids)
        # the next service task is published
        self.assertIsNotNone(out["task"])
        self.assertNotEqual(out["task"]["res_id"], first_id)

    def test_precondition_rejects_closed_task(self):
        from odoo.exceptions import UserError
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        task = self._task("MMUX Service Closed")
        task.write({"state": "1_done"})
        cap = CAPABILITY_REGISTRY.require("service_task_complete")
        with self.assertRaises(UserError):
            cap.precondition(task, {})

    def test_guided_flow_binds_to_the_task(self):
        task = self._task("MMUX Service Guided")
        res = self.service.task_next("field_service", source_id="fs_task")
        view = self.service.task_guide(res["loop_key"])
        self.assertEqual(view["flow_id"], "fs_guided_service")
        self.assertIn(str(task.id), view["current"]["step_id"])

    def test_novice_more_steps_than_expert(self):
        task = self._task("MMUX Service Levels")
        self.service.set_skill_level("novice")
        novice = self.service.guide_start("fs_guided_service",
                                          {"task_id": task.id})
        self.service.set_skill_level("expert")
        expert = self.service.guide_start("fs_guided_service",
                                          {"task_id": task.id})
        self.assertGreater(len(novice["steps"]), len(expert["steps"]))
        self.service.set_skill_level("novice")
