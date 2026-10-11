# -*- coding: utf-8 -*-
"""Odoo-side tests for data-driven flow overrides (v19.0.6).

Needs a real Odoo runtime. Covers: override precedence over the code
flow, the fallback when no row exists, step validation, and the
mid-session recipe-snapshot semantics.
"""
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

CODE_FLOW = "wh_guided_picking"          # registered by the warehouse addon


@tagged("mmux", "post_install", "-at_install")
class TestFlowOverride(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Override = self.env["mmux.flow.override"]
        self.service = self.env["mmux.service"]

    def _row_vals(self, flow_id=CODE_FLOW, step_id="ovr_1"):
        return {
            "flow_id": flow_id,
            "name": {"zh_CN": "覆盖流程", "en_US": "Override flow"},
            "steps": [{
                "step_id": step_id,
                "title": {"zh_CN": "第一步", "en_US": "Step one"},
                "voice_prompt": {"zh_CN": "请执行第一步",
                                 "en_US": "Do step one"},
                "input_type": "confirm",
            }],
        }

    def tearDown(self):
        # never leak an override into other tests
        self.Override.search([]).unlink()
        super().tearDown()

    def test_override_row_wins_over_code(self):
        """`_flow_for` prefers the DB row; the built TaskFlow carries the
        row's steps, not the code builder's."""
        self.Override.create(self._row_vals())
        flow = self.service._flow_for(CODE_FLOW)
        self.assertIsNotNone(flow)
        steps = flow.steps_builder(self.env, {})
        self.assertEqual([s.step_id for s in steps], ["ovr_1"])

    def test_no_row_falls_back_to_code(self):
        flow = self.service._flow_for(CODE_FLOW)
        self.assertIsNotNone(flow)
        # the code flow is the warehouse picking flow: its builder needs
        # real records, so only assert the identity here, not the steps.
        self.assertEqual(flow.flow_id, CODE_FLOW)

    def test_unknown_flow_id_resolves_to_none(self):
        self.assertIsNone(self.service._flow_for("no_such_flow_xyz"))

    def test_guide_flows_merges_overrides(self):
        """The frontend menu lists code flows, with same-id override rows
        replacing them — and the order is stable (by flow_id)."""
        before = [f["flow_id"] for f in self.service.guide_flows()]
        self.assertIn(CODE_FLOW, before)
        self.Override.create(self._row_vals())
        after = [f["flow_id"] for f in self.service.guide_flows()]
        self.assertEqual(after, sorted(after), "menu order must be stable")
        self.assertIn(CODE_FLOW, after)

    def test_invalid_steps_are_rejected(self):
        bad_cases = [
            "not a list",
            [],
            [{"title": "no step_id"}],
            [{"step_id": "a", "title": "t", "voice_prompt": "v",
              "input_type": "bogus"}],
            [{"step_id": "a", "title": "t", "voice_prompt": "v"},
             {"step_id": "a", "title": "t2", "voice_prompt": "v2"}],
            [{"step_id": "a", "title": "t", "voice_prompt": "v",
              "capability_id": "no_such_capability_xyz"}],
        ]
        for steps in bad_cases:
            with self.assertRaises(ValidationError):
                self.Override.create(dict(self._row_vals(), steps=steps))

    def test_multilingual_texts_resolve(self):
        """{lang: text} fields flow through resolve_text like everywhere
        else — an override is not a second i18n mechanism."""
        self.Override.create(self._row_vals())
        flow = self.service._flow_for(CODE_FLOW)
        step = flow.steps_builder(self.env, {})[0]
        self.assertEqual(step.title, {"zh_CN": "第一步",
                                      "en_US": "Step one"})

    def test_recipe_snapshot_is_stable_mid_session(self):
        """The agent snapshots the recipe into the loop blob ONCE; an
        override edited afterwards must not shift the running script.
        (This pins the deliberate mid-task stability semantics.)"""
        self.Override.create(self._row_vals())
        blob = {"flow_id": CODE_FLOW, "params": {}, "skill_level": "novice"}
        obs = self.service._agent_observe(blob, "k1")
        first_recipe = blob["recipe"]
        # the admin now rewrites the override mid-session
        row = self.Override.search([("flow_id", "=", CODE_FLOW)])
        row.write({"steps": [{
            "step_id": "ovr_CHANGED",
            "title": "changed",
            "voice_prompt": "changed",
            "input_type": "confirm",
        }]})
        blob2 = {"flow_id": CODE_FLOW, "params": {}, "skill_level": "novice",
                 "recipe": first_recipe}
        self.service._agent_observe(blob2, "k1")
        # the existing snapshot was NOT re-rendered
        self.assertEqual(blob2["recipe"], first_recipe)
        # a NEW session sees the change
        blob3 = {"flow_id": CODE_FLOW, "params": {}, "skill_level": "novice"}
        self.service._agent_observe(blob3, "k2")
        self.assertIn("ovr_CHANGED",
                      [s.get("step_id") for s in blob3["recipe"]])
