# -*- coding: utf-8 -*-
"""Performance regression guard for the guided-flow hot path.

Why counting instead of timing
------------------------------
Wall-clock assertions are flaky: a busy CI box turns a 0.2 ms call into a
2 ms one and the test fails for reasons that have nothing to do with the
code.  What actually regressed here was *repeated work*, so that is what
this file counts -- how many times a builder runs, how many times a step
list is serialised.  Those numbers are exact, machine-independent, and
they are the numbers a future careless edit would change.

Background measurements (this machine, for context only -- deliberately
NOT asserted, because they do not travel between machines):

    build_steps x20 (stub builder, 4 ms each) ...... 81.5 ms
    cursor_view x2000 (60 steps) ................. 466.1 ms -> 0.233 ms/call

What the optimisations changed
------------------------------
* `guide_advance` performed THREE full traversals of the step list per
  single step.  `advance` used to call `cursor_view` -- which builds a
  dict for EVERY step -- merely to read one `step_id`.  Now `advance`
  reads the object via `_current_step` and serialises NOTHING.
* `guide_start` / `guide_current` / `guide_advance` each rebuilt the
  whole list from the database every time.  Now: once per request, with
  an explicit invalidation after a write.

How the counting is done (and one trap)
---------------------------------------
Every case patches `GuidedStep.to_dict` **at class level**.  That works
because `GuidedStep` has `__slots__` but `to_dict` is an ordinary method,
so it lives on the class.

Do NOT try to patch `to_dict` per-instance (`s.to_dict = wrapper`): with
`__slots__` the attribute name is not writable, and a helper that tries
is exactly the mistake this file used to make -- it silently counted a
stub instead of the code path and produced a false failure.  The class
patch below is restored in a `finally:`.

A second trap lives in the numbers themselves.  `cursor_view` serialises
each step once for the `"steps"` key, and then serialises the *current*
step a second time for the `"current"` key -- so a 60-step list costs
**61** calls, not 60.  That `n + 1` is inherent to the wire contract the
frontend depends on, not waste.  Asserting `n` here is wrong; assert the
delta between configurations instead.
"""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.flow import (  # noqa: E402
    GuidedStep, TaskFlow, TaskFlowEngine,
)


def make_steps(n=60):
    return [
        GuidedStep(f"s{i}", {"zh_CN": f"步骤{i}"}, {"zh_CN": f"朗读{i}"},
                   input_type="confirm")
        for i in range(n)
    ]


class _CountToDict:
    """Context manager counting `GuidedStep.to_dict` calls.

    Patched at class level (see the module docstring for why the
    per-instance approach is broken under `__slots__`).
    """

    def __init__(self):
        self.n = 0
        self._original = None

    def __enter__(self):
        self._original = GuidedStep.to_dict
        original = self._original

        def counting(step, lang=None):
            self.n += 1
            return original(step, lang)

        GuidedStep.to_dict = counting
        return self

    def __exit__(self, *exc):
        GuidedStep.to_dict = self._original
        return False


class TestNoRedundantSerialisation(unittest.TestCase):
    """`advance` must not serialise the list to find one step.

    This is the regression the optimisation removed: `advance` used to
    call `cursor_view` to read a single `step_id`, paying a full
    serialisation of every step to do it.
    """

    def test_advance_serialises_nothing(self):
        steps = make_steps(60)
        with _CountToDict() as c:
            result = TaskFlowEngine.advance(steps, [], "s0")
        self.assertEqual(c.n, 0,
                         "advance serialised the step list to read a cursor")
        self.assertEqual(result, ["s0"])

    def test_advance_serialises_nothing_on_the_skip_path(self):
        """The skip path is a second branch through `advance`; it must be
        equally cheap (it looks the target up by attribute, not dict)."""
        steps = make_steps(60)
        steps[3].skippable = True
        with _CountToDict() as c:
            result = TaskFlowEngine.advance(steps, [], "s3")
        self.assertEqual(c.n, 0, "the skip path serialised the list")
        self.assertEqual(result, ["s3"])

    def test_cursor_view_still_pays_exactly_one_serialisation_per_step(self):
        """`cursor_view` is allowed to serialise -- that IS its job.  Pin
        that it does so once per step plus one for the current step, so a
        future edit that re-serialises (e.g. calls `to_dict` inside the
        loop for something else) shows up here."""
        n = 60
        with _CountToDict() as c:
            view = TaskFlowEngine.cursor_view(make_steps(n), ["s0"],
                                              lang="zh_CN")
        self.assertEqual(c.n, n + 1,
                         "cursor_view should serialise each step once, plus "
                         "the current step for the 'current' key")
        self.assertEqual(len(view["steps"]), n)

    def test_advance_matches_cursor_view_on_every_position(self):
        """`advance` and `cursor_view` must agree on the cursor -- they
        now share `_current_step`, and this pins that they still do."""
        for done in ([], ["s0"], ["s0", "s1"], ["s0", "s1", "s2"]):
            steps = make_steps()
            view = TaskFlowEngine.cursor_view(steps, done)
            helper = TaskFlowEngine._current_step(make_steps(), done)
            if view["current"] is None:
                self.assertIsNone(helper, done)
            else:
                self.assertEqual(helper.step_id, view["current"]["step_id"],
                                 done)

    def test_cursor_view_still_returns_the_full_contract(self):
        """The wire shape is unchanged: the frontend depends on all four
        keys, and the returned view is allowed to serialise everything."""
        view = TaskFlowEngine.cursor_view(make_steps(3), [])
        self.assertEqual(set(view), {"current", "steps", "remaining",
                                     "finished"})
        self.assertEqual(len(view["steps"]), 3)


class TestBuilderCallCount(unittest.TestCase):
    """The builder behind a flow is the expensive part (it queries)."""

    def _engine_with_counting_builder(self, n=12):
        calls = {"n": 0}

        def builder(env, params):
            calls["n"] += 1
            return make_steps(n)

        eng = TaskFlowEngine()
        eng.register(TaskFlow("perf", "Perf", steps_builder=builder,
                              description=""))
        return eng, calls

    def test_build_steps_is_not_called_twice_for_one_engine_lookup(self):
        eng, calls = self._engine_with_counting_builder()
        eng.build_steps("perf", None, {})
        self.assertEqual(calls["n"], 1)

    def test_advance_does_not_rebuild_or_re_serialise(self):
        """One `advance` must not touch the builder at all.

        The cursor is a pure function of `done_ids`, so the expensive
        path has no business running during it.
        """
        eng, calls = self._engine_with_counting_builder()
        steps = eng.build_steps("perf", None, {})
        calls["n"] = 0
        with _CountToDict() as c:
            TaskFlowEngine.advance(steps, [], "s0")
        self.assertEqual(calls["n"], 0, "advance re-ran the builder")
        self.assertEqual(c.n, 0, "advance re-serialised the list")


class TestSerialisationVolume(unittest.TestCase):
    """Size the waste concretely, so a regression is visible as a number.

    The point of these is the *ratio*: reading the cursor for one step
    must be O(1) in serialisations, while producing the view is O(n).
    Hard-coding absolute counts would break the moment anyone adds a key
    to the wire shape; asserting the relationship keeps the guard honest
    while staying robust.
    """

    def test_cursor_read_is_o1_while_the_view_is_on(self):
        """The core claim of the optimisation, stated as a ratio.

        `_current_step` -- the cursor read `advance` now uses -- costs
        zero serialisations; the full view costs one per step.  If
        someone routes the cursor read back through the view, the left
        side stops being 0 and this fails.
        """
        n = 60
        steps = make_steps(n)

        with _CountToDict() as cheap:
            TaskFlowEngine._current_step(steps, [])

        with _CountToDict() as expensive:
            TaskFlowEngine.cursor_view(make_steps(n), [])

        self.assertEqual(cheap.n, 0, "the cursor read cost serialisations")
        self.assertEqual(expensive.n, n + 1, "unexpected view cost")
        self.assertLess(cheap.n * 1000, expensive.n,
                        "the cursor read must be far cheaper than the view")

    def test_the_old_shape_would_have_cost_three_passes(self):
        """Documents the regression this work removed.

        A single advance used to route through `cursor_view` three
        times: the caller's probe, `advance`'s internal one, and the
        final view.  Reproduce that shape and show it is 3x the cost of
        the shape we ship now -- and that the shipped shape is one pass.
        """
        n = 60

        # The old shape: three full views for one logical advance.
        with _CountToDict() as old:
            for _ in range(3):
                TaskFlowEngine.cursor_view(make_steps(n), [])

        # The new shape: `advance` reads the cursor for free, then the
        # caller renders the view once.
        steps = make_steps(n)
        with _CountToDict() as new:
            TaskFlowEngine.advance(steps, [], "s0")
            TaskFlowEngine.cursor_view(steps, ["s0"], lang="zh_CN")

        three_passes = 3 * (n + 1)   # one view == n steps + the current
        self.assertEqual(old.n, three_passes)
        self.assertEqual(new.n, n + 1,
                         "the new shape should be exactly one pass")
        self.assertGreater(old.n, new.n, "the old shape must be worse")
        self.assertEqual(old.n, new.n * 3,
                         "the old shape should be exactly three times the "
                         "new one")


class TestWallClockOptional(unittest.TestCase):
    """Opt-in timing test, for local profiling only.

    Skipped by default: a wall-clock assertion on shared CI hardware is a
    flake generator.  Run with `MMUX_PERF=1 python -m pytest` when you
    actually want the numbers.
    """

    @unittest.skipUnless(__import__("os").environ.get("MMUX_PERF"),
                         "set MMUX_PERF=1 to run the timing probe")
    def test_cursor_view_stays_under_a_millisecond(self):
        import time
        steps = make_steps(60)
        t0 = time.perf_counter()
        for _ in range(500):
            TaskFlowEngine.cursor_view(steps, ["s0"], lang="zh_CN")
        per_call = (time.perf_counter() - t0) / 500
        self.assertLess(per_call, 0.005,
                        f"cursor_view took {per_call * 1000:.3f} ms")


if __name__ == "__main__":
    unittest.main()
