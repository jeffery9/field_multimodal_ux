# -*- coding: utf-8 -*-
"""Field-service task source + completion capability for the task loop.

Task source: a **project.task** assigned to me and still open is one work
item. The guided flow is `fs_guided_service`; completing it means closing
the task through the native state flow, so the completion capability is a
dedicated L2 capability.

The completion capability posts the dictated service summary (if any) and
then closes the task — but customer acceptance and invoicing remain in
their native flows, untouched (PRD §4.5).
"""
from odoo.exceptions import UserError
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    CAPABILITY_REGISTRY, Capability, ParamSchema,
    Field, TaskSource, register_task_source,
)


# ------------------------------------------------------------------ #
# completion executor / precondition
# ------------------------------------------------------------------ #
def _service_complete_precondition(record, params):
    if record.state in ("1_done", "1_canceled"):
        raise UserError(
            _("Task %s is already closed.") % record.display_name)


def _complete_service_task(env, record, params):
    """L2: finish the work item = close the service task natively.

    An optional dictated summary is posted to the chatter BEFORE closing,
    so the record keeps the evidence trail. No invoicing / acceptance is
    triggered here — those stay native.
    """
    task = record  # project.task
    summary = params.get("summary") or params.get("note")
    if summary:
        task.message_post(body=summary, subject=_("Service summary (field)"))
    task.write({"state": "1_done"})
    return {"task": task.display_name, "state": task.state,
            "summary_posted": bool(summary)}


# ------------------------------------------------------------------ #
# capability registration
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="service_task_complete",
    name="Complete service task",
    description="任务循环的完成动作：按原生状态流关闭服务任务，可选先记录口述服务摘要",
    target_model="project.task",
    action="state_change",
    schema=ParamSchema([
        Field("summary", "str", required=False,
              help="optional dictated service summary posted before closing"),
    ]),
    risk="L2", require_confirmation=True, scenario="field_service",
    precondition=_service_complete_precondition,
    executor=_complete_service_task,
))


# ------------------------------------------------------------------ #
# task source registration
# ------------------------------------------------------------------ #
def _my_open_tasks(env, user):
    """Work items = service tasks assigned to me that are still open."""
    return [("user_ids", "in", user.id),
            ("state", "not in", ("1_done", "1_canceled"))]


register_task_source(TaskSource(
    source_id="fs_task",
    scenario="field_service",
    task_model="project.task",
    flow_id="fs_guided_service",
    complete_capability="service_task_complete",
    title_field="name",
    priority_field="priority, date_deadline",
    domain=_my_open_tasks,
    label={"zh_CN": "服务任务", "en_US": "Service task"},
    description={
        "zh_CN": "分配给我且未关闭的服务任务，按优先级与截止日期排序。",
        "en_US": "Open service tasks assigned to me, ordered by priority and "
                 "deadline.",
    },
    state_field="state",
    pending_states=("0_new", "1_in_progress"),
))
