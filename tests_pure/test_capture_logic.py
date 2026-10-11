# -*- coding: utf-8 -*-
"""Pure-logic tests for device capture (photo + barcode).

`mmux_core/static/src/capture/capture_logic.js` holds the decisions behind
photographing and scanning that are easy to get subtly wrong:

* how far to shrink a frame (too big and the upload stalls on a field
  network, too small and a nameplate becomes unreadable);
* how aggressive barcode de-duplication should be -- a camera re-reads the
  same code every frame, so no de-duplication means one scan submitted
  dozens of times, while too much means two items in a row collapse into
  one;
* how to read a `/mmux/vision` reply, whose shape varies and which -- unlike
  `/mmux/transcribe` -- carries no `fallback` key.

None of it needs a browser, so the logic is plain functions and is
exercised here under Node.  Skipped (not failed) when Node is unavailable,
matching `test_voice_logic.py`.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGIC = os.path.join(ROOT, "mmux_core", "static", "src", "capture",
                     "capture_logic.js")

NODE = shutil.which("node")


def _run_js(body):
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
class TestFitWithin(unittest.TestCase):
    """Downscaling must preserve the aspect ratio and never upscale."""

    def test_a_phone_photo_is_capped_on_its_long_edge(self):
        # A 12MP frame is typical; 4032x3024 -> 1280x960 keeps 4:3.
        got = _run_js('return m.fitWithin(4032, 3024, 1280);')
        self.assertEqual(got, {"width": 1280, "height": 960})

    def test_a_portrait_photo_is_capped_on_the_other_edge(self):
        got = _run_js('return m.fitWithin(3024, 4032, 1280);')
        self.assertEqual(got, {"width": 960, "height": 1280})

    def test_an_already_small_frame_is_left_alone(self):
        # Upscaling would only make the file bigger and blurrier.
        got = _run_js('return m.fitWithin(640, 480, 1280);')
        self.assertEqual(got, {"width": 640, "height": 480})

    def test_the_long_edge_never_exceeds_the_cap(self):
        for w, h in ((8000, 6000), (6000, 8000), (4000, 1), (1, 4000)):
            got = _run_js(f"return m.fitWithin({w}, {h}, 1280);")
            self.assertLessEqual(max(got["width"], got["height"]), 1280,
                                 f"{w}x{h} overflows the cap")

    def test_a_very_thin_frame_does_not_round_to_zero(self):
        # A 0-pixel dimension makes canvas throw; clamping keeps it valid.
        got = _run_js('return m.fitWithin(4000, 2, 1280);')
        self.assertGreaterEqual(got["width"], 1)
        self.assertGreaterEqual(got["height"], 1)

    def test_a_degenerate_frame_yields_no_canvas(self):
        for w, h in ((0, 0), (-5, 10), (None, None)):
            got = _run_js(f"return m.fitWithin({json.dumps(w)}, "
                          f"{json.dumps(h)}, 1280);")
            self.assertEqual(got, {"width": 0, "height": 0})


class TestBarcodeDeduper(unittest.TestCase):
    """A camera re-reads the same code every frame; one scan is one event."""

    def test_the_same_code_inside_the_window_is_not_reported_twice(self):
        got = _run_js(
            "const d = new m.BarcodeDeduper({windowMs: 1200});\n"
            "return [d.accept('ABC123', 1000), d.accept('ABC123', 1100)];"
        )
        self.assertEqual(got, [True, False])

    def test_the_same_code_after_the_window_is_a_new_scan(self):
        # Scanning the same item again deliberately must still work.
        got = _run_js(
            "const d = new m.BarcodeDeduper({windowMs: 1200});\n"
            "return [d.accept('ABC123', 1000), d.accept('ABC123', 5000)];"
        )
        self.assertEqual(got, [True, True])

    def test_two_different_codes_in_a_row_both_report(self):
        # The narrow rule that matters: only *identical* values collapse.
        got = _run_js(
            "const d = new m.BarcodeDeduper({windowMs: 1200});\n"
            "return [d.accept('AAA', 1000), d.accept('BBB', 1010)];"
        )
        self.assertEqual(got, [True, True])

    def test_a_long_stare_does_not_re_fire_when_the_window_lapses(self):
        # Holding the camera on one code keeps refreshing the window, so
        # it does not buzz once per second forever.
        got = _run_js(
            "const d = new m.BarcodeDeduper({windowMs: 1200});\n"
            "const out = [];\n"
            "for (let t = 0; t < 6000; t += 500) out.push(d.accept('X', t));\n"
            "return out;"
        )
        self.assertEqual(got[0], True)
        self.assertFalse(any(got[1:]), "the same held code must not re-fire")

    def test_empty_readings_are_never_reported(self):
        got = _run_js(
            "const d = new m.BarcodeDeduper();\n"
            "return [d.accept(''), d.accept('   '), d.accept(null)];"
        )
        self.assertEqual(got, [False, False, False])


class TestPickVisionText(unittest.TestCase):
    """Every shape `/mmux/vision` can return, including no text at all."""

    def test_a_plain_text_field_wins(self):
        got = _run_js('return m.pickVisionText({text: "  SN-12345  "});')
        self.assertEqual(got, "SN-12345")

    def test_a_parsed_data_object_yields_its_text(self):
        got = _run_js('return m.pickVisionText({data: {text: "AB-99"}});')
        self.assertEqual(got, "AB-99")

    def test_an_unparsed_raw_json_is_read(self):
        # Provider could not parse the model's answer, so it arrives as
        # `raw` with `data: null` -- this is the common case.
        got = _run_js(
            'return m.pickVisionText({data: null, raw: \'{"text": "Z-7"}\'});'
        )
        self.assertEqual(got, "Z-7")

    def test_prose_in_raw_is_still_a_usable_reading(self):
        got = _run_js(
            'return m.pickVisionText({data: null, raw: "the label reads Q-1"});'
        )
        self.assertEqual(got, "the label reads Q-1")

    def test_an_error_yields_nothing(self):
        # Deliberately not a fallback: the caller must say "could not read"
        # rather than fill a field from a broken reply.
        got = _run_js('return m.pickVisionText({error: "vision unavailable"});')
        self.assertIsNone(got)

    def test_an_empty_reply_yields_nothing(self):
        for payload in ("{}", '{data: null, raw: ""}', "null"):
            got = _run_js(f"return m.pickVisionText({payload});")
            self.assertIsNone(got, payload)

    def test_the_vision_reply_has_no_fallback_key_to_rely_on(self):
        """Guard against copying the transcription handling.

        `/mmux/transcribe` answers `{error, fallback}`; `/mmux/vision` does
        not.  If someone assumes symmetry, an OCR failure would write
        `undefined` into a field.
        """
        got = _run_js(
            'return m.pickVisionText({error: "x", fallback: "text"});'
        )
        self.assertIsNone(got)


class TestPickQuantity(unittest.TestCase):
    """Filling a quantity step from OCR must never invent a number."""

    def test_a_bare_number_is_read(self):
        self.assertEqual(_run_js('return m.pickQuantity("120");'), 120)

    def test_a_number_inside_a_sentence_is_read(self):
        got = _run_js('return m.pickQuantity("counted 48 cartons");')
        self.assertEqual(got, 48)

    def test_no_number_yields_null_not_zero(self):
        # The value can reach a stock move, so "unknown" must stay unknown.
        got = _run_js('return m.pickQuantity("no reading");')
        self.assertIsNone(got)

    def test_an_empty_reading_yields_null(self):
        for text in ('""', "null", "undefined"):
            got = _run_js(f"return m.pickQuantity({text});")
            self.assertIsNone(got, text)


if __name__ == "__main__":
    unittest.main()
