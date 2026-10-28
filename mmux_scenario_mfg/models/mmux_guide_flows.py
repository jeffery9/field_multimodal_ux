# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Manufacturing guided work-order flow, with novice / expert variants.

Both variants map to the same capabilities; only granularity and prompt
verbosity differ. The production report stays L2 (explicit confirmation)
in both modes.
"""
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    GuidedStep, TaskFlow, register_flow,
)


def T(zh, en):
    """Per-language text; resolved to the user's language at the edge."""
    return {"zh_CN": zh, "zh": zh, "en_US": en, "en": en}


def _workorders(env, params):
    Workorder = env["mrp.workorder"]
    wo_id = (params or {}).get("workorder_id")
    mo_id = (params or {}).get("production_id")
    domain = [("state", "in", ("pending", "ready", "progress"))]
    if wo_id:
        domain = [("id", "=", int(wo_id))]
    elif mo_id:
        domain = [("production_id", "=", int(mo_id))]
    return Workorder.search(domain, order="production_id, sequence", limit=5)


def _novice_failure_step(wo):
    return GuidedStep(
        f"fault_{wo.id}",
        title=T("④ 异常上报（可选）· %s" % wo.name,
                "4) Report a fault (optional) - %s" % wo.name),
        voice_prompt=T(
            "如果这台设备有异响、漏油或其他异常，可以口述异常现象，"
            "我会生成维修申请。没有异常就直接跳过。",
            "If this machine shows noise, leakage or another anomaly, "
            "describe it and a maintenance request is drafted. Skip "
            "otherwise."),
        input_type="capability",
        capability_id="equipment_report_failure",
        params={"description": None},
        skippable=True,
        hint=T("口述「主轴异响」这类现象即可，会生成维修请求草稿。",
               "Describe it briefly, e.g. 'spindle noise'; a draft request "
               "is created."))


def _novice_steps(env, params):
    steps = []
    for wo in _workorders(env, params):
        product = wo.product_id.display_name
        operation = wo.operation_id.display_name or wo.name
        steps.append(GuidedStep(
            f"goto_wo_{wo.id}",
            title=T("① 到达工位 · %s" % wo.name,
                    "1) Go to workstation - %s" % wo.name),
            voice_prompt=T(
                "准备开始工序 %s。请先到工位，确认设备已通电、"
                "物料已到位、防护装置正常。" % operation,
                "Starting operation %s. Go to the workstation and check that "
                "the machine is powered, material is ready and guards are in "
                "place." % operation),
            input_type="confirm", skippable=True,
            hint=T("开工前检查：设备通电、物料到位、安全防护。",
                   "Pre-start checks: power, material, safety guards."),
            expected=operation))
        steps.append(GuidedStep(
            f"scan_wo_{wo.id}",
            title=T("② 核对工单 · %s" % wo.name,
                    "2) Verify work order - %s" % wo.name),
            voice_prompt=T(
                "请扫描工单 %s 的条码，或直接扫描产品 %s 核对。" % (wo.name, product),
                "Scan the work order %s, or scan the product %s to verify."
                % (wo.name, product)),
            input_type="scan",
            capability_id="stock_query_product",
            params={"barcode": None},
            hint=T("应为：%s" % product, "Expected: %s" % product),
            expected=product, skippable=False))
        steps.append(GuidedStep(
            f"report_wo_{wo.id}",
            title=T("③ 口述报工 · %s" % wo.name,
                    "3) Dictate production - %s" % wo.name),
            voice_prompt=T(
                "请口述本工序的完成数量和不合格数量，例如「完成 120 件，"
                "3 件不合格」。系统计划数量是 %s，如果产量不同请如实口述，"
                "报工需要你确认后才会提交。" % wo.qty_production,
                "Dictate the finished and rejected quantities, e.g. 'done 120, "
                "3 scrap'. The planned quantity is %s; report the real counts. "
                "Nothing is submitted until you confirm." % wo.qty_production),
            input_type="voice_qty",
            capability_id="wo_report_production",
            params={"qty_scrapped": 0},
            record_ref=wo.id,
            hint=T("计划 %s，不合格数量要如实上报" % wo.qty_production,
                   "Planned %s; report rejects honestly" % wo.qty_production),
            expected=wo.qty_production, skippable=False))
        steps.append(_novice_failure_step(wo))
        steps.append(GuidedStep(
            f"next_check_{wo.id}",
            title=T("⑤ 工序收尾 · %s" % wo.name,
                    "5) Close out - %s" % wo.name),
            voice_prompt=T(
                "工序 %s 报工完成。确认产量、废品数量都正确后继续下一道工序。" % operation,
                "Operation %s reported. Confirm the quantities, then continue "
                "with the next operation." % operation),
            input_type="confirm", skippable=True))
    return steps


def _expert_steps(env, params):
    steps = []
    for wo in _workorders(env, params):
        product = wo.product_id.display_name
        operation = wo.operation_id.display_name or wo.name
        steps.append(GuidedStep(
            f"scan_wo_{wo.id}",
            title=T("核对 %s" % wo.name, "Verify %s" % wo.name),
            voice_prompt=T("%s，%s。" % (wo.name, product),
                           "%s, %s." % (wo.name, product)),
            input_type="scan",
            capability_id="stock_query_product",
            params={"barcode": None},
            expected=product, skippable=False))
        steps.append(GuidedStep(
            f"report_wo_{wo.id}",
            title=T("报工 %s" % wo.name, "Report %s" % wo.name),
            voice_prompt=T("%s，计划 %s，报数。" % (operation, wo.qty_production),
                           "%s, planned %s, report." % (operation, wo.qty_production)),
            input_type="voice_qty",
            capability_id="wo_report_production",
            params={"qty_scrapped": 0},
            record_ref=wo.id,
            expected=wo.qty_production, skippable=False))
    return steps


_wo_flow = register_flow(TaskFlow(
    "mfg_guided_workorder",
    {"zh_CN": "语音引导工单作业", "en_US": "Voice-guided work orders"},
    description={
        "zh_CN": "按待办工单逐道工序语音导航。新手模式含开工检查、复核与异常上报，熟手模式精简为核对+报工。",
        "en_US": "Voice navigation across pending work orders. Novice mode adds "
                 "pre-start checks and fault reporting; expert mode is verify + report.",
    },
    steps_builder_novice=_novice_steps,
    steps_builder_expert=_expert_steps))
_wo_flow._keywords = ("引导工单", "语音报工", "guided workorder")
