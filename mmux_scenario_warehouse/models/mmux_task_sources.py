# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Warehouse task source + completion capability for the task loop.

Task source: a pending, user-assigned **stock.picking** is one work item.
The guided flow that narrates it is `wh_guided_picking`; completing it
means validating the transfer, so the loop's completion capability is a
dedicated L2 capability that calls the SAME native `button_validate()`.

Why a dedicated capability instead of reusing `picking_validate`?
The task loop's semantics are "this work item is finished". Keeping a
separate id makes the loop contract explicit and audit-friendly while
executing the identical native method — no new business mutation is
introduced (PRD §7.1).
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
def _picking_complete_precondition(record, params):
    """A picking that cannot be validated natively must be refused early
    with a clear message (acceptance §10.2/§10.3)."""
    if record.state == "done":
        raise UserError(_("Picking %s is already done.") % record.name)
    if record.state in ("cancel",):
        raise UserError(_("Picking %s is cancelled.") % record.name)


def _complete_picking(env, record, params):
    """L2: finish the task by delegating to the native button_validate.
    Backorders, lot requirements and any other native rule stay
    authoritative — the loop adds no bypass."""
    picking = record  # stock.picking
    action = picking.button_validate()
    # button_validate may open a wizard dict (e.g. backorder / immediate
    # transfer); return it so the caller can surface it instead of
    # pretending the task is fully done.
    if isinstance(action, dict) and action.get("res_model"):
        return {"picking": picking.name, "state": picking.state,
                "wizard": action.get("res_model"),
                "needs_action": True}
    return {"picking": picking.name, "state": picking.state,
            "needs_action": False}


# ------------------------------------------------------------------ #
# capability registration
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="picking_task_complete",
    name="Complete picking task",
    description="任务循环的完成动作：确认拣货单已完成并调用原生 button_validate 过账",
    target_model="stock.picking",
    action="button_validate",
    schema=ParamSchema([]),
    risk="L2", require_confirmation=True, scenario="warehouse",
    precondition=_picking_complete_precondition,
    executor=_complete_picking,
))


# ------------------------------------------------------------------ #
# task source registration
# ------------------------------------------------------------------ #
def _pending_pickings(env, user):
    """Work items = transfers assigned to me that still need doing."""
    return [("state", "in", ("assigned", "confirmed")),
            ("user_id", "=", user.id)]


register_task_source(TaskSource(
    source_id="wh_picking",
    scenario="warehouse",
    task_model="stock.picking",
    flow_id="wh_guided_picking",
    complete_capability="picking_task_complete",
    title_field="name",
    priority_field="scheduled_date, id",
    domain=_pending_pickings,
    label={"zh_CN": "拣货/收货单", "en_US": "Picking / receipt"},
    description={
        "zh_CN": "分配给我且处于已分配/待处理的调拨单，按计划日期排序。",
        "en_US": "Transfers assigned to me in assigned/confirmed state, "
                 "ordered by scheduled date.",
    },
    state_field="state",
    pending_states=("assigned", "confirmed"),
))
