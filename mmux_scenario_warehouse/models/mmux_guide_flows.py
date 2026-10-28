# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Warehouse guided picking flow: voice navigation over live records.

Two skill variants share the SAME capabilities, differing only in
granularity and verbosity:

- **novice**: every composite action is split (arrive → find location →
  scan → verify the scanned product matches → dictate quantity → confirm
  the echoed quantity), with "why" explanations and expected values so a
  new operator can self-check at each point.
- **expert**: composite actions merged into one step per line with terse
  prompts (destination + product + quantity in a single sentence).

Neither variant changes authority: the quantity write stays an L2
capability and picking validation still requires explicit confirmation.
"""
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    GuidedStep, TaskFlow, register_flow,
)


def T(zh, en):
    """Per-language step text; the engine resolves to the user's language
    with a fallback chain (zh_CN -> zh -> en_US)."""
    return {"zh_CN": zh, "zh": zh, "en_US": en, "en": en}


def _pickings(env, params):
    Picking = env["stock.picking"]
    picking_id = (params or {}).get("picking_id")
    domain = [("state", "in", ("assigned", "confirmed")),
              ("user_id", "=", env.user.id)]
    if picking_id:
        domain = [("id", "=", int(picking_id))]
    return Picking.search(domain, order="scheduled_date", limit=5)


# --------------------------------------------------------------------- #
# NOVICE: granular, explanatory
# --------------------------------------------------------------------- #
def _novice_steps(env, params):
    steps = []
    for picking in _pickings(env, params):
        lines = picking.move_ids.move_line_ids
        steps.append(GuidedStep(
            f"goto_{picking.id}",
            title=T("① 到达拣货单 %s" % picking.name,
                    "1) Go to picking %s" % picking.name),
            voice_prompt=T(
                "开始处理拣货单 %s，共 %s 个明细。先把拣货车推到拣货位，准备好扫码枪。"
                % (picking.name, len(picking.move_ids)),
                "Starting picking %s with %s lines. Move your cart to the "
                "picking area and get your scanner ready."
                % (picking.name, len(picking.move_ids))),
            input_type="confirm", skippable=True,
            hint=T("确认已到达起始位置再继续。",
                   "Confirm once you have arrived at the start location."),
            expected=T("%s 个明细" % len(lines),
                       "%s lines" % len(lines))))
        for idx, line in enumerate(lines, start=1):
            product = line.product_id.display_name
            steps.append(GuidedStep(
                f"loc_{line.id}",
                title=T("② 前往库位（第 %s 项）" % idx,
                        "2) Go to location (line %s)" % idx),
                voice_prompt=T(
                    "第 %s 项：请前往库位 %s。到达后点确认。" % (
                        idx, line.location_id.display_name),
                    "Line %s: go to location %s, then confirm." % (
                        idx, line.location_id.display_name)),
                input_type="confirm", skippable=False,
                hint=T("库位：%s" % line.location_id.display_name,
                       "Location: %s" % line.location_id.display_name),
                expected=line.location_id.display_name))
            steps.append(GuidedStep(
                f"scan_{line.id}",
                title=T("③ 扫描商品（第 %s 项）" % idx,
                        "3) Scan product (line %s)" % idx),
                voice_prompt=T(
                    "请扫描商品 %s 的条码。如果扫错商品系统会提示你。" % product,
                    "Scan the barcode of %s. The system warns you if it is "
                    "the wrong product." % product),
                input_type="scan",
                capability_id="stock_query_product",
                params={"barcode": None},
                hint=T("应为：%s" % product, "Expected: %s" % product),
                expected=product, skippable=False))
            steps.append(GuidedStep(
                f"qty_{line.id}",
                title=T("④ 口述数量（第 %s 项）" % idx,
                        "4) Dictate quantity (line %s)" % idx),
                voice_prompt=T(
                    "请口述 %s 的拣货数量。系统建议数量是 %s %s，"
                    "如果实物数量不同，请如实口述。" % (
                        product, int(line.product_uom_qty),
                        line.product_uom_id.name),
                    "Dictate the picked quantity for %s. The suggested "
                    "quantity is %s %s; report the real count if it differs."
                    % (product, int(line.product_uom_qty),
                       line.product_uom_id.name)),
                input_type="voice_qty",
                capability_id="picking_report_qty",
                params={"quantity": None},
                record_ref=line.id,
                hint=T("建议 %s %s，差异要如实上报" % (
                            int(line.product_uom_qty), line.product_uom_id.name),
                       "Suggested %s %s; report any difference honestly" % (
                            int(line.product_uom_qty), line.product_uom_id.name)),
                expected=line.product_uom_qty, skippable=False))
            steps.append(GuidedStep(
                f"check_{line.id}",
                title=T("⑤ 复核（第 %s 项）" % idx,
                        "5) Verify (line %s)" % idx),
                voice_prompt=T(
                    "已录入 %s，数量请再核对一次，确认无误后继续下一项。" % product,
                    "Recorded %s. Double-check the quantity before moving on."
                    % product),
                input_type="confirm", skippable=False,
                hint=T("核对实物与录入数量是否一致。",
                       "Check the physical count matches what you entered.")))
        steps.append(GuidedStep(
            f"validate_{picking.id}",
            title=T("⑥ 验证单据 %s" % picking.name,
                    "6) Validate %s" % picking.name),
            voice_prompt=T(
                "拣货单 %s 的所有明细已录入。系统会核对库存和批次，"
                "确认后才会过账。" % picking.name,
                "All lines of %s are recorded. Stock and lots are verified; "
                "nothing is posted until you confirm." % picking.name),
            input_type="capability",
            capability_id="picking_validate",
            params={},
            record_ref=picking.id,
            hint=T("过账后库存才会变动，确认前可以先复核。",
                   "Stock only changes after posting; review before confirming."),
            skippable=True))
    return steps


# --------------------------------------------------------------------- #
# EXPERT: merged, terse
# --------------------------------------------------------------------- #
def _expert_steps(env, params):
    steps = []
    for picking in _pickings(env, params):
        steps.append(GuidedStep(
            f"goto_{picking.id}",
            title=T("拣货 %s" % picking.name, "Pick %s" % picking.name),
            voice_prompt=T(
                "拣货单 %s，%s 个明细。" % (picking.name, len(picking.move_ids)),
                "%s, %s lines." % (picking.name, len(picking.move_ids))),
            input_type="confirm", skippable=True))
        for line in picking.move_ids.move_line_ids:
            steps.append(GuidedStep(
                f"qty_{line.id}",
                title=T("%s @ %s" % (line.product_id.display_name,
                                     line.location_id.display_name),
                        "%s @ %s" % (line.location_id.display_name,
                                     line.product_id.display_name)),
                voice_prompt=T(
                    "%s，%s，%s %s。" % (
                        line.location_id.display_name,
                        line.product_id.display_name,
                        int(line.product_uom_qty),
                        line.product_uom_id.name),
                    "%s, %s, %s %s." % (
                        line.location_id.display_name,
                        line.product_id.display_name,
                        int(line.product_uom_qty),
                        line.product_uom_id.name)),
                input_type="voice_qty",
                capability_id="picking_report_qty",
                params={"quantity": None},
                record_ref=line.id, skippable=False))
        steps.append(GuidedStep(
            f"validate_{picking.id}",
            title=T("验证 %s" % picking.name, "Validate %s" % picking.name),
            voice_prompt=T("%s 验证。" % picking.name, "Validate %s." % picking.name),
            input_type="capability",
            capability_id="picking_validate",
            params={},
            record_ref=picking.id, skippable=True))
    return steps


_picking_flow = register_flow(TaskFlow(
    "wh_guided_picking",
    {"zh_CN": "语音引导拣货", "en_US": "Voice-guided picking"},
    description={
        "zh_CN": "按待办拣货单语音导航。新手模式逐步讲解并复核，熟手模式合并步骤精简播报。",
        "en_US": "Voice navigation over assigned pickings. Novice mode explains "
                 "and double-checks each action; expert mode merges steps.",
    },
    steps_builder_novice=_novice_steps,
    steps_builder_expert=_expert_steps))
_picking_flow._keywords = ("引导拣货", "语音拣货", "guided picking")
