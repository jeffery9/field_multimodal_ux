# -*- coding: utf-8 -*-
"""Warehouse scenario capabilities (PRD §4.1 / §10.2).

Every capability maps to *existing* Odoo business methods:
 - quantity reporting writes stock.move.line.quantity (Odoo 17+ field)
   and is validated by the picking's native state;
 - picking validation calls stock.picking.button_validate;
 - inventory adjustment goes through the native stock.quant inventory flow;
 - evidence capture uses ir.attachment + message_post (chatter).
No new business models are created (PRD §7.1/§7.2).
"""
from odoo.exceptions import UserError
from odoo.tools.translate import _

from odoo.addons.mmux_core.services.mmux_core import (
    CAPABILITY_REGISTRY, Capability,
    ParamSchema, Field,
)


# ------------------------------------------------------------------ #
# executors — always call existing business methods
# ------------------------------------------------------------------ #
def _query_product_stock(env, record, params):
    """L0: query available qty for a product (by barcode or id)."""
    Product = env["product.product"].with_context(
        company_id=env.company.id, warehouse=env["stock.warehouse"].search([], limit=1).id or None)
    if params.get("product_id"):
        product = Product.browse(int(params["product_id"])).exists()
    elif params.get("barcode"):
        product = Product.search([("barcode", "=", params["barcode"])], limit=1) or \
                  Product.search([("default_code", "=", params["barcode"])], limit=1)
    else:
        raise UserError(_("need product_id or barcode"))
    if not product:
        return {"found": False, "message": _("no product matched")}
    Quant = env["stock.quant"]
    quants = Quant.search([("product_id", "=", product.id),
                           ("location_id.usage", "in", ["internal", "transit"])])
    return {
        "found": True,
        "product_id": product.id,
        "product_name": product.display_name,
        "available": sum(q.quantity - q.reserved_quantity for q in quants),
        "on_hand": sum(q.quantity for q in quants),
    }


def _report_picking_qty(env, record, params):
    """L1->L2: write the done quantity on a stock.move.line.
    Native state constraint enforced by precondition (below)."""
    qty = float(params["quantity"])
    record.quantity = qty
    return {"move_line_id": record.id, "quantity": record.quantity}


def _picking_precondition(record, params):
    """A picking that is not in a quantity-editable state must be rejected
    by the native flow — refuse early with a clear message (§10.3)."""
    picking = record.picking_id
    if picking and picking.state not in ("assigned", "confirmed", "draft"):
        raise UserError(
            _("Picking %s is in state '%s' and cannot receive quantities.")
            % (picking.name, picking.state))


def _capture_evidence(env, record, params):
    """L1: attach a note and/or photos to a business record via chatter."""
    record = record or env["stock.picking"]
    subtype_note = params.get("note") or _("Field evidence")
    attachment_ids = env["mmux.service"]._attach_photos(
        record, params.get("photos"), subject=_("Field evidence"))
    msg = record.message_post(
        body=subtype_note,
        subject=_("Field evidence (multimodal capture)"),
        attachment_ids=attachment_ids or None)
    result = {"record": record.display_name, "note": subtype_note,
              "message_id": msg.id}
    if attachment_ids:
        result["attachment_ids"] = attachment_ids
        result["photo_count"] = len(attachment_ids)
    return result


def _validate_picking(env, record, params):
    """L2/L3: delegate to the native button_validate. Everything the native
    method enforces (backorders, lot requirements, etc.) stays authoritative."""
    picking = record  # stock.picking
    picking.button_validate()
    return {"picking": picking.name, "state": picking.state}


def _adjust_inventory(env, record, params):
    """L2: counted quantity on a stock.quant -> native _apply_inventory."""
    quant = record  # stock.quant
    quant.inventory_quantity = float(params["counted_qty"])
    quant.action_apply_inventory()
    return {"quant": quant.id, "quantity": quant.quantity}


# ------------------------------------------------------------------ #
# capability registrations (the whitelist — FR-014)
# ------------------------------------------------------------------ #
CAPABILITY_REGISTRY.register(Capability(
    capability_id="stock_query_product",
    name="Query product availability",
    description="按条码/内部编号查询产品可用库存（只读查询）",
    target_model="product.product",
    action="read",
    schema=ParamSchema([
        Field("barcode", "str", required=False, help="Product barcode or default_code"),
        Field("product_id", "int", required=False, help="Known product id"),
    ]),
    risk="L0", require_confirmation=False, scenario="warehouse",
    executor=_query_product_stock,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="picking_report_qty",
    name="Report picked/received quantity",
    description="口述或录入当前 stock.move.line 的实际数量（写入受拣货单状态约束）",
    target_model="stock.move.line",
    action="write",
    schema=ParamSchema([
        Field("quantity", "float", required=True, min_val=0),
    ]),
    risk="L2", require_confirmation=False, scenario="warehouse",
    precondition=_picking_precondition,
    executor=_report_picking_qty,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="picking_capture_evidence",
    name="Attach field evidence",
    description="将现场备注/照片证据通过 chatter 关联到当前单据",
    target_model="stock.picking",
    action="message_post",
    schema=ParamSchema([
        Field("note", "str", required=True, help="Evidence description"),
        Field("photos", "list", required=False,
              help="[{image_b64, name, mimetype}] captured at the site"),
    ]),
    risk="L1", require_confirmation=False, scenario="warehouse",
    executor=_capture_evidence,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="picking_validate",
    name="Validate picking",
    description="调用原生 button_validate 完成库存过账；展示影响并强制确认",
    target_model="stock.picking",
    action="button_validate",
    schema=ParamSchema([]),
    risk="L2", require_confirmation=True, scenario="warehouse",
    executor=_validate_picking,
))

CAPABILITY_REGISTRY.register(Capability(
    capability_id="stock_adjust_count",
    name="Apply counted inventory",
    description="盘点数量写入 stock.quant 并走原生 inventory 流程过账",
    target_model="stock.quant",
    action="action_apply_inventory",
    schema=ParamSchema([
        Field("counted_qty", "float", required=True),
    ]),
    risk="L2", require_confirmation=True, scenario="warehouse",
    executor=_adjust_inventory,
))

