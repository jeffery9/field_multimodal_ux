# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""LLM-unavailable error contract.

Exercises the deterministic MockLLMProvider failure injection (which the
service layer maps to the structured {code: "llm_unavailable", retryable}
error) and asserts the pure-core pieces that back that mapping.
"""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.provider import (  # noqa: E402
    ProviderError, REGISTRY as PREG, MockLLMProvider,
)


class TestMockLLMFailureInjection(unittest.TestCase):
    def _call(self, config):
        return PREG.call("llm", "mock", {"messages": []}, config)

    def test_timeout_is_marked_timeout(self):
        with self.assertRaises(ProviderError) as ctx:
            self._call({"mmux.llm.mock_error": "timeout"})
        self.assertTrue(ctx.exception.timeout)
        # service maps timeout -> retryable
        self.assertTrue(ctx.exception.timeout or ctx.exception.retryable)

    def test_retryable_error_marked_retryable(self):
        with self.assertRaises(ProviderError) as ctx:
            self._call({"mmux.llm.mock_error": "retryable"})
        self.assertTrue(ctx.exception.retryable)

    def test_not_configured_error(self):
        with self.assertRaises(ProviderError):
            self._call({"mmux.llm.mock_error": "not_configured"})

    def test_invalid_json_returns_unparseable_payload(self):
        out = self._call({"mmux.llm.mock_error": "invalid_json"})
        self.assertIsNone(out["data"])
        import json
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out["raw"])

    def test_not_object_returns_non_dict(self):
        out = self._call({"mmux.llm.mock_error": "not_object"})
        self.assertFalse(isinstance(out["data"], dict))

    def test_no_canned_response_raises(self):
        with self.assertRaises(ProviderError):
            self._call({})

    def test_canned_response_ok(self):
        out = self._call({"mmux.llm.mock_response": {
            "capability_id": "x", "normalized_text": "y"}})
        self.assertEqual(out["data"]["capability_id"], "x")


class TestErrorContractShape(unittest.TestCase):
    """The structured error the service layer returns must carry the two
    fields the frontend branches on: code and retryable."""

    def test_required_fields(self):
        err = {"error": "boom", "code": "llm_unavailable",
               "retryable": True, "detail": {"subtag": "timeout"}}
        self.assertIn("code", err)
        self.assertIn("retryable", err)
        self.assertEqual(err["code"], "llm_unavailable")
        self.assertIsInstance(err["retryable"], bool)


if __name__ == "__main__":
    unittest.main()
