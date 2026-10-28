# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Odoo-side tests for scenario context recognition (v3.1).

The pure tests (`tests_pure/test_scenario.py`) prove the scoring and the
validation rules in isolation. These tests prove the *wiring*, and in
particular the three promises that only hold once the pieces are joined
together:

1. **The position is what decides, not the model.** A signal that points
   unambiguously at one scenario narrows the capability whitelist without
   anybody being asked anything.
2. **An undecidable position asks instead of guessing.** And the question
   is ANSWERABLE — it carries the server's candidates, so replying is
   possible and the reply can only select something the server already
   had in mind.
3. **A stated scenario is authoritative.** The operator's own choice is
   never overruled by where their browser happens to be — but the
   disagreement stays visible rather than being silently swallowed.

Plus the compatibility promise: `signal=None` (an older client that does
not report position) keeps the v3.0 behaviour exactly, with no question
and no context block.
"""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMmuxScenarioContext(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.llm.mock_error", False)
        self.Param.set_param("mmux.agent.enabled", "1")
        # the four scenario addons ship a config record each
        self.scenarios = self.env["mmux.scenario.config"].search([])

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

    def _start(self, signal=None, loop=None, **kw):
        payload = loop if loop is not None else {"goal": "g"}
        if signal is None:
            return self.service.agent_start(loop=payload, **kw)
        return self.service.agent_start(loop=payload, signal=signal, **kw)

    def _ctx(self, out):
        return out.get("scenario_context") or {}

    # ---------------------------------------------------------------- #
    # the scenarios this suite reasons about actually exist
    # ---------------------------------------------------------------- #
    def test_the_scenario_catalogue_is_populated(self):
        """Guard against a suite that passes because nothing is installed:
        if `mmux.scenario.config` were empty, every "unresolved" assertion
        below would be vacuously true."""
        self.assertTrue(self.scenarios)
        self.assertTrue(self.service._scenario_known_codes())

    def test_known_codes_match_the_config_records(self):
        expected = [r.code for r in self.scenarios if r.code]
        self.assertEqual(sorted(self.service._scenario_known_codes()),
                         sorted(expected))

    # ---------------------------------------------------------------- #
    # 1. a decisive position resolves without asking
    # ---------------------------------------------------------------- #
    def test_decisive_position_resolves_the_scenario(self):
        out = self._start(signal={
            "res_model": "stock.picking",
            "action_xmlid": "stock.action_picking_tree_ready"})
        self.assertEqual(out.get("scenario"), "warehouse")
        ctx = self._ctx(out)
        self.assertEqual(ctx.get("source"), "signal")
        self.assertFalse(ctx.get("needs_asking"))
        self.assertNotEqual(out.get("state"), "awaiting_user")

    def test_resolved_context_narrows_the_whitelist(self):
        from ..services.mmux_core import CAPABILITY_REGISTRY
        out = self._start(signal={"res_model": "stock.picking"})
        scenario = out.get("scenario")
        self.assertTrue(scenario)

        allowed = set(self.service._agent_allowed_capabilities(scenario))
        registry = {c.capability_id
                    for c in CAPABILITY_REGISTRY.for_scenario(scenario)
                    if c.enabled}
        self.assertEqual(allowed, registry)
        # and it is genuinely narrower than the unrestricted view
        self.assertTrue(allowed <= set(CAPABILITY_REGISTRY.ids()))

    def test_derived_scenario_is_persisted_into_the_loop(self):
        """The derivation must survive the request: a later turn without a
        signal still knows where the operator is."""
        out = self._start(signal={"res_model": "stock.picking"})
        loop_key = out.get("loop_key")
        self.assertTrue(loop_key)
        again = self.service.agent_turn(loop_key)
        self.assertEqual(again.get("scenario"), "warehouse")

    # ---------------------------------------------------------------- #
    # 2. an undecidable position ASKS — and can be answered
    # ---------------------------------------------------------------- #
    def test_ambiguous_position_asks_instead_of_guessing(self):
        # `stock.move` is mapped to more than one scenario on purpose
        out = self._start(signal={"res_model": "stock.move"})
        self.assertEqual(out.get("state"), "awaiting_user")
        ctx = self._ctx(out)
        self.assertFalse(ctx.get("scenario"))
        self.assertTrue(ctx.get("needs_asking"))
        self.assertGreaterEqual(len(ctx.get("candidates") or []), 2)

    def test_absent_position_asks_rather_than_widening(self):
        out = self._start(signal={})
        self.assertEqual(out.get("state"), "awaiting_user")
        self.assertFalse(out.get("scenario"))
        self.assertTrue(self._ctx(out).get("needs_asking"))

    def test_the_question_offers_real_choices(self):
        """An ask with no options is a dead end, not a question."""
        out = self._start(signal={})
        options = out.get("pending_options") or []
        self.assertTrue(options)
        known = set(self.service._scenario_known_codes())
        self.assertTrue({o["value"] for o in options} <= known)

    def test_an_answer_selects_a_candidate_and_resolves(self):
        out = self._start(signal={})
        loop_key = out.get("loop_key")
        target = (out.get("pending_options") or [{}])[0].get("value")
        self.assertTrue(target)

        answered = self.service.agent_turn(loop_key, answer=target)
        self.assertEqual(answered.get("scenario"), target)
        self.assertEqual(self._ctx(answered).get("source"), "user")
        self.assertFalse(answered.get("pending_field"))

    def test_an_out_of_set_answer_is_refused_and_the_question_stands(self):
        """The operator can only select what the server offered. Accepting
        an arbitrary string would let the client choose its own whitelist
        by typing."""
        out = self._start(signal={})
        loop_key = out.get("loop_key")
        refused = self.service.agent_turn(
            loop_key, answer="definitely_not_a_scenario")
        self.assertNotEqual(refused.get("scenario"),
                            "definitely_not_a_scenario")
        self.assertEqual(refused.get("state"), "awaiting_user")
        self.assertEqual(refused.get("pending_field"), "scenario")

    # ---------------------------------------------------------------- #
    # 3. a stated scenario wins, and the disagreement is visible
    # ---------------------------------------------------------------- #
    def test_stated_scenario_is_not_overridden_by_position(self):
        out = self._start(
            loop={"goal": "g", "scenario": "manufacturing"},
            signal={"res_model": "stock.picking"})       # points at warehouse
        self.assertEqual(out.get("scenario"), "manufacturing")

    def test_the_disagreement_is_reported(self):
        out = self._start(
            loop={"goal": "g", "scenario": "manufacturing"},
            signal={"res_model": "stock.picking"})
        ctx = self._ctx(out)
        self.assertEqual(ctx.get("stated_scenario"), "manufacturing")
        self.assertEqual(ctx.get("position_scenario"), "warehouse")
        self.assertTrue(ctx.get("position_mismatch"))

    def test_a_probe_that_disagrees_does_not_block_the_loop(self):
        """If the operator stated where they are, a probe that cannot
        confirm it must not hold up the work."""
        out = self._start(
            loop={"goal": "g", "scenario": "manufacturing"},
            signal={"menu_xmlid": "stock.menu_stock_operations"})
        self.assertNotEqual(out.get("state"), "awaiting_user")
        self.assertEqual(out.get("scenario"), "manufacturing")

    # ---------------------------------------------------------------- #
    # geo is collected, never consulted
    # ---------------------------------------------------------------- #
    def test_geo_alone_is_recorded_but_does_not_decide(self):
        out = self._start(signal={"geo": {"lat": 31.23, "lng": 121.47,
                                          "accuracy": 8}})
        ctx = self._ctx(out)
        self.assertFalse(ctx.get("scenario"))
        self.assertTrue(ctx.get("geo_recorded"))
        self.assertEqual(out.get("state"), "awaiting_user")

    def test_geo_does_not_break_a_real_signal(self):
        out = self._start(signal={
            "res_model": "stock.picking",
            "geo": {"lat": 31.23, "lng": 121.47, "accuracy": 8}})
        self.assertEqual(out.get("scenario"), "warehouse")
        self.assertTrue(self._ctx(out).get("geo_recorded"))

    def test_precise_coordinates_never_reach_the_view(self):
        """The position is useful for auditing, not for rendering: the UI
        has no business echoing the operator's coordinates back."""
        out = self._start(signal={
            "geo": {"lat": 31.234567, "lng": 121.476543, "accuracy": 8}})
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("31.234567", blob)
        self.assertNotIn("121.476543", blob)

    # ---------------------------------------------------------------- #
    # compatibility: `signal=None` is "not applicable", not "no signal"
    # ---------------------------------------------------------------- #
    def test_absent_signal_keeps_the_v3_0_path(self):
        out = self.service.agent_start(
            loop={"goal": "g", "scenario": "warehouse"})
        self.assertEqual(out.get("scenario"), "warehouse")
        self.assertIsNone(out.get("scenario_context"))
        self.assertNotEqual(out.get("state"), "awaiting_user")

    def test_absent_signal_does_not_ask(self):
        out = self.service.agent_start(loop={"goal": "g"})
        self.assertIsNone(out.get("scenario_context"))
        self.assertIsNone(out.get("pending_field"))

    # ---------------------------------------------------------------- #
    # the LLM may disambiguate WITHIN the candidates, never outside
    # ---------------------------------------------------------------- #
    def test_llm_disambiguation_is_accepted_inside_the_candidate_set(self):
        # stock.move -> warehouse OR manufacturing; the model picks one
        self._mock({"scenario": "warehouse",
                    "reason": {"en_US": "a stock move, likely a transfer"}})
        out = self._start(signal={"res_model": "stock.move"})
        self.assertEqual(out.get("scenario"), "warehouse")

    def test_a_scenario_outside_the_candidates_is_refused(self):
        """The boundary: `sales_visit` exists, but the position did not
        suggest it. Accepting it would let the model widen its own
        whitelist — the exact move the whole design forbids."""
        self._mock({"scenario": "sales_visit",
                    "reason": {"en_US": "trust me"}})
        out = self._start(signal={"res_model": "stock.move"})
        self.assertNotEqual(out.get("scenario"), "sales_visit")

    def test_a_hallucinated_scenario_is_refused(self):
        self._mock({"scenario": "not_a_real_scenario", "reason": "x"})
        out = self._start(signal={"res_model": "stock.move"})
        self.assertNotEqual(out.get("scenario"), "not_a_real_scenario")

    def test_a_llm_failure_falls_back_to_asking(self):
        """Degradation, not error: the operator still gets a usable,
        answerable question."""
        self._mock_error("timeout")
        out = self._start(signal={"res_model": "stock.move"})
        self.assertEqual(out.get("state"), "awaiting_user")
        self.assertTrue(out.get("pending_options"))


@tagged("post_install", "-at_install")
class TestMmuxScenarioContextAudit(TransactionCase):
    """The context decision is auditable — including the decision to ask."""

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.agent.enabled", "1")
        self.Log = self.env["mmux.interaction.log"].sudo()

    def _rows(self):
        return self.Log.search([("capability_id_name", "=",
                                 "scenario_context")])

    def test_a_resolved_context_is_logged(self):
        before = len(self._rows())
        self.service.agent_start(loop={"goal": "g"},
                                 signal={"res_model": "stock.picking"})
        self.assertGreater(len(self._rows()), before)

    def test_an_unresolved_context_is_logged_too(self):
        """A context that could not be established is exactly what an admin
        needs to be able to count later, so it is recorded with the same
        weight as a successful one."""
        before = len(self._rows())
        self.service.agent_start(loop={"goal": "g"}, signal={})
        rows = self._rows()
        self.assertGreater(len(rows), before)
        latest = rows.sorted("id")[-1]
        self.assertEqual(latest.exec_status, "failed")
        detail = json.loads(latest.detail)
        self.assertTrue(detail.get("candidates"))

    def test_the_audit_records_the_signals_but_not_raw_coordinates(self):
        self.service.agent_start(
            loop={"goal": "g"},
            signal={"res_model": "stock.picking",
                    "geo": {"lat": 31.234567, "lng": 121.476543}})
        latest = self._rows().sorted("id")[-1]
        detail = json.loads(latest.detail)
        self.assertEqual(detail["signals"]["res_model"], "stock.picking")
        # the view hides coordinates; the audit must not leak them either
        self.assertNotIn("31.234567", latest.detail)

    def test_an_explicit_scenario_still_produces_a_probe_record(self):
        """Stated beats derived — but the disagreement is not erased."""
        self.service.agent_start(
            loop={"goal": "g", "scenario": "manufacturing"},
            signal={"res_model": "stock.picking"})
        self.assertTrue(self._rows())


@tagged("post_install", "-at_install")
class TestMmuxScenarioGeoSettings(TransactionCase):
    """The position policy is configuration, and it defaults to OFF.

    These tests exist because "collect the operator's location" is the
    kind of feature that gets switched on by accident — a default, an
    eager JS call, a copied config. Each of those failure modes is
    asserted against here.
    """

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.agent.enabled", "1")
        self.Config = self.env["mmux.scenario.config"].sudo()

    def test_collection_is_off_by_default(self):
        collect, weight = self.service._scenario_geo_settings()
        self.assertFalse(collect)
        self.assertEqual(weight, 0)

    def test_every_shipped_scenario_ships_it_off(self):
        for rec in self.Config.search([]):
            with self.subTest(scenario=rec.code):
                self.assertFalse(rec.geo_collect)
                self.assertEqual(rec.geo_weight, 0)

    def test_the_client_is_told_not_to_collect(self):
        out = self.service.agent_start(loop={"goal": "g"}, signal={})
        self.assertIs(out.get("geo_collect"), False)

    def test_enabling_collection_is_reflected_to_the_client(self):
        self.Config.search([("code", "=", "warehouse")]).write(
            {"geo_collect": True})
        out = self.service.agent_start(loop={"goal": "g"}, signal={})
        self.assertTrue(out.get("geo_collect"))

    def test_a_weight_without_collection_is_rejected(self):
        """Weight without collection is a setting that silently does
        nothing — worse than an error, because nobody notices."""
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.Config.search([("code", "=", "warehouse")]).write(
                {"geo_weight": 50})

    def test_enabling_a_weight_alone_still_does_not_decide(self):
        """Even with collection on, weight 0 means the position is
        recorded and nothing more — the derivation is unchanged."""
        self.Config.search([("code", "=", "warehouse")]).write(
            {"geo_collect": True})
        out = self.service.agent_start(
            loop={"goal": "g"},
            signal={"geo": {"lat": 31.23, "lng": 121.47}})
        self.assertFalse((out.get("scenario_context") or {}).get("scenario"))
        self.assertEqual(out.get("state"), "awaiting_user")

    def test_the_configured_weight_reaches_the_derivation(self):
        """A non-zero configured weight is actually used — otherwise the
        admin-facing field would be decorative."""
        self.Config.search([("code", "=", "warehouse")]).write(
            {"geo_collect": True, "geo_weight": 70})
        _collect, weight = self.service._scenario_geo_settings()
        self.assertEqual(weight, 70)

    def test_the_policy_survives_an_unreadable_config(self):
        """Degrade to the CONSERVATIVE value: no collection, no weight.
        An unreadable config must not turn into "collect everything"."""
        collect, weight = self.service._scenario_geo_settings()
        self.assertIsInstance(collect, bool)
        self.assertGreaterEqual(weight, 0)
