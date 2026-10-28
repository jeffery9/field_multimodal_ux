# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Provider abstraction for ASR / OCR / Vision / LLM services.

PRD §2.6 (progressive enhancement): every provider call may fail; the
registry offers a deterministic Mock provider used by tests and offline
demos. Real providers (in mmux_provider_ai) talk to OpenAI-compatible
endpoints configured through ir.config_parameter.
"""
from typing import Any, Dict, List, Optional


class ProviderError(RuntimeError):
    def __init__(self, message, *, timeout: bool = False, retryable: bool = False):
        super().__init__(message)
        self.timeout = timeout
        self.retryable = retryable


class BaseProvider:
    """A provider must declare a kind and a name; implementations override
    `call`. Providers are stateless; configuration is injected at call time."""

    kind = None  # "asr" | "llm" | "vision" | "ocr"
    name = None  # unique within kind, e.g. "openai", "mock"

    def call(self, payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class ProviderRegistry:
    def __init__(self):
        self._providers: Dict[str, Dict[str, BaseProvider]] = {}

    def register(self, provider: BaseProvider) -> BaseProvider:
        if not provider.kind or not provider.name:
            raise ValueError("provider must define kind and name")
        self._providers.setdefault(provider.kind, {})[provider.name] = provider
        return provider

    def get(self, kind: str, name: str) -> BaseProvider:
        try:
            return self._providers[kind][name]
        except KeyError:
            raise ProviderError(f"no {kind} provider named {name!r} registered")

    def names(self, kind: str) -> List[str]:
        return sorted(self._providers.get(kind, {}))

    def call(self, kind: str, name: str, payload, config) -> Dict[str, Any]:
        provider = self.get(kind, name)
        try:
            return provider.call(payload, config or {})
        except ProviderError:
            raise
        except Exception as exc:  # wrap arbitrary backend failures
            raise ProviderError(f"{kind}/{name} failed: {exc}") from exc


REGISTRY = ProviderRegistry()


class MockASRProvider(BaseProvider):
    """Deterministic ASR test double: maps known fixture texts, echoes otherwise."""
    kind, name = "asr", "mock"

    def call(self, payload, config):
        fixture = config.get("mmux.asr.mock_transcript")
        if fixture:
            return {"text": fixture, "confidence": 0.99, "language": config.get("mmux.asr.language", "zh-CN")}
        # echo heuristic: some clients send a pre-transcribed hint for tests
        hint = payload.get("text_hint")
        if hint:
            return {"text": hint, "confidence": 0.95, "language": config.get("mmux.asr.language", "zh-CN")}
        raise ProviderError("mock ASR: no audio transcription available (testing double)")


class MockLLMProvider(BaseProvider):
    """Deterministic LLM test double.

    Two config keys drive it:

    - ``mmux.llm.mock_response``: a canned JSON answer (dict or JSON str)
      returned as the parsed LLM output. This is how tests deterministically
      exercise the LLM-first intent path.
    - ``mmux.llm.mock_error``: inject a controlled failure so the error
      branches can be tested —
        ``"timeout"``       -> ProviderError(timeout=True)
        ``"retryable"``     -> ProviderError(retryable=True)
        ``"invalid_json"``  -> returns a non-JSON string as the raw payload
        ``"not_object"``    -> returns a JSON array (structurally invalid)
    """
    kind, name = "llm", "mock"

    def call(self, payload, config):
        error = config.get("mmux.llm.mock_error")
        if error == "timeout":
            raise ProviderError("mock LLM: injected timeout", timeout=True)
        if error in ("retryable", "http_error"):
            raise ProviderError("mock LLM: injected retryable error",
                                retryable=True)
        if error in ("no_api_key", "not_configured"):
            raise ProviderError("mock LLM: injected configuration error")
        if error == "invalid_json":
            return {"raw": "{not valid json", "data": None}
        if error == "not_object":
            return {"raw": "[1, 2, 3]", "data": [1, 2, 3]}

        canned = config.get("mmux.llm.mock_response")
        if canned is not None:
            return {"raw": canned, "data": canned if isinstance(canned, dict) else None}
        raise ProviderError("mock LLM: no canned response configured (testing double)")


REGISTRY.register(MockASRProvider())
REGISTRY.register(MockLLMProvider())
