# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Agent-loop SCRATCHPAD + DEGRADATION + i18n tests.

Three concerns that share one property — they are all about what the loop
does when things are *not* ideal:

**Scratchpad** — the agent's memory is persisted into the interaction log
and restored on resume, so it crosses a trust boundary in both
directions. It must emit only its declared keys, must survive a corrupt
blob, and must never gain a `confirmed` field.

**Degradation** — the architecture is "pure agent", which would be a
single point of failure if the agent were mandatory. Every degradation
path must keep the operator moving on the pre-v3.0 deterministic recipe.

**i18n** — the operator's language governs what they hear, and the audit
record keeps every language (evidence-complete) rather than resolving to
one and destroying the rest.
"""

import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.agentloop import (  # noqa: E402
    AgentAction, AgentObservation, AgentScratchpad, run_loop,
    build_loop_prompt, validate_action,
    ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER, ACT_ANSWER,
    ACT_FINISH, ACT_DELEGATE_PICK,
    SCRATCHPAD_KEYS, MAX_AGENT_TURNS, MAX_TEXT_LEN,
    LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE, LOOP_SOURCE_NONE,
)

WHITELIST = ["picking_task_complete", "picking_report_qty",
             "product_lookup"]


def recipe_step(step_id="s1"):
    return {"step_id": step_id, "title": "前往货架 A-01",
            "voice_prompt": "扫描货架 A-01 上的商品",
            "input_type": "scan",
            "capability_id": None, "params": {}, "record_ref": None,
            "hint": "看标签"}


def obs(**kw):
    kw.setdefault("goal", "pick SO001")
    kw.setdefault("available_capabilities", WHITELIST)
    return AgentObservation(**kw)


# ===================================================================== #
# scratchpad: persistence boundary
# ===================================================================== #
class TestScratchpadPersistence(unittest.TestCase):

    def test_to_dict_is_exactly_the_declared_keys(self):
        s = AgentScratchpad(goal="g", task_id="wh:1", flow_id="f",
                            skill_level="expert", lang="zh_CN",
                            done_steps=["a"], last_error="e",
                            user_answers={"q": 1}, turn=3,
                            capability_calls=2)
        self.assertEqual(sorted(s.to_dict()), sorted(SCRATCHPAD_KEYS))

    def test_no_confirmed_key_can_ever_be_persisted(self):
        """Structural: `confirmed` is absent from the key set, and
        __slots__ makes adding it impossible."""
        self.assertNotIn("confirmed", SCRATCHPAD_KEYS)
        s = AgentScratchpad()
        with self.assertRaises(AttributeError):
            s.confirmed = True                       # type: ignore[attr-defined]
        self.assertNotIn("confirmed", s.to_dict())

    def test_roundtrip_preserves_everything(self):
        s = AgentScratchpad(goal="拣货", task_id="wh:7", flow_id="wh_flow",
                            skill_level="expert", lang="zh_CN",
                            done_steps=["s1", "s2"], last_error="failed",
                            user_answers={"quantity": 10}, turn=4,
                            capability_calls=1)
        back = AgentScratchpad.from_dict(s.to_dict())
        self.assertEqual(back.goal, "拣货")
        self.assertEqual(back.task_id, "wh:7")
        self.assertEqual(back.done_steps, ["s1", "s2"])
        self.assertEqual(back.user_answers, {"quantity": 10})
        self.assertEqual(back.turn, 4)
        self.assertEqual(back.capability_calls, 1)

    def test_from_dict_drops_unknown_keys_silently(self):
        back = AgentScratchpad.from_dict(
            {"goal": "g", "confirmed": True, "sudo": True, "junk": [1]})
        self.assertEqual(back.goal, "g")
        self.assertFalse(hasattr(back, "confirmed"))
        self.assertFalse(hasattr(back, "sudo"))

    def test_from_dict_survives_corrupt_blobs(self):
        for bad in (None, "text", 42, [1, 2], (), {"turn": "abc"}):
            back = AgentScratchpad.from_dict(bad)
            self.assertIsInstance(back, AgentScratchpad)

    def test_from_dict_partial_blob_uses_defaults(self):
        back = AgentScratchpad.from_dict({"goal": "only goal"})
        self.assertEqual(back.goal, "only goal")
        self.assertEqual(back.turn, 0)
        self.assertEqual(back.done_steps, [])
        self.assertEqual(back.skill_level, "novice")

    def test_marks_are_idempotent_and_ordered(self):
        s = AgentScratchpad()
        s.mark_step("a")
        s.mark_step("b")
        s.mark_step("a")
        s.mark_step(None)
        s.mark_step("")
        self.assertEqual(s.done_steps, ["a", "b"])

    def test_answers_overwrite(self):
        s = AgentScratchpad()
        s.answer("quantity", 5)
        s.answer("quantity", 10)
        s.answer("", "ignored")
        s.answer(None, "ignored")
        self.assertEqual(s.user_answers, {"quantity": 10})

    def test_note_error_truncates(self):
        s = AgentScratchpad()
        s.note_error("x" * 5000)
        self.assertEqual(len(s.last_error), MAX_TEXT_LEN)
        s.note_error(None)
        self.assertIsNone(s.last_error)

    def test_budget_left_never_goes_negative(self):
        s = AgentScratchpad(turn=99, capability_calls=99)
        self.assertEqual(s.budget_left, 0)
        self.assertEqual(s.calls_left, 0)

    def test_constructor_survives_garbage_counts(self):
        s = AgentScratchpad(turn="abc", capability_calls=None)
        self.assertEqual(s.turn, 0)
        self.assertEqual(s.capability_calls, 0)


# ===================================================================== #
# degradation: every path keeps the operator moving
# ===================================================================== #
class TestDegradationAlwaysAvailable(unittest.TestCase):

    def test_missing_provider_still_narrates(self):
        t = run_loop(AgentScratchpad(), obs(current_step=recipe_step()))
        self.assertEqual(t.source, LOOP_SOURCE_NONE)
        self.assertEqual(t.state, "running")
        self.assertTrue(t.speak)

    def test_provider_exception_still_narrates(self):
        def boom(payload, config):
            raise TimeoutError("network")

        t = run_loop(AgentScratchpad(), obs(current_step=recipe_step()),
                     llm_call=boom)
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)
        self.assertEqual(t.state, "running")
        self.assertEqual(t.action.payload["step_id"], "s1")

    def test_garbage_answer_still_narrates(self):
        for raw in ({"raw": "not json", "data": None},
                    {"raw": "[1,2]", "data": [1, 2]},
                    {"data": None},
                    {}):
            t = run_loop(AgentScratchpad(),
                         obs(current_step=recipe_step()),
                         llm_call=lambda p, c, r=raw: r)
            self.assertEqual(t.state, "running", raw)
            self.assertEqual(t.action.action, ACT_PROPOSE_STEP, raw)

    def test_recipe_narration_carries_the_step_content(self):
        t = run_loop(AgentScratchpad(), obs(current_step=recipe_step()))
        self.assertEqual(t.action.payload["title"], "前往货架 A-01")
        self.assertEqual(t.action.payload["input_type"], "scan")
        self.assertEqual(t.action.payload["hint"], "看标签")

    def test_recipe_with_no_current_step_delegates(self):
        t = run_loop(AgentScratchpad(), obs(current_step=None))
        self.assertEqual(t.action.action, ACT_DELEGATE_PICK)
        self.assertEqual(t.state, "finished_all")

    def test_recipe_skips_a_step_already_done(self):
        """The recipe must not re-narrate a finished step — the scratchpad
        is the reason it can tell."""
        s = AgentScratchpad(done_steps=["s1"])
        t = run_loop(s, obs(current_step=recipe_step("s1")))
        # nothing left to narrate -> the recipe closes out
        self.assertEqual(t.action.action, ACT_FINISH)

    def test_hallucination_does_not_stop_the_operator(self):
        call = lambda p, c: {"data": {"action": ACT_CALL_CAPABILITY,
                                      "payload": {"capability_id":
                                                  "drop_all_tables"}}}
        t = run_loop(AgentScratchpad(), obs(current_step=recipe_step()),
                     llm_call=call, allowed_capabilities=WHITELIST)
        self.assertEqual(t.state, "running")
        self.assertEqual(t.action.action, ACT_PROPOSE_STEP)

    def test_every_degraded_turn_still_has_something_to_say(self):
        cases = [
            {"action": "unknown_thing", "payload": {}},
            {"payload": {}},
            None,
            "just a string",
        ]
        for raw in cases:
            t = run_loop(AgentScratchpad(), obs(current_step=recipe_step()),
                         llm_call=lambda p, c, r=raw: {"data": r})
            self.assertTrue(t.speak or t.action.action in (ACT_FINISH,),
                            raw)


# ===================================================================== #
# i18n
# ===================================================================== #
class TestLocalization(unittest.TestCase):

    def test_speak_is_localized_to_the_operator_language(self):
        call = lambda p, c: {"data": {
            "action": ACT_ASK_USER,
            "payload": {"question": {"zh_CN": "实际拣了多少件？",
                                     "en_US": "how many did you pick?"}}}}
        t = run_loop(AgentScratchpad(lang="zh_CN"),
                     obs(lang="zh_CN"), llm_call=call)
        self.assertEqual(t.to_dict("zh_CN")["speak"], "实际拣了多少件？")
        self.assertEqual(t.to_dict("en_US")["speak"], "how many did you pick?")

    def test_raw_payload_keeps_all_languages(self):
        """The audit record must stay evidence-complete: resolving the
        reason to one language would destroy the others."""
        call = lambda p, c: {"data": {
            "action": ACT_ASK_USER,
            "payload": {"question": {"zh_CN": "几件？", "en_US": "how many?"}}}}
        t = run_loop(AgentScratchpad(), obs(lang="zh_CN"), llm_call=call)
        self.assertEqual(t.action.payload["question"]["en_US"], "how many?")

    def test_missing_translation_falls_back_never_raises(self):
        call = lambda p, c: {"data": {
            "action": ACT_ANSWER,
            "payload": {"text": {"ja_JP": "いくつ？"}}}}
        t = run_loop(AgentScratchpad(lang="zh_CN"), obs(lang="zh_CN"),
                     llm_call=call)
        # `resolve_text` walks the fallback chain and then takes whatever
        # exists, so a Japanese-only answer still reaches a Chinese user
        # rather than blanking out (PRD §9.2 explicit feedback).
        self.assertEqual(t.to_dict("zh_CN")["speak"], "いくつ？")
        self.assertEqual(t.to_dict("fr_FR")["speak"], "いくつ？")

    def test_mixed_language_payload_is_accepted(self):
        """Input may mix languages; the action's multilingual payload is
        kept whole so the operator gets their language."""
        call = lambda p, c: {"data": {
            "action": ACT_PROPOSE_STEP,
            "payload": {"step_id": "s1",
                        "voice_prompt": {"zh_CN": "扫描 shelf A-01 商品",
                                         "en_US": "scan shelf A-01"}}}}
        t = run_loop(AgentScratchpad(lang="zh_CN"), obs(lang="zh_CN"),
                     llm_call=call)
        self.assertIn("扫描", t.to_dict("zh_CN")["speak"])

    def test_observation_localizes_goal_and_task(self):
        o = AgentObservation(
            goal={"zh_CN": "完成拣货", "en_US": "complete the picking"},
            task={"title": {"zh_CN": "拣货单 SO001",
                            "en_US": "Pick SO001"}},
            lang="zh_CN")
        d = o.to_prompt_dict("zh_CN")
        self.assertEqual(d["goal"], "完成拣货")
        self.assertEqual(d["task"]["title"], "拣货单 SO001")
        d_en = o.to_prompt_dict("en_US")
        self.assertEqual(d_en["goal"], "complete the picking")

    def test_prompt_tells_the_model_which_language_to_use(self):
        msgs = build_loop_prompt(AgentScratchpad(lang="zh_CN"),
                                 obs(lang="zh_CN"))
        self.assertIn("zh_CN", msgs[0]["content"])

    def test_lang_argument_overrides_scratchpad(self):
        msgs = build_loop_prompt(AgentScratchpad(lang="zh_CN"),
                                 obs(lang="zh_CN"), lang="en_US")
        self.assertIn("en_US", msgs[0]["content"])

    def test_non_string_text_does_not_raise(self):
        for bad in (5, None, [], {"zh_CN": 42}):
            a = validate_action({"action": ACT_ANSWER,
                                 "payload": {"text": bad}}, WHITELIST)
            self.assertIsInstance(a, AgentAction)


if __name__ == "__main__":                      # pragma: no cover
    unittest.main()
