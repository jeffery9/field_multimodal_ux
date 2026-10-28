# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Unit tests for the task queue / task loop layer (get -> do -> report ->
next)."""

import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.taskqueue import (  # noqa: E402
    TaskSource, Task, TaskQueueEngine, TASK_ENGINE,
    register_task_source, next_task, build_loop_state, loop_label,
    LOOP_STATES,
)


def src(source_id, scenario="warehouse", flow_id="wh_guided_picking",
        capability="picking_task_complete", model="stock.picking",
        priority_field="scheduled_date", domain=None):
    return TaskSource(source_id, scenario=scenario, task_model=model,
                      flow_id=flow_id, complete_capability=capability,
                      priority_field=priority_field,
                      domain=domain if domain is not None else [])


def task(task_id, source_id, scenario="warehouse", priority=10,
         flow_id="wh_guided_picking", capability="picking_task_complete"):
    res_id = int(str(task_id).split(":")[-1]) if ":" in str(task_id) else 1
    return Task(task_id, source_id, scenario, f"Task {task_id}", "stock.picking",
                res_id, flow_id, capability, priority=priority)


class TestTaskSource(unittest.TestCase):

    def test_requires_ids(self):
        with self.assertRaises(ValueError):
            TaskSource("", scenario="warehouse", task_model="stock.picking",
                       flow_id="f", complete_capability="c")
        with self.assertRaises(ValueError):
            TaskSource("s1", scenario="warehouse", task_model="",
                       flow_id="f", complete_capability="c")
        with self.assertRaises(ValueError):
            TaskSource("s1", scenario="warehouse", task_model="stock.picking",
                       flow_id="", complete_capability="c")
        with self.assertRaises(ValueError):
            TaskSource("s1", scenario="warehouse", task_model="stock.picking",
                       flow_id="f", complete_capability="")

    def test_static_domain_copied(self):
        d = [("state", "=", "assigned")]
        s = src("wh", domain=d)
        got = s.resolve_domain(None, None)
        self.assertEqual(got, d)
        got.append(("extra", "=", 1))
        self.assertEqual(len(s.resolve_domain(None, None)), 1)  # not mutated

    def test_dynamic_domain_called(self):
        s = src("wh", domain=lambda env, user: [("user_id", "=", 7)])
        self.assertEqual(s.resolve_domain(None, None), [("user_id", "=", 7)])

    def test_to_dict_localized(self):
        s = src("wh")
        s.label = {"zh_CN": "拣货单", "en_US": "Pickings"}
        self.assertEqual(s.to_dict("zh_CN")["label"], "拣货单")
        self.assertEqual(s.to_dict("en_US")["label"], "Pickings")
        self.assertEqual(s.to_dict("ja_JP")["label"], "Pickings")  # fallback


class TestEngineRegistry(unittest.TestCase):

    def test_register_and_lookup(self):
        eng = TaskQueueEngine()
        s = eng.register(src("wh"))
        self.assertIs(s, eng.get("wh"))
        self.assertEqual([x.source_id for x in eng.all()], ["wh"])
        self.assertIsNone(eng.get("nope"))

    def test_duplicate_source_rejected(self):
        eng = TaskQueueEngine()
        eng.register(src("wh"))
        with self.assertRaises(ValueError):
            eng.register(src("wh"))

    def test_for_scenario_filters(self):
        eng = TaskQueueEngine()
        eng.register(src("wh", scenario="warehouse"))
        eng.register(src("mfg", scenario="manufacturing", model="mrp.workorder",
                         flow_id="mfg_guided_workorder",
                         capability="wo_task_complete"))
        eng.register(src("any", scenario=""))
        self.assertEqual([s.source_id for s in eng.for_scenario("warehouse")],
                         ["wh", "any"])
        self.assertEqual(len(eng.for_scenario("")), 3)

    def test_registration_order_is_tie_breaker(self):
        eng = TaskQueueEngine()
        eng.register(src("a"))
        eng.register(src("b"))
        self.assertEqual(eng.rank("a"), 0)
        self.assertEqual(eng.rank("b"), 1)
        self.assertGreater(eng.rank("unknown"), 1)


class TestNextTask(unittest.TestCase):

    def test_empty_pool_returns_none(self):
        eng = TaskQueueEngine()
        eng.register(src("wh"))
        self.assertIsNone(eng.next_task([]))

    def test_lowest_priority_wins(self):
        eng = TaskQueueEngine()
        eng.register(src("wh"))
        pool = [task("wh:1", "wh", priority=30),
                task("wh:2", "wh", priority=10),
                task("wh:3", "wh", priority=20)]
        self.assertEqual(eng.next_task(pool).task_id, "wh:2")

    def test_same_priority_uses_source_registration_order(self):
        eng = TaskQueueEngine()
        eng.register(src("a", scenario="warehouse"))
        eng.register(src("b", scenario="warehouse"))
        pool = [task("b:1", "b", priority=5), task("a:1", "a", priority=5)]
        self.assertEqual(eng.next_task(pool).task_id, "a:1")

    def test_then_task_id_is_final_tie_breaker(self):
        eng = TaskQueueEngine()
        eng.register(src("wh"))
        pool = [task("wh:9", "wh", priority=1), task("wh:2", "wh", priority=1)]
        self.assertEqual(eng.next_task(pool).task_id, "wh:2")

    def test_only_source_filter(self):
        eng = TaskQueueEngine()
        eng.register(src("wh", scenario="warehouse"))
        eng.register(src("fs", scenario="field_service", model="project.task",
                         flow_id="fs_guided_service",
                         capability="service_task_complete"))
        pool = [task("wh:1", "wh", priority=1),
                task("fs:1", "fs", scenario="field_service", priority=50)]
        self.assertEqual(eng.next_task(pool, only_source="fs").task_id, "fs:1")
        # nothing pending for that source -> None (not a fallback to others)
        self.assertIsNone(eng.next_task(pool, only_source="mfg"))

    def test_selection_is_deterministic(self):
        eng = TaskQueueEngine()
        eng.register(src("wh"))
        pool = [task("wh:3", "wh", priority=2),
                task("wh:1", "wh", priority=2),
                task("wh:2", "wh", priority=2)]
        picks = {eng.next_task(pool).task_id for _ in range(5)}
        self.assertEqual(picks, {"wh:1"})

    def test_module_level_helper(self):
        pool = [task("wh:5", "wh", priority=4), task("wh:6", "wh", priority=1)]
        self.assertEqual(next_task(pool).task_id, "wh:6")


class TestLoopState(unittest.TestCase):

    def test_states_constant(self):
        self.assertEqual(LOOP_STATES,
                         ("idle", "task_active", "awaiting_confirmation",
                          "finished_all"))

    def test_idle_when_no_current_but_next_pending(self):
        st = build_loop_state(current=None, next_task=task("wh:1", "wh"))
        self.assertEqual(st["state"], "idle")
        self.assertFalse(st["has_current"])
        self.assertTrue(st["has_next"])

    def test_task_active_with_current(self):
        st = build_loop_state(current=task("wh:1", "wh"),
                              next_task=task("wh:2", "wh"))
        self.assertEqual(st["state"], "task_active")
        self.assertEqual(st["current_task_id"], "wh:1")
        self.assertEqual(st["next_task_id"], "wh:2")

    def test_awaiting_confirmation_wins(self):
        st = build_loop_state(current=task("wh:1", "wh"),
                              next_task=task("wh:2", "wh"),
                              awaiting_confirmation=True)
        self.assertEqual(st["state"], "awaiting_confirmation")

    def test_finished_all_when_nothing_left(self):
        st = build_loop_state(current=None, next_task=None, done_count=7)
        self.assertEqual(st["state"], "finished_all")
        self.assertEqual(st["done_count"], 7)

    def test_loop_label_localized(self):
        self.assertEqual(loop_label("task_active", "zh_CN"), "任务进行中")
        self.assertEqual(loop_label("task_active", "en_US"), "Task in progress")
        self.assertEqual(loop_label("finished_all", "zh_CN"), "当前没有待办任务")
        # unknown state degrades gracefully (never raises)
        self.assertEqual(loop_label("weird", "en_US"), "weird")


class TestTaskSerialization(unittest.TestCase):

    def test_to_dict_resolves_multilingual_title(self):
        t = task("wh:1", "wh")
        t.title = {"zh_CN": "拣货单 WH/OUT/001", "en_US": "Picking WH/OUT/001"}
        self.assertEqual(t.to_dict("zh_CN")["title"], "拣货单 WH/OUT/001")
        self.assertEqual(t.to_dict("en_US")["title"], "Picking WH/OUT/001")

    def test_to_dict_carries_flow_and_capability(self):
        t = task("wh:1", "wh")
        d = t.to_dict("en_US")
        self.assertEqual(d["flow_id"], "wh_guided_picking")
        self.assertEqual(d["complete_capability"], "picking_task_complete")
        self.assertEqual(d["res_model"], "stock.picking")
        self.assertEqual(d["key"] if "key" in d else d["task_id"], "wh:1")

    def test_global_engine_registers(self):
        self.assertIsInstance(TASK_ENGINE, TaskQueueEngine)
        eng = TaskQueueEngine()
        s = eng.register(src("probe"))
        self.assertEqual(s.source_id, "probe")


if __name__ == "__main__":
    unittest.main()


class TestLoopIntegrity(unittest.TestCase):
    """Contract checks that mirror what the Odoo layer relies on."""

    def _engine_with_four_sources(self):
        eng = TaskQueueEngine()
        eng.register(src("wh_picking", scenario="warehouse",
                         model="stock.picking", flow_id="wh_guided_picking",
                         capability="picking_task_complete",
                         priority_field="scheduled_date, id"))
        eng.register(src("mfg_workorder", scenario="manufacturing",
                         model="mrp.workorder", flow_id="mfg_guided_workorder",
                         capability="wo_task_complete",
                         priority_field="production_id, sequence"))
        eng.register(src("fs_task", scenario="field_service",
                         model="project.task", flow_id="fs_guided_service",
                         capability="service_task_complete",
                         priority_field="priority, date_deadline"))
        eng.register(src("sales_visit", scenario="sales_visit",
                         model="crm.lead", flow_id="sales_guided_visit",
                         capability="visit_task_complete",
                         priority_field="priority, date_deadline"))
        return eng

    def test_scenario_isolation(self):
        eng = self._engine_with_four_sources()
        self.assertEqual([s.source_id for s in eng.for_scenario("warehouse")],
                         ["wh_picking"])
        self.assertEqual(
            [s.source_id for s in eng.for_scenario("field_service")],
            ["fs_task"])
        self.assertEqual(len(eng.for_scenario("")), 4)

    def test_priority_field_documented_per_source(self):
        eng = self._engine_with_four_sources()
        pf = {s.source_id: s.priority_field for s in eng.all()}
        # mirrors the real manifests: warehouse by scheduled date, mfg by
        # MO + operation sequence, the rest by priority then deadline
        self.assertIn("scheduled_date", pf["wh_picking"])
        self.assertIn("sequence", pf["mfg_workorder"])
        self.assertIn("priority", pf["fs_task"])
        self.assertIn("priority", pf["sales_visit"])
        # a source must declare *some* ordering
        self.assertTrue(all(v for v in pf.values()))

    def test_cross_scenario_pick_respects_filter(self):
        """即便全局池里有更高优先级任务，只取指定来源也必须遵守。"""
        eng = self._engine_with_four_sources()
        pool = [task("sales_visit:1", "sales_visit", scenario="sales_visit",
                     priority=1),
                task("wh_picking:1", "wh_picking", priority=99)]
        self.assertEqual(eng.next_task(pool).task_id, "sales_visit:1")
        self.assertEqual(
            eng.next_task(pool, only_source="wh_picking").task_id,
            "wh_picking:1")

    def test_relative_priority_ordering(self):
        """数字优先级越小越先被选中（紧迫/早到期在前）。"""
        eng = self._engine_with_four_sources()
        pool = [task("wh_picking:1", "wh_picking", priority=100),
                task("wh_picking:2", "wh_picking", priority=5),
                task("wh_picking:3", "wh_picking", priority=50)]
        order = []
        while pool:
            pick = eng.next_task(pool)
            order.append(pick.task_id)
            pool.remove(pick)
        self.assertEqual(order, ["wh_picking:2", "wh_picking:3",
                                 "wh_picking:1"])
