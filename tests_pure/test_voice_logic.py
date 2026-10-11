# -*- coding: utf-8 -*-
"""Pure-logic tests for the hands-free voice loop.

`mmux_core/static/src/services/voice_logic.js` holds the decisions that are
easy to get subtly wrong and expensive to get wrong:

* a missed wake word is a broken product (the worker talks and nothing
  happens);
* a false confirmation executes a REAL business action.

Neither needs a browser, so the logic is plain functions and is tested
here by running them under Node and asserting on the results.  The test is
skipped (not failed) when Node is unavailable, so the pure suite stays
runnable in a bare Python environment.

The safety cases below are the point of this file.  They are written to
fail loudly if someone later "simplifies" consent detection into a
substring check.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGIC = os.path.join(ROOT, "mmux_core", "static", "src", "services",
                     "voice_logic.js")

NODE = shutil.which("node")


def _run_js(body):
    """Evaluate `body` with the voice-logic module in scope, return JSON."""
    script = (
        'import(%s).then((m) => {\n'
        "  const out = (() => { %s })();\n"
        "  process.stdout.write(JSON.stringify(out));\n"
        "}).catch((e) => { console.error(e); process.exit(1); });\n"
    ) % (json.dumps(LOGIC), body)
    proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                          capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


@unittest.skipUnless(NODE, "node is not available")
class TestWakeWord(unittest.TestCase):
    """The wake word must fire on natural speech but not on stray words."""

    def test_the_wake_word_is_recognised_alone(self):
        out = _run_js("return m.matchWakeWord('助手');")
        self.assertTrue(out["matched"])

    def test_the_wake_word_is_recognised_mid_sentence(self):
        """People interject before it; strict utterance matching would
        make the feature feel broken."""
        out = _run_js("return m.matchWakeWord('那个…助手');")
        self.assertTrue(out["matched"])

    def test_the_instruction_rides_along_with_the_wake_word(self):
        """'助手 扫描货架A-01' in one breath must not lose the instruction."""
        out = _run_js("return m.matchWakeWord('助手 扫描货架A-01');")
        self.assertTrue(out["matched"])
        self.assertIn("扫描", out["rest"])

    def test_punctuation_and_case_do_not_matter(self):
        out = _run_js("return m.matchWakeWord('Hey, Assistant.');")
        self.assertTrue(out["matched"])

    def test_the_latin_wake_word_is_not_matched_inside_a_longer_word(self):
        """Otherwise 'assistantship' would arm the microphone."""
        out = _run_js("return m.matchWakeWord('assistantship meeting');")
        self.assertFalse(out["matched"])

    def test_unrelated_speech_does_not_arm_the_loop(self):
        out = _run_js("return m.matchWakeWord('扫描货架 A-01');")
        self.assertFalse(out["matched"])


@unittest.skipUnless(NODE, "node is not available")
class TestConsentClassification(unittest.TestCase):
    """Consent gates real business actions -- these are the safety tests."""

    def test_a_bare_affirmative_is_consent(self):
        for word in ("确认", "确定", "confirm", "yes"):
            out = _run_js(f"return m.classifyUtterance({json.dumps(word)});")
            self.assertEqual(out, "confirm", f"{word!r} should be consent")

    def test_a_bare_negative_is_a_cancellation(self):
        for word in ("取消", "放弃", "cancel", "no"):
            out = _run_js(f"return m.classifyUtterance({json.dumps(word)});")
            self.assertEqual(out, "cancel", f"{word!r} should cancel")

    def test_a_NEGATED_affirmative_is_NOT_consent(self):
        """'别确认' contains 确认. Treating that as consent would execute a
        high-risk action on the exact words that refused it."""
        out = _run_js("return m.classifyUtterance('别确认');")
        self.assertEqual(out, "content")

    def test_an_affirmative_inside_a_longer_sentence_is_NOT_consent(self):
        """'ok let me think' must not approve anything."""
        out = _run_js("return m.classifyUtterance('ok let me think');")
        self.assertEqual(out, "content")

    def test_consent_is_refused_when_nothing_is_pending(self):
        """A stray '确认' during ordinary work must not pre-approve a later
        step, so consent words only count while a confirmation is open."""
        out = _run_js(
            "return m.classifyUtterance('确认', {allowsConsent: false});")
        self.assertEqual(out, "content")

    def test_ordinary_instructions_pass_through_as_content(self):
        out = _run_js("return m.classifyUtterance('数量 十二');")
        self.assertEqual(out, "content")

    def test_an_empty_transcript_is_not_content(self):
        out = _run_js("return m.classifyUtterance('   ');")
        self.assertEqual(out, "empty")


@unittest.skipUnless(NODE, "node is not available")
class TestNavigationCommands(unittest.TestCase):
    """Full hands-free also needs the workflow to move without taps."""

    def test_next_is_a_command_in_the_task_loop(self):
        out = _run_js(
            "return m.classifyUtterance('下一个', {context: 'taskloop'});")
        self.assertEqual(out, "next")

    def test_a_QUESTION_about_the_next_step_does_NOT_advance(self):
        """'下一步是什么' contains 下一个. Advancing on it would skip real
        work because the worker merely asked what was coming."""
        out = _run_js(
            "return m.classifyUtterance('下一步是什么', {context: 'taskloop'});")
        self.assertEqual(out, "content")

    def test_help_is_a_command_where_help_exists(self):
        for ctx in ("taskloop", "guide", "agent"):
            out = _run_js(
                f"return m.classifyUtterance('怎么办', {{context: {json.dumps(ctx)}}});")
            self.assertEqual(out, "help", f"{ctx} should accept help")

    def test_skip_is_not_a_command_in_the_panel(self):
        """The panel has no step to skip, so the word is just speech --
        otherwise it would silently discard something the worker did not
        mean to discard."""
        out = _run_js(
            "return m.classifyUtterance('跳过', {context: 'panel'});")
        self.assertEqual(out, "content")

    def test_skip_is_a_command_in_the_guide(self):
        out = _run_js(
            "return m.classifyUtterance('跳过', {context: 'guide'});")
        self.assertEqual(out, "skip")

    def test_repeat_works_where_prompts_are_spoken(self):
        for ctx in ("panel", "guide", "taskloop", "agent"):
            out = _run_js(
                f"return m.classifyUtterance('再说一次', {{context: {json.dumps(ctx)}}});")
            self.assertEqual(out, "repeat")

    def test_an_UNKNOWN_context_yields_no_commands(self):
        """A typo must not widen what a word is allowed to do."""
        out = _run_js(
            "return m.classifyUtterance('下一个', {context: 'taskloup'});")
        self.assertEqual(out, "content")

    def test_consent_WINS_over_a_navigation_command(self):
        """While a confirmation is pending, '确认' is consent even in a
        context that has commands -- the ordering must not be reversible."""
        out = _run_js(
            "return m.classifyUtterance('确认', {context: 'taskloop', allowsConsent: true});")
        self.assertEqual(out, "confirm")


@unittest.skipUnless(NODE, "node is not available")
class TestSilenceDetector(unittest.TestCase):
    """End-of-speech detection replaces the 'tap to stop' button."""

    def _feed(self, samples, dt=50):
        """Push a level sequence and report the verdict."""
        return _run_js(
            "const d = new m.SilenceDetector({threshold: 0.02, holdMs: 800, maxMs: 15000});\n"
            "  let verdict = null;\n"
            f"  for (const lvl of {json.dumps(samples)}) {{\n"
            "    verdict = d.push(lvl, %d) || verdict;\n"
            "    if (verdict) break;\n"
            "  }\n"
            "  return verdict || {done: false};" % dt)

    def test_silence_alone_never_ends_an_utterance(self):
        """The worker has not started talking; finishing here would submit
        an empty clip."""
        out = self._feed([0.0] * 40)
        self.assertFalse(out["done"])

    def test_a_short_pause_mid_sentence_does_not_end_it(self):
        """'数量... 十二' must survive the ellipsis: a 300ms gap is shorter
        than the 800ms hold."""
        samples = [0.3] * 4 + [0.0] * 6 + [0.3] * 4
        out = self._feed(samples)
        self.assertFalse(out["done"], "a 300ms gap ended the utterance")

    def test_a_long_pause_after_speech_ends_it(self):
        """>800ms of quiet means the sentence is over."""
        samples = [0.3] * 4 + [0.0] * 18
        out = self._feed(samples)
        self.assertTrue(out["done"])
        self.assertEqual(out["reason"], "silence")
        self.assertTrue(out["sawSpeech"])

    def test_a_noisy_room_cannot_record_forever(self):
        """Continuous loud input hits the hard cap instead of hanging."""
        out = self._feed([0.5] * 400)
        self.assertTrue(out["done"])
        self.assertEqual(out["reason"], "timeout")


@unittest.skipUnless(NODE, "node is not available")
class TestRms(unittest.TestCase):
    def test_silence_has_zero_level(self):
        out = _run_js("return m.rmsFromWaveform(new Float32Array(64));")
        self.assertEqual(out, 0)

    def test_a_loud_signal_has_a_high_level(self):
        out = _run_js(
            "const a = new Float32Array(64); a.fill(0.8);"
            "  return m.rmsFromWaveform(a);")
        self.assertAlmostEqual(out, 0.8, places=5)

    def test_byte_encoded_samples_are_normalised(self):
        """AnalyserNode may hand back 0..255 bytes; those must land in
        0..1 rather than being compared against a fraction threshold."""
        out = _run_js(
            "const a = new Float32Array(64); a.fill(128);"
            "  return m.rmsFromWaveform(a);")
        self.assertLess(out, 0.05)


if __name__ == "__main__":
    unittest.main()
