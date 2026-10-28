# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""`mmux.service` -- the Odoo-side facade in front of the pure core.

This file holds the model declaration only.  The behaviour lives in sibling
files that all `_inherit = "mmux.service"`, so at runtime they merge into one
model and every caller keeps using `env["mmux.service"]` exactly as before.

Nothing should be added here: put new behaviour in the cluster that owns it,
so the facade stays a table of contents rather than a dumping ground.

Layout (dependency order, bottom first):

    mmux_service_infra.py        providers / execution guard / idempotency
    mmux_service_skill.py        language + skill level
    mmux_service_capability.py   access control + capability execution
    mmux_service_scenario.py     scenario detection + audit trail
    mmux_service_loop.py         the agent loop
    mmux_service_flow.py         tasks + scripted guides
    mmux_service_plan.py         plan proposal + execution
"""

from odoo import models


class MmuxService(models.AbstractModel):
    _name = "mmux.service"
    _description = "Field Multimodal UX - core service facade"
