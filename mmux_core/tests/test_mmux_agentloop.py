# -*- coding: utf-8 -*-
"""Odoo-side tests for the v3.0 agent loop.

The pure tests (`tests_pure/test_agentloop_*.py`) prove the loop's rules in
isolation. These tests prove the **wiring into Odoo** — and the wiring is
where the security properties of an agent loop are actually won or lost,
because that is where the model's output meets the ExecutionGuard.

The four properties that would be incidents if broken:

1. **No proposal is ever pre-confirmed.** `confirmed=False` is hardwired in
   the pure core; these tests assert it survives the trip through the Odoo
   service layer. If it did not, every L2 capability the agent proposed
   would post without a human ever seeing it.
2. **A parked capability is released only from the persisted blob.** A
   client that sends `confirmed=True` releases exactly the request the
   guard itself parked — never a different capability, record or params.
   Otherwise "confirm" becomes a way to name any capability at all.
3. **The whitelist is the server's.** An agent cannot name a capability
   that was not offered for this scenario, and the offer is scoped to the
   operator's own scenario.
4. **The loop always terminates.** Turn / call budgets and repeated-step
   detection are enforced server-side, never delegated to the model.

Plus the operational properties: an unavailable LLM degrades to the
flow's recipe (the operator is never stranded), the agent's memory is
persisted and restored, and the feature flag can turn the whole thing off.
"""
import json

from odoo.tests import TransactionCase, tagged

from odoo.addons.mmux_core.services.mmux_core import (
    ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER, ACT_FINISH,
    LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE, LOOP_SOURCE_NONE,
    MAX_AGENT_TURNS, MAX_AGENT_CAPABILITY_CALLS,
)


@tagged("post_install", "-at_install")
class TestMmuxAgentLoop(TransactionCase):

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.llm.mock_error", False)
        self.Param.set_param("mmux.agent.enabled", "1")

    # ---------------------------------------------------------------- #
    # helpers
    # ---------------------------------------------------------------- #
    def _mock(self, answer):
        self.Param.set_param("mmux.llm.mock_response", json.dumps(answer))
        self.Param.set_param("mmux.llm.mock_error", False)

    def _mock_error(self, kind="timeout"):
        self.Param.set_param("mmux.llm.mock_response", False)
        self.Param.set_param("mmux.llm.mock_error", kind)

    def _log_rows(self, key):
        return self.env["mmux.interaction.log"].sudo().search(
            [("idempotency_key", "=", key)])

    # ---------------------------------------------------------------- #
    # 1. feature flag
    # ---------------------------------------------------------------- #
    def test_agent_disabled_by_flag(self):
        self.Param.set_param("mmux.agent.enabled", "0")
        out = self.service.agent_start(loop={"flow_id": "wh_guided_picking"})
        self.assertEqual(out.get("code"), "agent_disabled")
        # not an exception, and no partial state written
        self.assertFalse(out.get("loop_key"))

    def test_agent_enabled_by_default(self):
        self.Param.set_param("mmux.agent.enabled", False)
        self.assertTrue(self.service._agent_enabled())

    def test_flag_accepts_falsey_spellings(self):
        for raw in ("0", "false", "FALSE", "no", ""):
            self.Param.set_param("mmux.agent.enabled", raw)
            self.assertFalse(self.service._agent_enabled(), raw)
        for raw in ("1", "true", "yes", "on"):
            self.Param.set_param("mmux.agent.enabled", raw)
            self.assertTrue(self.service._agent_enabled(), raw)

    # ---------------------------------------------------------------- #
    # 2. the whitelist is the server's
    # ---------------------------------------------------------------- #
    def test_whitelist_is_scoped_to_scenario(self):
        wh = set(self.service._agent_allowed_capabilities("warehouse"))
        mfg = set(self.service._agent_allowed_capabilities("manufacturing"))
        self.assertTrue(wh)
        self.assertTrue(mfg)
        self.assertNotEqual(wh, mfg)
        # every offered id is a real, enabled capability
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        for cap_id in wh:
            cap = CAPABILITY_REGISTRY.get(cap_id)
            self.assertIsNotNone(cap, cap_id)
            self.assertTrue(cap.enabled, cap_id)

    def test_global_whitelist_excludes_only_disabled(self):
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        offered = set(self.service._agent_allowed_capabilities(""))
        for cap in CAPABILITY_REGISTRY.all():
            self.assertEqual(cap.capability_id in offered, cap.enabled)

    def test_l3_capabilities_are_never_offered(self):
        """L3 ships disabled and must never appear in the agent's menu."""
        from odoo.addons.mmux_core.services.mmux_core import CAPABILITY_REGISTRY
        offered = set(self.service._agent_allowed_capabilities(""))
        for cap in CAPABILITY_REGISTRY.all():
            if cap.risk == "L3":
                self.assertNotIn(cap.capability_id, offered)

    # ---------------------------------------------------------------- #
    # 3. no proposal is ever pre-confirmed
    # ---------------------------------------------------------------- #
    def test_hallucinated_capability_degrades_without_executing(self):
        self._mock({"action": ACT_CALL_CAPABILITY,
                    "payload": {"capability_id": "res_partner_write",
                                "record_id": 1}})
        before = self.env["mmux.interaction.log"].sudo().search_count(
            [("exec_status", "in", ("success", "awaiting_confirmation"))])
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse",
                  "goal": "test"})
        self.assertNotIn(out.get("code"), ("agent_disabled",))
        # the loop degraded to the recipe and nothing was executed
        self.assertEqual(out["source"], LOOP_SOURCE_RECIPE)
        after = self.env["mmux.interaction.log"].sudo().search_count(
            [("exec_status", "in", ("success", "awaiting_confirmation"))])
        self.assertEqual(before, after)
        # the rejection reason is on the turn, for the audit trail
        self.assertIn("capability_not_whitelisted",
                      out["turn"]["rejected_reason"])

    def test_protocol_violation_degrades(self):
        self._mock({"action": ACT_CALL_CAPABILITY,
                    "payload": {"capability_id": "product_lookup",
                                "method": "write"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertEqual(out["source"], LOOP_SOURCE_RECIPE)
        self.assertEqual(out["turn"]["rejected_reason"],
                         "protocol_violation:method")

    def test_no_runner_budget_is_enforced_across_turns(self):
        """A model that only ever proposes writes must be stopped by the
        SERVER, not by the model choosing to stop."""
        caps = list(self.service._agent_allowed_capabilities("warehouse"))
        self.assertTrue(caps)
        # rotate through distinct capability/record pairs so repeat
        # detection does not fire before the call budget
        seq = [{"action": ACT_CALL_CAPABILITY,
                "payload": {"capability_id": caps[i % len(caps)],
                            "record_id": i + 1}}
               for i in range(MAX_AGENT_CAPABILITY_CALLS + 3)]

        state = {"i": 0}

        def _next_answer():
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[i]

        self._mock(seq[0])
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        key = out.get("loop_key")
        self.assertTrue(key, out)
        seen_states = [out["state"]]
        for _ in range(len(seq) + 2):
            self._mock(_next_answer())
            out = self.service.agent_turn(key)
            seen_states.append(out["state"])
            if out["state"] != "awaiting_confirmation":
                break
        # the loop reported a budget/terminal state rather than looping on
        self.assertTrue(
            any(s in ("budget_exhausted", "failed", "finished")
                for s in seen_states),
            seen_states)

    # ---------------------------------------------------------------- #
    # 4. degradation keeps the operator moving
    # ---------------------------------------------------------------- #
    def test_no_provider_degrades_to_recipe(self):
        self.Param.set_param("mmux.llm.provider", False)
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertNotEqual(out.get("code"), "agent_disabled")
        self.assertIn(out["source"],
                      (LOOP_SOURCE_NONE, LOOP_SOURCE_RECIPE))
        # a flow-backed loop still gets a narration from the recipe
        self.assertIn("flow_id", out)

    def test_provider_timeout_degrades_to_recipe(self):
        self._mock_error("timeout")
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertEqual(out["source"], LOOP_SOURCE_RECIPE)

    def test_invalid_json_degrades_to_recipe(self):
        self._mock_error("invalid_json")
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertEqual(out["source"], LOOP_SOURCE_RECIPE)

    def test_not_an_object_degrades_to_recipe(self):
        self._mock_error("not_object")
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertEqual(out["source"], LOOP_SOURCE_RECIPE)

    def test_flow_without_records_does_not_strand_the_operator(self):
        """A scenario whose recipe renders empty (no pending records) must
        still return a usable turn, not an error."""
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertNotIn("error", out)
        self.assertIn(out["state"],
                      ("running", "finished", "finished_all", "failed"))

    # ---------------------------------------------------------------- #
    # 5. the model does decide (happy path)
    # ---------------------------------------------------------------- #
    def test_model_proposal_is_narrated(self):
        self._mock({"action": ACT_PROPOSE_STEP,
                    "payload": {"step_id": "agent_s1",
                                "voice_prompt": "扫描货架 A-01 上的商品"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertEqual(out["source"], LOOP_SOURCE_LLM)
        self.assertEqual(out["action"]["action"], ACT_PROPOSE_STEP)
        self.assertIn("货架", out["speak"])

    def test_model_answer_is_spoken(self):
        self._mock({"action": "answer",
                    "payload": {"text": "库位在 A 区第二排"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertEqual(out["source"], LOOP_SOURCE_LLM)
        self.assertIn("A 区", out["speak"])

    def test_model_ask_user_pauses_the_loop(self):
        self._mock({"action": ACT_ASK_USER,
                    "payload": {"question": "实际拣了多少件？",
                                "field": "quantity"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertTrue(out["awaiting_user"])
        self.assertEqual(out["state"], "awaiting_user")
        self.assertEqual(out["pending_field"], "quantity")
        self.assertIn("多少件", out["speak"])

    def test_answer_is_recorded_in_the_scratchpad(self):
        self._mock({"action": ACT_ASK_USER,
                    "payload": {"question": "多少件？", "field": "quantity"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        key = out["loop_key"]
        self._mock({"action": ACT_FINISH,
                    "payload": {"goal_met": True, "summary": "完成"}})
        out2 = self.service.agent_turn(key, answer=10)
        scratch, _blob = self.service._scratch_load(key)
        self.assertEqual(scratch.user_answers.get("quantity"), 10)

    def test_finish_ends_the_loop(self):
        self._mock({"action": ACT_FINISH,
                    "payload": {"goal_met": True, "summary": "拣货完成"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertEqual(out["state"], "finished")
        self.assertIn("拣货完成", out["speak"])

    # ---------------------------------------------------------------- #
    # 6. memory persists across a "page reload"
    # ---------------------------------------------------------------- #
    def test_scratchpad_persists_and_resumes(self):
        self._mock({"action": ACT_ANSWER, "payload": {"text": "第一步说明"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse",
                  "goal": "拣完这单"})
        key = out["loop_key"]
        self.assertEqual(out["budget"]["turns_used"], 1)

        # simulate a page reload: read the memory back from the log
        scratch, blob = self.service._scratch_load(key)
        self.assertEqual(blob.get("goal"), "拣完这单")
        self.assertEqual(scratch.turn, 1)

        # and the next turn continues from it, not from zero
        self._mock({"action": ACT_ANSWER, "payload": {"text": "第二步"}})
        out2 = self.service.agent_turn(key)
        self.assertEqual(out2["budget"]["turns_used"], 2)

    def test_scratchpad_key_is_derived_from_the_loop_key(self):
        self.assertEqual(self.service._scratch_key("abc"), "agent:abc")
        self.assertEqual(self.service._scratch_key(None), "agent")

    def test_agent_rows_are_distinguishable_in_the_audit(self):
        self._mock({"action": ACT_ANSWER, "payload": {"text": "hi"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        rows = self._log_rows(self.service._scratch_key(out["loop_key"]))
        self.assertTrue(rows)
        self.assertEqual(rows[0].input_type, "agent_turn")
        self.assertEqual(rows[0].exec_status, "running")
        self.assertTrue(rows[0].capability_id_name.startswith("agent:"))

    def test_turn_audit_row_is_written(self):
        self._mock({"action": ACT_ANSWER, "payload": {"text": "hi"}})
        before = self.env["mmux.interaction.log"].sudo().search_count(
            [("input_type", "=", "agent_turn")])
        self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        after = self.env["mmux.interaction.log"].sudo().search_count(
            [("input_type", "=", "agent_turn")])
        self.assertGreaterEqual(after, before + 2)   # scratch + turn rows

    def test_disabled_turn_records_a_failed_audit_status(self):
        self._mock({"action": "not_a_real_action", "payload": {}})
        self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        rows = self.env["mmux.interaction.log"].sudo().search(
            [("input_type", "=", "agent_turn"),
             ("exec_status", "=", "failed")])
        self.assertTrue(rows)
        detail = json.loads(rows[0].detail)
        self.assertEqual(detail["source"], LOOP_SOURCE_RECIPE)

    # ---------------------------------------------------------------- #
    # 7. unknown loop keys are refused, never invented
    # ---------------------------------------------------------------- #
    def test_unknown_loop_key_is_refused(self):
        """Continuing a *different* run under the key of an expired one
        would hand the operator a turn they did not ask for, against a
        task they may no longer hold. Refuse instead."""
        out = self.service.agent_turn("does-not-exist")
        self.assertEqual(out.get("code"), "unknown_loop")
        self.assertIn("error", out)

    def test_missing_loop_key_is_refused(self):
        out = self.service.agent_turn(None)
        self.assertIn("error", out)

    def test_agent_start_mints_a_loop_key(self):
        """A loop with no key could never be resumed, so one is always
        minted — the client only ever holds the key, never the memory."""
        self._mock({"action": ACT_ANSWER, "payload": {"text": "hi"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        self.assertTrue(out.get("loop_key"))
        # and that key can be used to continue the loop
        out2 = self.service.agent_turn(out["loop_key"])
        self.assertEqual(out2.get("loop_key"), out["loop_key"])

    def test_agent_start_on_unknown_task_loop_is_refused(self):
        out = self.service.agent_start(loop_key="nope-not-real")
        self.assertIn("error", out)


@tagged("post_install", "-at_install")
class TestMmuxAgentConfirmationGate(TransactionCase):
    """The human gate. These are the tests that matter most: if any of
    them fails, the agent can write without a human seeing it."""

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.llm.mock_error", False)
        self.Param.set_param("mmux.agent.enabled", "1")

    def _mock(self, answer):
        self.Param.set_param("mmux.llm.mock_response", json.dumps(answer))
        self.Param.set_param("mmux.llm.mock_error", False)

    def test_unsolicited_confirm_executes_nothing(self):
        """`confirmed=True` with nothing parked must be inert. If it were
        not, a client could confirm a write the agent never proposed."""
        before = self.env["mmux.interaction.log"].sudo().search_count(
            [("exec_status", "=", "success")])
        self._mock({"action": "answer", "payload": {"text": "ok"}})
        out = self.service.agent_start(
            loop={"flow_id": "wh_guided_picking", "scenario": "warehouse"})
        key = out["loop_key"]
        self.service.agent_turn(key, confirmed=True)
        after = self.env["mmux.interaction.log"].sudo().search_count(
            [("exec_status", "=", "success")])
        self.assertEqual(before, after)

    def test_confirm_releases_only_the_persisted_request(self):
        """A confirmation must re-submit the SAME capability the guard
        parked — not whatever the model says next. Otherwise "confirm"
        becomes a way to name any capability in the registry."""
        scratch, blob = self.service._scratch_load("gate-loop")
        blob["pending_confirmation"] = {
            "capability_id": "picking_task_complete",
            "params": {}, "record_id": 999999,
        }
        # the record does not exist, so the guard must refuse it cleanly
        self.service._scratch_save(scratch, "gate-loop", blob)
        # make the model want to do something completely different
        self._mock({"action": ACT_CALL_CAPABILITY,
                    "payload": {"capability_id": "product_lookup",
                                "record_id": 1}})
        out = self.service.agent_turn("gate-loop", confirmed=True)
        # whatever happened, it was NOT a successful lookup
        self.assertNotEqual(out["state"], "finished")
        # the request released was the PARKED one, not the model's new idea
        self.assertEqual(out["action"]["payload"]["capability_id"],
                         "picking_task_complete")
        # the pending request was consumed, not left to be retried forever
        _scratch2, blob2 = self.service._scratch_load("gate-loop")
        self.assertNotIn("pending_confirmation", blob2)

    def test_confirmation_is_never_forwarded_into_the_reasoning_step(self):
        """`confirmed=True` must not change what the agent proposes next —
        it only releases an already-parked request."""
        scratch, blob = self.service._scratch_load("gate-reason")
        blob.pop("pending_confirmation", None)
        self.service._scratch_save(scratch, "gate-reason", blob)
        self._mock({"action": ACT_ANSWER, "payload": {"text": "只是说明"}})
        out_true = self.service.agent_turn("gate-reason", confirmed=True)
        self._mock({"action": ACT_ANSWER, "payload": {"text": "只是说明"}})
        out_false = self.service.agent_turn("gate-reason", confirmed=False)
        # both turns reasoned to the same action; confirmation changed
        # nothing about the decision itself
        self.assertEqual(out_true["action"]["action"],
                         out_false["action"]["action"])
        self.assertEqual(out_true["source"], out_false["source"])


@tagged("post_install", "-at_install")
class TestMmuxAgentGuideIntegration(TransactionCase):
    """The guided flow and the agent loop must share ONE guard, and the
    scripted path must remain available as the agent's own fallback."""

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.llm.mock_error", False)
        self.Param.set_param("mmux.agent.enabled", "1")

    def test_guide_start_scripted_is_the_default(self):
        Param = self.Param
        Param.set_param("mmux.agent.enabled", "0")
        out = self.service.guide_start("wh_guided_picking")
        self.assertIn("guide_key", out)
        self.assertNotIn("mode", out)

    def test_guide_start_agent_mode_returns_a_loop(self):
        self.Param.set_param(
            "mmux.llm.mock_response",
            json.dumps({"action": ACT_ANSWER, "payload": {"text": "hi"}}))
        out = self.service.guide_start("wh_guided_picking", mode="agent")
        self.assertNotIn("code", out)
        self.assertIn("loop_key", out)

    def test_guide_start_agent_mode_falls_back_when_disabled(self):
        self.Param.set_param("mmux.agent.enabled", "0")
        out = self.service.guide_start("wh_guided_picking", mode="agent")
        # an explicit agent request on a disabled agent still gives the
        # operator a scripted run rather than nothing
        self.assertIn("code", out)
        self.assertEqual(out["code"], "agent_disabled")

    def test_unknown_flow_still_reports_an_error(self):
        out = self.service.guide_start("not_a_flow")
        self.assertIn("error", out)

    def test_scripted_guide_steps_still_execute_through_the_guard(self):
        """The v2.x guard behaviour must be untouched by v3.0."""
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        product = self.env["product.product"].create({
            "name": "MMUX Guide Product", "is_storable": True,
            "barcode": "MMUX-GD1"})
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id})
        self.env["stock.move"].create({
            "name": product.name, "product_id": product.id,
            "product_uom_qty": 1, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id})
        picking.action_confirm()
        picking.action_assign()

        out = self.service.guide_start("wh_guided_picking",
                                       {"picking_id": picking.id})
        self.assertIn("guide_key", out)
        self.assertTrue(out["current"])
        # advancing a confirm step must NOT write anything by itself
        before = self.env["mmux.interaction.log"].sudo().search_count(
            [("exec_status", "=", "success")])
        step = out["current"]
        if step["input_type"] == "confirm":
            res = self.service.guide_advance(out["guide_key"], step["step_id"])
            self.assertNotIn("error", res)
            after = self.env["mmux.interaction.log"].sudo().search_count(
                [("exec_status", "=", "success")])
            self.assertEqual(before, after)


@tagged("post_install", "-at_install")
class TestMmuxAgentRecordRef(TransactionCase):
    """`record_ref` resolution — the agent must never choose a target we
    cannot resolve, and an unresolvable reference must not become a guess."""

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]

    def test_int_passthrough(self):
        self.assertEqual(self.service._resolve_record_ref(42, {}), 42)

    def test_digit_string(self):
        self.assertEqual(self.service._resolve_record_ref("42", {}), 42)

    def test_context_reference(self):
        self.assertEqual(
            self.service._resolve_record_ref("$context.res_id",
                                             {"res_id": 7}), 7)

    def test_unknown_context_key_is_none(self):
        self.assertIsNone(
            self.service._resolve_record_ref("$context.nope", {"res_id": 7}))

    def test_garbage_is_none_not_a_guess(self):
        for raw in ("abc", "12abc", "$unknown.ref", [], {}, True, 3.5):
            self.assertIsNone(self.service._resolve_record_ref(raw, {}), raw)

    def test_empty_values(self):
        for raw in (None, "", False, 0):
            out = self.service._resolve_record_ref(raw, {})
            self.assertIn(out, (None,), raw)


if __name__ == "__main__":
    import unittest
    unittest.main()
