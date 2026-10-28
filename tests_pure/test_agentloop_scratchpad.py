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
        """The deterministic fallback must not re-narrate a finished
        step — the scratchpad is the reason it can tell.

        v3.5: with the SOP present in `steps`, an exhausted procedure
        escalates to the task agent (`delegate_pick`) instead of closing
        out blindly — "the script ran out, reasoning is needed".
        """
        s = AgentScratchpad(done_steps=["s1"])
        t = run_loop(s, obs(current_step=recipe_step("s1"),
                            steps=[recipe_step("s1")]))
        self.assertEqual(t.action.action, ACT_DELEGATE_PICK)
        # no SOP context at all -> unchanged pre-v3.5 behaviour: close
        # out rather than invent content for the degraded mode.
        t2 = run_loop(AgentScratchpad(done_steps=["s1"]),
                      obs(current_step=recipe_step("s1")))
        self.assertEqual(t2.action.action, ACT_FINISH)

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


# ===================================================================== #
# v3.5 reasoning-first: SOP reference, single ledger, progress stall
# ===================================================================== #
class TestSOPReference(unittest.TestCase):
    """The recipe arrives as an annotated standard operating procedure,
    never as a bare cursor the model must recite (docs/CONFIG.md §14.2).
    """

    def _prompt_payload(self, done_ids=(), current=None, world=None):
        import json
        o = obs(steps=[recipe_step("s1"), recipe_step("s2")],
                current_step=current or recipe_step("s1"),
                done_ids=list(done_ids),
                world_state=world or {"steps_done": len(done_ids)})
        msgs = build_loop_prompt(AgentScratchpad(lang="zh_CN"), o)
        return json.loads(msgs[-1]["content"].split("\n", 1)[-1]) \
            if False else msgs[-1]["content"]

    def test_observation_carries_annotated_recipe_not_a_cursor(self):
        o = obs(steps=[recipe_step("s1"), recipe_step("s2")],
                current_step=recipe_step("s2"),
                done_ids=["s1"])
        d = o.to_prompt_dict()
        self.assertNotIn("current_step", d)      # no bare cursor any more
        self.assertNotIn("steps_remaining", d)   # nor a queue length
        ref = d["recipe_reference"]
        self.assertEqual([e["step_id"] for e in ref], ["s1", "s2"])
        self.assertTrue(ref[0]["done"])          # server ledger annotates
        self.assertFalse(ref[0]["next"])
        self.assertTrue(ref[1]["next"])
        self.assertFalse(ref[1]["done"])

    def test_world_state_and_outcomes_reach_the_prompt(self):
        o = obs(world_state={"task": {"completed": True}},
                recent_outcomes=[{"turn": 1, "capability_id": "x",
                                  "status": "success", "detail": "ok"}])
        d = o.to_prompt_dict()
        self.assertEqual(d["world_state"], {"task": {"completed": True}})
        self.assertEqual(len(d["recent_outcomes"]), 1)

    def test_prompt_declares_sop_semantics(self):
        msgs = build_loop_prompt(AgentScratchpad(),
                                 obs(steps=[recipe_step("s1")]))
        system = msgs[0]["content"]
        self.assertIn("standard operating procedure", system)
        self.assertIn("NOT as a queue", system)
        # reasoning-first wording replaced the old script-following hint
        self.assertNotIn("Prefer `propose_step` while the operator",
                         system)


class TestSingleLedger(unittest.TestCase):
    """done_steps holds ONLY human-readable step ids; capability effects
    live in recent_outcomes (§14.3)."""

    def test_mark_step_refuses_internal_signatures(self):
        s = AgentScratchpad()
        s.mark_step("call_capability:product_lookup:1")   # pollution
        s.mark_step(7)                                     # not a str
        s.mark_step("s1")                                  # legit id
        self.assertEqual(s.done_steps, ["s1"])

    def test_capability_effect_goes_to_outcome_ledger(self):
        call = lambda p, c: {"data": {"action": ACT_CALL_CAPABILITY,
                                      "payload": {
                                          "capability_id": "product_lookup",
                                          "record_id": 1,
                                          "params": {"quantity": 5}}}}
        runner = lambda req: {"status": "success",
                              "result": {"qty": 5}}
        s = AgentScratchpad()
        t = run_loop(s, obs(), llm_call=call, run_capability=runner)
        self.assertEqual(t.state, "running")
        self.assertEqual(s.done_steps, [])            # no signature leak
        self.assertEqual(len(s.recent_outcomes), 1)   # effect recorded
        entry = s.recent_outcomes[0]
        self.assertEqual(entry["capability_id"], "product_lookup")
        self.assertEqual(entry["status"], "success")

    def test_recent_outcomes_are_bounded(self):
        from mmux_core.agentloop import MAX_RECENT_OUTCOMES
        s = AgentScratchpad()
        action = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "product_lookup",
                         "record_id": 1}}, WHITELIST)
        for i in range(MAX_RECENT_OUTCOMES + 4):
            s.record_outcome(action, {"status": "success",
                                      "result": {"i": i}})
        self.assertEqual(len(s.recent_outcomes), MAX_RECENT_OUTCOMES)
        self.assertEqual(s.recent_outcomes[-1]["detail"],
                         str({"i": MAX_RECENT_OUTCOMES + 3}))

    def test_from_dict_sanitises_hand_edited_outcomes(self):
        back = AgentScratchpad.from_dict({
            "goal": "g",
            "recent_outcomes": [{"status": "ok"}, "junk", 5, None]})
        self.assertEqual(back.recent_outcomes, [{"status": "ok"}])


class TestProgressStall(unittest.TestCase):
    """Spin detection is about PROGRESS, not textual repetition (§14.5)."""

    def _cap_action(self, quantity):
        # distinct record ids keep the exact-repeat ledger out of these
        # tests: the stall counter must trip on ABSENCE OF PROGRESS,
        # never on textual repetition (that is the repeat ledger's job).
        return validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "picking_report_qty",
                         "record_id": int(quantity),
                         "params": {"quantity": quantity}}}, WHITELIST)

    def _fold(self, s, world, action, status="success"):
        from mmux_core.agentloop import fold_turn_progress
        o = obs(world_state=world)
        return fold_turn_progress(s, action, o, {"status": status})

    def test_corrected_retry_is_not_a_stall(self):
        s = AgentScratchpad()
        s._max_stall = 3
        w = {"q": 5}
        self.assertFalse(self._fold(s, w, self._cap_action(5)))
        # same text would have tripped the OLD repeat rule; here the
        # world moved (5 -> 9), so the counter resets.
        self.assertFalse(self._fold(s, {"q": 9}, self._cap_action(9)))

    def test_no_world_change_no_new_info_trips_after_k_turns(self):
        s = AgentScratchpad()
        s._max_stall = 3
        # one fixed world: nothing moves, nothing is learned. Distinct
        # record ids keep the exact-repeat ledger silent so ONLY the
        # stall counter can trip — which is exactly the point (§14.5).
        w = {"q": "same"}
        stalled = [self._fold(s, w, self._cap_action(i))
                   for i in range(4)]
        self.assertEqual(stalled, [False, False, True, True])

    def test_informative_status_resets_the_stall(self):
        s = AgentScratchpad()
        s._max_stall = 3
        w = {}
        self._fold(s, w, self._cap_action(5))
        # parked on a human: no world change, but new information —
        # punishing that would penalise exactly the escalation we want.
        self.assertFalse(self._fold(s, w, self._cap_action(5),
                                   status="awaiting_confirmation"))

    def test_conversation_is_outside_the_stall_ledger(self):
        from mmux_core.agentloop import fold_turn_progress
        s = AgentScratchpad()
        s._max_stall = 1
        ask = validate_action({"action": ACT_ASK_USER,
                               "payload": {"field": "qty",
                                           "question": "多少?"}}, WHITELIST)
        ans = validate_action({"action": ACT_ANSWER,
                               "payload": {"text": "好的"}}, WHITELIST)
        for _ in range(5):
            self.assertFalse(fold_turn_progress(s, ask, obs(), None))
            self.assertFalse(fold_turn_progress(s, ans, obs(), None))


class TestDerivedHistory(unittest.TestCase):
    """§14.4: the prompt window is built from the agent's own memory,
    so a resumed session is not amnesic every turn."""

    def test_outcomes_replay_as_assistant_user_pairs(self):
        s = AgentScratchpad(recent_outcomes=[
            {"turn": 1, "capability_id": "product_lookup",
             "status": "success", "detail": "found"}])
        msgs = build_loop_prompt(s, obs(), history=None)
        body = [m for m in msgs if m["role"] != "system"]
        self.assertTrue(any(m["role"] == "assistant"
                            and "product_lookup" in m["content"]
                            for m in body))
        self.assertTrue(any(m["role"] == "user" and "found" in m["content"]
                            for m in body))

    def test_explicit_history_wins_over_derivation(self):
        s = AgentScratchpad(recent_outcomes=[
            {"turn": 1, "capability_id": "zzz", "status": "success",
             "detail": "d"}])
        explicit = [{"role": "assistant", "content": '{"a":1}'},
                    {"role": "user", "content": "{}"}]
        msgs = build_loop_prompt(s, obs(), history=explicit)
        joined = " ".join(m["content"] for m in msgs)
        self.assertNotIn("zzz", joined)

    def test_run_loop_without_history_arg_still_sees_its_past(self):
        """End-to-end: after a capability ran, the NEXT turn's prompt
        contains the outcome — before v3.5 the model restarted blind."""
        seq = [
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "product_lookup",
                         "record_id": 1}},
            {"action": ACT_ANSWER, "payload": {"text": "查到了"}},
        ]
        state = {"i": 0}

        def call(payload, config):
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return {"data": seq[i]}

        s = AgentScratchpad()
        runner = lambda req: {"status": "success", "result": {"sku": "A1"}}
        run_loop(s, obs(), llm_call=call, run_capability=runner)   # turn 1
        run_loop(s, obs(), llm_call=call, run_capability=runner)   # turn 2
        second_prompt = None
        state["i"] = 1
        run_loop(s, obs(), llm_call=lambda p, c: (
            second_prompt := p) or {"data": seq[1]})
        joined = " ".join(m["content"] for m in second_prompt["messages"])
        self.assertIn("product_lookup", joined)


if __name__ == "__main__":                      # pragma: no cover
    unittest.main()
