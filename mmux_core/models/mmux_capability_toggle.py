# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""DB-backed capability enable/disable switches (v19.0.6).

Why this model exists
---------------------
Before v19.0.6, `Capability.enabled` was fixed at code registration time
(an `L3` capability shipped permanently disabled, and the only way to
change any capability's state was to edit code and redeploy). That made
"enable the high-risk capability for this site" a developer task.

This model is the ONE write surface that changes that. Each row is a
per-capability override row: ``(capability_id, enabled)``. The service
layer (`mmux.service._sync_capability_toggles`) applies these rows to the
in-memory registry on the hot read paths, so the guard, the plan
orchestrator and the agent whitelist all see the same state without any
of them touching the database.

What deliberately does NOT live here
------------------------------------
* No risk, groups, schema or executor fields: those stay code-defined.
  This table can only switch a capability on or off — never widen it.
* The read-only window `mmux.capability.view` (FR-014) is untouched: it
  shows the registry as it is, and after a sync that includes these
  overrides.

Write path security (three layers, each independent)
----------------------------------------------------
1. ACL: only `mmux_core.group_mmux_manager` gets any access line at all
   (see ir.model.access.csv) — regular workers cannot even read the
   table, so its contents cannot leak what capabilities exist.
2. Model-level check in create/write: `user_has_groups` is verified in
   the method itself, so a mis-configured ACL cannot silently open the
   surface. L3 capabilities need nothing extra on top of manager — the
   manager group is by definition the L3 authority (see
   mmux_security.xml) — but the audit row tags `risk` explicitly.
3. Every change is audited to `mmux.interaction.log`: who, which
   capability, on/off, and the capability's risk level.

Multi-worker consistency: each worker process holds its own copy of the
registry. A toggle takes effect on each worker at its next hot-path
read (`_sync_capability_toggles`). That window is availability-only:
disabling a capability late cannot bypass safety, because the L2
confirmation gate is independent of `enabled`; and enabling late only
delays availability. Never a security degradation.
"""
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.mmux_core import CAPABILITY_REGISTRY


class MmuxCapabilityToggle(models.Model):
    _name = "mmux.capability.toggle"
    _description = "Capability on/off switch (DB override)"
    _order = "capability_id"

    capability_id = fields.Char(
        "Capability id", required=True, index=True,
        help="Must be an id registered in code (the whitelist itself is "
             "code-defined; this row can only switch it on or off).")
    enabled = fields.Boolean(
        "Enabled",
        help="Applied to the in-memory registry on the next hot-path "
             "read. The pure-core guard and the agent whitelist then see "
             "the change without any code deployment.")

    # registry-derived, read-only: context for the admin, never a second
    # source of truth (compute reads the registry, it does not feed it).
    name = fields.Char("Capability", compute="_compute_registry_info")
    risk = fields.Char("Risk", compute="_compute_registry_info")
    require_confirmation = fields.Boolean(
        "Requires confirmation", compute="_compute_registry_info")
    scenario = fields.Char("Scenario", compute="_compute_registry_info")

    _sql_constraints = [
        ("capability_id_uniq", "unique (capability_id)",
         "One switch row per capability."),
    ]

    # ------------------------------------------------------------------ #
    # compute: registry context (display only)
    # ------------------------------------------------------------------ #
    def _compute_registry_info(self):
        for rec in self:
            cap = CAPABILITY_REGISTRY.get(rec.capability_id)
            rec.name = cap.name if cap else ""
            rec.risk = cap.risk if cap else ""
            rec.require_confirmation = (cap.require_confirmation
                                        if cap else False)
            rec.scenario = cap.scenario if cap else ""

    # ------------------------------------------------------------------ #
    # constraints
    # ------------------------------------------------------------------ #
    @api.constrains("capability_id")
    def _check_capability_registered(self):
        """The switch may only name a code-registered capability.

        The whitelist is closed by code; accepting an unregistered id
        would store a row that can never do anything but confuse the
        next administrator.
        """
        for rec in self:
            if not CAPABILITY_REGISTRY.get(rec.capability_id):
                raise ValidationError(
                    "Unknown capability id %r. The whitelist is "
                    "code-defined; this switch can only turn a "
                    "registered capability on or off."
                    % rec.capability_id)

    # ------------------------------------------------------------------ #
    # write path: manager-only, audited (active record style: the ORM
    # verbs themselves are the API — no separate button methods)
    # ------------------------------------------------------------------ #
    def _audit_toggle(self, capability_id, enabled):
        """One audit row per toggle change (who / what / on-off / risk)."""
        cap = CAPABILITY_REGISTRY.get(capability_id)
        # sudo: mmux.interaction.log writes are elevated by design
        # (check_standards rule 6 whitelist) — the audit must record the
        # change even though the log model is write-restricted for users.
        self.env["mmux.interaction.log"].sudo().create({
            "user_id": self.env.user.id,
            "input_type": "nl",
            "capability_id_name": capability_id,
            "exec_status": "validated",
            "idempotency_key": "cap_toggle:%s:%s" % (
                capability_id, "on" if enabled else "off"),
            "detail": json.dumps({
                "op": "toggle",
                "enabled": bool(enabled),
                "risk": cap.risk if cap else None,
            }),
        })

    def _guard_manager(self):
        """Model-level authority check, independent of the ACL.

        The ACL already restricts this model to the manager group; this
        second check exists so a misconfigured or future-added ACL line
        cannot silently open the write surface. `user_has_groups` is the
        same primitive Odoo's own ir-rule layer uses.
        """
        if not self.user_has_groups("mmux_core.group_mmux_manager"):
            raise AccessError(
                "Only multimodal administrators "
                "(group_mmux_manager) may change capability switches.")

    @api.model_create_multi
    def create(self, vals_list):
        self._guard_manager()
        records = super().create(vals_list)
        for rec in records:
            rec._audit_toggle(rec.capability_id, rec.enabled)
        return records

    def write(self, vals):
        self._guard_manager()
        # audit only when the switch actually moves
        changes = [(rec, rec.enabled) for rec in self
                   if "enabled" in vals and rec.enabled != vals["enabled"]]
        result = super().write(vals)
        for rec, _old in changes:
            rec._audit_toggle(rec.capability_id, rec.enabled)
        return result
