# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Unit tests for the agent-loop ACTION-VALIDATION layer.

This is the newest security-critical surface in the codebase. Before
v3.0 the model's influence was limited to *classifying* (`intent.py`),
*ordering* (`plan.py`) or *choosing* (`agent.py`). Now the model decides
**what happens next**, so `validate_action` is the load-bearing wall.

The tests therefore concentrate on the boundaries that must never move:

  * the action set is CLOSED — an unknown action is rejected, never
    interpreted or coerced into something adjacent;
  * each action's payload keys are CLOSED — one undeclared key rejects
    the whole action (no "use the good half");
  * `FORBIDDEN_KEYS` is enforced on the payload (naming a model/method
    is a protocol violation) even though the closed key set already
    excludes those names — the blacklist is checked *first* so the
    rejection reason describes intent, not a typo;
  * `call_capability` may only name a capability the server OFFERED;
  * nothing here ever raises: a malformed answer always yields an
    `AgentAction` with `ok=False` and a reason.

Everything runs on the pure core (no Odoo runtime).
"""

import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.agentloop import (  # noqa: E402
    AgentAction, validate_action,
    AGENT_ACTIONS, ACTION_PAYLOAD_KEYS, SCRATCHPAD_KEYS,
    ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER, ACT_ANSWER,
    ACT_FINISH, ACT_DELEGATE_PICK,
    MAX_TEXT_LEN, MAX_SUMMARY_LEN, MAX_PARAMS_KEYS, MAX_OPTIONS,
    _sig_key,
)
from mmux_core.intent import FORBIDDEN_KEYS  # noqa: E402


WHITELIST = ["picking_task_complete", "picking_report_qty",
             "product_lookup"]


# ===================================================================== #
# the closed action set
# ===================================================================== #
class TestClosedActionSet(unittest.TestCase):

    def test_actions_are_exactly_six(self):
        self.assertEqual(len(AGENT_ACTIONS), 6)
        self.assertEqual(tuple(sorted(AGENT_ACTIONS)), tuple(sorted(
            (ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER,
             ACT_ANSWER, ACT_FINISH, ACT_DELEGATE_PICK))))

    def test_every_action_declares_its_payload_keys(self):
        # a missing entry would make ACTION_PAYLOAD_KEYS[action] raise
        # inside validate_action; assert the table is total.
        for action in AGENT_ACTIONS:
            self.assertIn(action, ACTION_PAYLOAD_KEYS)
            self.assertIsInstance(ACTION_PAYLOAD_KEYS[action], frozenset)

    def test_no_action_declares_a_forbidden_key(self):
        """Structural guarantee: the contract cannot itself leak a way to
        name a model or method."""
        for action, keys in ACTION_PAYLOAD_KEYS.items():
            leaked = set(keys) & FORBIDDEN_KEYS
            self.assertEqual(leaked, set(),
                             f"action {action} declares forbidden key(s) "
                             f"{leaked}")

    def test_unknown_action_rejected(self):
        a = validate_action({"action": "execute_sql", "payload": {}}, WHITELIST)
        self.assertFalse(a.ok)
        self.assertEqual(a.rejected_reason, "unknown_action:execute_sql")

    def test_near_miss_action_is_not_coerced(self):
        # a typo must NOT be silently mapped onto the real action
        for bad in ("call_capabilities", "call", "invoke_capability",
                    "CALL_CAPABILITY"):
            a = validate_action({"action": bad, "payload": {}}, WHITELIST)
            self.assertFalse(a.ok, bad)
            self.assertTrue(a.rejected_reason.startswith("unknown_action:"),
                            bad)

    def test_missing_action(self):
        for data in ({}, {"payload": {}}, {"action": None}, {"action": ""},
                     {"action": 17}):
            a = validate_action(data, WHITELIST)
            self.assertFalse(a.ok)
            self.assertEqual(a.rejected_reason, "missing_action")

    def test_not_a_mapping(self):
        for data in (None, "finish", 42, [1, 2], True):
            a = validate_action(data, WHITELIST)
            self.assertFalse(a.ok)
            self.assertEqual(a.rejected_reason, "not_a_mapping")


# ===================================================================== #
# payload shape + closed keys
# ===================================================================== #
class TestPayloadShape(unittest.TestCase):

    def test_payload_optional_becomes_empty(self):
        # finish needs nothing, so a bare action is legal
        a = validate_action({"action": ACT_FINISH}, WHITELIST)
        self.assertTrue(a.ok)
        self.assertEqual(a.payload["summary"], "")

    def test_payload_null_becomes_empty(self):
        a = validate_action({"action": ACT_FINISH, "payload": None}, WHITELIST)
        self.assertTrue(a.ok)

    def test_payload_must_be_a_mapping(self):
        for bad in ("text", [1], 5):
            a = validate_action({"action": ACT_FINISH, "payload": bad},
                                WHITELIST)
            self.assertFalse(a.ok)
            self.assertEqual(a.rejected_reason, "bad_payload")

    def test_undeclared_key_rejects_whole_action(self):
        a = validate_action(
            {"action": ACT_FINISH,
             "payload": {"summary": "done", "extra": "smuggled"}},
            WHITELIST)
        self.assertFalse(a.ok)
        self.assertEqual(a.rejected_reason, "undeclared_payload:extra")
        # the good half is NOT kept
        self.assertEqual(a.payload, {})

    def test_all_undeclared_keys_listed_sorted(self):
        a = validate_action(
            {"action": ACT_FINISH,
             "payload": {"zebra": 1, "apple": 2}},
            WHITELIST)
        self.assertEqual(a.rejected_reason,
                         "undeclared_payload:apple,zebra")

    def test_every_forbidden_key_rejected_on_payload(self):
        """Loop the whole shared blacklist — the same one intent.py uses."""
        for key in sorted(FORBIDDEN_KEYS):
            a = validate_action(
                {"action": ACT_FINISH, "payload": {key: "x"}}, WHITELIST)
            self.assertFalse(a.ok, key)
            self.assertEqual(a.rejected_reason,
                             f"protocol_violation:{key}", key)

    def test_protocol_violation_takes_precedence_over_undeclared(self):
        """`model` is BOTH forbidden and undeclared; the reason must
        describe the protocol violation (intent), not the typo."""
        a = validate_action(
            {"action": ACT_FINISH, "payload": {"model": "res.partner"}},
            WHITELIST)
        self.assertEqual(a.rejected_reason, "protocol_violation:model")

    def test_forbidden_keys_checked_on_every_action(self):
        for action in AGENT_ACTIONS:
            a = validate_action({"action": action,
                                 "payload": {"method": "write"}}, WHITELIST)
            self.assertFalse(a.ok, action)
            self.assertEqual(a.rejected_reason, "protocol_violation:method",
                             action)


# ===================================================================== #
# call_capability — the whitelist anchor
# ===================================================================== #
class TestCallCapability(unittest.TestCase):

    def test_valid_call(self):
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "picking_report_qty",
                         "params": {"quantity": 5}, "record_id": 42}},
            WHITELIST)
        self.assertTrue(a.ok)
        self.assertTrue(a.is_capability_call)
        self.assertEqual(a.capability_id, "picking_report_qty")
        self.assertEqual(a.params, {"quantity": 5})
        self.assertEqual(a.record_id, 42)

    def test_missing_capability_id(self):
        for payload in ({}, {"params": {"x": 1}}, {"capability_id": None},
                        {"capability_id": ""}, {"capability_id": 7}):
            a = validate_action({"action": ACT_CALL_CAPABILITY,
                                 "payload": payload}, WHITELIST)
            self.assertFalse(a.ok)
            self.assertEqual(a.rejected_reason,
                             "call_capability_requires_capability_id")

    def test_capability_outside_whitelist_rejected(self):
        """THE invariant — the model may only name what it was offered."""
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "res_partner_write"}}, WHITELIST)
        self.assertFalse(a.ok)
        self.assertEqual(
            a.rejected_reason,
            "capability_not_whitelisted:res_partner_write")

    def test_empty_whitelist_rejects_everything(self):
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "picking_report_qty"}}, [])
        self.assertFalse(a.ok)
        self.assertIn("capability_not_whitelisted", a.rejected_reason)

    def test_none_whitelist_means_unscoped(self):
        """`allowed_capabilities=None` = "no per-loop scoping", used by the
        Odoo layer when it wants the global enabled set. It still does not
        let an arbitrary *model* through, because the action has no such
        field."""
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "anything_registered"}}, None)
        self.assertTrue(a.ok)

    def test_params_coerced_to_dict(self):
        for bad in ("nope", [1, 2], 5):
            a = validate_action(
                {"action": ACT_CALL_CAPABILITY,
                 "payload": {"capability_id": "product_lookup",
                             "params": bad}}, WHITELIST)
            self.assertTrue(a.ok, bad)
            self.assertEqual(a.params, {})

    def test_params_non_string_keys_dropped(self):
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "product_lookup",
                         "params": {"ok": 1, 5: "bad", None: "bad"}}},
            WHITELIST)
        self.assertTrue(a.ok)
        self.assertEqual(a.params, {"ok": 1})

    def test_params_capped_in_width(self):
        wide = {f"k{i}": i for i in range(MAX_PARAMS_KEYS + 20)}
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "product_lookup", "params": wide}},
            WHITELIST)
        self.assertTrue(a.ok)
        self.assertEqual(len(a.params), MAX_PARAMS_KEYS)

    def test_record_id_coercions(self):
        cases = [("42", 42), (42, 42), (None, None), (True, None),
                 ("$context.res_id", "$context.res_id"),
                 ("abc", None), (3.5, None), ([], None), ({}, None)]
        for raw, expected in cases:
            a = validate_action(
                {"action": ACT_CALL_CAPABILITY,
                 "payload": {"capability_id": "product_lookup",
                             "record_id": raw}}, WHITELIST)
            self.assertTrue(a.ok, raw)
            self.assertEqual(a.record_id, expected, raw)

    def test_confirmed_is_not_a_payload_key(self):
        """The agent can never ask to be pre-confirmed."""
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "product_lookup",
                         "confirmed": True}}, WHITELIST)
        self.assertFalse(a.ok)
        self.assertEqual(a.rejected_reason,
                         "undeclared_payload:confirmed")

    def test_why_bounded(self):
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "product_lookup",
                         "why": "z" * 5000}}, WHITELIST)
        self.assertTrue(a.ok)
        self.assertEqual(len(a.payload["why"]), MAX_TEXT_LEN)


# ===================================================================== #
# propose_step
# ===================================================================== #
class TestProposeStep(unittest.TestCase):

    def _step(self, payload):
        return validate_action({"action": ACT_PROPOSE_STEP,
                                "payload": payload}, WHITELIST)

    def test_valid_step(self):
        a = self._step({"step_id": "s1", "title": "Scan the shelf",
                        "voice_prompt": "Scan shelf A-01",
                        "input_type": "scan", "capability_id": "product_lookup",
                        "params": {}, "hint": "look for the label"})
        self.assertTrue(a.ok)
        self.assertEqual(a.payload["step_id"], "s1")
        self.assertEqual(a.payload["input_type"], "scan")
        self.assertIsNone(a.rejected_reason)

    def test_step_id_required(self):
        a = self._step({"title": "x"})
        self.assertFalse(a.ok)
        self.assertEqual(a.rejected_reason, "propose_step_requires_step_id")

    def test_text_required(self):
        a = self._step({"step_id": "s1"})
        self.assertFalse(a.ok)
        self.assertEqual(a.rejected_reason, "propose_step_requires_text")

    def test_title_alone_is_enough(self):
        self.assertTrue(self._step({"step_id": "s1", "title": "x"}).ok)

    def test_prompt_alone_is_enough(self):
        self.assertTrue(
            self._step({"step_id": "s1", "voice_prompt": "say this"}).ok)

    def test_input_type_closed_set(self):
        """The frontend renders by these only (flow.py GuidedStep rule);
        an unknown value would render nothing, so it degrades to confirm."""
        for raw in ("scan", "voice_qty", "confirm", "capability"):
            a = self._step({"step_id": "s1", "title": "x",
                            "input_type": raw})
            self.assertEqual(a.payload["input_type"], raw, raw)
        for raw in ("drag", "sql", None, 5, "SCAN"):
            a = self._step({"step_id": "s1", "title": "x",
                            "input_type": raw})
            self.assertTrue(a.ok, raw)
            self.assertEqual(a.payload["input_type"], "confirm", raw)

    def test_multilingual_text_preserved(self):
        a = self._step({"step_id": "s1",
                        "voice_prompt": {"zh_CN": "扫描货架",
                                         "en_US": "scan the shelf"}})
        self.assertTrue(a.ok)
        self.assertEqual(a.payload["voice_prompt"]["zh_CN"], "扫描货架")

    def test_step_capability_id_not_whitelist_checked_here(self):
        """A step's capability is bound at execution time by the guard; a
        step that merely *mentions* one is not itself an execution. It is
        still normalized to str-or-None."""
        a = self._step({"step_id": "s1", "title": "x",
                        "capability_id": "not_registered"})
        self.assertTrue(a.ok)
        self.assertEqual(a.payload["capability_id"], "not_registered")

    def test_step_capability_id_type_normalized(self):
        a = self._step({"step_id": "s1", "title": "x", "capability_id": 99})
        self.assertTrue(a.ok)
        self.assertIsNone(a.payload["capability_id"])


# ===================================================================== #
# ask_user / answer / finish / delegate_pick
# ===================================================================== #
class TestOtherActions(unittest.TestCase):

    def test_ask_user_valid(self):
        a = validate_action(
            {"action": ACT_ASK_USER,
             "payload": {"question": "How many did you pick?",
                         "options": ["5", "10"], "field": "quantity"}},
            WHITELIST)
        self.assertTrue(a.ok)
        self.assertEqual(a.payload["question"], "How many did you pick?")
        self.assertEqual(a.payload["options"], ["5", "10"])

    def test_ask_user_requires_question(self):
        for payload in ({}, {"question": ""}, {"question": None},
                        {"options": ["a"]}):
            a = validate_action({"action": ACT_ASK_USER, "payload": payload},
                                WHITELIST)
            self.assertFalse(a.ok)
            self.assertEqual(a.rejected_reason, "ask_user_requires_question")

    def test_ask_user_options_capped_and_cleaned(self):
        opts = [f"opt{i}" for i in range(MAX_OPTIONS + 10)]
        opts.insert(1, 42)               # non-string
        opts.insert(2, "   ")            # blank
        a = validate_action(
            {"action": ACT_ASK_USER,
             "payload": {"question": "q", "options": opts}}, WHITELIST)
        self.assertTrue(a.ok)
        self.assertLessEqual(len(a.payload["options"]), MAX_OPTIONS)
        for o in a.payload["options"]:
            self.assertIsInstance(o, str)
            self.assertTrue(o.strip())

    def test_ask_user_options_non_list_becomes_empty(self):
        a = validate_action(
            {"action": ACT_ASK_USER,
             "payload": {"question": "q", "options": "5"}}, WHITELIST)
        self.assertTrue(a.ok)
        self.assertEqual(a.payload["options"], [])

    def test_answer_valid(self):
        a = validate_action({"action": ACT_ANSWER,
                             "payload": {"text": "The shelf is A-01"}},
                            WHITELIST)
        self.assertTrue(a.ok)
        self.assertEqual(a.payload["text"], "The shelf is A-01")

    def test_answer_requires_text(self):
        for payload in ({}, {"text": ""}):
            a = validate_action({"action": ACT_ANSWER, "payload": payload},
                                WHITELIST)
            self.assertFalse(a.ok)
            self.assertEqual(a.rejected_reason, "answer_requires_text")

    def test_finish_needs_nothing(self):
        a = validate_action({"action": ACT_FINISH, "payload": {}}, WHITELIST)
        self.assertTrue(a.ok)
        self.assertTrue(a.is_terminal)
        self.assertIsNone(a.payload["goal_met"])

    def test_finish_goal_met_flags(self):
        self.assertTrue(validate_action(
            {"action": ACT_FINISH, "payload": {"goal_met": True}},
            WHITELIST).payload["goal_met"])
        self.assertFalse(validate_action(
            {"action": ACT_FINISH, "payload": {"goal_met": False}},
            WHITELIST).payload["goal_met"])
        self.assertIsNone(validate_action(
            {"action": ACT_FINISH, "payload": {"goal_met": "yes"}},
            WHITELIST).payload["goal_met"])

    def test_finish_summary_bounded(self):
        a = validate_action(
            {"action": ACT_FINISH, "payload": {"summary": "s" * 9000}},
            WHITELIST)
        self.assertEqual(len(a.payload["summary"]), MAX_SUMMARY_LEN)

    def test_delegate_pick_is_terminal_handover(self):
        a = validate_action({"action": ACT_DELEGATE_PICK,
                             "payload": {"reason": "nothing pending"}},
                            WHITELIST)
        self.assertTrue(a.ok)
        self.assertFalse(a.is_terminal)   # not a `finish`, a hand-over
        self.assertFalse(a.is_capability_call)

    def test_delegate_pick_takes_no_capability(self):
        a = validate_action(
            {"action": ACT_DELEGATE_PICK,
             "payload": {"capability_id": "product_lookup"}}, WHITELIST)
        self.assertFalse(a.ok)
        self.assertEqual(a.rejected_reason,
                         "undeclared_payload:capability_id")


# ===================================================================== #
# never raises
# ===================================================================== #
class TestNeverRaises(unittest.TestCase):

    def test_adversarial_inputs_all_return(self):
        evil = [
            None, [], "finish", 42, True,
            {"action": "finish", "payload": {"summary": {"__proto__": "x"}}},
            {"action": "ask_user", "payload": {"question": {"zz": "q"}}},
            {"action": "propose_step", "payload": {"step_id": [], "title": 5}},
            {"action": "call_capability",
             "payload": {"capability_id": [], "params": {"a": object()}}},
            {"action": "call_capability", "payload": {"capability_id": "x",
                                                      "record_id": object()}},
            {"action": "answer", "payload": {"text": {"zh_CN": 5}}},
            {"action": "\u0000"},
            {"action": {"nested": "dict"}},
        ]
        for data in evil:
            a = validate_action(data, WHITELIST)        # must not raise
            self.assertIsInstance(a, AgentAction)

    def test_rejected_action_always_carries_a_reason(self):
        for data in (None, {}, {"action": "nope"},
                     {"action": "finish", "payload": {"bad": 1}}):
            a = validate_action(data, WHITELIST)
            self.assertFalse(a.ok)
            self.assertTrue(a.rejected_reason, data)


# ===================================================================== #
# AgentAction contract
# ===================================================================== #
class TestAgentActionObject(unittest.TestCase):

    def test_defaults_inert(self):
        a = AgentAction()
        self.assertFalse(a.ok)
        self.assertIsNone(a.action)
        self.assertEqual(a.payload, {})
        self.assertFalse(a.is_terminal)
        self.assertFalse(a.is_capability_call)

    def test_payload_is_copied_not_aliased(self):
        src = {"capability_id": "product_lookup"}
        a = AgentAction(action=ACT_CALL_CAPABILITY, payload=src, ok=True)
        src["capability_id"] = "mutated"
        self.assertEqual(a.payload["capability_id"], "product_lookup")

    def test_signature_distinguishes_capability_and_record(self):
        a = "picking_report_qty"
        s1 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": a,
                                          "record_id": 1}},
                             WHITELIST).signature
        s2 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": a,
                                          "record_id": 2}},
                             WHITELIST).signature
        s3 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1}},
                             WHITELIST).signature
        self.assertNotEqual(s1, s2)      # different target record
        self.assertNotEqual(s1, s3)      # different capability

    def test_signature_none_record_does_not_collide_with_zero(self):
        """`record_id` DEFAULTS to the context record, so the signature
        must default it to "0" too — otherwise "no target" and "record 0"
        would look like the same step and a real call could be suppressed
        as a repeat."""
        a = validate_action({"action": ACT_CALL_CAPABILITY,
                             "payload": {"capability_id": "product_lookup"}},
                            WHITELIST)
        b = validate_action({"action": ACT_CALL_CAPABILITY,
                             "payload": {"capability_id": "product_lookup",
                                         "record_id": 0}}, WHITELIST)
        self.assertIsNone(a.record_id)
        self.assertEqual(b.record_id, 0)
        self.assertNotEqual(a.signature, b.signature)

    def test_signature_reflects_params_width(self):
        """Params are part of a call's identity: the same capability on
        the same record with different param *keys* is a different call.

        v3.5 (docs/CONFIG.md §14.5): param VALUES now also distinguish
        signatures. The old keys-only rule guarded a scripted loop where
        "same action, tweaked number" was evasion; under the reasoning
        model it is self-correction (wrong quantity -> right quantity),
        and the anti-abuse backstop moved to the total budgets plus the
        progress-based stall test.
        """
        s1 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1,
                                          "params": {"quantity": 5}}},
                             WHITELIST).signature
        s2 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1,
                                          "params": {"lot": "L1"}}},
                             WHITELIST).signature
        self.assertNotEqual(s1, s2)
        # ...and a changed *value* on the same key is a DIFFERENT call:
        # a corrected retry must not be swallowed as a repeat.
        s3 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1,
                                          "params": {"quantity": 9}}},
                             WHITELIST).signature
        self.assertNotEqual(s1, s3)
        # identical params still compare equal, regardless of key order
        s4 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1,
                                          "params": {"quantity": 5,
                                                     "lot": "L1"}}},
                             WHITELIST).signature
        s5 = validate_action({"action": ACT_CALL_CAPABILITY,
                              "payload": {"capability_id": "product_lookup",
                                          "record_id": 1,
                                          "params": {"lot": "L1",
                                                     "quantity": 5}}},
                             WHITELIST).signature
        self.assertEqual(s4, s5)

    def test_signature_stable_for_identical_proposals(self):
        p = {"action": ACT_PROPOSE_STEP,
             "payload": {"step_id": "s1", "title": "one"}}
        q = {"action": ACT_PROPOSE_STEP,
             "payload": {"step_id": "s1", "title": "totally different"}}
        self.assertEqual(validate_action(p, WHITELIST).signature,
                         validate_action(q, WHITELIST).signature)

    def test_sig_key_is_hashable_string(self):
        a = validate_action({"action": ACT_PROPOSE_STEP,
                             "payload": {"step_id": "s1", "title": "t"}},
                            WHITELIST)
        self.assertIsInstance(_sig_key(a), str)
        hash(_sig_key(a))                # must not raise

    def test_to_dict_localizes_texts(self):
        a = validate_action(
            {"action": ACT_ASK_USER,
             "payload": {"question": {"zh_CN": "多少个？",
                                      "en_US": "how many?"}}},
            WHITELIST)
        d = a.to_dict("zh_CN")
        self.assertEqual(d["payload"]["question"], "多少个？")
        self.assertEqual(d["action"], ACT_ASK_USER)
        self.assertTrue(d["ok"])

    def test_to_dict_keeps_multilingual_when_no_lang(self):
        """`to_dict(None)` still resolves via the fallback chain — the raw
        mapping is preserved separately by `AgentAction.payload`, which is
        what the audit record stores (evidence-complete)."""
        a = validate_action(
            {"action": ACT_ASK_USER,
             "payload": {"question": {"zh_CN": "多少个？",
                                      "en_US": "how many?"}}},
            WHITELIST)
        # the raw mapping survives on the action itself
        self.assertEqual(a.payload["question"]["zh_CN"], "多少个？")
        # serialization without a lang falls back to en_US (NORMALIZED_LANG)
        d = a.to_dict()
        self.assertEqual(d["payload"]["question"], "how many?")

    def test_typed_accessors_on_rejected_action(self):
        a = validate_action({"action": "nope"}, WHITELIST)
        self.assertIsNone(a.capability_id)
        self.assertEqual(a.params, {})
        self.assertIsNone(a.record_id)
        self.assertEqual(a.text, "")


# ===================================================================== #
# scratchpad contract
# ===================================================================== #
class TestScratchpad(unittest.TestCase):

    def test_keys_are_closed_and_contain_no_confirmed(self):
        self.assertNotIn("confirmed", SCRATCHPAD_KEYS)
        self.assertIn("turn", SCRATCHPAD_KEYS)
        self.assertIn("capability_calls", SCRATCHPAD_KEYS)

    def test_to_dict_emits_only_declared_keys(self):
        from mmux_core.agentloop import AgentScratchpad
        s = AgentScratchpad(goal="g")
        d = s.to_dict()
        # __slots__ makes the memory physically non-extensible: assigning
        # an undeclared attribute raises rather than being silently kept.
        with self.assertRaises(AttributeError):
            s.injected = "ignored"       # type: ignore[attr-defined]
        self.assertEqual(sorted(d), sorted(SCRATCHPAD_KEYS))

    def test_roundtrip(self):
        from mmux_core.agentloop import AgentScratchpad
        s = AgentScratchpad(goal="pick 10", task_id="wh:1", turn=2,
                            capability_calls=1)
        s.mark_step("s1")
        s.answer("quantity", 10)
        r = AgentScratchpad.from_dict(s.to_dict())
        self.assertEqual(r.goal, "pick 10")
        self.assertEqual(r.task_id, "wh:1")
        self.assertEqual(r.turn, 2)
        self.assertEqual(r.done_steps, ["s1"])
        self.assertEqual(r.user_answers, {"quantity": 10})

    def test_from_dict_drops_unknown_and_survives_garbage(self):
        from mmux_core.agentloop import AgentScratchpad
        r = AgentScratchpad.from_dict({"goal": "g", "confirmed": True,
                                       "junk": 1})
        self.assertEqual(r.goal, "g")
        self.assertFalse(hasattr(r, "confirmed"))
        for bad in (None, "x", 5, [1]):
            self.assertIsInstance(AgentScratchpad.from_dict(bad),
                                  AgentScratchpad)

    def test_mark_step_is_idempotent(self):
        from mmux_core.agentloop import AgentScratchpad
        s = AgentScratchpad()
        s.mark_step("s1")
        s.mark_step("s1")
        s.mark_step(None)
        s.mark_step("")
        self.assertEqual(s.done_steps, ["s1"])

    def test_budgets(self):
        from mmux_core.agentloop import (AgentScratchpad, MAX_AGENT_TURNS,
                                         MAX_AGENT_CAPABILITY_CALLS)
        s = AgentScratchpad()
        self.assertEqual(s.budget_left, MAX_AGENT_TURNS)
        self.assertEqual(s.calls_left, MAX_AGENT_CAPABILITY_CALLS)
        for _ in range(MAX_AGENT_TURNS + 5):
            s.note_turn()
        self.assertEqual(s.budget_left, 0)


# ===================================================================== #
# execution_request — the confirmed=False hardwiring
# ===================================================================== #
class TestExecutionRequest(unittest.TestCase):

    def test_confirmed_is_always_false(self):
        from mmux_core.agentloop import execution_request
        a = validate_action(
            {"action": ACT_CALL_CAPABILITY,
             "payload": {"capability_id": "picking_report_qty",
                         "params": {"quantity": 5}, "record_id": 9}},
            WHITELIST)
        req = execution_request(a)
        self.assertIs(req["confirmed"], False)
        self.assertEqual(req["capability_id"], "picking_report_qty")
        self.assertEqual(req["params"], {"quantity": 5})
        self.assertEqual(req["record_id"], 9)

    def test_wrong_action_yields_none(self):
        from mmux_core.agentloop import execution_request
        self.assertIsNone(execution_request(None))
        self.assertIsNone(execution_request(
            validate_action({"action": ACT_FINISH}, WHITELIST)))


# ===================================================================== #
# step_from_action
# ===================================================================== #
class TestStepFromAction(unittest.TestCase):

    def test_shape_matches_frontend_contract(self):
        from mmux_core.agentloop import step_from_action
        a = validate_action(
            {"action": ACT_PROPOSE_STEP,
             "payload": {"step_id": "s1", "title": "T",
                         "voice_prompt": "V", "input_type": "scan",
                         "capability_id": "product_lookup", "params": {},
                         "hint": "H"}},
            WHITELIST)
        step = step_from_action(a, skill_level="expert")
        for key in ("step_id", "title", "voice_prompt", "input_type",
                    "capability_id", "params", "hint", "skill"):
            self.assertIn(key, step)
        self.assertEqual(step["skill"], "expert")
        self.assertTrue(step["agent_generated"])

    def test_wrong_action_yields_none(self):
        from mmux_core.agentloop import step_from_action
        self.assertIsNone(step_from_action(None))
        self.assertIsNone(step_from_action(
            validate_action({"action": ACT_FINISH}, WHITELIST)))


if __name__ == "__main__":                      # pragma: no cover
    unittest.main()
