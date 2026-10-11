# -*- coding: utf-8 -*-
"""Manufacturing scenario capabilities (PRD §4.2 / §10.2).

Report production through the native mrp.workorder methods; state
constraints are the native ones — a non-pending/ready/progress workorder
is rejected up front, and quality checks stay with the quality module.
"""
from odoo.exceptions import UserError
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    CAPABILITY_REGISTRY, Capability, ParamSchema, Field,
)


# ------------------------------------------------------------------ #
# executors / preconditions
# ------------------------------------------------------------------ #
def _wo_precondition(record, params):
    """Illegal state changes must be rejected (acceptance §10.2)."""
    if record.state not in ("ready", "progress"):
        raise UserError(
            _("Work order %s is in state '%s'; production can only be "
              "reported on ready or in-progress work orders.")
            % (record.name, record.state))


def _query_next_operation(env, record, params):
    """L0: next operation of the current manufacturing order."""
    mo = record  # mrp.production
    wo = env["mrp.workorder"].search(
        [("production_id", "=", mo.id), ("state", "in", ["pending", "ready"])],
        order="sequence", limit=1)
    if not wo:
        return {"done": True, "message": _("all operations scheduled are finished")}
    return {
        "done": False,
        "workorder_id": wo.id,
        "name": wo.name,
        "state": wo.state,
        "operation": wo.operation_id.display_name,
    }


def _report_production(env, record, params):
    """L2: voice-reported production -> structured params -> native
    record_production (and scrap through the native flow)."""
    wo = record  # mrp.workorder
    wo.qty_production = float(params["qty_production"])
    scrapped = float(params.get("qty_scrapped") or 0)
    if scrapped > 0:
        wo.qty_scrapped = scrapped
    wo.record_production()
    return {
        "workorder": wo.name,
        "state": wo.state,
        "qty_produced": wo.qty_produced,
    }


def _report_failure(env, record, params):
    """L1: dictate an equipment issue -> maintenance.request draft.
    The request stays a draft for the maintenance workflow to pick up."""
    Equipment = env["maintenance.equipment"]
    equipment = record
    if not equipment or equipment._name != "maintenance.equipment":
        equipment = Equipment.search([], limit=1)
    Request = env["maintenance.request"]
    req = Request.create({
        "name": params.get("description") or _("Field-reported issue"),
        "equipment_id": equipment.id,
        "description": params.get("description") or "",
    })
    return {"request_id": req.id, "name": req.name, "state": req.stage_id.name or "draft"}


# ------------------------------------------------------------------ #
# capabilities
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="mo_query_next_operation",
    name="Query next work order",
    description="查询当前生产单的下一道待执行工序",
    target_model="mrp.production",
    action="read",
    schema=ParamSchema([]),
    risk="L0", require_confirmation=False, scenario="manufacturing",
    executor=_query_next_operation,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="wo_report_production",
    name="Report work order production",
    description="口述报工：完成数量与不合格数量，确认后调用原生 record_production",
    target_model="mrp.workorder",
    action="record_production",
    schema=ParamSchema([
        Field("qty_production", "float", required=True, min_val=0,
              help="good quantity finished"),
        Field("qty_scrapped", "float", required=False, min_val=0,
              help="rejected quantity"),
    ]),
    risk="L2", require_confirmation=True, scenario="manufacturing",
    precondition=_wo_precondition,
    executor=_report_production,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="equipment_report_failure",
    name="Report equipment issue",
    description="口述设备异常，生成维护请求草稿",
    target_model="maintenance.equipment",
    action="create_request",
    schema=ParamSchema([
        Field("description", "str", required=True),
    ]),
    risk="L1", require_confirmation=False, scenario="manufacturing",
    executor=_report_failure,
))

