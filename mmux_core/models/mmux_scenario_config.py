# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Scenario configuration (PRD §1.2 #3, FR-012).

Scenario adapters ship default records via XML data; admins can adjust
which interaction modes are emphasized and whether confirmations are
strengthened (never weakened below the capability's risk policy).

v3.1 adds the *position* settings. They describe how the browser's
position is COLLECTED and whether it is allowed to INFLUENCE the derived
scenario. The default is the conservative one on both counts:

* ``geo_collect`` off — no position is requested from the browser at all
  until an admin turns it on. Field workers' whereabouts are personal
  data, and "we might use it later" is not a reason to start collecting.
* ``geo_weight`` 0 — even once collected, positions do not decide
  anything. They are recorded for auditing only. This mirrors
  ``WEIGHT_GEO`` in the pure core, and exists so that a future geofence
  feature can be enabled by configuration rather than by a code change.
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class MmuxScenarioConfig(models.Model):
    _name = "mmux.scenario.config"
    _description = "Field scenario configuration"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True,
                       help="Stable key referenced by capabilities, e.g. 'warehouse'")
    active = fields.Boolean(default=True)
    default_input = fields.Selection([
        ("barcode", "Barcode first"), ("voice", "Voice first"),
        ("camera", "Camera first"), ("form", "Structured form"),
    ], default="barcode")
    description = fields.Text()
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    capability_ids_text = fields.Text(
        "Capabilities (computed)",
        compute="_compute_capability_ids_text",
        help="Read-only view of capabilities registered in code for this scenario.")

    # ---- position signals (v3.1) ------------------------------------- #
    geo_collect = fields.Boolean(
        "Collect device position", default=False,
        help="Ask the browser for its position while this scenario is "
             "active. Off by default: the position is personal data and "
             "the feature only earns the request when an admin turns it "
             "on deliberately.")
    geo_weight = fields.Integer(
        "Position weight", default=0,
        help="How much a position is allowed to influence the derived "
             "scenario. 0 (the default) means 'record it, never act on "
             "it'. Raise this only when geofencing has been configured "
             "and its boundaries have been verified.")
    geo_radius_m = fields.Integer(
        "Geofence radius (m)", default=0,
        help="Reserved for site-based scenarios. 0 disables the geofence.")

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Scenario code must be unique"),
    ]

    @api.depends_context("code")
    def _compute_capability_ids_text(self):
        from ..services.mmux_core import CAPABILITY_REGISTRY
        for rec in self:
            caps = [c.capability_id for c in CAPABILITY_REGISTRY.for_scenario(rec.code)
                    if c.enabled]
            rec.capability_ids_text = "\n".join(caps)

    @api.constrains("code")
    def _check_code(self):
        for rec in self:
            if not rec.code.replace("_", "").isalnum() or not rec.code.islower():
                raise ValidationError("Scenario code must be lowercase snake_case.")

    @api.constrains("geo_weight")
    def _check_geo_weight(self):
        """A position may only be allowed to influence the outcome when it
        is actually being collected. Weight without collection is a
        configuration that silently does nothing, and that is worse than
        an error because nobody notices."""
        for rec in self:
            if rec.geo_weight and not rec.geo_collect:
                raise ValidationError(
                    "Position weight requires 'Collect device position' to "
                    "be enabled — otherwise the weight can never have any "
                    "effect.")
