# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Interaction audit log (PRD §9.4 observability).

Deliberately stores NO raw audio/image bytes — only references. Media
evidence goes to ir.attachment, linked by the scenario capabilities.
"""
from odoo import api, fields, models


class MmuxInteractionLog(models.Model):
    _name = "mmux.interaction.log"
    _description = "Multimodal interaction audit log"
    _order = "id desc"

    user_id = fields.Many2one("res.users", string="User", required=True, index=True)
    input_type = fields.Selection([
        ("voice", "Voice"), ("image", "Image"), ("barcode", "Barcode"),
        ("nl", "Natural language"), ("combo", "Combined"),
        # v3.0: one row per agent reasoning step (see agent_loop).
        ("agent_turn", "Agent turn"),
    ], string="Input type", default="nl")
    capability_id_name = fields.Char("Capability")
    scenario = fields.Char("Scenario")
    exec_status = fields.Selection([
        ("running", "Running"),
        ("recognized", "Recognized"),
        ("validated", "Validated"),
        ("awaiting_confirmation", "Awaiting confirmation"),
        ("input_required", "Input required"),
        ("submitted", "Submitted"),
        ("success", "Success"),
        ("failed", "Failed"),
        ("unknown", "Unknown (verify)"),
    ], string="Execution status", index=True)
    idempotency_key = fields.Char("Idempotency key", index=True)
    detail = fields.Text("Detail")
    duration_ms = fields.Integer("Duration (ms)")

    _sql_constraints = [
        # one *processed* row per idempotency key; attempts may reuse a key
        # only while status is failed (retry-safe, PRD §6.4).
    ]
