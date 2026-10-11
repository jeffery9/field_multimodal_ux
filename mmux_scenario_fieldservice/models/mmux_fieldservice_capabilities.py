# -*- coding: utf-8 -*-
"""Field service & after-sales scenario capabilities (PRD §4.5 / §10.2).

Community-native models only: project.task (service task), 
maintenance.equipment (equipment), stock.picking (parts), chatter for
evidence and service report drafts. Constraints honored:
 - nameplate/scan identification produces CANDIDATES, never auto-picks
   a service target (FR-008);
 - AI-organized evidence and suggested steps are drafts; unverified
   guesses never become confirmed fault conclusions (PRD §4.5);
 - task completion follows the native project.task state flow with
   explicit confirmation (L2).
"""
from odoo.exceptions import UserError
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    CAPABILITY_REGISTRY, Capability, ParamSchema, Field,
)


# ------------------------------------------------------------------ #
# executors
# ------------------------------------------------------------------ #
def _identify_equipment(env, record, params):
    """L0: resolve equipment by nameplate barcode/serial/name -> candidates."""
    Equipment = env["maintenance.equipment"]
    key = params.get("barcode") or params.get("name")
    if not key:
        raise UserError(_("need barcode, serial or name"))
    recs = Equipment.search([
        "|", "|",
        ("name", "ilike", key),
        ("serial_no", "=", key),
        (("barcode", "=", key) if "barcode" in Equipment._fields
         else ("name", "=", f"__no_match__{key}")),
    ], limit=5)
    return {
        "candidates": [{"id": e.id, "name": e.display_name,
                        "serial_no": e.serial_no or "",
                        "category": e.category_id.display_name or ""}
                       for e in recs],
        "count": len(recs),
    }


def _query_equipment_history(env, record, params):
    """L0: equipment service history — open task + past requests."""
    equipment = record  # maintenance.equipment
    Task = env["project.task"]
    tasks = Task.search([("name", "ilike", equipment.name)], order="id desc", limit=5)
    Request = env["maintenance.request"]
    reqs = Request.search([("equipment_id", "=", equipment.id)],
                          order="id desc", limit=5)
    return {
        "equipment": equipment.display_name,
        "open_tasks": [{"id": t.id, "name": t.name, "stage": t.stage_id.name}
                       for t in tasks if t.stage_id and not t.is_closed],
        "maintenance_requests": [{"id": r.id, "name": r.name} for r in reqs],
    }


def _report_service_note(env, record, params):
    """L1: dictated repair steps / hours / replaced parts -> service report
    DRAFT posted to the task chatter; user reviews before it stands."""
    task = record  # project.task
    body = _("""<b>服务报告草稿（多模态采集）</b><br/>
步骤：%s<br/>工时：%s 小时<br/>更换备件：%s<br/>备注：%s""") % (
        params.get("steps") or "—",
        params.get("hours") or "—",
        params.get("parts") or "—",
        params.get("note") or "—",
    )
    msg = task.message_post(body=body, subject=_("Service report draft"))
    return {"message_id": msg.id, "task": task.display_name}


def _capture_service_evidence(env, record, params):
    """L1: before/after photos + a description, linked to the task.

    The photos are real ``ir.attachment`` records filed against the task
    and referenced by the chatter message, so the evidence a worker
    captured is actually retrievable later — not just a note saying that
    evidence was taken.
    """
    task = record  # project.task
    attachment_ids = env["mmux.service"]._attach_photos(
        task, params.get("photos"), subject=_("Field evidence"))
    body = _("现场证据：%s") % params["description"]
    msg = task.message_post(body=body, subject=_("Field evidence"),
                            attachment_ids=attachment_ids or None)
    result = {"message_id": msg.id, "task": task.display_name}
    if attachment_ids:
        result["attachment_ids"] = attachment_ids
        result["photo_count"] = len(attachment_ids)
    return result


def _log_part_consumed(env, record, params):
    """L1: record a consumed spare part as a chatter line on the task.
    Actual stock moves stay with the native warehouse flows (no automatic
    inventory posting from the field UX — PRD §4.5 + risk policy)."""
    task = record
    body = _("消耗备件：%s × %s") % (params.get("part") or "—", params.get("qty") or 1)
    msg = task.message_post(body=body, subject=_("Part consumed"))
    return {"message_id": msg.id, "task": task.display_name}


def _task_mark_done_precondition(record, params):
    if record.state == "1_done" or (record.stage_id and record.stage_id.fold):
        raise UserError(_("Task %s is already closed.") % record.display_name)


def _task_mark_done(env, record, params):
    """L2: close the service task through the native state flow. Customer
    acceptance / invoicing stay entirely in native flows."""
    task = record  # project.task
    task.write({"state": "1_done"})
    return {"task": task.display_name, "state": task.state}


# ------------------------------------------------------------------ #
# capabilities
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="equipment_identify",
    name="Identify equipment",
    description="扫描铭牌/序列号识别设备，返回候选列表供用户确认",
    target_model="maintenance.equipment",
    action="read",
    schema=ParamSchema([
        Field("barcode", "str", required=False, help="Nameplate barcode or serial"),
        Field("name", "str", required=False, help="Equipment name fragment"),
    ]),
    risk="L0", require_confirmation=False, scenario="field_service",
    executor=_identify_equipment,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="equipment_query_history",
    name="Query equipment history",
    description="查询设备相关服务任务与维修请求历史（只读）",
    target_model="maintenance.equipment",
    action="read",
    schema=ParamSchema([]),
    risk="L0", require_confirmation=False, scenario="field_service",
    executor=_query_equipment_history,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="service_report_note",
    name="Draft service report",
    description="口述维修步骤/工时/备件，生成服务报告草稿（chatter，可检查修正）",
    target_model="project.task",
    action="message_post",
    schema=ParamSchema([
        Field("steps", "str", required=False),
        Field("hours", "float", required=False, min_val=0),
        Field("parts", "str", required=False),
        Field("note", "str", required=False),
    ]),
    risk="L1", require_confirmation=False, scenario="field_service",
    executor=_report_service_note,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="service_capture_evidence",
    name="Capture service evidence",
    description="拍摄维修前后状态并关联当前服务任务",
    target_model="project.task",
    action="message_post",
    schema=ParamSchema([
        Field("description", "str", required=True),
        Field("photos", "list", required=False,
              help="[{image_b64, name, mimetype}] captured at the site"),
    ]),
    risk="L1", require_confirmation=False, scenario="field_service",
    executor=_capture_service_evidence,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="service_log_part",
    name="Log consumed part",
    description="记录现场消耗的备件（草稿记录，库存过账走原生仓库流程）",
    target_model="project.task",
    action="message_post",
    schema=ParamSchema([
        Field("part", "str", required=True),
        Field("qty", "float", required=False, min_val=0),
    ]),
    risk="L1", require_confirmation=False, scenario="field_service",
    executor=_log_part_consumed,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="service_task_mark_done",
    name="Mark service task done",
    description="按原生状态流转关闭当前服务任务；客户验收/开票留在原生流程",
    target_model="project.task",
    action="state_change",
    schema=ParamSchema([]),
    risk="L2", require_confirmation=True, scenario="field_service",
    precondition=_task_mark_done_precondition,
    executor=_task_mark_done,
))

