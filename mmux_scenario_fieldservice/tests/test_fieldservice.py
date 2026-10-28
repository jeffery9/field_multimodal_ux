# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Field service BDD tests (PRD §10.2 现场服务验收)."""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFieldServiceScenario(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.project = self.env["project.project"].create({"name": "MMUX 售后服务"})
        self.task = self.env["project.task"].create({
            "name": "MMUX 设备检修任务",
            "project_id": self.project.id,
        })
        self.equipment = self.env["maintenance.equipment"].create({
            "name": "MMUX 空压机",
            "serial_no": "SN-MMUX-001",
        })

    def test_identify_equipment_returns_candidates(self):
        """识别设备 → 候选列表，不自动选择（FR-008）。"""
        res = self.service.execute_capability(
            "equipment_identify", {"barcode": "SN-MMUX-001"},
            idem_key="mmux:fs-i1")
        self.assertEqual(res["status"], "success")
        self.assertGreaterEqual(res["result"]["count"], 1)
        self.assertEqual(res["result"]["candidates"][0]["serial_no"], "SN-MMUX-001")

    def test_equipment_history(self):
        self.env["maintenance.request"].create({
            "name": "MMUX 历史维修",
            "equipment_id": self.equipment.id,
        })
        res = self.service.execute_capability(
            "equipment_query_history", {}, record_id=self.equipment.id,
            idem_key="mmux:fs-h1")
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["result"]["maintenance_requests"]), 1)

    def test_service_report_draft(self):
        res = self.service.execute_capability(
            "service_report_note",
            {"steps": "更换滤芯，测试气压", "hours": 1.5, "parts": "滤芯 ×1"},
            record_id=self.task.id, idem_key="mmux:fs-r1")
        self.assertEqual(res["status"], "success")
        self.assertIn("更换滤芯", self.task.message_ids[0].body)

    def test_evidence_and_part_logged(self):
        self.service.execute_capability(
            "service_capture_evidence", {"description": "维修前照片已上传"},
            record_id=self.task.id, idem_key="mmux:fs-e1")
        res = self.service.execute_capability(
            "service_log_part", {"part": "密封圈", "qty": 2},
            record_id=self.task.id, idem_key="mmux:fs-p1")
        self.assertEqual(res["status"], "success")

    def test_mark_done_requires_confirmation(self):
        res = self.service.execute_capability(
            "service_task_mark_done", {}, record_id=self.task.id,
            idem_key="mmux:fs-d1")
        self.assertEqual(res["status"], "awaiting_confirmation")
        res = self.service.execute_capability(
            "service_task_mark_done", {}, record_id=self.task.id,
            idem_key="mmux:fs-d1", confirmed=True)
        self.assertEqual(res["status"], "success")
        self.assertEqual(self.task.state, "1_done")

    def test_mark_done_rejected_on_closed_task(self):
        self.task.write({"state": "1_done"})
        res = self.service.execute_capability(
            "service_task_mark_done", {}, record_id=self.task.id,
            idem_key="mmux:fs-d2", confirmed=True)
        self.assertEqual(res["status"], "failed")
