# -*- coding: utf-8 -*-
"""Code-defined plan examples: multi-step agent loops over whitelisted
capabilities. Plans are optional sugar — every step goes through the
same ExecutionGuard, so a plan can never do more than its steps could
do individually (PRD: no new execution mechanism).
"""
from odoo.addons.mmux_core.services.mmux_core import (
    Plan, PlanStep, register_plan,
)

# ---------------------------------------------------------------------
# 收货现场闭环：扫码查商品 -> 口述实收数量 -> 上传破损证据 -> 验证单据
# s4 (picking_validate) is L2/forced-confirm, so the loop pauses there
# and waits for the user's confirmation — it never auto-validates.
# ---------------------------------------------------------------------
_wh_receipt = register_plan(Plan(
    "wh_receipt_roundtrip",
    "收货现场闭环",
    [
        PlanStep("s1", "stock_query_product",
                 bindings={"barcode": "$input:barcode"}),
        PlanStep("s2", "picking_report_qty",
                 bindings={"quantity": "$input:quantity"},
                 record_ref="$input:move_line_id"),
        PlanStep("s3", "picking_capture_evidence",
                 bindings={"note": "$input:damage_note"},
                 record_ref="$input:picking_id"),
        PlanStep("s4", "picking_validate",
                 bindings={}, record_ref="$input:picking_id"),
    ],
    "扫码定位商品，口述实收数量，记录破损证据，最后经确认完成收货过账。"))
_wh_receipt._keywords = ("收货闭环", "完成收货", "receipt roundtrip")

