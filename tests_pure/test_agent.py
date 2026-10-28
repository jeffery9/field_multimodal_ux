# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Unit tests for the agent task-selection layer (`agent.py`).

The agent is the security-critical new surface: it is the only place
where a model's output can influence *which record* the operator is sent
to. These tests therefore focus less on "does it pick the right task" and
more on the invariants that must never break:

  * a selection outside the server's candidate set is REJECTED;
  * `alternatives` are filtered individually, never trusted;
  * a protocol violation is caught with the same blacklist as `intent.py`;
  * a malformed answer never raises and never blocks the loop;
  * the fallback reproduces the queue ordering exactly.

Everything here runs without an Odoo runtime (pure core only).
"""

import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.agent import (  # noqa: E402
    AgentPick, validate_pick, fallback_pick,
    SOURCE_LLM, SOURCE_FALLBACK, SOURCE_EMPTY,
    MAX_ALTERNATIVES, REASON_LANG_KEYS, MAX_REASON_LEN,
)
from mmux_core.intent import FORBIDDEN_KEYS  # noqa: E402
from mmux_core.taskqueue import Task, TaskQueueEngine  # noqa: E402


def task(task_id, source_id="wh_picking", scenario="warehouse", priority=10):
    res_id = int(str(task_id).split(":")[-1]) if ":" in str(task_id) else 1
    return Task(task_id, source_id, scenario, f"Task {task_id}",
                "stock.picking", res_id, "wh_guided_picking",
                "picking_task_complete", priority=priority)


def two():
    # t_urgent sorts first in the queue (lower priority value)
    return [task("wh:1", priority=5), task("wh:2", priority=1)]


# ===================================================================== #
# accepted picks
# ===================================================================== #
class TestValidPick(unittest.TestCase):

    def test_accepts_member_of_candidates(self):
        p = validate_pick({"selected_task_id": "wh:2"}, two())
        self.assertTrue(p.is_valid)
        self.assertTrue(p.is_autonomous)
        self.assertEqual(p.source, SOURCE_LLM)
        self.assertEqual(p.task_id, "wh:2")
        self.assertIsNone(p.rejected_reason)

    def test_confidence_clamped(self):
        self.assertEqual(
            validate_pick({"selected_task_id": "wh:1", "confidence": 5},
                          two()).confidence, 1.0)
        self.assertEqual(
            validate_pick({"selected_task_id": "wh:1", "confidence": -3},
                          two()).confidence, 0.0)
        self.assertEqual(
            validate_pick({"selected_task_id": "wh:1", "confidence": "n/a"},
                          two()).confidence, 0.0)

    def test_reason_keeps_whitelisted_keys_only(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "reason": {"zh_CN": "客户加急", "en_US": "customer rush",
                        "xx_XX": "junk", "note": "junk2"}},
            two())
        self.assertEqual(p.reason, {"zh_CN": "客户加急", "en_US": "customer rush"})
        # nothing outside the whitelist survives into the audit payload
        self.assertTrue(set(p.reason).issubset(set(REASON_LANG_KEYS)))

    def test_reason_accepts_plain_string(self):
        p = validate_pick({"selected_task_id": "wh:1", "reason": "最紧急"},
                          two())
        self.assertEqual(p.reason, {"default": "最紧急"})

    def test_reason_bounded_length(self):
        p = validate_pick(
            {"selected_task_id": "wh:1", "reason": {"en_US": "x" * 5000}},
            two())
        self.assertEqual(len(p.reason["en_US"]), MAX_REASON_LEN)

    def test_localizes_reason_on_serialize(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "reason": {"zh_CN": "更紧急", "en_US": "more urgent"}}, two())
        self.assertEqual(p.to_dict("zh_CN")["reason"], "更紧急")
        self.assertEqual(p.to_dict("en_US")["reason"], "more urgent")
        # the raw mapping is preserved for the audit trail
        self.assertEqual(p.to_dict("zh_CN")["reason_i18n"]["en_US"],
                         "more urgent")

    def test_reason_falls_back_when_language_absent(self):
        p = validate_pick({"selected_task_id": "wh:1",
                           "reason": {"zh_CN": "只有中文"}}, two())
        self.assertEqual(p.to_dict("fr_FR")["reason"], "只有中文")


# ===================================================================== #
# rejected picks -- the security anchor
# ===================================================================== #
class TestRejectedPick(unittest.TestCase):

    def test_hallucinated_task_id_rejected(self):
        p = validate_pick({"selected_task_id": "wh:9999"}, two())
        self.assertFalse(p.is_valid)
        self.assertFalse(p.is_autonomous)
        self.assertIn("task_not_in_candidates", p.rejected_reason)

    def test_task_id_from_another_user_hidden(self):
        # a real id that simply is not among THIS user's visible candidates
        p = validate_pick({"selected_task_id": "wh:777"}, two())
        self.assertFalse(p.is_valid)
        self.assertEqual(p.rejected_reason,
                         "task_not_in_candidates:wh:777")

    def test_non_dict_rejected(self):
        for bad in (None, [], "wh:1", 42, True):
            with self.subTest(bad=bad):
                p = validate_pick(bad, two())
                self.assertFalse(p.is_valid)
                self.assertEqual(p.rejected_reason, "not_a_mapping")

    def test_missing_task_id_rejected(self):
        p = validate_pick({"reason": {"en_US": "hi"}}, two())
        self.assertFalse(p.is_valid)
        self.assertEqual(p.rejected_reason, "missing_selected_task_id")

    def test_null_task_id_rejected(self):
        p = validate_pick({"selected_task_id": None}, two())
        self.assertFalse(p.is_valid)
        self.assertEqual(p.rejected_reason, "missing_selected_task_id")

    def test_non_string_task_id_rejected(self):
        # an integer id must NOT be coerced -- coercion is guessing
        p = validate_pick({"selected_task_id": 1}, two())
        self.assertFalse(p.is_valid)
        self.assertEqual(p.rejected_reason, "missing_selected_task_id")

    def test_empty_candidates_is_empty_source(self):
        p = validate_pick({"selected_task_id": "wh:1"}, [])
        self.assertFalse(p.is_valid)
        self.assertEqual(p.source, SOURCE_EMPTY)
        self.assertEqual(p.rejected_reason, "no_candidates")

    def test_every_forbidden_key_rejected(self):
        """Protocol violations use the SAME blacklist as the intent path."""
        for key in sorted(FORBIDDEN_KEYS):
            with self.subTest(key=key):
                p = validate_pick(
                    {"selected_task_id": "wh:1", key: "anything"}, two())
                self.assertFalse(p.is_valid)
                self.assertTrue(
                    p.rejected_reason.startswith("protocol_violation:"), 
                    p.rejected_reason)
                self.assertIn(key, p.rejected_reason)

    def test_never_raises_on_hostile_input(self):
        hostile = [
            {"selected_task_id": "wh:1", "alternatives": "not-a-list"},
            {"selected_task_id": "wh:1", "reason": 12345},
            {"selected_task_id": "wh:1", "confidence": object()},
            {"selected_task_id": "wh:1", "alternatives": [None, 1, "x", {}]},
            {"selected_task_id": "wh:1", "reason": {"en_US": 7}},
        ]
        for payload in hostile:
            with self.subTest(payload=str(payload)[:40]):
                p = validate_pick(payload, two())
                self.assertTrue(p.is_valid)   # tolerant on non-decision keys


# ===================================================================== #
# only_source scoping
# ===================================================================== #
class TestOnlySourceScope(unittest.TestCase):

    def _mixed(self):
        return [task("wh:1", "wh_picking"), task("mfg:1", "mfg_workorder",
                                                 scenario="manufacturing")]

    def test_selection_outside_only_source_rejected(self):
        p = validate_pick({"selected_task_id": "mfg:1"}, self._mixed(),
                          only_source="wh_picking")
        self.assertFalse(p.is_valid)
        # scoping means the mfg task is not in the candidate set at all --
        # and because wh:1 IS in scope, this is a membership rejection
        # (not an empty-pool case), which is the more useful diagnosis.
        self.assertEqual(p.source, SOURCE_FALLBACK)
        self.assertIn("task_not_in_candidates", p.rejected_reason)

    def test_only_source_with_no_pool_is_empty(self):
        p = validate_pick({"selected_task_id": "wh:1"}, self._mixed(),
                          only_source="fs_task")
        self.assertFalse(p.is_valid)
        self.assertEqual(p.source, SOURCE_EMPTY)

    def test_selection_inside_only_source_accepted(self):
        p = validate_pick({"selected_task_id": "wh:1"}, self._mixed(),
                          only_source="wh_picking")
        self.assertTrue(p.is_valid)

    def test_alternatives_respect_only_source(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "alternatives": [{"task_id": "mfg:1", "why_not": "leak"}]},
            self._mixed(), only_source="wh_picking")
        self.assertTrue(p.is_valid)
        self.assertEqual(p.alternatives, [])   # cross-source alt dropped


# ===================================================================== #
# alternatives -- the "display-only" field that must still be validated
# ===================================================================== #
class TestAlternatives(unittest.TestCase):

    def test_out_of_set_alternative_dropped_not_fatal(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "alternatives": [
                 {"task_id": "wh:2", "why_not": "later"},
                 {"task_id": "wh:999", "why_not": "made up"},
             ]},
            two())
        self.assertTrue(p.is_valid)                    # primary survives
        self.assertEqual([a["task_id"] for a in p.alternatives], ["wh:2"])

    def test_alternatives_capped(self):
        cands = [task(f"wh:{i}") for i in range(1, 12)]
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "alternatives": [{"task_id": f"wh:{i}", "why_not": "x"}
                              for i in range(2, 12)]},
            cands)
        self.assertEqual(len(p.alternatives), MAX_ALTERNATIVES)

    def test_alternative_equal_to_selection_dropped(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "alternatives": [{"task_id": "wh:1", "why_not": "self"}]},
            two())
        self.assertEqual(p.alternatives, [])

    def test_duplicate_alternatives_deduped(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "alternatives": [{"task_id": "wh:2", "why_not": "a"},
                              {"task_id": "wh:2", "why_not": "b"}]},
            two())
        self.assertEqual(len(p.alternatives), 1)

    def test_why_not_localized(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "alternatives": [{"task_id": "wh:2",
                               "why_not": {"zh_CN": "计划更晚",
                                           "en_US": "scheduled later"}}]},
            two())
        self.assertEqual(p.to_dict("zh_CN")["alternatives"][0]["why_not"],
                         "计划更晚")
        self.assertEqual(p.to_dict("en_US")["alternatives"][0]["why_not"],
                         "scheduled later")

    def test_malformed_alternative_entries_ignored(self):
        p = validate_pick(
            {"selected_task_id": "wh:1",
             "alternatives": [None, 5, "wh:2", {}, {"why_not": "no id"},
                              {"task_id": 7, "why_not": "bad type"}]},
            two())
        self.assertTrue(p.is_valid)
        self.assertEqual(p.alternatives, [])


# ===================================================================== #
# fallback -- must equal the plain queue
# ===================================================================== #
class TestFallback(unittest.TestCase):

    def test_fallback_matches_queue_order(self):
        cands = [task("wh:1", priority=5), task("wh:2", priority=1),
                 task("wh:3", priority=9)]
        engine = TaskQueueEngine()
        expected = engine.next_task(cands)
        got = fallback_pick(cands, engine=engine)
        self.assertEqual(got.task_id, expected.task_id)
        self.assertEqual(got.source, SOURCE_FALLBACK)
        self.assertFalse(got.is_autonomous)

    def test_fallback_never_leaves_candidate_set(self):
        cands = [task("wh:1"), task("wh:2")]
        for _ in range(5):
            self.assertIn(fallback_pick(cands).task_id,
                          [t.task_id for t in cands])

    def test_fallback_respects_only_source(self):
        cands = [task("wh:1", "wh_picking", priority=1),
                 task("mfg:1", "mfg_workorder", priority=0)]
        p = fallback_pick(cands, only_source="wh_picking")
        self.assertEqual(p.task_id, "wh:1")   # mfg excluded despite better priority

    def test_fallback_empty_pool(self):
        p = fallback_pick([])
        self.assertFalse(p.is_valid)
        self.assertEqual(p.source, SOURCE_EMPTY)

    def test_fallback_only_source_empty_pool(self):
        p = fallback_pick([task("wh:1")], only_source="mfg_workorder")
        self.assertFalse(p.is_valid)
        self.assertEqual(p.source, SOURCE_EMPTY)

    def test_fallback_reason_is_multilingual(self):
        p = fallback_pick([task("wh:1")])
        self.assertIn("zh_CN", p.reason)
        self.assertTrue(p.to_dict("en_US")["reason"])


# ===================================================================== #
# serialization
# ===================================================================== #
class TestSerialization(unittest.TestCase):

    def test_to_dict_shape(self):
        p = validate_pick(
            {"selected_task_id": "wh:2", "confidence": 0.85,
             "reason": {"en_US": "urgent"},
             "alternatives": [{"task_id": "wh:1", "why_not": "less urgent"}]},
            two())
        d = p.to_dict("en_US")
        for key in ("task_id", "source", "reason", "reason_i18n",
                    "confidence", "alternatives", "rejected_reason"):
            self.assertIn(key, d)
        self.assertEqual(d["task_id"], "wh:2")
        self.assertEqual(d["source"], SOURCE_LLM)
        self.assertEqual(d["confidence"], 0.85)
        self.assertEqual(d["alternatives"][0]["task_id"], "wh:1")

    def test_rejected_pick_serializes_for_audit(self):
        d = validate_pick({"selected_task_id": "wh:9"}, two()).to_dict("en_US")
        self.assertIsNone(d["task_id"])
        self.assertEqual(d["source"], SOURCE_FALLBACK)
        self.assertIn("task_not_in_candidates", d["rejected_reason"])

    def test_empty_pick_serializes(self):
        d = validate_pick({"selected_task_id": "wh:1"}, []).to_dict("en_US")
        self.assertIsNone(d["task_id"])
        self.assertEqual(d["source"], SOURCE_EMPTY)
        self.assertEqual(d["confidence"], 0.0)


# ===================================================================== #
# AgentPick contract
# ===================================================================== #
class TestAgentPickObject(unittest.TestCase):

    def test_defaults_are_inert(self):
        p = AgentPick()
        self.assertFalse(p.is_valid)
        self.assertFalse(p.is_autonomous)
        self.assertEqual(p.reason, {})
        self.assertEqual(p.alternatives, [])
        self.assertEqual(p.confidence, 0.0)

    def test_autonomous_requires_valid_llm_pick(self):
        self.assertFalse(AgentPick(task_id="wh:1",
                                   source=SOURCE_FALLBACK).is_autonomous)
        self.assertFalse(AgentPick(task_id=None,
                                   source=SOURCE_LLM).is_autonomous)
        self.assertTrue(AgentPick(task_id="wh:1",
                                  source=SOURCE_LLM).is_autonomous)

    def test_reason_is_copied_not_aliased(self):
        src = {"en_US": "x"}
        p = AgentPick(task_id="wh:1", reason=src)
        src["en_US"] = "mutated"
        self.assertEqual(p.reason["en_US"], "x")


if __name__ == "__main__":                      # pragma: no cover
    unittest.main()
