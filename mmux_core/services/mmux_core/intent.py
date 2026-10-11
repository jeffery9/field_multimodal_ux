# -*- coding: utf-8 -*-
"""Intent parsing: natural language -> capability id + params.

Architecture (LLM-first, single semantic path):

  any input (mixed-language or single-language)
      -> ① LLM semantic recognition (the ONLY parsing path)
      -> ② normalise to a single canonical language (fixed en_US)
      -> ③ Odoo executes on capability_id + params

There is no rule/keyword parser any more. Every utterance — however it
mixes scripts — is sent to the LLM, which returns strict JSON containing:

  {
    "normalized_text": "<the utterance rewritten as plain English>",
    "capability_id":   "<one registered id, or null>",
    "params":          {<only fields declared by that capability>},
    "missing":         [<required info the user must supply>],
    "ambiguous":       [<competing readings the user must resolve>],
    "confidence":      0.0-1.0
  }

Security boundary (PRD, unchanged):
- The model may ONLY name a registered capability id. It may never name
  an Odoo model, field or method. `validate_llm_output` enforces this and
  additionally scans for protocol-violating top-level keys.
- `normalized_text` is for audit + language context ONLY. It is never fed
  back into the execution path: execution still takes capability_id +
  params through the ten-step guard.
"""
from typing import Any, Dict, List, Optional

from .capability import CapabilityRegistry
from .schema import ValidationError
from .i18n import detect_languages
from .quantity import extract_quantities, reconcile_with_text


# Top-level keys that MUST NOT appear in an LLM answer: their presence is
# a protocol violation (an attempt to name a model / invoke a method).
# This is the server-side backstop for the "no arbitrary ORM" rule.
FORBIDDEN_KEYS = frozenset({
    "model", "model_name", "method", "method_name", "ids", "id",
    "create", "write", "unlink", "search", "search_read", "browse",
    "execute", "sql", "query", "env", "context_override", "sudo",
})

# Normalisation target language is FIXED to English (product decision).
NORMALIZED_LANG = "en_US"


class Intent:
    __slots__ = ("capability_id", "params", "confidence", "missing",
                 "ambiguous", "raw_text", "normalized_text", "source",
                 "languages", "lang_hint")

    def __init__(self, capability_id=None, params=None, confidence=0.0,
                 missing=None, ambiguous=None, raw_text="",
                 normalized_text="", source="llm", languages=None,
                 lang_hint="und"):
        self.capability_id = capability_id
        self.params = params or {}
        self.confidence = confidence
        self.missing = missing or []
        self.ambiguous = ambiguous or []
        self.raw_text = raw_text
        # canonical English rewrite produced by the LLM (audit only)
        self.normalized_text = normalized_text
        self.source = source
        # detected scripts of the original input (audit / display only)
        self.languages = languages or []
        self.lang_hint = lang_hint

    @property
    def is_empty(self):
        return self.capability_id is None

    def to_dict(self):
        return {
            "capability_id": self.capability_id,
            "params": self.params,
            "confidence": round(self.confidence, 3),
            "missing": self.missing,
            "ambiguous": self.ambiguous,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "source": self.source,
            "languages": self.languages,
            "lang_hint": self.lang_hint,
        }


class IntentParser:
    """Validates raw LLM output against the capability whitelist and each
    capability's parameter schema. It is the ONLY entry that may turn LLM
    JSON into an executable intent, and it is deliberately strict: an
    invalid structure is rejected, never guessed at (FR-005)."""

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    # ------------------------------------------------------------------ #
    def validate_llm_output(self, data: Any, text: str = "",
                            normalized_lang: str = NORMALIZED_LANG,
                            source_lang_hint: Optional[str] = None) -> Intent:
        """Validate raw LLM JSON into an Intent.

        Rejects anything that is not
        {normalized_text, capability_id: <registered id>, params: {...}}.
        The model cannot name arbitrary models or methods (FR-015).
        """
        langs = detect_languages(text)
        lang_hint = source_lang_hint or self._hint(langs)

        if not isinstance(data, dict):
            intent = Intent(raw_text=text, normalized_text="", source="llm",
                            languages=langs, lang_hint=lang_hint)
            intent.missing = ["capability_id"]
            return intent

        # --- protocol-violation backstop (no arbitrary ORM) ----------- #
        violations = sorted(set(data.keys()) & FORBIDDEN_KEYS)
        if violations:
            return Intent(
                raw_text=text, source="llm", languages=langs,
                lang_hint=lang_hint,
                ambiguous=["LLM output attempted to name a model/method: "
                           + ", ".join(violations)])

        normalized_text = data.get("normalized_text") or ""
        if not isinstance(normalized_text, str):
            normalized_text = ""
        cap_id = data.get("capability_id")
        params = data.get("params") or {}
        missing = list(data.get("missing") or [])
        ambiguous = list(data.get("ambiguous") or [])
        confidence = self._clamp(data.get("confidence"))

        if not normalized_text:
            # non-fatal: audit falls back to the raw text
            missing = list(dict.fromkeys(missing + ["normalized_text"]))

        if cap_id is None:
            intent = Intent(raw_text=text, normalized_text=normalized_text,
                            source="llm", languages=langs, lang_hint=lang_hint,
                            missing=missing, ambiguous=ambiguous,
                            confidence=confidence)
            intent.missing = list(dict.fromkeys(missing + ["capability_id"]))
            return intent

        cap = self.registry.get(cap_id)
        if cap is None:
            # hallucinated id: expose only registered ids, no guessing
            return Intent(raw_text=text, normalized_text=normalized_text,
                          source="llm", languages=langs, lang_hint=lang_hint,
                          confidence=confidence,
                          ambiguous=ambiguous + [f"unknown capability {cap_id!r}"])

        if not isinstance(params, dict):
            return Intent(capability_id=cap_id, params={},
                          raw_text=text, normalized_text=normalized_text,
                          source="llm", languages=langs, lang_hint=lang_hint,
                          missing=missing + ["params"], ambiguous=ambiguous,
                          confidence=confidence)

        # deterministic cross-check against the raw text (consistency guard)
        params, notes = reconcile_with_text(params, text)
        if notes:
            ambiguous = ambiguous + [f"quantity/text mismatch: {n}" for n in notes]

        try:
            clean = cap.schema.validate(params or {})
            return Intent(capability_id=cap_id, params=clean,
                          confidence=confidence or 0.85,
                          raw_text=text, normalized_text=normalized_text,
                          source="llm", languages=langs, lang_hint=lang_hint,
                          missing=missing, ambiguous=ambiguous)
        except ValidationError as exc:
            return Intent(capability_id=cap_id, params=params,
                          confidence=confidence or 0.5,
                          raw_text=text, normalized_text=normalized_text,
                          source="llm", languages=langs, lang_hint=lang_hint,
                          missing=list(dict.fromkeys(missing + [exc.field])),
                          ambiguous=ambiguous)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp(value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, v))

    @staticmethod
    def _hint(langs):
        return "+".join(langs) if langs else "und"
