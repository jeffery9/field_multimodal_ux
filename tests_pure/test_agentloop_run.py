# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Unit tests for the agent-loop RUNTIME (`run_loop`).

`test_agentloop_validate.py` covers "is this action allowed?".
This file covers the harder question: **what does the loop DO with an
allowed action, and does it always terminate safely?**

The properties under test are the ones that would turn an agent loop
into an incident:

  * a model that is unreachable, returns garbage, or hallucinates
    degrades to the deterministic recipe — the operator is never stuck;
  * `confirmed` is never True on a capability call, so an L2 write
    always parks and the loop stops at the human gate;
  * the loop always TERMINATES: turn budget, capability-call budget and
    repeated-step detection are all enforced server-side;
  * `unknown` (une certain outcome) is never treated as success;
  * a rejected action's reason survives into the turn for audit.

Everything runs on the pure core with fake callables — no Odoo.
"""

import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.agentloop import (  # noqa: E402
    AgentAction, AgentObservation, AgentScratchpad, AgentTurn,
    run_loop, build_loop_prompt, validate_action,
    ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER, ACT_ANSWER,
    ACT_FINISH, ACT_DELEGATE_PICK,
    MAX_AGENT_TURNS, MAX_AGENT_CAPABILITY_CALLS, MAX_REPEATED_STEP,
    LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE, LOOP_SOURCE_NONE,
)

WHITELIST = ["picking_task_complete", "picking_report_qty",
             "product_lookup"]


# ===================================================================== #
# fakes
# ===================================================================== #
def llm_returning(payload_value, raises=None):
    """A fake provider call that always answers the same thing.

    `payload_value` mirrors what a real provider returns: {"data": ...}.
    """
    calls = []

    def call(payload, config):
        calls.append(payload)
        if raises is not None:
            raise raises
        if callable(payload_value):
            return payload_value(payload, config)
        return {"data": payload_value}

    call.calls = calls
    return call


def llm_sequence(*values):
    """Answers each call in turn (last one repeats)."""
    state = {"i": 0}

    def call(payload, config):
        i = min(state["i"], len(values) - 1)
        state["i"] += 1
        return {"data": values[i]}

    return call


def observation(current_step=None, steps=None, goal="pick the order"):
    return AgentObservation(
        goal=goal,
        task={"task_id": "wh:1", "title": "Pick SO001"},
        flow_id="wh_guided_picking",
        skill_level="novice",
        lang="zh_CN",
        current_step=current_step,
        steps=steps or [],
        available_capabilities=WHITELIST,
    )


def recipe_step(step_id="s1"):
    return {"step_id": step_id, "title": "前往货架 A-01",
            "voice_prompt": "前往货架 A-01 并扫描商品",
            "input_type": "scan",
            "capability_id": None,
            "params": {},
            "record_ref": None,
            "hint": "看货架标签"}


def runner_returning(status, extra=None):
    calls = []

    def run(request):
        calls.append(request)
        out = {"status": status, "result": {"ok": True}}
        if extra:
            out.update(extra)
        return out

    run.calls = calls
    return run


# ===================================================================== #
# happy path: the model decides
# ===================================================================== #
class TestLLMDecides(unittest.TestCase):

    def test_propose_step_is_narrated(self):
        call = llm_returning({"action": ACT_PROPOSE_STEP,
                              "payload": {"step_id": "s1",
                                          "voice_prompt": "扫描货架 A-01"}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call)
        self.assertEqual(t.state, "running")
        self.assertEqual(t.source, LOOP_SOURCE_LLM)
        self.assertEqual(t.action.action, ACT_PROPOSE_STEP)
        self.assertEqual(t.speak, "扫描货架 A-01")

    def test_answer_action_speaks_without_executing(self):
        call = llm_returning({"action": ACT_ANSWER,
                              "payload": {"text": "库位在 A 区"}})
        runner = runner_returning("success")
        t = run_loop(AgentScratchpad(), observation(), llm_call=call,
                     run_capability=runner)
        self.assertEqual(t.state, "running")
        self.assertEqual(t.speak, "库位在 A 区")
        self.assertEqual(runner.calls, [])          # nothing executed

    def test_finish_ends_the_loop(self):
        call = llm_returning({"action": ACT_FINISH,
                              "payload": {"summary": "已经完成",
                                          "goal_met": True}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call)
        self.assertEqual(t.state, "finished")
        self.assertTrue(t.is_terminal)
        self.assertEqual(t.speak, "已经完成")

    def test_finish_with_goal_met_false_is_failure(self):
        call = llm_returning({"action": ACT_FINISH,
                              "payload": {"goal_met": False,
                                          "summary": "无法完成"}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call)
        self.assertEqual(t.state, "failed")
        self.assertTrue(t.is_terminal)

    def test_ask_user_pauses_for_the_human(self):
        call = llm_returning({"action": ACT_ASK_USER,
                              "payload": {"question": "实际拣了多少件？",
                                          "field": "quantity"}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call)
        self.assertEqual(t.state, "awaiting_user")
        self.assertEqual(t.speak, "实际拣了多少件？")

    def test_delegate_pick_hands_over_to_the_task_agent(self):
        call = llm_returning({"action": ACT_DELEGATE_PICK,
                              "payload": {"reason": "没有待办任务"}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call)
        self.assertEqual(t.state, "finished_all")

    def test_successful_capability_keeps_loop_running(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 7}})
        runner = runner_returning("success")
        s = AgentScratchpad()
        t = run_loop(s, observation(), llm_call=call, run_capability=runner)
        self.assertEqual(t.state, "running")
        self.assertEqual(t.exec_result["status"], "success")
        self.assertEqual(s.capability_calls, 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["record_id"], 7)


# ===================================================================== #
# THE human gate
# ===================================================================== #
class TestHumanConfirmationGate(unittest.TestCase):

    def test_awaiting_confirmation_parks_the_loop(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id":
                                          "picking_task_complete",
                                          "record_id": 1}})
        runner = runner_returning("awaiting_confirmation",
                                  {"confirmation": {"capability_id":
                                                    "picking_task_complete"}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call,
                     run_capability=runner)
        self.assertEqual(t.state, "awaiting_confirmation")
        self.assertNotIn("speak", t.exec_result)
        self.assertEqual(t.exec_result["confirmation"]["capability_id"],
                         "picking_task_complete")

    def test_confirmed_is_never_true_on_any_capability_call(self):
        """The single most important assertion in this file."""
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 3}})
        runner = runner_returning("success")
        run_loop(AgentScratchpad(), observation(), llm_call=call,
                 run_capability=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertIs(runner.calls[0]["confirmed"], False)

    def test_model_cannot_smuggle_confirmed_into_the_payload(self):
        """A `confirmed` key is undeclared, so the action is rejected and
        the loop degrades to the recipe — it never reaches the runner."""
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "confirmed": True}})
        runner = runner_returning("success")
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()),
                     llm_call=call, run_capability=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)
        self.assertEqual(t.rejected_reason, "undeclared_payload:confirmed")

    def test_confirmation_does_not_advance_the_scratchpad_past_the_gate(self):
        """The parked capability is recorded as attempted, but the loop
        state stays at the gate — no auto-retry on the next turn."""
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id":
                                          "picking_task_complete",
                                          "record_id": 1}})
        runner = runner_returning("awaiting_confirmation")
        s = AgentScratchpad()
        t = run_loop(s, observation(), llm_call=call, run_capability=runner)
        self.assertEqual(t.state, "awaiting_confirmation")
        # a second turn with the identical proposal must not re-execute
        # into `success`; it is detected as a repeat / stays parked.
        t2 = run_loop(s, observation(), llm_call=call, run_capability=runner)
        self.assertNotEqual(t2.state, "finished")


# ===================================================================== #
# degradation: the loop must never strand the operator
# ===================================================================== #
class TestDegradation(unittest.TestCase):

    def test_no_llm_wired_uses_the_recipe(self):
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()))
        self.assertEqual(t.source, LOOP_SOURCE_NONE)
        self.assertEqual(t.action.action, ACT_PROPOSE_STEP)
        self.assertEqual(t.action.payload["step_id"], "s1")
        self.assertEqual(t.state, "running")

    def test_provider_raises_falls_back_to_recipe(self):
        call = llm_returning(None, raises=RuntimeError("timeout"))
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()),
                     llm_call=call)
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)
        self.assertEqual(t.action.action, ACT_PROPOSE_STEP)

    def test_invalid_json_falls_back_to_recipe(self):
        # the provider returns a non-JSON string
        call = llm_returning(None)
        call = lambda payload, config: {"raw": "{not json", "data": None}
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()),
                     llm_call=call)
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)
        self.assertEqual(t.action.payload["step_id"], "s1")

    def test_not_an_object_falls_back_to_recipe(self):
        call = lambda payload, config: {"raw": "[1,2,3]", "data": [1, 2, 3]}
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()),
                     llm_call=call)
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)

    def test_hallucinated_capability_degrades_and_records_reason(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "res_partner_write",
                                          "record_id": 1}})
        runner = runner_returning("success")
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()),
                     llm_call=call, run_capability=runner,
                     allowed_capabilities=WHITELIST)
        self.assertEqual(runner.calls, [])                 # nothing ran
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)
        self.assertEqual(
            t.rejected_reason,
            "capability_not_whitelisted:res_partner_write")
        # the recipe still gives the operator something to do
        self.assertEqual(t.action.action, ACT_PROPOSE_STEP)

    def test_protocol_violation_degrades(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "method": "write"}})
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()),
                     llm_call=call)
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)
        self.assertEqual(t.rejected_reason, "protocol_violation:method")

    def test_unknown_action_degrades(self):
        call = llm_returning({"action": "drop_table", "payload": {}})
        t = run_loop(AgentScratchpad(), observation(current_step=recipe_step()),
                     llm_call=call)
        self.assertEqual(t.source, LOOP_SOURCE_RECIPE)
        self.assertEqual(t.rejected_reason, "unknown_action:drop_table")

    def test_recipe_falls_back_to_finish_when_nothing_left(self):
        # no current step -> the recipe has nothing to narrate
        call = llm_returning(None, raises=RuntimeError("down"))
        t = run_loop(AgentScratchpad(), observation(current_step=None),
                     llm_call=call)
        self.assertEqual(t.action.action, ACT_DELEGATE_PICK)
        self.assertEqual(t.state, "finished_all")

    def test_recipe_prefers_an_unfinished_step(self):
        s = AgentScratchpad()
        step = recipe_step("s1")
        # ask the model for garbage so the recipe decides
        call = llm_returning(None, raises=RuntimeError("down"))
        t = run_loop(s, observation(current_step=step), llm_call=call)
        self.assertEqual(t.action.payload["step_id"], "s1")
        self.assertIn("s1", s.done_steps)


# ===================================================================== #
# runner outcomes
# ===================================================================== #
class TestRunnerOutcomes(unittest.TestCase):

    def _run(self, status, obs=None):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1}})
        runner = runner_returning(status)
        return run_loop(AgentScratchpad(), obs or observation(),
                        llm_call=call, run_capability=runner), runner

    def test_failed_is_not_success(self):
        t, _ = self._run("failed")
        self.assertEqual(t.state, "failed")
        self.assertTrue(t.is_terminal)

    def test_unknown_is_never_treated_as_success(self):
        """FR-017: an uncertain outcome must not report success and must
        not be blind-retried."""
        t, _ = self._run("unknown")
        self.assertEqual(t.state, "failed")
        self.assertNotEqual(t.state, "running")
        self.assertEqual(t.exec_result["status"], "unknown")

    def test_duplicate_is_not_success(self):
        t, _ = self._run("failed")
        self.assertNotEqual(t.state, "running")

    def test_runner_raising_does_not_crash_the_loop(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup"}})

        def boom(request):
            raise RuntimeError("db gone")

        s = AgentScratchpad()
        t = run_loop(s, observation(), llm_call=call, run_capability=boom)
        self.assertEqual(t.state, "failed")
        self.assertIn("db gone", t.exec_result["error"])
        self.assertIn("db gone", s.last_error)

    def test_no_runner_reports_proposal_without_executing(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call)
        self.assertEqual(t.state, "awaiting_confirmation")
        self.assertEqual(t.rejected_reason, "no_runner")
        self.assertEqual(t.exec_result["status"], "proposed")


# ===================================================================== #
# termination guarantees
# ===================================================================== #
class TestTermination(unittest.TestCase):

    def test_turn_budget_is_enforced(self):
        # each turn says something DIFFERENT, so repeat detection cannot
        # fire first and the TURN budget is what stops the loop.
        call = llm_sequence(*[{"action": ACT_ANSWER,
                               "payload": {"text": f"note {i}"}}
                              for i in range(MAX_AGENT_TURNS + 3)])
        s = AgentScratchpad()
        obs = observation()
        states = []
        for _ in range(MAX_AGENT_TURNS):
            t = run_loop(s, obs, llm_call=call)
            states.append(t.state)
            self.assertNotEqual(t.state, "budget_exhausted")
        t = run_loop(s, obs, llm_call=call)
        self.assertEqual(t.state, "budget_exhausted")
        self.assertEqual(t.rejected_reason, "budget_exhausted")
        self.assertEqual(states, ["running"] * MAX_AGENT_TURNS)

    def test_capability_call_budget_is_enforced(self):
        # each call names a DIFFERENT capability+record so repeat
        # detection does not fire first and the call budget is what stops it
        caps = [("product_lookup", 1), ("picking_report_qty", 2),
                ("picking_task_complete", 3), ("product_lookup", 4)]
        call = llm_sequence(*[
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": c, "record_id": r}}
            for c, r in caps])
        runner = runner_returning("success")
        s = AgentScratchpad()
        obs = observation()
        seen = []
        for _ in range(len(caps)):
            seen.append(run_loop(s, obs, llm_call=call,
                                 run_capability=runner).state)
        self.assertEqual(s.capability_calls, MAX_AGENT_CAPABILITY_CALLS)
        self.assertEqual(seen[-1], "budget_exhausted")
        self.assertLessEqual(len(runner.calls), MAX_AGENT_CAPABILITY_CALLS)

    def test_repeated_step_ends_the_loop(self):
        """The classic ReAct spin: propose the SAME step forever."""
        call = llm_returning({"action": ACT_PROPOSE_STEP,
                              "payload": {"step_id": "s1",
                                          "title": "do the thing"}})
        s = AgentScratchpad()
        obs = observation()
        states = [run_loop(s, obs, llm_call=call).state
                  for _ in range(MAX_REPEATED_STEP + 2)]
        self.assertEqual(states[-1], "budget_exhausted")

    def test_repeated_capability_call_is_detected(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1}})
        # runner always says the write is parked -> identical re-proposal
        runner = runner_returning("failed")
        s = AgentScratchpad()
        obs = observation()
        last = None
        for _ in range(MAX_REPEATED_STEP + 3):
            last = run_loop(s, obs, llm_call=call, run_capability=runner)
        self.assertIn(last.state, ("budget_exhausted", "failed"))

    def test_loop_always_reaches_a_terminal_state(self):
        """Fuzz-ish: whatever the model says, the loop terminates within
        the budget."""
        answers = [
            {"action": ACT_ANSWER, "payload": {"text": "x"}},
            {"action": ACT_ASK_USER, "payload": {"question": "q"}},
            {"action": ACT_PROPOSE_STEP, "payload": {"step_id": "s1",
                                                     "title": "t"}},
            {"action": "bogus", "payload": {}},
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "product_lookup", "record_id": 1}},
        ]
        call = llm_sequence(*answers)
        s = AgentScratchpad()
        obs = observation()
        for _ in range(MAX_AGENT_TURNS):
            run_loop(s, obs, llm_call=call,
                     run_capability=runner_returning("success"))
        t = run_loop(s, obs, llm_call=call)
        self.assertTrue(t.is_terminal, t.state)

    def test_max_turns_parameter_is_honoured(self):
        call = llm_returning({"action": ACT_ANSWER, "payload": {"text": "x"}})
        s = AgentScratchpad()
        obs = observation()
        run_loop(s, obs, llm_call=call, max_turns=1)
        t = run_loop(s, obs, llm_call=call, max_turns=1)
        self.assertEqual(t.state, "budget_exhausted")


# ===================================================================== #
# configurable budgets (v19.0.6): the pure core accepts caller-injected
# limits so the deployment can tune them without a code change. These
# tests pin that injection actually steers each of the three budgets —
# and that omitting them reproduces the old constant behaviour exactly.
# ===================================================================== #
class TestConfigurableBudgets(unittest.TestCase):

    def test_injected_turn_budget_is_enforced(self):
        # distinct answers so repeat detection cannot fire first
        call = llm_sequence(*[{"action": ACT_ANSWER,
                               "payload": {"text": f"note {i}"}}
                              for i in range(10)])
        s = AgentScratchpad()
        obs = observation()
        run_loop(s, obs, llm_call=call, max_turns=2)
        run_loop(s, obs, llm_call=call, max_turns=2)
        t = run_loop(s, obs, llm_call=call, max_turns=2)
        self.assertEqual(t.state, "budget_exhausted")
        self.assertEqual(t.rejected_reason, "budget_exhausted")
        self.assertEqual(s.turn, 2)

    def test_injected_capability_budget_is_enforced(self):
        # a DIFFERENT capability+record each time so repeat detection
        # does not fire first — the injected call budget must be what
        # stops the loop, not the default of 3.
        caps = [("product_lookup", 1), ("picking_report_qty", 2),
                ("picking_task_complete", 3), ("product_lookup", 4)]
        call = llm_sequence(*[
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": c, "record_id": r}}
            for c, r in caps])
        runner = runner_returning("success")
        s = AgentScratchpad()
        obs = observation()
        for _ in range(3):
            run_loop(s, obs, llm_call=call, run_capability=runner,
                     max_capability_calls=1)
        self.assertEqual(s.capability_calls, 1)
        self.assertEqual(len(runner.calls), 1)

    def test_injected_repeat_budget_is_enforced(self):
        """max_repeated_step=1: the SECOND identical proposal already
        terminates — with the default (2) it would not."""
        call = llm_returning({"action": ACT_PROPOSE_STEP,
                              "payload": {"step_id": "s1",
                                          "title": "do the thing"}})
        s = AgentScratchpad()
        obs = observation()
        first = run_loop(s, obs, llm_call=call, max_repeated_step=1)
        self.assertEqual(first.state, "running")
        second = run_loop(s, obs, llm_call=call, max_repeated_step=1)
        self.assertEqual(second.state, "budget_exhausted")
        self.assertEqual(second.rejected_reason, "repeated_step")

    def test_invalid_injected_budgets_fall_back_to_defaults(self):
        """Zero/negative/None must mean "module default", never
        "unlimited" — a misconfigured deployment cannot disable the
        safety budgets."""
        call = llm_sequence(*[{"action": ACT_ANSWER,
                               "payload": {"text": f"n{i}"}}
                              for i in range(MAX_AGENT_TURNS + 3)])
        s = AgentScratchpad()
        obs = observation()
        states = [run_loop(s, obs, llm_call=call, max_turns=0,
                           max_capability_calls=-1,
                           max_repeated_step=None).state
                  for _ in range(MAX_AGENT_TURNS)]
        self.assertEqual(states, ["running"] * MAX_AGENT_TURNS)
        t = run_loop(s, obs, llm_call=call, max_turns=0)
        self.assertEqual(t.state, "budget_exhausted")

    def test_prompt_reports_the_injected_budget(self):
        """The model must SEE the configured budget (it reads the
        scratchpad's budget_left/calls_left), so it can plan within it."""
        call = llm_returning({"action": ACT_ANSWER,
                              "payload": {"text": "x"}})
        s = AgentScratchpad()
        run_loop(s, observation(), llm_call=call, max_turns=4,
                 max_capability_calls=1)
        # payload shape is {"messages": [...]} (the provider contract)
        user_msg = call.calls[-1]["messages"][-1]["content"]
        # the prompt is built BEFORE this turn is counted, so the first
        # turn still sees the full injected budget.
        self.assertIn('"turns_left": 4', user_msg)
        self.assertIn('"capability_calls_left": 1', user_msg)

    def test_budgets_are_not_persisted(self):
        """The injected budgets are runtime-only slots: a scratchpad
        round-tripped through to_dict/from_dict must not carry them
        (they are re-injected by the next run_loop call)."""
        s = AgentScratchpad(max_turns=2, max_capability_calls=1)
        self.assertEqual(s.budget_left, 2)
        self.assertEqual(s.calls_left, 1)
        blob = s.to_dict()
        self.assertNotIn("_max_turns", blob)
        self.assertNotIn("_max_calls", blob)
        restored = AgentScratchpad.from_dict(blob)
        # restored defaults until the next run_loop injects again
        self.assertEqual(restored.budget_left, MAX_AGENT_TURNS)
        self.assertEqual(restored.calls_left, MAX_AGENT_CAPABILITY_CALLS)


# ===================================================================== #
# scratchpad bookkeeping across turns
# ===================================================================== #
class TestScratchpadAcrossTurns(unittest.TestCase):

    def test_turn_counter_advances_only_on_real_work(self):
        call = llm_returning({"action": ACT_ANSWER, "payload": {"text": "x"}})
        s = AgentScratchpad()
        run_loop(s, observation(), llm_call=call)
        run_loop(s, observation(), llm_call=call)
        self.assertEqual(s.turn, 2)

    def test_budget_exhausted_does_not_consume_a_turn(self):
        call = llm_returning({"action": ACT_ANSWER, "payload": {"text": "x"}})
        s = AgentScratchpad(turn=MAX_AGENT_TURNS)
        run_loop(s, observation(), llm_call=call)
        self.assertEqual(s.turn, MAX_AGENT_TURNS)

    def test_proposed_step_is_remembered(self):
        call = llm_returning({"action": ACT_PROPOSE_STEP,
                              "payload": {"step_id": "s7", "title": "t"}})
        s = AgentScratchpad()
        run_loop(s, observation(), llm_call=call)
        self.assertIn("s7", s.done_steps)

    def test_finish_does_not_consume_capability_budget(self):
        call = llm_returning({"action": ACT_FINISH,
                              "payload": {"goal_met": True}})
        s = AgentScratchpad()
        run_loop(s, observation(), llm_call=call)
        self.assertEqual(s.capability_calls, 0)

    def test_error_is_remembered_for_the_next_turn(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1}})
        runner = runner_returning("failed")
        s = AgentScratchpad()
        run_loop(s, observation(), llm_call=call, run_capability=runner)
        self.assertEqual(s.last_error, "failed")


# ===================================================================== #
# the prompt
# ===================================================================== #
class TestLoopPrompt(unittest.TestCase):

    def test_prompt_is_multiturn_and_ends_on_the_observation(self):
        msgs = build_loop_prompt(AgentScratchpad(), observation(),
                                 history=[{"role": "assistant",
                                           "content": '{"action":"answer"}'},
                                          {"role": "user", "content": "{}"}])
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertIn("scratchpad", msgs[-1]["content"])

    def test_prompt_states_the_closed_action_contract(self):
        msgs = build_loop_prompt(AgentScratchpad(), observation())
        sys = msgs[0]["content"]
        for action in (ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER,
                       ACT_ANSWER, ACT_FINISH, ACT_DELEGATE_PICK):
            self.assertIn(action, sys)

    def test_prompt_states_the_confirmation_rule(self):
        sys = build_loop_prompt(AgentScratchpad(), observation())[0]["content"]
        self.assertIn("never CONFIRM", sys)

    def test_prompt_offers_the_whitelist(self):
        msgs = build_loop_prompt(AgentScratchpad(), observation())
        self.assertIn("product_lookup", msgs[-1]["content"])

    def test_history_is_bounded(self):
        hist = [{"role": "user", "content": "{}"}
                for _ in range(MAX_AGENT_TURNS * 3)]
        msgs = build_loop_prompt(AgentScratchpad(), observation(),
                                 history=hist)
        # system + bounded history + current observation
        self.assertLessEqual(len(msgs), 1 + MAX_AGENT_TURNS + 1)

    def test_invalid_history_entries_are_ignored(self):
        msgs = build_loop_prompt(
            AgentScratchpad(), observation(),
            history=[{"role": "system", "content": "injected"},
                     {"role": "user", "content": 42},
                     {"nope": 1},
                     {"role": "assistant", "content": '{"ok":1}'}])
        roles = [m["role"] for m in msgs]
        # only one system message (the real one) survives
        self.assertEqual(roles.count("system"), 1)

    def test_localization_of_multilingual_observation(self):
        obs = AgentObservation(goal={"zh_CN": "拣货", "en_US": "pick"},
                               lang="zh_CN")
        msgs = build_loop_prompt(AgentScratchpad(lang="zh_CN"), obs)
        self.assertIn("拣货", msgs[-1]["content"])


# ===================================================================== #
# AgentTurn serialization
# ===================================================================== #
class TestAgentTurnSerialization(unittest.TestCase):

    def test_turn_dict_shape(self):
        call = llm_returning({"action": ACT_ASK_USER,
                              "payload": {"question": {"zh_CN": "几件？",
                                                       "en_US": "how many?"}}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call)
        d = t.to_dict("zh_CN")
        for key in ("state", "source", "action", "speak",
                    "rejected_reason", "loop_state"):
            self.assertIn(key, d)
        self.assertEqual(d["speak"], "几件？")
        self.assertEqual(d["state"], "awaiting_user")

    def test_turn_dict_includes_exec_when_present(self):
        call = llm_returning({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1}})
        t = run_loop(AgentScratchpad(), observation(), llm_call=call,
                     run_capability=runner_returning("success"))
        self.assertIn("exec", t.to_dict("en_US"))
        self.assertEqual(t.to_dict("en_US")["exec"]["status"], "success")

    def test_terminal_states_are_classified(self):
        for state in ("finished", "finished_all", "failed",
                      "budget_exhausted"):
            self.assertTrue(AgentTurn(state=state).is_terminal, state)
        for state in ("running", "awaiting_user", "awaiting_confirmation"):
            self.assertFalse(AgentTurn(state=state).is_terminal, state)


if __name__ == "__main__":                      # pragma: no cover
    unittest.main()
