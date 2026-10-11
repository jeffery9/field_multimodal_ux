# -*- coding: utf-8 -*-
"""Unit tests for the guided task flow engine (voice navigation)."""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.flow import (  # noqa: E402
    GuidedStep, TaskFlow, TaskFlowEngine, FLOW_ENGINE,
)


def make_steps():
    return [
        GuidedStep("s1", "到达拣货单", "请前往拣货位，处理拣货单 WH/OUT/001",
                   input_type="confirm", skippable=True),
        GuidedStep("s2", "扫描商品", "前往库位 A-01，扫描商品 MMUX-1",
                   input_type="scan", capability_id="stock_query_product"),
        GuidedStep("s3", "报数", "请口述拣货数量，需求 5 个",
                   input_type="voice_qty", capability_id="picking_report_qty"),
        GuidedStep("s4", "验证", "明细录入完毕，请确认后验证过账",
                   input_type="capability", capability_id="picking_validate",
                   skippable=True),
    ]


class TestSkillLevels(unittest.TestCase):
    """Novice vs expert: same capabilities, different granularity."""

    def _dual_flow(self):
        def novice(env, params):
            return [
                GuidedStep("s1", "到达", "请前往库位 A-01，到了点确认。",
                           input_type="confirm", hint="库位 A-01"),
                GuidedStep("s2", "扫描", "请扫描商品条码。", input_type="scan",
                           capability_id="stock_query_product",
                           expected="MMUX-1"),
                GuidedStep("s3", "报数", "请口述数量，建议 5 个。",
                           input_type="voice_qty",
                           capability_id="picking_report_qty"),
                GuidedStep("s4", "复核", "请核对数量。", input_type="confirm"),
            ]

        def expert(env, params):
            return [
                GuidedStep("e1", "A-01 / MMUX-1", "A-01，MMUX-1，5 个。",
                           input_type="voice_qty",
                           capability_id="picking_report_qty"),
            ]

        return TaskFlow("dual", "Dual", steps_builder_novice=novice,
                        steps_builder_expert=expert)

    def test_flow_requires_builder(self):
        with self.assertRaises(ValueError):
            TaskFlow("bad", "Bad")

    def test_supports_skill_levels_flag(self):
        flow = self._dual_flow()
        self.assertTrue(flow.supports_skill_levels)
        single = TaskFlow("s", "S", lambda e, p: [GuidedStep("a", "t", "p")])
        self.assertFalse(single.supports_skill_levels)

    def test_novice_more_granular_than_expert(self):
        engine = TaskFlowEngine()
        engine.register(self._dual_flow())
        novice = engine.build_steps("dual", None, skill_level="novice")
        expert = engine.build_steps("dual", None, skill_level="expert")
        self.assertGreater(len(novice), len(expert))
        self.assertEqual(len(novice), 4)
        self.assertEqual(len(expert), 1)

    def test_both_levels_use_same_capabilities(self):
        engine = TaskFlowEngine()
        engine.register(self._dual_flow())
        novice = engine.build_steps("dual", None, skill_level="novice")
        expert = engine.build_steps("dual", None, skill_level="expert")
        cap_n = {s.capability_id for s in novice if s.capability_id}
        cap_e = {s.capability_id for s in expert if s.capability_id}
        # expert merges steps but must not gain/lose capability authority
        self.assertTrue(cap_e.issubset(cap_n))

    def test_novice_prompts_longer_with_hints(self):
        engine = TaskFlowEngine()
        engine.register(self._dual_flow())
        novice = engine.build_steps("dual", None, skill_level="novice")
        expert = engine.build_steps("dual", None, skill_level="expert")
        # total spoken text is longer in novice mode (more explanation),
        # even though expert prompts are individually more compact
        total_n = sum(len(s.voice_prompt) for s in novice)
        total_e = sum(len(s.voice_prompt) for s in expert)
        self.assertGreater(total_n, total_e)
        self.assertTrue(any(s.hint for s in novice))
        self.assertTrue(any(s.expected for s in novice))

    def test_skill_tagged_on_steps(self):
        engine = TaskFlowEngine()
        engine.register(self._dual_flow())
        steps = engine.build_steps("dual", None, skill_level="expert")
        self.assertEqual(steps[0].skill, "expert")
        # unknown level falls back to novice
        steps = engine.build_steps("dual", None, skill_level="wizard")
        self.assertEqual(steps[0].skill, "novice")

    def test_single_builder_fallback(self):
        engine = TaskFlowEngine()
        engine.register(TaskFlow("s", "S", lambda e, p: [GuidedStep("a", "t", "p")]))
        for lvl in ("novice", "expert"):
            steps = engine.build_steps("s", None, skill_level=lvl)
            self.assertEqual(len(steps), 1)


class TestCursorSharedHelper(unittest.TestCase):
    """`_current_step` is the single definition of "where am I".

    It exists because `advance` used to call `cursor_view` merely to read
    the current step's id -- paying for a full serialization of every
    step (titles, prompts, hints, in the requested language) to answer a
    question about one of them.  These tests pin the helper's agreement
    with `cursor_view` and the side-effect difference between the two.
    """

    def test_helper_agrees_with_cursor_view(self):
        for done in ([], ["s1"], ["s1", "s2"], ["s1", "s2", "s3"],
                     ["s1", "s2", "s3", "s4"]):
            steps = make_steps()
            view = TaskFlowEngine.cursor_view(steps, done)
            cur = TaskFlowEngine._current_step(make_steps(), done)
            if view["current"] is None:
                self.assertIsNone(cur, f"done={done}")
            else:
                self.assertEqual(cur.step_id, view["current"]["step_id"],
                                 f"done={done}")

    def test_helper_returns_the_object_not_a_dict(self):
        cur = TaskFlowEngine._current_step(make_steps(), [])
        self.assertIsInstance(cur, GuidedStep)
        self.assertEqual(cur.step_id, "s1")

    def test_helper_is_none_when_finished(self):
        self.assertIsNone(
            TaskFlowEngine._current_step(make_steps(), ["s1", "s2", "s3", "s4"]))

    def test_helper_does_not_mark_done(self):
        """The helper must not write the `done` flag.

        `cursor_view` owns that flag; if the helper also wrote it, its
        callers would inherit a mutation they did not ask for.
        """
        steps = make_steps()
        TaskFlowEngine._current_step(steps, ["s1"])
        self.assertFalse(any(s.done for s in steps))

    def test_advance_has_no_side_effects(self):
        """`advance` must not mutate the steps it is handed.

        Regression guard: it previously reached `cursor_view`, which sets
        `s.done = True` as a side effect.  A caller caching a step list
        (the guide does) would then find it pre-marked on the next use.
        """
        steps = make_steps()
        before = [s.done for s in steps]
        TaskFlowEngine.advance(steps, ["s1"], "s2")
        self.assertEqual([s.done for s in steps], before)
        self.assertFalse(any(s.done for s in steps))

    def test_advance_keeps_its_errors(self):
        """The two failure modes keep their exact wording."""
        steps = make_steps()
        with self.assertRaises(ValueError) as ctx:
            TaskFlowEngine.advance(steps, [], "s3")
        self.assertIn("is not the current step", str(ctx.exception))
        done = ["s1", "s2", "s3", "s4"]
        with self.assertRaises(ValueError) as ctx:
            TaskFlowEngine.advance(make_steps(), done, "s4")
        self.assertEqual(str(ctx.exception), "flow already finished")


class TestFlowEngine(unittest.TestCase):
    def test_register_and_build(self):
        engine = TaskFlowEngine()
        flow = TaskFlow("f1", "F1", lambda env, p: make_steps())
        engine.register(flow)
        steps = engine.build_steps("f1", None, {})
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0].input_type, "confirm")
        with self.assertRaises(KeyError):
            engine.build_steps("nope", None, {})

    def test_duplicate_flow_rejected(self):
        engine = TaskFlowEngine()
        engine.register(TaskFlow("f1", "F1", lambda e, p: []))
        with self.assertRaises(ValueError):
            engine.register(TaskFlow("f1", "F1", lambda e, p: []))

    def test_invalid_input_type_rejected(self):
        with self.assertRaises(ValueError):
            GuidedStep("s", "t", "p", input_type="telepathy")

    def test_cursor_starts_at_first(self):
        steps = make_steps()
        view = TaskFlowEngine.cursor_view(steps, [])
        self.assertEqual(view["current"]["step_id"], "s1")
        self.assertEqual(view["remaining"], 4)
        self.assertFalse(view["finished"])

    def test_advance_in_order_only(self):
        steps = make_steps()
        done = TaskFlowEngine.advance(steps, [], "s1")
        self.assertEqual(done, ["s1"])
        # cannot jump to s3 while s2 is current
        with self.assertRaises(ValueError):
            TaskFlowEngine.advance(steps, done, "s3")
        done = TaskFlowEngine.advance(steps, done, "s2")
        self.assertEqual(done, ["s1", "s2"])
        view = TaskFlowEngine.cursor_view(steps, done)
        self.assertEqual(view["current"]["step_id"], "s3")

    def test_skippable_only_when_declared(self):
        steps = make_steps()
        # completing the CURRENT step is always allowed (no skip needed)
        done = TaskFlowEngine.advance(steps, ["s1"], "s2", skippable_ok=False)
        self.assertEqual(done, ["s1", "s2"])
        # jumping PAST the current step to a non-skippable later step fails
        with self.assertRaises(ValueError):
            TaskFlowEngine.advance(steps, ["s1"], "s3", skippable_ok=True)
        # s4 IS skippable
        done = ["s1", "s2", "s3"]
        done = TaskFlowEngine.advance(steps, done, "s4", skippable_ok=True)
        view = TaskFlowEngine.cursor_view(steps, done)
        self.assertTrue(view["finished"])
        self.assertEqual(view["remaining"], 0)

    def test_finished_view_has_no_current(self):
        steps = [GuidedStep("s1", "t", "p")]
        view = TaskFlowEngine.cursor_view(steps, ["s1"])
        self.assertIsNone(view["current"])
        self.assertTrue(view["finished"])
        with self.assertRaises(ValueError):
            TaskFlowEngine.advance(steps, ["s1"], "s1")

    def test_global_registry(self):
        self.assertIsInstance(FLOW_ENGINE, TaskFlowEngine)
        self.assertTrue(hasattr(FLOW_ENGINE, "register"))


# ===================================================================== #
# v3.0: the flow demoted to a recipe the agent can fall back to
# ===================================================================== #
class TestTaskFlowRecipe(unittest.TestCase):
    """`TaskFlow.recipe()` is the deterministic script the pure agent loop
    degrades to. It must be data (dicts), must not need registration, and
    must never raise — an empty recipe is a valid answer, a crash is not.
    """

    @staticmethod
    def _flow(steps=None, crash=False):
        if crash:
            def builder(env, params):
                raise ValueError("half-built record")
        else:
            def builder(env, params):
                return [GuidedStep(f"s{i}", f"t{i}", f"say {i}",
                                   input_type="confirm")
                        for i in range(1, (steps or 2) + 1)]
        return TaskFlow("f_recipe", "Recipe", steps_builder=builder)

    def test_recipe_returns_plain_dicts(self):
        r = self._flow().recipe(None)
        self.assertIsInstance(r, list)
        for item in r:
            self.assertIsInstance(item, dict)
            self.assertIn("step_id", item)
            self.assertIn("voice_prompt", item)

    def test_recipe_does_not_need_registration(self):
        # the flow is never registered with the engine in this test
        r = self._flow(steps=3).recipe(None)
        self.assertEqual([s["step_id"] for s in r], ["s1", "s2", "s3"])

    def test_recipe_matches_build_steps_output(self):
        """The two paths share one implementation — they must not drift."""
        flow = self._flow(steps=2)
        registered = TaskFlowEngine()
        registered.register(flow)
        via_engine = [s.to_dict() for s in
                      registered.build_steps("f_recipe", None)]
        self.assertEqual(flow.recipe(None), via_engine)

    def test_crashing_builder_yields_empty_recipe(self):
        """A scenario builder that raises on an incomplete record must not
        take the fallback down with it."""
        self.assertEqual(self._flow(crash=True).recipe(None), [])

    def test_empty_builder_yields_empty_recipe(self):
        flow = TaskFlow("f_empty", "E", steps_builder=lambda env, p: [])
        self.assertEqual(flow.recipe(None), [])

    def test_recipe_tags_the_skill_level(self):
        flow = TaskFlow(
            "f_skill", "S",
            steps_builder_novice=lambda env, p: [GuidedStep("n1", "t", "p")],
            steps_builder_expert=lambda env, p: [GuidedStep("e1", "t", "p")])
        self.assertEqual(flow.recipe(None, skill_level="novice")[0]["skill"],
                         "novice")
        self.assertEqual(flow.recipe(None, skill_level="expert")[0]["skill"],
                         "expert")

    def test_recipe_is_read_only(self):
        """Calling it twice must yield the same script (no hidden state)."""
        flow = self._flow(steps=2)
        self.assertEqual(flow.recipe(None), flow.recipe(None))

    def test_build_steps_for_matches_id_lookup(self):
        flow = self._flow(steps=2)
        eng = TaskFlowEngine()
        eng.register(flow)
        self.assertEqual(
            [s.to_dict() for s in TaskFlowEngine.build_steps_for(flow, None)],
            [s.to_dict() for s in eng.build_steps("f_recipe", None)])

    def test_build_steps_still_raises_on_unknown_flow(self):
        # the id-keyed path keeps its contract
        with self.assertRaises(KeyError):
            TaskFlowEngine().build_steps("nope", None)


if __name__ == "__main__":
    unittest.main()
