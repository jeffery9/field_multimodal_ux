# -*- coding: utf-8 -*-
"""Contract tests for the v3.0 frontend <-> server boundary.

There is no browser in this test suite, and there should not be: what can
actually rot in the OWL layer is not its rendering but its *vocabulary*.
The trace component branches on server vocabulary — states, actions,
sources, field names — and every one of those branches is a claim about
what the server can produce. A claim that is wrong is worse than a
missing feature: it renders an empty panel, or a badge that says
"autonomous" over a scripted fallback.

So these tests pin the *contract*, in the cheapest place to check it:
the source text of the assets, read next to the pure core that defines
the vocabulary. They are deliberately text-based rather than an import:
the assets are ES modules built by Odoo's bundler and are not importable
here, but the names they use are stable, and that is exactly what we
assert on.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.agentloop import (  # noqa: E402
    AGENT_ACTIONS, LOOP_SOURCE_LLM, LOOP_SOURCE_NONE, LOOP_SOURCE_RECIPE,
    LOOP_STATES,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mmux_core", "static", "src")


def read(rel):
    with open(os.path.join(SRC, rel), encoding="utf-8") as fh:
        return fh.read()


def _method_body(source, name):
    """The body of one `name(...) { ... }` method, BRACE-MATCHED.

    Two things the naive version got wrong, both of which made this test
    read the wrong text while still passing:

    * `split("}\\n")` stops at the first closing brace, which is fine
      only while a method has no nested blocks — and stops being fine the
      moment one gains an `if`;
    * the FIRST occurrence of `name(` is usually in the docstring ABOVE
      the method (`... "agentStart mints the loop key"`), not in the
      declaration. Anchoring on the declaration form `name(...) {` is
      what makes this read the code rather than the prose about it.
    """
    pattern = re.compile(
        rf"\n\s*{re.escape(name)}\([^\n]*?\)\s*\{{")
    match = pattern.search(source)
    if not match:
        return ""
    open_brace = match.end() - 1
    depth = 0
    for i in range(open_brace, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace:i + 1]
    return source[open_brace:]


def const_block(source, name):
    """The literal body of one exported `const NAME = {...};`."""
    match = re.search(rf"export const {name} = \{{(.*?)\n\}};", source, re.S)
    return match.group(1) if match else ""


def const_keys(source, name):
    return set(re.findall(r"^\s{4}([a-z_]+):", const_block(source, name), re.M))


class TestFrontendVocabulary(unittest.TestCase):
    """Every label table the UI uses must be a *subset* of what the server
    can emit, and must cover it — an unlabelled state is a blank badge."""

    @classmethod
    def setUpClass(cls):
        cls.api_js = read("services/mmux_api.js")

    def test_state_labels_cover_every_loop_state(self):
        keys = const_keys(self.api_js, "AGENT_STATE_LABELS")
        self.assertEqual(keys, set(LOOP_STATES))

    def test_action_labels_cover_every_agent_action(self):
        keys = const_keys(self.api_js, "AGENT_ACTION_LABELS")
        self.assertEqual(keys, set(AGENT_ACTIONS))

    def test_source_labels_cover_every_loop_source(self):
        keys = const_keys(self.api_js, "AGENT_SOURCE_LABELS")
        self.assertEqual(
            keys,
            {LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE, LOOP_SOURCE_NONE})

    def test_no_label_table_invents_a_state(self):
        """A label for a state the server cannot produce would create a
        branch that can never run — dead UI that hides a real gap."""
        keys = const_keys(self.api_js, "AGENT_STATE_LABELS")
        self.assertFalse(keys - set(LOOP_STATES))

    def test_no_label_table_invents_an_action(self):
        keys = const_keys(self.api_js, "AGENT_ACTION_LABELS")
        self.assertFalse(keys - set(AGENT_ACTIONS))

    def test_neighbouring_status_table_is_untouched(self):
        """`STATUS_LABELS` is the v1.x execution vocabulary and must not be
        conflated with the agent's loop vocabulary."""
        keys = const_keys(self.api_js, "STATUS_LABELS")
        self.assertIn("success", keys)
        self.assertIn("unknown", keys)
        self.assertNotIn("budget_exhausted", keys)


class TestFrontendNeverBypassesTheFunnel(unittest.TestCase):
    """The trace component is a renderer and a pump. If it ever grew an
    execute path of its own, the "pure agent" switch would quietly have
    become a permission expansion — the one thing it must never be."""

    @classmethod
    def setUpClass(cls):
        cls.js = read("agentloop/agent_trace.js")
        cls.xml = read("agentloop/agent_trace.xml")

    def test_never_calls_the_execute_route(self):
        self.assertNotIn("/mmux/execute", self.js)
        self.assertNotIn("api.execute(", self.js)

    def test_never_constructs_an_execute_request(self):
        self.assertNotIn("capability_id:", self.js)

    def test_confirmed_is_sent_from_exactly_one_place(self):
        sends = re.findall(r"confirmed:\s*true", self.js)
        self.assertEqual(len(sends), 1)

    def test_confirmed_is_sent_only_through_the_gate_helper(self):
        self.assertIn("advance({ confirmed: true })", self.js)

    def test_no_confirmed_true_in_the_template(self):
        """The flag must come from a click handler, never from markup that
        could be evaluated on render."""
        self.assertNotIn("confirmed: true", self.xml)
        self.assertNotIn("confirmed:true", self.xml)

    def test_gate_button_is_gated_on_the_server_flag(self):
        self.assertIn('t-if="isAwaitingConfirmation"', self.xml)

    def test_auto_pump_is_suppressed_across_a_park(self):
        """A mid-loop step (an answer, a proposal) should not require a
        click. A *park* is the opposite: it exists to hand control back to
        a person, so the pump must never cross it — checked both when the
        timer is queued and when it fires."""
        self.assertRegex(self.js, r"res\.state === \"running\" && !this\.isParked")
        self.assertIn("this._alive && !this.isParked && !this.state.busy",
                      self.js)

    def test_auto_pump_is_single_flight(self):
        self.assertIn("!this.state.autoPumped", self.js)
        self.assertIn("this.state.autoPumped = true", self.js)

    def test_answer_box_is_not_cleared_before_the_server_confirms(self):
        """A failed submit must not eat what the operator typed."""
        submit = self.js.split("async submitAnswer()", 1)[1].split("}", 1)[0]
        self.assertNotIn("this.state.askAnswer = \"\"", submit)
        self.assertIn("if (!res.awaiting_user)", self.js)

    def test_unknown_vocabulary_renders_rather_than_disappears(self):
        """A server-side action added later must show up in the trace as
        its raw value. Silently dropping it would make the trace lie."""
        self.assertIn("AGENT_ACTION_LABELS[act.action] || act.action", self.js)
        self.assertIn("AGENT_SOURCE_LABELS[source] || source", self.js)
        self.assertIn("AGENT_STATE_LABELS[st] || st", self.js)

    def test_degradation_is_visually_distinguished(self):
        self.assertIn("isDegraded", self.js)
        self.assertIn("isDegraded ? 'bg-secondary' : 'bg-primary'", self.xml)

    def test_agent_disabled_has_its_own_exit(self):
        """A disabled feature flag must degrade to the scripted guide, so
        the component needs to recognize the code, not just the message."""
        self.assertIn("agent_disabled", self.js)


class TestFrontendApiSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read("services/mmux_api.js")

    def test_agent_start_targets_the_start_route(self):
        self.assertIn("/mmux/agent/start", self.js)
        self.assertIn("agentStart(", self.js)

    def test_agent_turn_targets_the_turn_route(self):
        self.assertIn("/mmux/agent/turn", self.js)
        self.assertIn("agentTurn(", self.js)

    def test_agent_turn_can_carry_an_answer_and_a_confirmation(self):
        body = self.js.split("agentTurn(loopKey", 1)[1].split("}\n", 1)[0]
        self.assertIn("answer", body)
        self.assertIn("confirmed", body)

    def test_every_api_method_is_a_single_jsonrpc_call(self):
        """No caching, no retries with a fresh key: the loop's idempotency
        is the server's business (agentStart mints the loop key)."""
        for name in ("agentStart", "agentTurn"):
            body = _method_body(self.js, name)
            self.assertEqual(body.count("jsonrpc("), 1, name)

    def test_client_never_mints_the_agent_loop_key(self):
        """The loop key is the *server's* handle on the agent's memory.
        If the client minted one, a reload could not resume the loop."""
        body = _method_body(self.js, "agentStart")
        self.assertNotIn("newIdempotencyKey", body)

    def test_agent_start_omits_the_signal_when_there_is_none(self):
        """`signal: undefined` and `signal: {}` mean DIFFERENT things to
        the server ("not applicable" vs "I looked and found nothing"), and
        it answers them differently — the first keeps v3.0 behaviour, the
        second asks the operator a question. So the key must be omitted,
        never defaulted."""
        body = _method_body(self.js, "agentStart")
        self.assertIn("signal !== undefined && signal !== null", body)
        self.assertIn("params.signal = signal", body)


class TestTaskLoopMount(unittest.TestCase):
    """The task loop is where the architecture switch becomes visible."""

    @classmethod
    def setUpClass(cls):
        cls.js = read("taskloop/taskloop.js")
        cls.xml = read("taskloop/taskloop.xml")

    def test_trace_is_imported_and_registered(self):
        self.assertIn("import { MmuxAgentTrace }", self.js)
        self.assertIn("MmuxAgentTrace", self.js)

    def test_trace_is_mounted_in_the_template(self):
        self.assertIn("<MmuxAgentTrace", self.xml)

    def test_mount_is_bound_to_the_loop_key(self):
        block = self.xml.split("<MmuxAgentTrace", 1)[1].split("/>", 1)[0]
        self.assertIn("loopKey", block)

    def test_both_drivers_are_reachable(self):
        """Scripted is not merely tolerated — it is the agent's own
        fallback, and field connectivity is not a given."""
        self.assertIn("setGuideMode('agent')", self.xml)
        self.assertIn("setGuideMode('scripted')", self.xml)

    def test_mode_is_sent_to_the_server(self):
        self.assertIn("taskGuide(", self.js)
        self.assertIn("mode", self.js)

    def test_downgrade_is_reported_to_the_operator(self):
        self.assertIn("已切换到脚本化引导", self.js)


class TestTemplateHygiene(unittest.TestCase):
    """Cheap structural checks that catch the mistakes a bundler would
    otherwise only report at page load."""

    TEMPLATES = ("agentloop/agent_trace.xml", "taskloop/taskloop.xml",
                 "workspace/workspace.xml", "guide/guide.xml",
                 "panel/panel.xml", "systray/systray_item.xml")

    def test_every_template_declares_a_name(self):
        for rel in self.TEMPLATES:
            with self.subTest(template=rel):
                self.assertIn("t-name=", read(rel))

    def test_every_template_uses_double_colons_in_handlers(self):
        """`t-on-click="fn()"` is correct; `t-on-click:fn()` is not. Both
        forms appear in the wild and only one builds."""
        for rel in self.TEMPLATES:
            with self.subTest(template=rel):
                src = read(rel)
                bad = re.findall(r"t-(?:on|if|elif|else|foreach|key|out|att)"
                                 r"_[a-z]", src)
                self.assertEqual(bad, [])

    def test_agent_trace_action_bar_is_an_exclusive_chain(self):
        """The confirmation gate, the answer box, the "next step" button
        and the terminal restart must be ONE mutually exclusive chain.
        Showing "continue" beside "confirm this write" invites the wrong
        click — which on an L2 capability is an irreversible posting.

        Asserted on the chain's own `<t>` branches, so a nested (and
        legitimate) if/else *inside* one branch does not confuse it.
        """
        src = read("agentloop/agent_trace.xml")
        bar = src.split("action bar", 1)[1].split("the trace", 1)[0]
        # the chain lives on indented <t> elements, one per branch
        branches = re.findall(r"^\s{8}<t (t-if|t-elif|t-else)=\"([^\"]*)\"",
                              bar, re.M)
        self.assertEqual(
            branches,
            [("t-if", "isAwaitingConfirmation"),
             ("t-elif", "isAwaitingUser"),
             ("t-elif", "!isParked"),
             ("t-else", "")],
            branches)
        # and no competing top-level branch was added alongside it
        self.assertEqual(bar.count('t-if="isAwaitingConfirmation"'), 1)
        self.assertEqual(bar.count('t-elif="'), 2)

    def test_js_files_are_es_modules(self):
        for rel in ("agentloop/agent_trace.js", "services/mmux_api.js",
                    "taskloop/taskloop.js"):
            with self.subTest(module=rel):
                self.assertIn("/** @odoo-module **/", read(rel))


if __name__ == "__main__":
    unittest.main()
