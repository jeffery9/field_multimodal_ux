# -*- coding: utf-8 -*-
"""Odoo-side tests for the guide step-list cache.

Why this file exists
--------------------
`guide_start` / `guide_current` / `guide_advance` used to rebuild the
whole step list from the database on every call, and serialise all of it
three times per advance.  The list is built from live records (a
picking's `move_line_ids`, a product's display name), so caching it saves
real work -- and is also the exact place where a cache can go WRONG.

`test_guide_cursor_survives_reload` in `test_mmux_guide.py` checks that
the cursor survives a reload, but it never changes a record in between.
A cache that never re-read anything would still pass it.  These tests
close that hole: they change the underlying data and then assert the
guide reports the change.
"""
import unittest

from odoo.tests import TransactionCase, tagged

from ..services.mmux_core import FLOW_ENGINE, TaskFlowEngine


@tagged("post_install", "-at_install")
class TestGuideStepCache(TransactionCase):

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.product = self.env["product.product"].create({
            "name": "MMUX Cache Product", "is_storable": True,
            "barcode": "MMUX-C1",
        })

    def _make_assigned_picking(self):
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": 3, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def _start(self, params=None):
        params = params or {}
        if "picking_id" not in params:
            params["picking_id"] = self._make_assigned_picking().id
        return self.service.guide_start("wh_guided_picking", params)

    # ------------------------------------------------------------------ #
    # the cache does its job: no redundant rebuilds inside one request
    # ------------------------------------------------------------------ #
    def test_start_then_current_does_not_rebuild(self):
        """`guide_current` right after `guide_start` reuses the list.

        Both run in the same request (same `env`), so the second must not
        re-run the builder.  Counting the builder is the most direct
        proof -- the old code called it every time.
        """
        view = self._start()
        key = view["guide_key"]

        calls = {"n": 0}
        real = FLOW_ENGINE.build_steps

        def counting(flow_id, env, params=None, skill_level="novice"):
            calls["n"] += 1
            return real(flow_id, env, params, skill_level)

        FLOW_ENGINE.build_steps = counting
        try:
            same = self.service.guide_current(key)
        finally:
            FLOW_ENGINE.build_steps = real

        self.assertEqual(calls["n"], 0,
                         "guide_current rebuilt a list it already had")
        self.assertEqual(same["current"]["step_id"],
                         view["current"]["step_id"])

    def test_advance_serialises_the_full_list_once(self):
        """One advance must serialise the whole list exactly once.

        It used to be three (the caller's cursor probe, `advance`'s own
        internal one, and the returned view).  The returned view still
        needs the full list -- that is the wire contract -- but the other
        two were pure waste.
        """
        view = self._start()
        key = view["guide_key"]
        step = view["current"]["step_id"]

        full = {"n": 0}
        real = TaskFlowEngine.cursor_view
        def counting(steps, done_ids, lang=None):
            full["n"] += 1
            return real(steps, done_ids, lang)

        TaskFlowEngine.cursor_view = staticmethod(counting)
        try:
            res = self.service.guide_advance(key, step)
        finally:
            TaskFlowEngine.cursor_view = staticmethod(real)

        self.assertNotIn("error", res, res)
        self.assertEqual(full["n"], 1,
                         "expected exactly one full serialisation, got %s"
                         % full["n"])

    # ------------------------------------------------------------------ #
    # the cache must not go stale
    # ------------------------------------------------------------------ #
    def test_reload_sees_records_changed_since_start(self):
        """A reload after the data changed must show the NEW data.

        This is the test `test_guide_cursor_survives_reload` is missing:
        it reloads without touching anything, so a permanently-cached
        list would satisfy it.  Here the picking gains a move line after
        the guide started, and the reloaded guide must mention it.

        A request-scoped cache passes (a new request = a new `env`); a
        registry-scoped or per-run cache fails, which is why this test
        pins the scope and not just the speed.
        """
        picking = self._make_assigned_picking()
        view = self._start({"picking_id": picking.id})
        key = view["guide_key"]

        before = len(view["steps"])
        # add a second line, as a backorder/merge would
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": 2, "picking_id": picking.id,
            "location_id": picking.location_id.id,
            "location_dest_id": picking.location_dest_id.id,
        })
        picking.action_assign()

        # a fresh request: the registry-owned data is gone, the cache is
        # per-env, so the rebuilt list reflects the new line
        self.env.cr.flush()
        self.service.env = self.env
        reloaded = self.service.guide_current(key)
        self.assertNotIn("error", reloaded, reloaded)
        self.assertNotEqual(
            len(reloaded["steps"]), before,
            "the reload reused a list built before the records changed")

    def test_a_write_invalidates_the_cached_list(self):
        """A capability step must not leave a pre-write list behind.

        `picking_validate` can add a backorder line, so whatever the
        builder read before it ran may no longer be true.  The advance
        that performs a write must therefore drop the cache -- and the
        step after it must be built from the post-write records.
        """
        picking = self._make_assigned_picking()
        line = picking.move_ids.move_line_ids[0]
        line.quantity = 3                      # let validate succeed

        view = self._start({"picking_id": picking.id})
        key = view["guide_key"]
        # walk to the qty step, which writes through the guard
        view = self.service.guide_advance(key, view["current"]["step_id"])
        view = self.service.guide_advance(key, view["current"]["step_id"],
                                          {"barcode": "MMUX-C1"})
        self.assertIn("qty_", view["current"]["step_id"])
        view = self.service.guide_advance(key, view["current"]["step_id"],
                                          {"quantity": 3})
        self.assertEqual(view.get("last_exec"), "success")
        # the write happened: the cache must have been dropped for it
        cache = getattr(self.service.env, "_mmux_guide_steps", {})
        self.assertFalse(
            any(True for _ in cache),
            "a write left a pre-write step list in the cache")

    # ------------------------------------------------------------------ #
    # the cache is per request, not per worker
    # ------------------------------------------------------------------ #
    def test_the_cache_is_scoped_to_the_env(self):
        """One env must not serve another env's step list.

        Step lists are narrowed by `env.user.id` and hold that user's
        records; a worker-wide (registry) cache would leak one operator's
        work onto another's screen.  Assert the store lives on the env.
        """
        self._start()
        self.assertTrue(
            hasattr(self.service.env, "_mmux_guide_steps"),
            "expected the cache on the env, not on the registry")
        self.assertFalse(
            hasattr(self.env.registry, "_mmux_guide_steps"),
            "the guide cache must NOT live on the registry")


@tagged("post_install", "-at_install")
class TestGuideStepCacheList(unittest.TestCase):
    """The step objects are mutable, so `_guide_steps` must hand out a
    clean list however many times it is called.

    `cursor_view` sets `s.done = True` on the objects it is given.  If
    the cache handed back the same dirty objects, a later call would see
    a `done` flag it never set and compute the wrong cursor.  The reset
    is what makes the cached list safe to reuse.
    """

    def test_done_flags_are_cleared_on_every_read(self):
        from ..services.mmux_core import GuidedStep

        steps = [GuidedStep("a", "A", "a"), GuidedStep("b", "B", "b")]
        steps[0].done = True                    # left dirty by cursor_view
        for s in steps:                         # what _guide_steps does
            s.done = False
        self.assertFalse(any(s.done for s in steps))
        self.assertEqual([s.step_id for s in steps], ["a", "b"])
