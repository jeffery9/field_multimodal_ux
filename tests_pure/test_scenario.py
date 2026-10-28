# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Unit tests for scenario-context recognition.

This module decides *which capability whitelist applies*, so it sits
directly upstream of the agent's permission boundary. That makes it the
kind of code where a small convenience ("let the model pick the scenario,
it's smarter") would quietly dismantle a guarantee built three layers
down. The tests below are written to make that specific mistake fail
loudly, and to keep failing:

  * the scenario comes from SERVER-side signal scoring, and the model may
    only choose WITHIN the candidate set the server derived;
  * a scenario outside that set is rejected — a hallucinated code and a
    real-but-not-offered code are treated identically;
  * geolocation is recorded but carries ZERO weight, so it can never tip
    a decision (the geofence mapping does not exist yet, and inventing
    one silently would route the operator to the wrong whitelist);
  * an unresolved context never degrades to "use everything" — it becomes
    a question, because silently widening the agent's world is worse than
    asking;
  * nothing raises: every malformed signal yields a valid, explainable
    "no scenario".
"""
import json
import random
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.intent import FORBIDDEN_KEYS  # noqa: E402
from mmux_core.scenario import (  # noqa: E402
    ACTION_XMLID_MAP, AMBIGUOUS_RES_MODEL_MAP, CHOICE_KEYS,
    CHOICE_SOURCE_DETERMINISTIC, CHOICE_SOURCE_LLM, ContextSignal,
    MAX_CANDIDATES, MENU_XMLID_MAP, MIN_DECISIVE_SCORE, RES_MODEL_MAP,
    SOURCE_AMBIGUOUS, SOURCE_NONE, SOURCE_SIGNAL, SOURCE_USER,
    WEIGHT_ACTION_XMLID, WEIGHT_GEO, WEIGHT_MENU_XMLID, WEIGHT_RES_MODEL,
    build_ask_question, build_scenario_prompt,
    choice_from_answer, collect_hints, resolve_scenario,
    validate_scenario_choice,
)


#: the four scenarios the scenario addons actually register
KNOWN = ("warehouse", "manufacturing", "sales_visit", "field_service")


# ===================================================================== #
class TestSignalParsing(unittest.TestCase):
    """The signal is untrusted client input. It must be coerced, never
    obeyed — and never trusted the way a server-derived value is."""

    def test_builds_from_dict(self):
        sig = ContextSignal.from_dict({
            "res_model": "stock.picking", "res_id": 42,
            "action_xmlid": "stock.action_picking_tree_ready",
            "menu_xmlid": "stock.menu_stock_operations",
            "action_id": 77, "tag": "stock_picking_form",
        })
        self.assertEqual(sig.res_model, "stock.picking")
        self.assertEqual(sig.res_id, 42)
        self.assertEqual(sig.action_xmlid, "stock.action_picking_tree_ready")
        self.assertEqual(sig.action_id, 77)

    def test_garbage_becomes_an_empty_signal(self):
        for bad in (None, [], "string", 42, object()):
            with self.subTest(bad=bad):
                sig = ContextSignal.from_dict(bad)
                self.assertTrue(sig.is_empty)

    def test_bool_is_not_an_id(self):
        """`True` is a very common accident and must not become record 1."""
        sig = ContextSignal.from_dict({"res_id": True, "action_id": True})
        self.assertIsNone(sig.res_id)
        self.assertIsNone(sig.action_id)

    def test_digit_string_is_an_id(self):
        sig = ContextSignal.from_dict({"res_id": "42"})
        self.assertEqual(sig.res_id, 42)

    def test_negative_and_zero_ids_are_dropped(self):
        for bad in (0, -1, "0", "-5"):
            with self.subTest(bad=bad):
                self.assertIsNone(ContextSignal.from_dict({"res_id": bad}).res_id)

    def test_blank_strings_are_not_signals(self):
        sig = ContextSignal.from_dict({"res_model": "   ", "tag": ""})
        self.assertIsNone(sig.res_model)
        self.assertIsNone(sig.tag)

    def test_oversized_strings_are_truncated(self):
        sig = ContextSignal.from_dict({"res_model": "x" * 5000})
        self.assertLessEqual(len(sig.res_model), 200)

    def test_geo_is_kept_verbatim_but_never_parsed(self):
        geo = {"lat": 31.23, "lng": 121.47, "accuracy": 12}
        sig = ContextSignal.from_dict({"geo": geo})
        self.assertEqual(sig.geo, geo)
        # and alone, it is NOT a signal (it decides nothing)
        self.assertTrue(sig.is_empty)

    def test_geo_non_dict_is_dropped(self):
        for bad in ("31.2,121.4", [1, 2], 42):
            with self.subTest(bad=bad):
                self.assertIsNone(ContextSignal.from_dict({"geo": bad}).geo)

    def test_roundtrip(self):
        """The DECISION fields survive a round trip untouched.

        `geo` deliberately does not: `to_dict()` redacts it to a boolean
        (see `test_to_dict_redacts_coordinates_by_default`), and `True` is
        not a dict, so rebuilding from an unsanitized view yields no geo.
        That asymmetry is the point — the redacted view is for display and
        audit, not for reconstructing the signal.
        """
        sig = ContextSignal(res_model="mrp.workorder", res_id=3,
                            action_xmlid="mrp.workorder_action",
                            geo={"lat": 1.0})
        again = ContextSignal.from_dict(sig.to_dict())
        self.assertEqual((again.res_model, again.res_id, again.action_xmlid),
                         (sig.res_model, sig.res_id, sig.action_xmlid))
        self.assertIsNone(again.geo)

    def test_roundtrip_through_the_unredacted_view_keeps_geo(self):
        sig = ContextSignal(res_model="mrp.workorder", res_id=3,
                            geo={"lat": 1.0})
        again = ContextSignal.from_dict(sig.to_dict(include_geo_coords=True))
        self.assertEqual(again.geo, {"lat": 1.0})

    def test_to_dict_redacts_coordinates_by_default(self):
        """`to_dict()` feeds the interaction log and the client view, and
        neither has any business holding the operator's exact position.
        The audit only needs to say "a position arrived"."""
        sig = ContextSignal(res_model="mrp.workorder",
                            geo={"lat": 31.234567, "lng": 121.476543})
        out = sig.to_dict()
        self.assertIs(out["geo"], True)
        self.assertNotIn("31.234567", json.dumps(out))

    def test_to_dict_can_opt_into_the_coordinates(self):
        """An explicit caller (a diagnostic tool, a future geofence) can
        still get the numbers — but the default must be redacted, so that
        "we accidentally logged the position" requires an act, not an
        omission."""
        sig = ContextSignal(res_model="mrp.workorder",
                            geo={"lat": 31.234567, "lng": 121.476543})
        out = sig.to_dict(include_geo_coords=True)
        self.assertEqual(out["geo"]["lat"], 31.234567)

    def test_to_dict_reports_absent_geo_as_false(self):
        self.assertIs(ContextSignal(res_model="x").to_dict()["geo"], False)
        self.assertIs(ContextSignal().to_dict()["geo"], False)


# ===================================================================== #
class TestDeterministicScoring(unittest.TestCase):
    """The core: signals -> weighted evidence -> candidates. No LLM here."""

    def test_res_model_alone_is_decisive(self):
        r = resolve_scenario(ContextSignal(res_model="stock.picking"), KNOWN)
        self.assertEqual(r.scenario, "warehouse")
        self.assertEqual(r.source, SOURCE_SIGNAL)
        self.assertEqual(r.candidates, ["warehouse"])
        self.assertFalse(r.needs_asking)

    def test_action_xmlid_alone_is_decisive(self):
        r = resolve_scenario(
            ContextSignal(action_xmlid="mrp.workorder_action"), KNOWN)
        self.assertEqual(r.scenario, "manufacturing")
        self.assertEqual(r.source, SOURCE_SIGNAL)

    def test_menu_xmlid_alone_is_NOT_decisive(self):
        """A menu is a broad container: it corroborates, it does not
        decide. Acting on it alone would be a confident guess about which
        job the operator is doing."""
        r = resolve_scenario(
            ContextSignal(menu_xmlid="stock.menu_stock_operations"), KNOWN)
        self.assertIsNone(r.scenario)
        self.assertEqual(r.source, SOURCE_NONE)
        self.assertTrue(r.needs_asking)
        # the weak hint is still reported, so the operator can be shown it
        self.assertEqual(r.candidates, ["warehouse"])

    def test_menu_corroborates_a_stronger_signal(self):
        r = resolve_scenario(ContextSignal(
            res_model="stock.picking",
            menu_xmlid="stock.menu_stock_operations"), KNOWN)
        self.assertEqual(r.scenario, "warehouse")
        self.assertGreater(r.confidence, 0.5)

    def test_weight_ordering_is_what_the_module_claims(self):
        self.assertGreater(WEIGHT_ACTION_XMLID, WEIGHT_RES_MODEL)
        self.assertGreater(WEIGHT_RES_MODEL, WEIGHT_MENU_XMLID)
        self.assertEqual(WEIGHT_GEO, 0)
        self.assertEqual(MIN_DECISIVE_SCORE, WEIGHT_RES_MODEL)

    def test_action_beats_a_conflicting_model(self):
        """The explicit job screen wins over the record model — the
        operator's intent is in the action they opened, not in whatever
        record happens to be behind it."""
        r = resolve_scenario(ContextSignal(
            action_xmlid="stock.action_picking_tree_ready",
            res_model="project.task"), KNOWN)
        self.assertEqual(r.scenario, "warehouse")

    def test_equal_weight_conflict_is_ambiguous_and_asks(self):
        """A model that genuinely spans two job types must produce a TIE,
        not a coin flip. `stock.move` exists inside a manufacturing order
        as much as inside a delivery; claiming one would be a guess
        dressed as a fact."""
        self.assertIn("stock.move", AMBIGUOUS_RES_MODEL_MAP)
        r = resolve_scenario(ContextSignal(res_model="stock.move"), KNOWN)
        self.assertIsNone(r.scenario)
        self.assertEqual(r.source, SOURCE_AMBIGUOUS)
        self.assertTrue(r.needs_asking)
        self.assertEqual(sorted(r.candidates), ["manufacturing", "warehouse"])
        self.assertEqual(r.confidence, 0.0)

    def test_every_ambiguous_model_offers_at_least_two(self):
        """A 'multi-valued' entry with one value is a single-valued entry
        in disguise, and would silently assert a scenario."""
        for model, scenarios in AMBIGUOUS_RES_MODEL_MAP.items():
            with self.subTest(model=model):
                self.assertNotIn(model, RES_MODEL_MAP, model)
                if scenarios:
                    self.assertGreaterEqual(len(scenarios), 2, model)
                    for scenario in scenarios:
                        self.assertIn(scenario, KNOWN, scenario)

    def test_all_mapped_models_are_disjoint(self):
        """Listing a model in BOTH tables would make the outcome depend on
        which branch happened to run first."""
        overlap = set(RES_MODEL_MAP) & set(AMBIGUOUS_RES_MODEL_MAP)
        self.assertEqual(overlap, set())

    def test_models_absent_from_both_tables_are_silent(self):
        """`res.partner` and `product.product` appear in every scenario's
        screens; mapping them would produce confident nonsense. They are
        listed as empty on purpose, and an unlisted model behaves the
        same way."""
        for model in ("res.partner", "product.product"):
            with self.subTest(model=model):
                self.assertEqual(AMBIGUOUS_RES_MODEL_MAP.get(model), ())
                r = resolve_scenario(ContextSignal(res_model=model), KNOWN)
                self.assertIsNone(r.scenario)
                self.assertTrue(r.needs_asking)

    def test_ambiguous_is_never_confidence_one(self):
        r = resolve_scenario(ContextSignal(res_model="stock.move"), KNOWN)
        self.assertLess(r.confidence, 1.0)

    def test_action_breaks_an_ambiguous_model_tie(self):
        """The point of the ambiguous table: a strong signal can still
        resolve it, which is what makes the tie state useful rather than
        merely honest."""
        r = resolve_scenario(ContextSignal(
            res_model="stock.move",
            action_xmlid="mrp.workorder_action"), KNOWN)
        self.assertEqual(r.scenario, "manufacturing")

    def test_action_breaks_the_tie_the_other_way_too(self):
        r = resolve_scenario(ContextSignal(
            res_model="stock.move",
            action_xmlid="stock.action_picking_tree_ready"), KNOWN)
        self.assertEqual(r.scenario, "warehouse")

    def test_model_alone_still_resolves(self):
        r = resolve_scenario(ContextSignal(res_model="crm.lead"), KNOWN)
        self.assertEqual(r.scenario, "sales_visit")
        self.assertFalse(r.needs_asking)

    def test_unknown_scenario_is_dropped_not_reported(self):
        """Recommending a scenario the server cannot serve would give the
        operator an empty toolset. Silence is the correct outcome."""
        r = resolve_scenario(ContextSignal(res_model="stock.picking"),
                             known_scenarios=("manufacturing",))
        self.assertIsNone(r.scenario)
        self.assertEqual(r.candidates, [])

    def test_known_scenarios_empty_means_no_filtering(self):
        r = resolve_scenario(ContextSignal(res_model="stock.picking"), ())
        self.assertEqual(r.scenario, "warehouse")

    def test_unmapped_model_is_silence_not_a_guess(self):
        """`res.partner`, `res.users`, `product.product` appear in every
        scenario's screens. Mapping them would produce confident nonsense,
        so they are deliberately absent."""
        for model in ("res.partner", "res.users", "product.product",
                      "ir.ui.menu", "mail.message"):
            with self.subTest(model=model):
                self.assertNotIn(model, RES_MODEL_MAP)
                r = resolve_scenario(ContextSignal(res_model=model), KNOWN)
                self.assertIsNone(r.scenario)
                self.assertTrue(r.needs_asking)

    def test_unknown_xmlids_are_ignored(self):
        r = resolve_scenario(ContextSignal(
            action_xmlid="some.other.action",
            menu_xmlid="another.menu"), KNOWN)
        self.assertIsNone(r.scenario)
        self.assertEqual(r.candidates, [])

    def test_no_signal_asks_instead_of_using_everything(self):
        r = resolve_scenario(ContextSignal(), KNOWN)
        self.assertIsNone(r.scenario)
        self.assertEqual(r.source, SOURCE_NONE)
        self.assertTrue(r.needs_asking)
        self.assertEqual(r.candidates, [])

    def test_confidence_is_bounded(self):
        r = resolve_scenario(ContextSignal(
            action_xmlid="stock.action_picking_tree_ready",
            res_model="stock.picking",
            menu_xmlid="stock.menu_stock_operations"), KNOWN)
        self.assertGreaterEqual(r.confidence, 0.0)
        self.assertLessEqual(r.confidence, 1.0)

    def test_agreement_raises_confidence(self):
        weak = resolve_scenario(ContextSignal(res_model="stock.picking"),
                                KNOWN)
        strong = resolve_scenario(ContextSignal(
            action_xmlid="stock.action_picking_tree_ready",
            res_model="stock.picking"), KNOWN)
        self.assertGreater(strong.confidence, weak.confidence)

    def test_candidate_list_is_stable_across_calls(self):
        """An unstable list would make the same situation look different
        on every refresh — and would change what the model is offered."""
        sig = ContextSignal(menu_xmlid="stock.menu_stock_operations",
                            res_model="stock.picking")
        first = resolve_scenario(sig, KNOWN).candidates
        for _ in range(5):
            self.assertEqual(resolve_scenario(sig, KNOWN).candidates, first)

    def test_candidates_are_capped(self):
        r = resolve_scenario(ContextSignal(menu_xmlid="stock.menu_x"),
                             KNOWN)
        self.assertLessEqual(len(r.candidates), MAX_CANDIDATES)

    def test_evidence_names_its_origin(self):
        r = resolve_scenario(ContextSignal(
            res_model="stock.picking",
            menu_xmlid="stock.menu_stock_operations"), KNOWN)
        sources = {h.source for h in r.evidence}
        self.assertEqual(sources, {"res_model", "menu_xmlid"})
        for hint in r.evidence:
            self.assertIn(hint.scenario, KNOWN)
            self.assertGreater(hint.weight, 0)

    def test_every_map_target_is_a_plausible_scenario_code(self):
        """A typo in a table would silently disable that signal forever."""
        for name, table in (("RES_MODEL_MAP", RES_MODEL_MAP),
                            ("ACTION_XMLID_MAP", ACTION_XMLID_MAP),
                            ("MENU_XMLID_MAP", MENU_XMLID_MAP)):
            with self.subTest(table=name):
                for key, scenario in table.items():
                    self.assertIsInstance(key, str)
                    self.assertTrue(key.strip(), key)
                    self.assertIn(scenario, KNOWN,
                                  f"{name}[{key}] -> {scenario}")

    def test_table_keys_are_well_formed(self):
        for model in RES_MODEL_MAP:
            self.assertIn(".", model, model)
        for xmlid in list(ACTION_XMLID_MAP) + list(MENU_XMLID_MAP):
            self.assertIn(".", xmlid, xmlid)


# ===================================================================== #
class TestGeoIsCollectedButDoesNotDecide(unittest.TestCase):
    """The reserved-but-unused nature of geolocation is a deliberate
    decision, so it gets its own test class. If someone later turns GPS
    into a scoring signal without wiring a reviewed geofence mapping,
    these fail."""

    def test_geo_weight_is_zero(self):
        self.assertEqual(WEIGHT_GEO, 0)

    def test_geo_alone_resolves_nothing(self):
        sig = ContextSignal(geo={"lat": 31.23, "lng": 121.47})
        r = resolve_scenario(sig, KNOWN)
        self.assertIsNone(r.scenario)
        self.assertTrue(r.needs_asking)

    def test_geo_is_recorded_for_audit(self):
        sig = ContextSignal(res_model="stock.picking",
                            geo={"lat": 31.23, "lng": 121.47, "accuracy": 8})
        r = resolve_scenario(sig, KNOWN)
        # `geo_recorded` is the audit-visible fact: the position WAS
        # received. The raw coordinates are logged by the service layer,
        # not echoed back through the scenario view — the view is shown in
        # the UI, and precise coordinates are not something to render.
        self.assertTrue(r.geo_recorded)
        self.assertTrue(r.to_dict()["geo_recorded"])

    def test_geo_recorded_is_false_without_a_position(self):
        r = resolve_scenario(ContextSignal(res_model="stock.picking"), KNOWN)
        self.assertFalse(r.geo_recorded)

    def test_geo_never_adds_evidence_at_default_weight(self):
        with_geo = resolve_scenario(
            ContextSignal(res_model="stock.picking",
                          geo={"lat": 1.0, "lng": 2.0}), KNOWN)
        without = resolve_scenario(ContextSignal(res_model="stock.picking"),
                                   KNOWN)
        self.assertEqual(with_geo.confidence, without.confidence)
        self.assertEqual(len(with_geo.evidence), len(without.evidence))

    def test_geo_cannot_outvote_a_real_signal(self):
        """A geofence hint for the WRONG scenario must not beat the model
        on screen, even if geo scoring is turned on later — and at weight
        0 it must not even be counted."""
        sig = ContextSignal(res_model="stock.picking",
                            geo={"scenario": "manufacturing"})
        r = resolve_scenario(sig, KNOWN)
        self.assertEqual(r.scenario, "warehouse")

    def test_geo_only_scores_when_explicitly_asked(self):
        """The opt-in path exists so the feature can be enabled later
        without touching this module — but it is never the default."""
        sig = ContextSignal(res_model="stock.picking",
                            geo={"scenario": "manufacturing"})
        r = resolve_scenario(sig, KNOWN, geo_weight=WEIGHT_ACTION_XMLID)
        # now geo(100) ties the action level, but there is no action here:
        # model(60) vs geo(100) -> geo wins, which is exactly the outcome
        # the default weight prevents.
        self.assertEqual(r.scenario, "manufacturing")
        r_default = resolve_scenario(sig, KNOWN)
        self.assertEqual(r_default.scenario, "warehouse")


# ===================================================================== #
class TestModelDisambiguationBoundary(unittest.TestCase):
    """THE security tests of this module: the model may choose WITHIN the
    candidate set and nowhere else."""

    def test_valid_choice_within_candidates(self):
        choice = validate_scenario_choice(
            {"scenario": "manufacturing", "confidence": 0.8},
            ["warehouse", "manufacturing"])
        self.assertTrue(choice.is_valid)
        self.assertEqual(choice.scenario, "manufacturing")
        self.assertEqual(choice.source, CHOICE_SOURCE_LLM)
        self.assertTrue(choice.is_autonomous)

    def test_hallucinated_scenario_is_rejected(self):
        choice = validate_scenario_choice(
            {"scenario": "quantum_warehouse"}, ["warehouse", "manufacturing"])
        self.assertFalse(choice.is_valid)
        self.assertIn("scenario_not_in_candidates", choice.rejected_reason)

    def test_REAL_but_unoffered_scenario_is_rejected(self):
        """The important case: `sales_visit` exists as a scenario, but the
        server did not offer it. Returning it must be just as invalid as
        returning nonsense — otherwise the model can widen its own
        whitelist by naming a plausible context."""
        choice = validate_scenario_choice(
            {"scenario": "sales_visit"}, ["warehouse"])
        self.assertFalse(choice.is_valid)
        self.assertEqual(choice.rejected_reason,
                         "scenario_not_in_candidates:sales_visit")

    def test_model_cannot_widen_the_candidate_set(self):
        """A model answer cannot introduce a scenario, however reasonable
        its 'reason' sounds."""
        choice = validate_scenario_choice(
            {"scenario": "sales_visit",
             "reason": {"en_US": "the user is clearly visiting a customer"}},
            ["warehouse"])
        self.assertFalse(choice.is_valid)
        self.assertEqual(choice.rejected_reason,
                         "scenario_not_in_candidates:sales_visit")

    def test_no_near_miss_repair(self):
        """Case and spelling are semantic differences: a different code is
        a different scenario, so it is refused, not repaired."""
        for near in ("WAREHOUSE", "warehous", "warehouse_visit",
                     "ware house", "warehouse;"):
            with self.subTest(near=near):
                choice = validate_scenario_choice({"scenario": near},
                                                  ["warehouse"])
                self.assertFalse(choice.is_valid, near)

    def test_leading_trailing_whitespace_IS_trimmed(self):
        """Whitespace is not a semantic difference (unlike case or
        spelling), so it is normalised before the membership check."""
        for padded in (" warehouse ", "  warehouse", "warehouse   "):
            with self.subTest(padded=padded):
                choice = validate_scenario_choice({"scenario": padded},
                                                  ["warehouse"])
                self.assertTrue(choice.is_valid, padded)
                self.assertEqual(choice.scenario, "warehouse")

    def test_non_dict_rejected(self):
        for bad in (None, [], "warehouse", 42):
            with self.subTest(bad=bad):
                self.assertFalse(
                    validate_scenario_choice(bad, ["warehouse"]).is_valid)

    def test_missing_scenario_rejected(self):
        c = validate_scenario_choice({"confidence": 0.9}, ["warehouse"])
        self.assertEqual(c.rejected_reason, "missing_scenario")

    def test_non_string_scenario_rejected(self):
        for bad in (42, True, [], {}, None):
            with self.subTest(bad=bad):
                self.assertFalse(
                    validate_scenario_choice({"scenario": bad},
                                             ["warehouse"]).is_valid)

    def test_empty_candidates_is_a_valid_state_not_an_error(self):
        c = validate_scenario_choice({"scenario": "warehouse"}, [])
        self.assertEqual(c.rejected_reason, "no_candidates")
        self.assertFalse(c.is_valid)

    def test_every_forbidden_key_is_rejected(self):
        """The same 18-key blacklist as `intent.py` and `agent.py` — from
        the same frozenset, so the 口径 cannot drift."""
        self.assertTrue(FORBIDDEN_KEYS)
        for key in sorted(FORBIDDEN_KEYS):
            with self.subTest(key=key):
                c = validate_scenario_choice(
                    {"scenario": "warehouse", key: "anything"},
                    ["warehouse"])
                self.assertFalse(c.is_valid)
                self.assertIn("protocol_violation", c.rejected_reason)
                self.assertIn(key, c.rejected_reason)

    def test_forbidden_key_is_reported_before_undeclared_key(self):
        """A payload naming a model is a PROTOCOL violation, not a typo in
        a key name — the message must say so, or the audit is misleading.
        Deliberate defence in depth, same ordering as `validate_action`."""
        c = validate_scenario_choice(
            {"scenario": "warehouse", "model": "stock.picking", "zzz": 1},
            ["warehouse"])
        self.assertIn("protocol_violation", c.rejected_reason)
        self.assertNotIn("undeclared", c.rejected_reason)

    def test_undeclared_key_rejects_the_whole_answer(self):
        """No 'use the good half' — one stray key invalidates the answer."""
        c = validate_scenario_choice(
            {"scenario": "warehouse", "extra": "x"}, ["warehouse"])
        self.assertFalse(c.is_valid)
        self.assertIn("undeclared_key", c.rejected_reason)

    def test_closed_key_set(self):
        self.assertEqual(CHOICE_KEYS, frozenset({"scenario", "reason",
                                                 "confidence"}))

    def test_confidence_is_clamped(self):
        for raw, expected in ((5, 1.0), (-3, 0.0), ("0.5", 0.5),
                              (None, 0.0), ("abc", 0.0)):
            with self.subTest(raw=raw):
                c = validate_scenario_choice(
                    {"scenario": "warehouse", "confidence": raw},
                    ["warehouse"])
                self.assertEqual(c.confidence, expected)

    def test_reason_keeps_only_whitelisted_languages(self):
        c = validate_scenario_choice(
            {"scenario": "warehouse",
             "reason": {"zh_CN": "有拣货单打开", "en_US": "a picking is open",
                        "klingon": "nope", "xx": "no"}},
            ["warehouse"])
        self.assertEqual(set(c.reason), {"zh_CN", "en_US"})
        self.assertNotIn("klingon", c.reason)

    def test_reason_length_is_bounded(self):
        c = validate_scenario_choice(
            {"scenario": "warehouse", "reason": {"en_US": "x" * 5000}},
            ["warehouse"])
        self.assertLessEqual(len(c.reason["en_US"]), 400)

    def test_reason_plain_string_is_accepted(self):
        c = validate_scenario_choice(
            {"scenario": "warehouse", "reason": "a picking is open"},
            ["warehouse"])
        self.assertEqual(c.reason.get("default"), "a picking is open")

    def test_reason_non_string_values_dropped(self):
        c = validate_scenario_choice(
            {"scenario": "warehouse",
             "reason": {"en_US": ["not", "a", "string"], "zh_CN": 42}},
            ["warehouse"])
        self.assertEqual(c.reason, {})

    def test_to_dict_exposes_both_localized_and_raw(self):
        c = validate_scenario_choice(
            {"scenario": "warehouse", "reason": {"zh_CN": "仓库", "en_US": "wh"}},
            ["warehouse"])
        out = c.to_dict("zh_CN")
        self.assertEqual(out["reason"], "仓库")
        self.assertEqual(out["reason_i18n"], {"zh_CN": "仓库", "en_US": "wh"})

    def test_never_raises_on_hostile_input(self):
        hostile = [
            {"scenario": {"nested": "dict"}},
            {"scenario": "warehouse", "confidence": object()},
            {"scenario": "warehouse", "reason": object()},
            {"scenario": "warehouse", "reason": {1: "x"}},
            {"scenario": "a" * 100000},
            {"__class__": "x"},
            {},
        ]
        for bad in hostile:
            with self.subTest(bad=str(bad)[:40]):
                c = validate_scenario_choice(bad, ["warehouse"])
                self.assertIsInstance(c.scenario, (str, type(None)))


# ===================================================================== #
class TestOperatorAnswer(unittest.TestCase):
    """The `ask_user` path — the honest fallback for ambiguity."""

    def test_exact_code_wins(self):
        c = choice_from_answer("warehouse", ["warehouse", "manufacturing"])
        self.assertTrue(c.is_valid)
        self.assertEqual(c.scenario, "warehouse")
        self.assertEqual(c.source, SOURCE_USER)

    def test_case_insensitive_code(self):
        c = choice_from_answer("WAREHOUSE", ["warehouse", "manufacturing"])
        self.assertEqual(c.scenario, "warehouse")

    def test_unambiguous_substring_hit(self):
        c = choice_from_answer("it's the warehouse one",
                               ["warehouse", "manufacturing"])
        self.assertEqual(c.scenario, "warehouse")

    def test_ambiguous_substring_is_refused(self):
        """Two candidates mentioned -> we cannot tell. Guessing here would
        silently pick the wrong capability whitelist. The reason is its own
        code so the UI can say "slow down, I heard two" rather than "I did
        not understand you"."""
        c = choice_from_answer("warehouse or manufacturing?",
                               ["warehouse", "manufacturing"])
        self.assertFalse(c.is_valid)
        self.assertEqual(c.rejected_reason, "ambiguous_answer")

    def test_a_typo_that_starts_with_a_code_is_refused(self):
        """`sales_visit_typo` CONTAINS `sales_visit`. Word-boundary matching
        means the typo is not silently repaired into the valid code — the
        operator who mistyped must be asked again, not sent to a scenario
        they did not choose."""
        c = choice_from_answer("sales_visit_typo", ["sales_visit", "warehouse"])
        self.assertFalse(c.is_valid)
        self.assertEqual(c.rejected_reason, "answer_not_in_candidates")

    def test_exact_code_is_never_stolen_by_a_longer_candidate(self):
        """`sales` is a prefix of `sales_visit`; with exact-match checked
        first and word boundaries after, the short code stays reachable even
        when a longer candidate shares its prefix."""
        c = choice_from_answer("sales", ["sales", "sales_visit"])
        self.assertTrue(c.is_valid)
        self.assertEqual(c.scenario, "sales")

    def test_a_phrase_around_the_code_still_matches(self):
        for phrase in ("the warehouse one", "仓库 warehouse",
                       "仓库 the warehouse please"):
            with self.subTest(phrase=phrase):
                c = choice_from_answer(phrase, ["warehouse", "manufacturing"])
                self.assertEqual(c.scenario, "warehouse")

    def test_unmatched_answer_is_refused(self):
        c = choice_from_answer("something else entirely", ["warehouse"])
        self.assertFalse(c.is_valid)

    def test_empty_answer_refused(self):
        for bad in ("", "   ", None, 42):
            with self.subTest(bad=bad):
                self.assertFalse(
                    choice_from_answer(bad, ["warehouse"]).is_valid)

    def test_no_candidates_refused(self):
        self.assertFalse(choice_from_answer("warehouse", []).is_valid)

    def test_ask_question_offers_the_candidates(self):
        r = resolve_scenario(ContextSignal(), KNOWN)
        # an empty resolution asks without options
        question, options = build_ask_question(r)
        self.assertEqual(options, [])
        self.assertIn("zh_CN", question)
        self.assertIn("en_US", question)

    def test_ask_question_lists_tied_candidates(self):
        r = resolve_scenario(ContextSignal(
            menu_xmlid="stock.menu_stock_operations"), KNOWN)
        r.source = SOURCE_AMBIGUOUS
        r.candidates = ["warehouse", "manufacturing"]
        question, options = build_ask_question(
            r, labels={"warehouse": "仓库", "manufacturing": "制造"})
        self.assertEqual([o["value"] for o in options],
                         ["warehouse", "manufacturing"])
        self.assertEqual(options[0]["label"], "仓库")
        self.assertIn("仓库", question["zh_CN"])

    def test_two_messages_are_distinct(self):
        """'Several possibilities' and 'I cannot tell' are different
        problems; the operator should not see the same text for both."""
        amb = resolve_scenario(ContextSignal(), KNOWN)
        amb.source = SOURCE_AMBIGUOUS
        amb.candidates = ["warehouse"]
        q_amb, _ = build_ask_question(amb)
        none_res = resolve_scenario(ContextSignal(), KNOWN)
        q_none, _ = build_ask_question(none_res)
        self.assertNotEqual(q_amb["zh_CN"], q_none["zh_CN"])
        self.assertNotEqual(q_amb["en_US"], q_none["en_US"])


# ===================================================================== #
class TestPrompt(unittest.TestCase):
    """The prompt must state the boundary, not merely imply it."""

    def test_prompt_lists_only_the_candidates(self):
        prompt = build_scenario_prompt(
            ContextSignal(res_model="stock.picking"),
            ["warehouse", "manufacturing"])
        user = prompt[1]["content"]
        self.assertIn("warehouse", user)
        self.assertIn("manufacturing", user)
        self.assertNotIn("sales_visit", user)

    def test_prompt_states_the_candidate_restriction(self):
        sys_text = build_scenario_prompt(ContextSignal(), ["warehouse"])[0]
        self.assertIn("content", sys_text)
        self.assertIn("MUST be one of the candidate codes", sys_text["content"])

    def test_prompt_calls_the_signals_unverified(self):
        """Client input is evidence, never authority — the model must be
        told that, or it will treat a spoofed xmlid as fact."""
        sys_text = build_scenario_prompt(ContextSignal(), ["warehouse"])[0]
        self.assertIn("NOT verified", sys_text["content"])

    def test_prompt_forbids_naming_models(self):
        sys_text = build_scenario_prompt(ContextSignal(), ["warehouse"])[0]
        self.assertIn("NEVER name an Odoo model", sys_text["content"])
        # the blacklist itself is restated, so the model cannot claim it
        # did not know a key was off limits
        for key in ("model", "search_read", "create"):
            self.assertIn(key, sys_text["content"])

    def test_prompt_embeds_the_closed_key_set(self):
        sys_text = build_scenario_prompt(ContextSignal(), ["warehouse"])[0]
        for key in sorted(CHOICE_KEYS):
            self.assertIn(key, sys_text["content"])

    def test_prompt_offers_low_confidence_as_the_honest_exit(self):
        """The model must have a way to say 'too weak to tell' that is not
        'invent something'."""
        sys_text = build_scenario_prompt(ContextSignal(), ["warehouse"])[0]
        self.assertIn("low confidence", sys_text["content"])

    def test_prompt_carries_the_signal_payload(self):
        import json
        prompt = build_scenario_prompt(
            ContextSignal(res_model="stock.picking", res_id=7), ["warehouse"])
        payload = json.loads(prompt[1]["content"])
        self.assertEqual(payload["signals"]["res_model"], "stock.picking")
        self.assertEqual(payload["signals"]["res_id"], 7)

    def test_prompt_is_deterministic(self):
        args = (ContextSignal(res_model="stock.picking"), ["warehouse"])
        self.assertEqual(build_scenario_prompt(*args),
                         build_scenario_prompt(*args))

    def test_labels_are_included_when_given(self):
        prompt = build_scenario_prompt(
            ContextSignal(), ["warehouse"],
            labels={"warehouse": "仓库与物流"})
        self.assertIn("仓库与物流", prompt[1]["content"])


# ===================================================================== #
class TestResilience(unittest.TestCase):
    """Nothing here may raise: a broken context must not break the loop."""

    def test_resolve_never_raises_on_garbage(self):
        rng = random.Random(20240810)
        junk = [None, [], {}, "x", 42, True, object(),
                {"res_model": []}, {"geo": "no"}, {"res_id": {}}]
        for item in junk:
            with self.subTest(item=str(item)[:30]):
                r = resolve_scenario(item, KNOWN)
                self.assertIsInstance(r.candidates, list)
                self.assertIsInstance(r.needs_asking, bool)

    def test_fuzz_resolve(self):
        rng = random.Random(7)
        pool = ["stock.picking", "mrp.workorder", "crm.lead", "",
                None, 42, [], {}, "x" * 300]
        for _ in range(300):
            sig = ContextSignal(
                res_model=rng.choice(pool),
                action_xmlid=rng.choice(pool),
                menu_xmlid=rng.choice(pool),
                res_id=rng.choice([None, 0, 1, "2", True, "abc"]),
                geo=rng.choice([None, {}, {"lat": 1}, "x"]))
            r = resolve_scenario(sig, KNOWN)
            self.assertIn(r.source, (SOURCE_SIGNAL, SOURCE_AMBIGUOUS,
                                     SOURCE_NONE))
            if r.scenario:
                self.assertIn(r.scenario, KNOWN)
                self.assertEqual(r.candidates, [r.scenario])
            else:
                self.assertTrue(r.needs_asking)

    def test_fuzz_validate(self):
        rng = random.Random(11)
        for _ in range(300):
            data = rng.choice([
                {}, [], None, 42, "warehouse",
                {"scenario": rng.choice(["warehouse", 42, None, "nope"])},
                {"scenario": "warehouse", "confidence": rng.random()},
                {rng.choice(list(FORBIDDEN_KEYS)): "x"},
            ])
            c = validate_scenario_choice(data, ["warehouse"])
            self.assertIsInstance(c.is_valid, bool)

    def test_resolution_serializes_cleanly(self):
        import json
        for sig in (ContextSignal(), ContextSignal(res_model="stock.picking"),
                    ContextSignal(menu_xmlid="stock.menu_stock_operations")):
            with self.subTest(sig=repr(sig)):
                out = resolve_scenario(sig, KNOWN).to_dict("zh_CN")
                json.dumps(out, ensure_ascii=False)   # must not raise

    def test_an_unresolved_context_is_not_an_error(self):
        """`needs_asking` is a plan, not a failure: every consumer must be
        able to proceed by asking."""
        r = resolve_scenario(ContextSignal(), KNOWN)
        self.assertFalse(r.is_resolved)
        self.assertTrue(r.needs_asking)
        self.assertIsNone(r.rejected_reason)


if __name__ == "__main__":
    unittest.main()
