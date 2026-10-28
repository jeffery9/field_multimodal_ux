# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Odoo-side tests for the DB capability switches (v19.0.6).

Needs a real Odoo runtime (groups, ACL, TransactionCase). Covers:
authority (manager-only, model-level), the audit row, the sync into the
in-memory registry, and cache staleness after a toggle.
"""
import json

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY


def _any_enabled_cap():
    """A registered capability that is currently enabled."""
    for cap in CAPABILITY_REGISTRY.all():
        if cap.enabled:
            return cap
    raise AssertionError("no enabled capability registered")


@tagged("mmux", "post_install", "-at_install")
class TestCapabilityToggle(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Toggle = self.env["mmux.capability.toggle"]
        self.manager = self.env.ref("base.user_admin")
        # a plain user: member of no mmux group
        self.worker = self.env["res.users"].create({
            "name": "warehouse worker",
            "login": "mmux_toggle_worker",
            "groups_id": [(6, 0, [self.env.ref("mmux_core.group_mmux_user").id])],
        })

    def test_worker_cannot_write(self):
        """A non-manager gets AccessError from the MODEL-level check,
        even before the ACL is consulted (defence in depth)."""
        cap = _any_enabled_cap()
        with self.assertRaises(AccessError):
            self.Toggle.with_user(self.worker).create({
                "capability_id": cap.capability_id,
                "enabled": False,
            })

    def test_manager_toggle_is_audited(self):
        cap = _any_enabled_cap()
        rec = self.Toggle.with_user(self.manager).create({
            "capability_id": cap.capability_id,
            "enabled": False,
        })
        log = self.env["mmux.interaction.log"].sudo().search(
            [("idempotency_key", "=",
              "cap_toggle:%s:off" % cap.capability_id)], limit=1)
        self.assertTrue(log, "the toggle must write an audit row")
        self.assertEqual(log.capability_id_name, cap.capability_id)
        detail = json.loads(log.detail)
        self.assertEqual(detail["op"], "toggle")
        self.assertFalse(detail["enabled"])
        self.assertEqual(detail["risk"], cap.risk)
        # and the write path works
        rec.with_user(self.manager).write({"enabled": True})
        on_row = self.env["mmux.interaction.log"].sudo().search(
            [("idempotency_key", "=",
              "cap_toggle:%s:on" % cap.capability_id)], limit=1)
        self.assertTrue(on_row, "the ON change must be audited too")

    def test_write_without_change_is_not_audited(self):
        """Writing the same value again must not spam the log."""
        cap = _any_enabled_cap()
        rec = self.Toggle.with_user(self.manager).create({
            "capability_id": cap.capability_id,
            "enabled": False,
        })
        before = self.env["mmux.interaction.log"].sudo().search_count([])
        rec.with_user(self.manager).write({"enabled": False})
        after = self.env["mmux.interaction.log"].sudo().search_count([])
        self.assertEqual(before, after)

    def test_unknown_capability_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.Toggle.with_user(self.manager).create({
                "capability_id": "no_such_capability_xyz",
                "enabled": True,
            })

    def test_sync_applies_the_toggle_to_the_registry(self):
        cap = _any_enabled_cap()
        self.assertTrue(cap.enabled)
        self.Toggle.with_user(self.manager).create({
            "capability_id": cap.capability_id,
            "enabled": False,
        })
        service = self.env["mmux.service"]
        # a fresh env/registry view: the sync runs on the hot path
        service._sync_capability_toggles()
        self.assertFalse(CAPABILITY_REGISTRY.get(cap.capability_id).enabled)
        # the whitelist (agent) and the payload (prompts) both drop it
        self.assertNotIn(cap.capability_id,
                         service._agent_allowed_capabilities())
        payload_ids = [c["capability_id"] for c in service._caps_payload()]
        self.assertNotIn(cap.capability_id, payload_ids)
        # restore for other tests: delete the row and re-sync
        rec = self.Toggle.search([("capability_id", "=", cap.capability_id)])
        rec.with_user(self.manager).unlink()
        service._sync_capability_toggles()
        self.assertTrue(CAPABILITY_REGISTRY.get(cap.capability_id).enabled)

    def test_caps_payload_is_not_stale_after_a_toggle(self):
        """The fingerprint cache must be dropped when the signature
        changes — a payload built BEFORE the toggle must not be served
        AFTER it."""
        cap = _any_enabled_cap()
        service = self.env["mmux.service"]
        before = service._caps_payload()
        self.assertIn(cap.capability_id,
                      [c["capability_id"] for c in before])
        self.Toggle.with_user(self.manager).create({
            "capability_id": cap.capability_id,
            "enabled": False,
        })
        after = service._caps_payload()
        self.assertNotIn(cap.capability_id,
                         [c["capability_id"] for c in after])
        # restore
        rec = self.Toggle.search([("capability_id", "=", cap.capability_id)])
        rec.with_user(self.manager).unlink()
        service._sync_capability_toggles()
