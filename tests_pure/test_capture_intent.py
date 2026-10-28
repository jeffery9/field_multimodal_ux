# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Pure-logic tests for the shared capture policy.

`capture_intent.js` and `capture_service.js` are where "which device does
this step want, and where does the result go" is decided ONCE for five
interfaces.  The failure mode this file exists to catch is divergence: if
each screen re-derived the answer, "拍照" would auto-submit on one screen
and fill a draft on another, and a mis-read photo would become an
unauthorized business write on exactly one of them.

So the assertions here are about *policy*, not implementation:

* a decision step (`confirm`, `capability`) offers no camera at all, because
  a camera there is a way to manufacture a decision;
* an OCR result lands in the field the step already has, and never in a
  field it does not have;
* a photo is only OCR'd when recognition is the point of the step;
* `photosParam` always produces `ir.attachment`'s shape (bare base64),
  whatever the caller passed in.

Skipped (not failed) when Node is unavailable.

The two modules are tested separately on purpose.  `capture_intent.js` is
pure -- no imports at all -- so `node` can load it directly.  The parts of
`capture_service.js` that are worth pinning (`photosParam`,
`captureNotice`) are equally pure but live behind an import of the Odoo
API client, so `test_capture_service.py` extracts those functions and runs
them standalone.  Testing them *through* the module would only prove that
`node` cannot resolve `@odoo-module` imports.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTENT = os.path.join(ROOT, "mmux_core", "static", "src", "capture",
                      "capture_intent.js")

NODE = shutil.which("node")


def _run_js(module_path, body):
    """Run `body` with the module bound to `m`; return its JSON result."""
    script = (
        'import(%s).then((m) => {\n'
        "  const out = (() => { %s })();\n"
        "  process.stdout.write(JSON.stringify(out));\n"
        "}).catch((e) => { console.error(e); process.exit(1); });\n"
    ) % (json.dumps(module_path), body)
    proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                          capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


def _run(body):
    return _run_js(INTENT, body)


@unittest.skipUnless(NODE, "node is not available")
class TestCaptureIntent(unittest.TestCase):
    """The step -> camera table."""

    def test_scan_step_wants_a_barcode(self):
        got = _run("return m.captureIntentFor('scan');")
        self.assertEqual(got["purpose"], "scan")
        self.assertEqual(got["field"], "barcode")

    def test_ocr_step_wants_a_photo_that_is_read(self):
        got = _run("return m.captureIntentFor('ocr');")
        self.assertEqual(got["purpose"], "ocr")
        self.assertEqual(got["field"], "text")

    def test_evidence_step_files_the_photo_itself(self):
        got = _run("return m.captureIntentFor('evidence');")
        self.assertEqual(got["purpose"], "evidence")
        self.assertEqual(got["field"], "photos")

    def test_quantity_step_reads_a_number_never_a_caption(self):
        """A quantity step may photograph a nameplate, but the photo is
        only ever read for a number -- it is never filed as the answer."""
        got = _run("return m.captureIntentFor('voice_qty');")
        self.assertEqual(got["purpose"], "ocr")
        self.assertEqual(got["field"], "quantity")
        self.assertEqual(
            _run("return m.captureIntentFor('qty');"),
            {"purpose": "ocr", "field": "quantity"},
        )

    def test_decision_steps_offer_no_camera(self):
        """A camera on a confirm/capability step would be a way to
        manufacture a decision the worker never made."""
        for step in ("confirm", "capability"):
            with self.subTest(step=step):
                self.assertIsNone(
                    _run(f"return m.captureIntentFor({json.dumps(step)});"))

    def test_unknown_and_empty_steps_are_refused(self):
        for step in ("", "  ", "wat", "SCAN"):
            with self.subTest(step=step):
                self.assertIsNone(_run(
                    f"return m.captureIntentFor({json.dumps(step)});"))

    def test_missing_step_does_not_throw(self):
        for arg in ("undefined", "null"):
            with self.subTest(arg=arg):
                self.assertIsNone(_run(f"return m.captureIntentFor({arg});"))


@unittest.skipUnless(NODE, "node is not available")
class TestStepField(unittest.TestCase):
    """Where a value may land.  A value must never be smuggled into a
    field the step does not have -- `_advance` is what writes to the
    business record."""

    def test_only_real_fields_are_ever_returned(self):
        got = _run(
            "return ['scan','ocr','evidence','voice_qty','confirm']"
            ".map((s) => m.stepFieldFor(s));")
        self.assertEqual(got, ["barcode", "text", "photos", "quantity", None])

    def test_value_for_quantity_refuses_text_without_a_number(self):
        """Returning null (not 0, not the raw text) is the whole point:
        the caller asks again rather than entering a wrong quantity."""
        got = _run("return m.valueForField('quantity', '看不清', null);")
        self.assertIsNone(got)

    def test_value_for_quantity_returns_the_parsed_number(self):
        self.assertEqual(
            _run("return m.valueForField('quantity', '120', 120);"), 120)

    def test_value_for_barcode_strips_ocr_separators(self):
        got = _run(
            "return m.valueForField('barcode', 'SN 12-345, 67', null);")
        self.assertEqual(got, "SN1234567")

    def test_empty_text_is_refused_for_every_field(self):
        got = _run(
            "return ['text','barcode','quantity'].map("
            "(f) => m.valueForField(f, '   ', 5));")
        self.assertEqual(got, [None, None, None])


@unittest.skipUnless(NODE, "node is not available")
class TestShouldRecognize(unittest.TestCase):
    """Whether a captured photo is also read.  An evidence photo is the
    record; running OCR over it would invite a value to be typed out of a
    picture, which is a different and unconfirmed write."""

    def test_reading_steps_are_recognized(self):
        for step in ("ocr", "receipt", "voice_qty", "qty"):
            with self.subTest(step=step):
                self.assertTrue(
                    _run(f"return m.shouldRecognize({json.dumps(step)});"))

    def test_evidence_is_filed_not_read(self):
        for step in ("evidence", "photo"):
            with self.subTest(step=step):
                self.assertFalse(
                    _run(f"return m.shouldRecognize({json.dumps(step)});"))

    def test_scan_is_decoded_not_recognized(self):
        """A barcode is decoded locally; sending it to a vision model
        would be a round trip for something the camera already knows."""
        self.assertFalse(_run("return m.shouldRecognize('scan');"))

    def test_decision_step_is_not_recognized(self):
        self.assertFalse(_run("return m.shouldRecognize('confirm');"))


if __name__ == "__main__":
    unittest.main()
