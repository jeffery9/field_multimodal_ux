# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Sales visit guided flow: voice navigation of a customer visit.

Previously this scenario had capabilities only — no flow. The task loop
needs one so the visit can be narrated step by step, with the same
novice/expert contract used everywhere else.

Novice: arrive -> review history -> dictate the visit note -> extract
structured requirements onto the lead -> schedule the follow-up ->
confirm the visit as done.
Expert: history -> note -> confirm done.

Nothing customer-facing is ever sent automatically: notes are chatter
comments, the lead update is whitelist-only, and the follow-up is a
native activity assigned to the user.
"""
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    GuidedStep, TaskFlow, register_flow,
)


def T(zh, en):
    """Per-language step text; resolved to the user's language at the edge."""
    return {"zh_CN": zh, "zh": zh, "en_US": en, "en": en}


def _leads(env, params):
    """The visit(s) this flow covers: an explicit lead when the task loop
    hands us `lead_id`/`res_id`, otherwise my open leads."""
    Lead = env["crm.lead"]
    lead_id = ((params or {}).get("lead_id") or (params or {}).get("res_id"))
    if lead_id:
        return Lead.search([("id", "=", int(lead_id))], limit=1)
    domain = [("user_id", "=", env.user.id)]
    return Lead.search(domain, order="priority, date_deadline", limit=5)


# --------------------------------------------------------------------- #
# NOVICE: granular, explanatory
# --------------------------------------------------------------------- #
def _novice_steps(env, params):
    steps = []
    for lead in _leads(env, params):
        partner = lead.partner_id.display_name or lead.name
        steps.append(GuidedStep(
            f"goto_{lead.id}",
            title=T("① 到达客户 · %s" % partner, "1) Arrive at %s" % partner),
            voice_prompt=T(
                "开始拜访 %s。到达后先确认对接人和本次拜访目标，再点确认。"
                % partner,
                "Starting the visit to %s. Confirm the contact and the goal "
                "of this visit, then continue." % partner),
            input_type="confirm", skippable=True,
            hint=T("明确本次要达成的目标（续约/报价/回访）。",
                   "Be clear on the goal (renewal / quote / courtesy)."),
            expected=partner))
        steps.append(GuidedStep(
            f"history_{lead.id}",
            title=T("② 查看客户历史 · %s" % partner,
                    "2) Review history - %s" % partner),
            voice_prompt=T(
                "先看这家客户最近的订单、商机和跟进记录，避免重复承诺或遗漏遗留问题。",
                "Review recent orders, leads and follow-ups first, so nothing "
                "is duplicated or left hanging."),
            input_type="confirm", skippable=True,
            hint=T("关注未关闭的商机与逾期跟进。",
                   "Watch for open leads and overdue follow-ups.")))
        steps.append(GuidedStep(
            f"note_{lead.id}",
            title=T("③ 口述拜访纪要 · %s" % partner,
                    "3) Dictate visit note - %s" % partner),
            voice_prompt=T(
                "请口述本次拜访纪要，例如「客户关心交期，要求下周一前给报价」。"
                "系统会生成纪要草稿，你可以检查后再保存。",
                "Dictate the visit summary, e.g. 'the customer cares about "
                "lead time and wants a quote by next Monday'. A draft note is "
                "created for you to review."),
            input_type="confirm",
            capability_id="visit_record_note",
            params={"summary": None},
            record_ref=lead.id,
            hint=T("纪要写入商机沟通记录，可修改。",
                   "The note is posted to the lead's chatter and can be edited."),
            skippable=True))
        steps.append(GuidedStep(
            f"fields_{lead.id}",
            title=T("④ 更新商机字段 · %s" % partner,
                    "4) Update the lead - %s" % partner),
            voice_prompt=T(
                "如果客户给了明确的预算或下一步动作，可以口述更新到商机；"
                "没有新增信息就跳过这一步。",
                "If the customer gave a budget or a clear next step, dictate "
                "it onto the lead; skip if there is nothing new."),
            input_type="confirm",
            capability_id="lead_update_fields",
            params={},
            record_ref=lead.id,
            hint=T("只写入白名单字段：名称 / 预期收入 / 下一步。",
                   "Only whitelisted fields: name / expected revenue / next step."),
            skippable=True))
        steps.append(GuidedStep(
            f"followup_{lead.id}",
            title=T("⑤ 安排跟进 · %s" % partner,
                    "5) Schedule follow-up - %s" % partner),
            voice_prompt=T(
                "口述下一次跟进的时间与事项，例如「下周一 上午 10 点电话跟进」。"
                "系统会创建一条跟进活动。",
                "Dictate the next follow-up, e.g. 'call next Monday at 10am'. "
                "An activity is created for you."),
            input_type="confirm",
            capability_id="schedule_followup",
            params={"summary": None, "date_deadline": None},
            record_ref=lead.id,
            hint=T("有明确日期才会创建跟进活动。",
                   "A concrete date is required to create the activity."),
            skippable=True))
        steps.append(GuidedStep(
            f"done_{lead.id}",
            title=T("⑥ 拜访完成 · %s" % partner,
                    "6) Finish the visit - %s" % partner),
            voice_prompt=T(
                "本次拜访已完成。确认后系统会把这个拜访任务标记完成，"
                "并发布下一个任务。商机阶段与报价仍走原生销售流程。",
                "The visit is done. Confirming marks this visit task complete "
                "and publishes the next task. The lead stage and quotes stay "
                "in the native sales flow."),
            input_type="capability",
            capability_id="visit_task_complete",
            params={},
            record_ref=lead.id,
            hint=T("确认前请核对纪要已保存。",
                   "Check the note is saved before confirming."),
            skippable=False))
    return steps


# --------------------------------------------------------------------- #
# EXPERT: merged, terse
# --------------------------------------------------------------------- #
def _expert_steps(env, params):
    steps = []
    for lead in _leads(env, params):
        partner = lead.partner_id.display_name or lead.name
        steps.append(GuidedStep(
            f"history_{lead.id}",
            title=T("历史 %s" % partner, "History %s" % partner),
            voice_prompt=T("%s，先看历史。" % partner,
                           "%s, review history first." % partner),
            input_type="confirm", skippable=True))
        steps.append(GuidedStep(
            f"note_{lead.id}",
            title=T("纪要 %s" % partner, "Note %s" % partner),
            voice_prompt=T("%s，口述纪要。" % partner,
                           "%s, dictate the note." % partner),
            input_type="confirm",
            capability_id="visit_record_note",
            params={"summary": None}, record_ref=lead.id, skippable=True))
        steps.append(GuidedStep(
            f"done_{lead.id}",
            title=T("完成 %s" % partner, "Done %s" % partner),
            voice_prompt=T("%s 拜访完成。" % partner,
                           "Visit to %s complete." % partner),
            input_type="capability",
            capability_id="visit_task_complete",
            params={}, record_ref=lead.id, skippable=False))
    return steps


_sales_flow = register_flow(TaskFlow(
    "sales_guided_visit",
    {"zh_CN": "语音引导客户拜访", "en_US": "Voice-guided sales visit"},
    description={
        "zh_CN": "按客户拜访任务语音导航：查看历史、口述纪要、更新商机、安排跟进、确认完成。新手模式逐步讲解，熟手模式合并步骤。",
        "en_US": "Voice navigation of customer visits: review history, dictate "
                 "the note, update the lead, schedule a follow-up, confirm "
                 "done. Novice explains each step; expert merges.",
    },
    steps_builder_novice=_novice_steps,
    steps_builder_expert=_expert_steps))
_sales_flow._keywords = ("引导拜访", "语音拜访", "guided visit")
