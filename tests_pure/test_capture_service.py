# -*- coding: utf-8 -*-
"""Pure-logic tests for the shared capture service's decision helpers.

`capture_service.js` imports the Odoo RPC client, so `node` cannot load it
directly (the `@odoo-module` specifiers do not resolve outside the Odoo
asset pipeline).  The functions worth pinning here -- `photosParam` and
`captureNotice` -- are pure and need none of that, so this file lifts their
source out of the module and evaluates just those declarations.

That is not a workaround for a weak test: it is the only way to exercise
the real code.  Re-implementing the logic in the test would assert that the
test agrees with itself.

Two properties are worth more than the rest:

* `photosParam` must always yield `ir.attachment`'s shape (bare base64 in a
  list of objects).  A bare string or a null element travels through
  `/mmux/execute` and is counted server-side as a photo, so a malformed
  entry becomes a phantom attachment rather than an error.
* no `captureNotice` may announce completion.  Recognition fills a draft;
  a notice that says "已完成" would tell the worker a write happened that
  did not.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = os.path.join(ROOT, "mmux_core", "static", "src", "capture",
                       "capture_service.js")

NODE = shutil.which("node")


def _source():
    """The capture_service.js source with its imports and re-export removed.

    Only `photosParam` and `captureNotice` are then evaluated, so dropping
    those lines cannot change what they do -- both are self-contained and
    neither is part of the re-export.
    """
    with open(SERVICE, encoding="utf-8") as fh:
        src = fh.read()
    # Drop the import statements (multi-line, `} from "...";`).
    src = re.sub(r"^import[\s\S]*?from\s+\"[^\"]+\";\s*$", "", src,
                 flags=re.MULTILINE)
    # Drop the convenience re-export; its names stay in scope unexported.
    src = re.sub(r"^export\s*\{[^}]*\};\s*$", "", src, flags=re.MULTILINE)
    return src


def _run(body):
    script = (
        "%s\n"
        "const out = (() => { %s })();\n"
        "process.stdout.write(JSON.stringify(out));\n"
    ) % (_source(), body)
    proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                          capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


@unittest.skipUnless(NODE, "node is not available")
class TestPhotosParam(unittest.TestCase):
    """The `photos` parameter must always be `ir.attachment`-shaped."""

    def test_strips_the_data_url_header(self):
        got = _run("return photosParam(['data:image/jpeg;base64,QUJD']);")
        self.assertEqual(got, [{"image_b64": "QUJD"}])

    def test_accepts_the_panel_state_shape(self):
        got = _run("return photosParam([{b64: 'QUJD', name: 'x'}]);")
        self.assertEqual(got, [{"image_b64": "QUJD"}])

    def test_skips_empty_entries_rather_than_sending_nulls(self):
        """A null would arrive server-side as a photo with no data and be
        silently counted as one."""
        got = _run("return photosParam([null, {b64: ''}, 'QQ==', 0]);")
        self.assertEqual(got, [{"image_b64": "QQ=="}])

    def test_never_returns_a_bare_string(self):
        got = _run("return photosParam('QUJD');")
        self.assertEqual(got, [{"image_b64": "QUJD"}])

    def test_empty_input_yields_an_empty_list(self):
        for arg in ("[]", "null", "undefined"):
            with self.subTest(arg=arg):
                self.assertEqual(_run(f"return photosParam({arg});"), [])

    def test_every_element_is_an_object_with_image_b64(self):
        """The shape the server's Attachment.create reads keys off."""
        got = _run(
            "return photosParam(['QQ==', {b64: 'gg=='}]);")
        self.assertEqual(len(got), 2)
        for item in got:
            with self.subTest(item=item):
                self.assertIsInstance(item, dict)
                self.assertEqual(list(item.keys()), ["image_b64"])


@unittest.skipUnless(NODE, "node is not available")
class TestCaptureNotice(unittest.TestCase):
    """The same situation must be described the same way on every screen."""

    def test_a_failed_read_is_reported_as_a_failure(self):
        got = _run("return captureNotice('text', null);")
        self.assertFalse(got["ok"])
        self.assertIn("没能识别", got["text"])

    def test_a_quantity_read_says_the_quantity(self):
        got = _run("return captureNotice('quantity', 120);")
        self.assertTrue(got["ok"])
        self.assertIn("120", got["text"])

    def test_a_barcode_read_says_the_code(self):
        got = _run("return captureNotice('barcode', 'SN123');")
        self.assertIn("SN123", got["text"])

    def test_a_recognized_value_is_never_reported_as_done(self):
        """Recognition fills a draft.  Every notice must therefore ask for
        confirmation, never announce completion -- a worker who hears
        "已完成" would stop waiting for a step that never ran."""
        notices = _run(
            "return ['text','quantity','barcode'].map("
            "(f) => captureNotice(f, 'X').text);")
        for text in notices:
            with self.subTest(text=text):
                self.assertIn("请确认", text)
                self.assertNotIn("已完成", text)

    def test_the_guard_reports_zero_as_a_real_value_not_a_miss(self):
        """`0` is falsy but a legitimate reading; treating it as a miss
        would ask the worker to re-shoot a photo that was read correctly."""
        got = _run("return captureNotice('quantity', 0);")
        self.assertTrue(got["ok"])
        self.assertIn("0", got["text"])


if __name__ == "__main__":
    unittest.main()
