# -*- coding: utf-8 -*-
"""User preference: skill level for field guidance (novice / expert)."""
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    mmux_skill_level = fields.Selection([
        ("novice", "Novice (detailed guidance)"),
        ("expert", "Expert (terse guidance)"),
    ], string="Field assistance level", default="novice",
        help="Novice mode gives more granular steps, longer prompts and "
             "self-check hints. Expert mode merges steps and speaks "
             "terse prompts. Neither mode changes permissions.")
