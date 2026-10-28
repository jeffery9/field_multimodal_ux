# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Sales-visit task source + completion capability for the task loop.

Task source: a **crm.lead** owned by me is one visit work item. The guided
flow is `sales_guided_visit`; completing it means the visit is finished,
so the completion capability is a dedicated L2 capability.

Deliberately NOT done here: advancing the lead stage, confirming or
sending a quote, or any customer-facing communication. Completing a visit
records the visit (optional summary to chatter + optional next action)
and leaves the sales pipeline entirely to the native flow (PRD §4.4).
"""
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    CAPABILITY_REGISTRY, Capability, ParamSchema,
    Field, TaskSource, register_task_source,
)


# ------------------------------------------------------------------ #
# completion executor
# ------------------------------------------------------------------ #
def _complete_visit(env, record, params):
    """L2: finish the work item = the visit happened.

    An optional dictated summary is posted to the chatter and an optional
    next action is written to the lead. The lead stays open — pipeline
    progression is a sales decision, not a field-UX side effect.
    """
    lead = record  # crm.lead
    summary = params.get("summary") or params.get("note")
    if summary:
        lead.message_post(body=summary,
                          subject=_("Visit summary (field, multimodal)"))
    next_action = params.get("next_action")
    if next_action:
        lead.write({"next_action": next_action})
    return {"lead": lead.name, "stage": lead.stage_id.name,
            "summary_posted": bool(summary),
            "next_action_set": bool(next_action),
            "pipeline_untouched": True}


# ------------------------------------------------------------------ #
# capability registration
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="visit_task_complete",
    name="Complete visit task",
    description="任务循环的完成动作：记录拜访结果并把该拜访任务标记完成；商机阶段仍走原生销售流程",
    target_model="crm.lead",
    action="message_post",
    schema=ParamSchema([
        Field("summary", "str", required=False,
              help="optional dictated visit summary"),
        Field("next_action", "str", required=False,
              help="optional next action description for the lead"),
    ]),
    risk="L2", require_confirmation=True, scenario="sales_visit",
    executor=_complete_visit,
))


# ------------------------------------------------------------------ #
# task source registration
# ------------------------------------------------------------------ #
def _my_open_leads(env, user):
    """Work items = leads I own that are still active."""
    return [("user_id", "=", user.id), ("active", "=", True)]


register_task_source(TaskSource(
    source_id="sales_visit",
    scenario="sales_visit",
    task_model="crm.lead",
    flow_id="sales_guided_visit",
    complete_capability="visit_task_complete",
    title_field="name",
    priority_field="priority, date_deadline",
    domain=_my_open_leads,
    label={"zh_CN": "客户拜访", "en_US": "Customer visit"},
    description={
        "zh_CN": "我负责且仍在进行的商机/客户拜访，按优先级与截止日期排序。",
        "en_US": "Active leads I own, ordered by priority and deadline.",
    },
    state_field="active",
    pending_states=(True,),
))
