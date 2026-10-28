# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Manufacturing task source + completion capability for the task loop.

Task source: a **mrp.workorder** still to be executed is one work item.
The guided flow is `mfg_guided_workorder`; completing it means reporting
the production for that operation, so the completion capability is a
dedicated L2 capability that reports and then reports the operation done
via the native flow.

Quantity reporting stays L2 (explicit confirmation): a human confirms the
good/rejected counts before anything is posted — the loop never
auto-reports production.
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
def _wo_complete_precondition(record, params):
    """Reuse the same native state rule as wo_report_production: only a
    ready / in-progress work order can be reported and finished."""
    if record.state not in ("ready", "progress"):
        raise UserError(
            _("Work order %s is in state '%s'; it can only be completed "
              "when ready or in progress.") % (record.name, record.state))


def _complete_workorder(env, record, params):
    """L2: finish the task = report the production of this operation.

    `qty_production` is required; `qty_scrapped` is optional. Both go
    through the native record_production(), so scrap handling and the
    work-order state machine stay entirely native.
    """
    wo = record  # mrp.workorder
    qty = params.get("qty_production")
    if qty is None:
        # no quantity dictated -> report the remaining planned quantity
        remaining = (wo.qty_production or 0.0) - (wo.qty_produced or 0.0)
        qty = remaining if remaining > 0 else (wo.qty_production or 0.0)
    wo.qty_production = float(qty)
    scrapped = float(params.get("qty_scrapped") or 0)
    if scrapped > 0:
        wo.qty_scrapped = scrapped
    wo.record_production()
    mo = wo.production_id
    next_wo = env["mrp.workorder"].search(
        [("production_id", "=", mo.id),
         ("state", "in", ["pending", "ready", "progress"])],
        order="sequence", limit=1)
    return {
        "workorder": wo.name,
        "state": wo.state,
        "qty_produced": wo.qty_produced,
        "mo": mo.name,
        "mo_done": not next_wo,
        "next_workorder_id": next_wo.id if next_wo else None,
    }


# ------------------------------------------------------------------ #
# capability registration
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="wo_task_complete",
    name="Complete work order task",
    description="任务循环的完成动作：口述/录入完成数量与不合格数量并调用原生 record_production",
    target_model="mrp.workorder",
    action="record_production",
    schema=ParamSchema([
        Field("qty_production", "float", required=False, min_val=0,
              help="good quantity finished; defaults to the remaining qty"),
        Field("qty_scrapped", "float", required=False, min_val=0,
              help="rejected quantity"),
    ]),
    risk="L2", require_confirmation=True, scenario="manufacturing",
    precondition=_wo_complete_precondition,
    executor=_complete_workorder,
))


# ------------------------------------------------------------------ #
# task source registration
# ------------------------------------------------------------------ #
def _pending_workorders(env, user):
    """Work items = operations not yet executed (no per-user assignment in
    Community MRP, so the native state is the queue)."""
    return [("state", "in", ("pending", "ready", "progress"))]


register_task_source(TaskSource(
    source_id="mfg_workorder",
    scenario="manufacturing",
    task_model="mrp.workorder",
    flow_id="mfg_guided_workorder",
    complete_capability="wo_task_complete",
    title_field="name",
    priority_field="production_id, sequence",
    domain=_pending_workorders,
    label={"zh_CN": "工单工序", "en_US": "Work order operation"},
    description={
        "zh_CN": "待执行/执行中的工序，按生产单与工序顺序排序。",
        "en_US": "Pending or in-progress operations, ordered by manufacturing "
                 "order and operation sequence.",
    },
    state_field="state",
    pending_states=("pending", "ready", "progress"),
))
