# -*- coding: utf-8 -*-
"""Sales visit scenario capabilities (PRD §4.4 / §10.2).

Community-native models only: res.partner, crm.lead, mail.activity,
sale.order. Constraints honored:
 - dictated visit summaries are DRAFTS the user can edit before saving
   (message_post with body, user reviews in chatter);
 - quote creation stays a draft sale.order; price/contract commitments
   and anything customer-facing requires explicit confirmation (L2);
 - no automatic outbound sending of anything (PRD §4.4).
"""
from odoo.exceptions import UserError
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    CAPABILITY_REGISTRY, Capability, ParamSchema, Field,
)


# ------------------------------------------------------------------ #
# executors
# ------------------------------------------------------------------ #
def _query_customer_history(env, record, params):
    """L0: partner's recent orders, open leads and meetings (read-only)."""
    partner = record  # res.partner
    SaleOrder = env["sale.order"]
    Lead = env["crm.lead"]
    orders = SaleOrder.search([("partner_id", "=", partner.id)], order="id desc", limit=5)
    leads = Lead.search([("partner_id", "=", partner.id)], order="id desc", limit=5)
    return {
        "partner": partner.display_name,
        "recent_orders": [{"id": o.id, "name": o.name, "state": o.state,
                           "amount": o.amount_total} for o in orders],
        "open_leads": [{"id": l.id, "name": l.name, "stage": l.stage_id.name}
                       for l in leads],
    }


def _record_visit_note(env, record, params):
    """L1: dictated visit summary -> chatter note (draft for user review;
    the user can edit/delete the message in the native chatter)."""
    target = record  # crm.lead or res.partner
    body = params["summary"]
    msg = target.message_post(
        body=body,
        subject=_("拜访纪要（语音/多模态采集）"),
        message_type="comment",
    )
    return {"message_id": msg.id, "model": target._name, "record_id": target.id}


def _extract_lead_fields(env, record, params):
    """L1: structured requirements onto the lead — only whitelisted fields,
    never arbitrary write. Missing keys are simply skipped."""
    lead = record  # crm.lead
    vals = {}
    if params.get("lead_name"):
        vals["name"] = params["lead_name"]
    if params.get("expected_revenue") is not None:
        vals["expected_revenue"] = params["expected_revenue"]
    if params.get("next_action"):
        vals["next_action"] = params["next_action"]
    if not vals:
        raise UserError(_("no updatable fields extracted"))
    lead.write(vals)
    return {"lead": lead.name, "updated": sorted(vals)}


def _schedule_followup(env, record, params):
    """L1: follow-up activity on the current record via native mail.activity."""
    target = record  # crm.lead / res.partner
    Activity = env["mail.activity"]
    act = Activity.create({
        "res_model_id": env["ir.model"]._get(target._name).id,
        "res_id": target.id,
        "activity_type_id": env["mail.activity.type"].search(
            [("category", "=", "default")], limit=1).id,
        "summary": params.get("summary") or _("Follow-up after visit"),
        "note": params.get("note") or "",
        "date_deadline": params["date_deadline"],
        "user_id": env.user.id,
    })
    return {"activity_id": act.id, "deadline": params["date_deadline"]}


def _create_quote_draft(env, record, params):
    """L2: create a DRAFT sale.order for the partner. Nothing is sent or
    confirmed; native sale workflow stays authoritative."""
    partner = record  # res.partner
    SaleOrder = env["sale.order"]
    order = SaleOrder.create({
        "partner_id": partner.id,
        "client_order_ref": params.get("reference") or _("Multimodal field draft"),
    })
    return {"order_id": order.id, "name": order.name, "state": order.state}


# ------------------------------------------------------------------ #
# capabilities
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="partner_query_history",
    name="Query customer history",
    description="查询客户近期订单、商机与跟进记录（只读）",
    target_model="res.partner",
    action="read",
    schema=ParamSchema([]),
    risk="L0", require_confirmation=False, scenario="sales_visit",
    executor=_query_customer_history,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="visit_record_note",
    name="Record visit note",
    description="将口述拜访纪要以草稿形式记录到当前客户/商机 chatter，可检查修正",
    target_model="crm.lead",
    action="message_post",
    schema=ParamSchema([
        Field("summary", "str", required=True, help="Dictated visit summary"),
    ]),
    risk="L1", require_confirmation=False, scenario="sales_visit",
    executor=_record_visit_note,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="lead_update_fields",
    name="Update lead from visit",
    description="将现场提取的结构化需求写入商机（白名单字段：名称/预期收入/下一步）",
    target_model="crm.lead",
    action="write",
    schema=ParamSchema([
        Field("lead_name", "str", required=False),
        Field("expected_revenue", "float", required=False, min_val=0),
        Field("next_action", "str", required=False),
    ]),
    risk="L1", require_confirmation=False, scenario="sales_visit",
    executor=_extract_lead_fields,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="schedule_followup",
    name="Schedule follow-up activity",
    description="为当前客户/商机创建后续跟进任务（原生 mail.activity）",
    target_model="crm.lead",
    action="create_activity",
    schema=ParamSchema([
        Field("summary", "str", required=False),
        Field("note", "str", required=False),
        Field("date_deadline", "str", required=True, help="YYYY-MM-DD"),
    ]),
    risk="L1", require_confirmation=False, scenario="sales_visit",
    executor=_schedule_followup,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="create_quote_draft",
    name="Create quote draft",
    description="根据已授权信息生成报价草稿（仅草稿，确认/发送走原生销售流程）",
    target_model="res.partner",
    action="create_draft",
    schema=ParamSchema([
        Field("reference", "str", required=False),
    ]),
    risk="L2", require_confirmation=True, scenario="sales_visit",
    executor=_create_quote_draft,
))

