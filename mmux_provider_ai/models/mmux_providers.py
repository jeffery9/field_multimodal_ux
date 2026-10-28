# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""OpenAI-compatible provider adapters (PRD §8.3 Provider Adapter layer).

All endpoints/keys/models/timeouts come from ir.config_parameter:
  mmux.asr.provider = openai | mock
  mmux.asr.base_url / mmux.asr.api_key / mmux.asr.model / mmux.asr.language
  mmux.llm.provider = openai | mock
  mmux.llm.base_url / mmux.llm.api_key / mmux.llm.model
  mmux.vision.provider = openai | mock ; mmux.vision.model

The HTTP client is `requests` (already a hard dependency of Odoo), with
connect+read timeout and no retries for business-critical calls — a
timeout surfaces as ExecStatus.UNKNOWN upstream, never as a false success.
"""
import base64
import json
import logging

import requests

from odoo import api, models

from odoo.addons.mmux_core.services.mmux_core import PROVIDER_REGISTRY, ProviderError
from odoo.addons.mmux_core.services.mmux_core.provider import BaseProvider

_logger = logging.getLogger(__name__)
TIMEOUT_DEFAULT = 20


def _timeout(config, key):
    raw = config.get(key) or TIMEOUT_DEFAULT
    try:
        return max(3, int(float(raw)))
    except (TypeError, ValueError):
        return TIMEOUT_DEFAULT


class OpenAIBaseProvider(BaseProvider):
    kind = None
    name = "openai"

    def _post(self, path, config, files=None, json_body=None, timeout_key=None):
        base = (config.get(f"mmux.{self._cfg}.base_url") or "https://api.openai.com/v1").rstrip("/")
        api_key = config.get(f"mmux.{self._cfg}.api_key")
        if not api_key:
            raise ProviderError(f"mmux.{self._cfg}.api_key is not configured")
        url = f"{base}{path}"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            resp = requests.post(
                url, headers=headers, files=files, json=json_body,
                timeout=_timeout(config, timeout_key or f"mmux.{self._cfg}.timeout"),
            )
        except requests.Timeout as exc:
            raise ProviderError(f"{self.kind} request timeout", timeout=True) from exc
        except requests.RequestException as exc:
            raise ProviderError(f"{self.kind} request failed: {exc}", retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.kind} HTTP {resp.status_code}: {resp.text[:300]}", retryable=True)
        return resp


class ASROpenAIProvider(OpenAIBaseProvider):
    kind = "asr"
    _cfg = "asr"

    def call(self, payload, config):
        audio_b64 = payload.get("audio_b64")
        if not audio_b64:
            raise ProviderError("no audio payload")
        audio = base64.b64decode(audio_b64)
        files = {
            "file": (payload.get("filename") or "audio.webm", audio, "audio/webm"),
            "model": (None, config.get("mmux.asr.model") or "whisper-1"),
        }
        lang = config.get("mmux.asr.language")
        if lang:
            files["language"] = (None, lang.split("-")[0])
        resp = self._post("/audio/transcriptions", config, files=files)
        data = resp.json()
        return {"text": data.get("text", ""), "confidence": 0.9, "language": lang or "auto"}


class LLMOpenAIProvider(OpenAIBaseProvider):
    kind = "llm"
    _cfg = "llm"

    def call(self, payload, config):
        messages = payload.get("messages")
        if not messages:
            raise ProviderError("no messages")
        body = {
            "model": config.get("mmux.llm.model") or "gpt-4o-mini",
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        resp = self._post("/chat/completions", config, json_body=body)
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError) as exc:
            # invalid structure is rejected server-side, never patched up (FR-005)
            raise ProviderError(f"LLM returned invalid JSON: {exc}") from exc
        return {"raw": content, "data": parsed}


class VisionOpenAIProvider(OpenAIBaseProvider):
    kind = "vision"
    _cfg = "vision"

    def call(self, payload, config):
        image_b64 = payload.get("image_b64")
        if not image_b64:
            raise ProviderError("no image payload")
        body = {
            "model": config.get("mmux.vision.model") or "gpt-4o-mini",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": payload.get("instruction") or "Describe the image."},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }],
            "temperature": 0,
        }
        resp = self._post("/chat/completions", config, json_body=body)
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        try:
            return {"data": json.loads(content), "raw": content}
        except (ValueError, TypeError):
            return {"data": None, "raw": content, "text": content}


class MmuxProviderRegister(models.AbstractModel):
    """Registers the OpenAI-compatible providers at module load."""
    _name = "mmux.provider.register"
    _description = "Register AI providers into the core registry"

    @api.model
    def _register_providers(self):
        # `ProviderRegistry.register` assigns by (kind, name), so registering
        # twice simply overwrites with an equivalent instance. An earlier
        # version guarded with `names(cls.kind)` twice before calling it --
        # duplicated work that re-stated a property `register` already has.
        for cls in (ASROpenAIProvider, LLMOpenAIProvider, VisionOpenAIProvider):
            PROVIDER_REGISTRY.register(cls())


MmuxProviderRegister._register_providers()
