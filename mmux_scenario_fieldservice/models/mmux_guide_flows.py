# -*- coding: utf-8 -*-
"""Field-service guided flow: voice navigation of a service task.

Previously this scenario only had capabilities + a plan, no guided flow.
The task loop needs one, so it ships here with the same novice/expert
contract as the other scenarios: identical capabilities, different
granularity and verbosity, no difference in authority.

Novice: arrive -> identify equipment -> check history -> dictate the
service report -> capture evidence -> log parts -> confirm close-out.
Expert: identify -> dictate report -> confirm close-out.
"""
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    GuidedStep, TaskFlow, register_flow,
)


def T(zh, en):
    """Per-language step text; resolved to the user's language at the edge."""
    return {"zh_CN": zh, "zh": zh, "en_US": en, "en": en}


def _tasks(env, params):
    """The service task(s) this flow covers: an explicit one when the task
    loop hands us `task_id`, otherwise my open tasks."""
    Task = env["project.task"]
    task_id = (params or {}).get("task_id") or (params or {}).get("res_id")
    domain = [("state", "not in", ("1_done", "1_canceled"))]
    if task_id:
        domain = [("id", "=", int(task_id))]
    else:
        domain = domain + [("user_ids", "in", env.user.id)]
    return Task.search(domain, order="priority, date_deadline", limit=5)


# --------------------------------------------------------------------- #
# NOVICE: granular, explanatory
# --------------------------------------------------------------------- #
def _novice_steps(env, params):
    steps = []
    for task in _tasks(env, params):
        steps.append(GuidedStep(
            f"goto_{task.id}",
            title=T("① 到达客户现场 · %s" % task.name,
                    "1) Arrive on site - %s" % task.name),
            voice_prompt=T(
                "开始处理服务任务 %s。到达客户现场后，先确认联系人、"
                "作业环境与安全事项，然后点确认。" % task.name,
                "Starting service task %s. Once on site, confirm the contact, "
                "the working area and any safety concern, then confirm."
                % task.name),
            input_type="confirm", skippable=True,
            hint=T("确认联系人、作业区域与安全防护已就绪。",
                   "Confirm contact, work area and safety are ready."),
            expected=task.name))
        steps.append(GuidedStep(
            f"identify_{task.id}",
            title=T("② 识别设备 · %s" % task.name,
                    "2) Identify equipment - %s" % task.name),
            voice_prompt=T(
                "请扫描设备铭牌或序列号，确认你要维修的这台设备。"
                "系统会给出候选，需要你确认后才继续。",
                "Scan the nameplate or serial number to confirm which unit "
                "you are servicing. The system returns candidates; you pick "
                "one before anything continues."),
            input_type="scan",
            capability_id="equipment_identify",
            params={"barcode": None},
            hint=T("铭牌/序列号即可，不要凭猜测选定设备。",
                   "Nameplate or serial; never guess which unit it is."),
            skippable=True))
        steps.append(GuidedStep(
            f"history_{task.id}",
            title=T("③ 查看历史 · %s" % task.name,
                    "3) Check history - %s" % task.name),
            voice_prompt=T(
                "先看这台设备的相关服务任务与维修历史，避免重复处理或漏掉遗留问题。",
                "Review this unit's open tasks and maintenance history first, "
                "so nothing is duplicated or left over."),
            input_type="confirm", skippable=True,
            hint=T("关注未关闭的历史工单。",
                   "Pay attention to any still-open past request.")))
        steps.append(GuidedStep(
            f"report_{task.id}",
            title=T("④ 口述服务报告 · %s" % task.name,
                    "4) Dictate service report - %s" % task.name),
            voice_prompt=T(
                "请口述维修步骤、工时和更换的备件，例如「更换滤芯，"
                "用时 1.5 小时」。系统会生成报告草稿，你可以检查后再确认。",
                "Dictate the repair steps, hours and parts replaced, e.g. "
                "'replaced the filter, 1.5 hours'. A draft report is created "
                "for you to review."),
            input_type="confirm",
            capability_id="service_report_note",
            params={"note": None},
            record_ref=task.id,
            hint=T("草稿会写入任务记录，可修改。",
                   "The draft is posted to the task and can be edited."),
            skippable=True))
        steps.append(GuidedStep(
            f"evidence_{task.id}",
            title=T("⑤ 拍摄现场证据 · %s" % task.name,
                    "5) Capture evidence - %s" % task.name),
            voice_prompt=T(
                "拍摄维修前后的对比照片并关联到任务。没有照片可以跳过。",
                "Take before/after photos and attach them to the task. Skip "
                "if you have none."),
            input_type="confirm", skippable=True,
            hint=T("照片与说明会作为证据留档。",
                   "Photos and a note are kept as evidence.")))
        steps.append(GuidedStep(
            f"part_{task.id}",
            title=T("⑥ 记录备件 · %s" % task.name,
                    "6) Log parts - %s" % task.name),
            voice_prompt=T(
                "如果更换了备件，请口述备件名称与数量。库存过账仍走原生仓库流程。",
                "If you replaced parts, dictate the part and quantity. Stock "
                "posting still goes through the native warehouse flow."),
            input_type="confirm", skippable=True,
            hint=T("这里只做现场记录，不自动过账库存。",
                   "This records the fact only; it does not post stock.")))
        steps.append(GuidedStep(
            f"close_{task.id}",
            title=T("⑦ 完工确认 · %s" % task.name,
                    "7) Confirm close-out - %s" % task.name),
            voice_prompt=T(
                "服务已完成。确认后任务会按原生状态流关闭；客户验收与开票"
                "留在原生流程里。",
                "Service is done. Confirming closes the task through the "
                "native state flow; customer acceptance and invoicing stay "
                "in their native flows."),
            input_type="capability",
            capability_id="service_task_mark_done",
            params={},
            record_ref=task.id,
            hint=T("关闭前请确认报告已核对。",
                   "Review the report before closing."),
            skippable=False))
    return steps


# --------------------------------------------------------------------- #
# EXPERT: merged, terse
# --------------------------------------------------------------------- #
def _expert_steps(env, params):
    steps = []
    for task in _tasks(env, params):
        steps.append(GuidedStep(
            f"identify_{task.id}",
            title=T("识别 %s" % task.name, "Identify %s" % task.name),
            voice_prompt=T("%s，扫铭牌。" % task.name,
                           "%s, scan the nameplate." % task.name),
            input_type="scan",
            capability_id="equipment_identify",
            params={"barcode": None}, skippable=True))
        steps.append(GuidedStep(
            f"report_{task.id}",
            title=T("报告 %s" % task.name, "Report %s" % task.name),
            voice_prompt=T("%s，口述报告。" % task.name,
                           "%s, dictate the report." % task.name),
            input_type="confirm",
            capability_id="service_report_note",
            params={"note": None}, record_ref=task.id, skippable=True))
        steps.append(GuidedStep(
            f"close_{task.id}",
            title=T("完工 %s" % task.name, "Close %s" % task.name),
            voice_prompt=T("%s 完工。" % task.name, "Close %s." % task.name),
            input_type="capability",
            capability_id="service_task_mark_done",
            params={}, record_ref=task.id, skippable=False))
    return steps


_fs_flow = register_flow(TaskFlow(
    "fs_guided_service",
    {"zh_CN": "语音引导现场服务", "en_US": "Voice-guided field service"},
    description={
        "zh_CN": "按服务任务语音导航：识别设备、查看历史、口述报告、取证、记录备件、完工确认。新手模式逐步讲解，熟手模式合并步骤。",
        "en_US": "Voice navigation of service tasks: identify the unit, check "
                 "history, dictate the report, capture evidence, log parts, "
                 "confirm close-out. Novice explains each step; expert merges.",
    },
    steps_builder_novice=_novice_steps,
    steps_builder_expert=_expert_steps))
_fs_flow._keywords = ("引导服务", "语音报修", "guided service")
