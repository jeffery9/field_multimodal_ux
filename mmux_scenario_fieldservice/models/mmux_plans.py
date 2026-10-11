# -*- coding: utf-8 -*-
"""Field-service plan example: identify equipment -> history -> report
draft -> confirmed task close. Each step passes the ExecutionGuard."""
from odoo.addons.mmux_core.services.mmux_core import (
    Plan, PlanStep, register_plan,
)

_fs_close = register_plan(Plan(
    "fs_service_closeout",
    "现场服务收尾",
    [
        PlanStep("s1", "equipment_identify",
                 bindings={"barcode": "$input:serial"}),
        PlanStep("s2", "equipment_query_history",
                 record_ref="$step1.result.candidates.0.id"),
        PlanStep("s3", "service_report_note",
                 bindings={"steps": "$input:repair_summary",
                           "hours": "$input:hours"},
                 record_ref="$input:task_id"),
        PlanStep("s4", "service_task_mark_done",
                 record_ref="$input:task_id"),
    ],
    "识别设备，查询维修历史，口述服务报告草稿，确认后关闭服务任务。"))
_fs_close._keywords = ("服务收尾", "服务完成闭环", "service closeout")
