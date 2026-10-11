# -*- coding: utf-8 -*-
"""Read-only window over the in-code capability registry (FR-014).

The registry itself stays Python-defined; this model exists only so
administrators can inspect the whitelist, risk levels and confirmation
policy from the Odoo UI. It creates no tables and writes nothing.
"""
from odoo import fields, models
from ..services.mmux_core import CAPABILITY_REGISTRY


class MmuxCapabilityView(models.TransientModel):
    _name = "mmux.capability.view"
    _description = "Capability registry (read-only)"

    name = fields.Char()
    capability_id_name = fields.Char()
    target_model = fields.Char()
    action = fields.Char()
    risk = fields.Selection([("L0", "L0"), ("L1", "L1"), ("L2", "L2"), ("L3", "L3")])
    require_confirmation = fields.Boolean()
    enabled = fields.Boolean()
    scenario = fields.Char()
    description = fields.Char()

    @fields.model_attribute
    def _read_group(self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True):  # noqa
        raise NotImplementedError

    @classmethod
    def _get_registry_rows(cls):
        return [{
            "name": c.name,
            "capability_id_name": c.capability_id,
            "target_model": c.target_model,
            "action": c.action,
            "risk": c.risk,
            "require_confirmation": c.require_confirmation,
            "enabled": c.enabled,
            "scenario": c.scenario,
            "description": c.description,
        } for c in CAPABILITY_REGISTRY.all()]
