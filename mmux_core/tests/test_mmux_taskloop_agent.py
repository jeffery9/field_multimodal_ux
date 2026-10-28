# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Odoo-side tests for the agent task-selection layer.

The pure tests (`tests_pure/test_agent.py`) prove the validator's rules in
isolation. These tests prove the *wiring*: that the service actually asks
the agent, that a rejected/hallucinated answer silently degrades to the
deterministic queue (so fetching a task never fails), that the decision is
audited, and — most importantly — that the agent gains **no authority**:
completion still travels the same L2-confirmed capability path.
"""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMmuxAgentPick(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.llm.mock_error", False)
        self.product = self.env["product.product"].create({
            "name": "MMUX Agent Product", "is_storable": True,
            "barcode": "MMUX-AG1",
        })

    # ---------------------------------------------------------------- #
    # helpers
    # ---------------------------------------------------------------- #
    def _mock(self, answer=None):
        self.Param.set_param(
            "mmux.llm.mock_response", json.dumps(answer) if answer else False)
        self.Param.set_param("mmux.llm.mock_error", False)

    def _mock_error(self, kind="timeout"):
        self.Param.set_param("mmux.llm.mock_response", False)
        self.Param.set_param("mmux.llm.mock_error", kind)

    def _picking(self, qty=2, scheduled=None):
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        vals = {
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id,
        }
        if scheduled:
            vals["scheduled_date"] = scheduled
        picking = self.env["stock.picking"].create(vals)
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": qty, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def _candidate_ids(self, picking_records):
        return [f"wh_picking:{p.id}" for p in picking_records]

    # ---------------------------------------------------------------- #
    # agent actually decides
    # ---------------------------------------------------------------- #
    def test_agent_choice_is_honoured(self):
        p1, p2 = self._picking(), self._picking()
        # the queue would pick p1 (lowest id); the agent picks p2 instead
        self._mock({"selected_task_id": f"wh_picking:{p2.id}",
                    "reason": {"zh_CN": "该客户加急", "en_US": "priority customer"},
                    "confidence": 0.91})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertEqual(res["task"]["res_id"], p2.id)
        self.assertEqual(res["agent"]["source"], "llm")
        self.assertEqual(res["agent"]["task_id"], f"wh_picking:{p2.id}")
        self.assertAlmostEqual(res["agent"]["confidence"], 0.91, places=2)

    def test_agent_reason_is_localized(self):
        p1 = self._picking()
        self._mock({"selected_task_id": f"wh_picking:{p1.id}",
                    "reason": {"zh_CN": "最紧急", "en_US": "most urgent"}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        reason = res["agent"]["reason"]
        self.assertTrue(reason)              # localized to the user's lang
        self.assertIn(reason, ("最紧急", "most urgent"))

    def test_agent_alternatives_are_scoped_to_candidates(self):
        p1, p2 = self._picking(), self._picking()
        self._mock({
            "selected_task_id": f"wh_picking:{p1.id}",
            "reason": {"en_US": "first"},
            "alternatives": [
                {"task_id": f"wh_picking:{p2.id}", "why_not": "later"},
                {"task_id": "wh_picking:999999", "why_not": "hallucinated"},
            ],
        })
        res = self.service.task_next("warehouse", source_id="wh_picking")
        alts = [a["task_id"] for a in res["agent"]["alternatives"]]
        self.assertIn(f"wh_picking:{p2.id}", alts)
        self.assertNotIn("wh_picking:999999", alts)   # out-of-set dropped

    # ---------------------------------------------------------------- #
    # degradation: fetching a task must never fail
    # ---------------------------------------------------------------- #
    def test_hallucinated_choice_degrades_to_queue(self):
        p1, _p2 = self._picking(), self._picking()
        self._mock({"selected_task_id": "wh_picking:999999",
                    "reason": {"en_US": "invented"}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNotNone(res["task"])              # still got a task
        self.assertEqual(res["agent"]["source"], "queue_fallback")
        self.assertIn("task_not_in_candidates",
                      res["agent"]["rejected_reason"])
        # and the queue's deterministic answer is the first pending record
        self.assertEqual(res["task"]["res_id"], min(p1.id, _p2.id))

    def test_llm_error_degrades_to_queue(self):
        p1 = self._picking()
        self._mock_error("timeout")
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNotNone(res["task"])
        self.assertEqual(res["task"]["res_id"], p1.id)
        self.assertEqual(res["agent"]["source"], "queue_fallback")

    def test_no_provider_degrades_to_queue(self):
        p1 = self._picking()
        self.Param.set_param("mmux.llm.provider", False)
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNotNone(res["task"])
        self.assertEqual(res["task"]["res_id"], p1.id)
        self.assertEqual(res["agent"]["source"], "queue_fallback")

    def test_invalid_json_degrades_to_queue(self):
        self._picking()
        self.Param.set_param("mmux.llm.mock_response", "not json at all")
        self.Param.set_param("mmux.llm.mock_error", False)
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNotNone(res["task"])
        self.assertEqual(res["agent"]["source"], "queue_fallback")

    def test_protocol_violation_degrades_to_queue(self):
        p1 = self._picking()
        self._mock({"selected_task_id": f"wh_picking:{p1.id}",
                    "method": "button_validate"})   # forbidden key
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNotNone(res["task"])
        self.assertEqual(res["agent"]["source"], "queue_fallback")
        self.assertIn("protocol_violation", res["agent"]["rejected_reason"])

    def test_empty_pool_reports_no_candidates(self):
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNone(res["task"])
        self.assertEqual(res["agent"]["source"], "no_candidates")
        self.assertEqual(res["loop"]["state"], "finished_all")

    # ---------------------------------------------------------------- #
    # agent gains no authority (FR-014/015/016 untouched)
    # ---------------------------------------------------------------- #
    def test_agent_does_not_weaken_L2_confirmation(self):
        """Whatever the agent "decides", completion is still parked for the
        human's explicit confirmation — the pick layer adds no authority."""
        picking = self._picking()
        self._mock({"selected_task_id": f"wh_picking:{picking.id}",
                    "reason": {"en_US": "urgent"}, "confidence": 0.99})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        ok = res["loop_key"]
        # report completion WITHOUT confirming -> L2 parks, nothing written
        self._mock({"capability_id": "picking_task_complete", "params": {},
                    "normalized_text": "done"})
        parked = self.service.task_report(ok, text="done")
        self.assertEqual(parked["state"], "awaiting_confirmation")
        self.assertEqual(picking.state, "assigned")     # unchanged
        # now confirm -> it completes, through the normal guard
        done = self.service.task_report(ok, text="done", confirmed=True)
        self.assertEqual(done["state"], "completed")

    def test_agent_cannot_name_a_task_it_was_not_given(self):
        """A record the user's domain excludes is not in the candidate set,
        so naming it is rejected even though the id is real."""
        other_user = self.env["res.users"].create({
            "name": "Agent Other", "login": "mmux_agent_other",
        })
        mine = self._picking()
        theirs = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": self.env.ref("stock.stock_location_stock").id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "user_id": other_user.id,
        })
        theirs.action_confirm()
        theirs.action_assign()
        self._mock({"selected_task_id": f"wh_picking:{theirs.id}",
                    "reason": {"en_US": "peek at someone else's work"}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertEqual(res["task"]["res_id"], mine.id)   # my own task
        self.assertEqual(res["agent"]["source"], "queue_fallback")
        self.assertNotIn(str(theirs.id), res["agent"]["task_id"] or "")

    # ---------------------------------------------------------------- #
    # audit
    # ---------------------------------------------------------------- #
    def test_decision_is_audited(self):
        p1 = self._picking()
        self._mock({"selected_task_id": f"wh_picking:{p1.id}",
                    "reason": {"en_US": "audit me"}, "confidence": 0.7})
        self.service.task_next("warehouse", source_id="wh_picking")
        log = self.env["mmux.interaction.log"].sudo().search([
            ("capability_id_name", "=", "agent_pick:warehouse")], limit=1)
        self.assertTrue(log)
        detail = json.loads(log.detail)
        self.assertEqual(detail["source"], "llm")
        self.assertEqual(detail["task_id"], f"wh_picking:{p1.id}")
        self.assertEqual(detail["candidate_count"], 1)

    def test_rejected_decision_is_also_audited(self):
        self._picking()
        self._mock({"selected_task_id": "wh_picking:999999"})
        self.service.task_next("warehouse", source_id="wh_picking")
        log = self.env["mmux.interaction.log"].sudo().search([
            ("capability_id_name", "=", "agent_pick:warehouse")], limit=1)
        self.assertTrue(log)
        detail = json.loads(log.detail)
        self.assertEqual(detail["source"], "queue_fallback")
        self.assertIn("task_not_in_candidates", detail["rejected_reason"])
        self.assertEqual(log.exec_status, "failed")

    # ---------------------------------------------------------------- #
    # cursor
    # ---------------------------------------------------------------- #
    def test_agent_reason_survives_reload(self):
        p1 = self._picking()
        self._mock({"selected_task_id": f"wh_picking:{p1.id}",
                    "reason": {"en_US": "keep me"}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        again = self.service.task_current(res["loop_key"])
        self.assertEqual(again["agent"]["task_id"], f"wh_picking:{p1.id}")
        self.assertTrue(again["agent"]["reason"])

    # ---------------------------------------------------------------- #
    # backward compatibility
    # ---------------------------------------------------------------- #
    def test_existing_response_keys_unchanged(self):
        """The four original keys keep their meaning; `agent` is additive."""
        self._picking()
        self._mock({"selected_task_id": None})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        for key in ("task", "loop", "loop_key", "loop_label", "sources"):
            self.assertIn(key, res)
        self.assertIn("agent", res)
        self.assertEqual(res["loop"]["state"], "task_active")

    def test_res_id_restore_does_not_consult_agent(self):
        p1, p2 = self._picking(), self._picking()
        # the agent would pick p1, but an explicit res_id targets p2
        self._mock({"selected_task_id": f"wh_picking:{p1.id}",
                    "reason": {"en_US": "would have picked p1"}})
        res = self.service.task_next("warehouse", source_id="wh_picking",
                                     res_id=p2.id)
        self.assertEqual(res["task"]["res_id"], p2.id)
        self.assertIsNone(res["agent"]["task_id"])   # no decision was made

    def test_use_agent_false_forces_queue(self):
        p1, p2 = self._picking(), self._picking()
        self._mock({"selected_task_id": f"wh_picking:{p2.id}",
                    "reason": {"en_US": "agent choice"}})
        chosen, _only, _src = self.service._select_task(
            "warehouse", "wh_picking", None, use_agent=False)
        self.assertEqual(chosen.res_id, min(p1.id, p2.id))

    def test_agent_selection_then_normal_completion_flow(self):
        """End to end: the agent picks, the operator completes, the loop
        publishes the next task through the very same guard."""
        p1, p2 = self._picking(), self._picking()
        self._mock({"selected_task_id": f"wh_picking:{p2.id}",
                    "reason": {"en_US": "agent picked p2"}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertEqual(res["task"]["res_id"], p2.id)
        ok = res["loop_key"]
        self._mock({"capability_id": "picking_task_complete", "params": {},
                    "normalized_text": "done"})
        done = self.service.task_report(ok, text="done", confirmed=True)
        self.assertEqual(done["state"], "completed")
        self.assertEqual(done["done_count"], 1)
        # the remaining task is p1 and it is published next
        self.assertIsNotNone(done["task"])
        self.assertEqual(done["task"]["res_id"], p1.id)


@tagged("post_install", "-at_install")
class TestMmuxAgentSerialization(TransactionCase):
    """`_serialize_candidates` is the data-minimisation boundary."""

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]

    def test_candidates_carry_only_decision_fields(self):
        from ..services.mmux_core import Task
        pool = [Task("wh:1", "wh_picking", "warehouse", "Move 3 pallets",
                     "stock.picking", 1, "wh_guided_picking",
                     "picking_task_complete", priority=2,
                     meta={"cost": 999, "supplier": "ACME", "secret": "x"})]
        out = self.service._serialize_candidates(pool, "en_US")
        self.assertEqual(len(out), 1)
        row = out[0]
        allowed = {"task_id", "source_id", "scenario", "title",
                   "priority", "due", "kind"}
        self.assertEqual(set(row), allowed)
        # sensitive meta never reaches the prompt
        self.assertNotIn("cost", row)
        self.assertNotIn("supplier", row)
        self.assertNotIn("secret", row)
        self.assertNotIn("ACME", json.dumps(row))
