# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Multilingual and mixed-language support.

Two distinct concerns (kept separate on purpose):

1. **Heterogeneous input** — a single user utterance may mix languages,
   e.g. "把 MMUX-0001 的 stock 数量改成 5 个" or "report 完成 120 件".
   In the LLM-first architecture the utterance is sent to the LLM whole
   and understood there; `detect_languages` is used only to (a) tell the
   model which scripts were present and (b) tag the audit record. It no
   longer gates or filters any rule matching (there are no rules).

2. **Localized output** — prompts, titles and hints spoken/displayed to
   the user must be in that user's language, with per-language variants
   for TTS voice selection. `resolve_text` picks the best match from a
   {lang: text} mapping following the standard fallback chain.

No machine translation is performed here: understanding of mixed input is
delegated to the LLM (which also produces the canonical English rewrite),
and output localization uses texts authored per language (scenario
modules supply them). This keeps the PRD boundary — no silent, unverified
translation of business-critical strings.
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Fallback chain when the exact language is unavailable: a Chinese user
# falls back to zh_CN, then en_US; a Japanese user to ja_JP, en_US, etc.
DEFAULT_FALLBACK = ("en_US",)


def normalize_lang(code: Optional[str]) -> str:
    """'zh-cn' / 'zh_CN' / 'zh' -> 'zh_CN'; unknown -> 'en_US'."""
    if not code:
        return "en_US"
    code = code.strip().replace("-", "_")
    parts = code.split("_")
    if len(parts) == 1:
        base = parts[0].lower()
        return {"zh": "zh_CN", "en": "en_US", "ja": "ja_JP",
                "ko": "ko_KR", "es": "es_ES", "fr": "fr_FR",
                "de": "de_DE", "pt": "pt_PT"}.get(base, "en_US")
    return f"{parts[0].lower()}_{parts[1].upper()}"


def language_chain(lang: Optional[str], extra_fallback=()) -> List[str]:
    """Ordered candidate list for text resolution."""
    primary = normalize_lang(lang)
    chain = [primary]
    base = primary.split("_")[0]
    # a base-language entry (untranslated authoring) is a valid step
    chain.append(base)
    for fb in tuple(extra_fallback) + DEFAULT_FALLBACK:
        fb_n = normalize_lang(fb)
        if fb_n not in chain:
            chain.append(fb_n)
    return chain


# --------------------------------------------------------------------- #
# input script detection (mixed-language utterances)
# --------------------------------------------------------------------- #
_SCRIPT_RANGES = (
    # (lang hint, regex of the script's own characters)
    ("zh", re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")),
    ("ja", re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")),  # kana
    ("ko", re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("cyrillic", re.compile(r"[\u0400-\u04ff]")),
)


def detect_languages(text: str) -> List[str]:
    """Return detected language hints in order of first appearance.

    Latin script alone is reported as 'en' (the safest default for the
    built-in rules, which are authored in en + zh). CJK is detected by
    script; kana is distinguished from Han so a Japanese sentence that
    also contains kanji is not misread as Chinese-only.
    """
    if not text:
        return []
    found: List[Tuple[int, str]] = []
    for lang, pattern in _SCRIPT_RANGES:
        m = pattern.search(text)
        if m:
            found.append((m.start(), lang))
    if re.search(r"[A-Za-z]", text):
        found.append((min((i for i, c in enumerate(text) if c.isascii() and c.isalpha()),
                          default=0), "en"))
    found.sort()
    ordered: List[str] = []
    for _, lang in found:
        if lang not in ordered:
            ordered.append(lang)
    # kana present -> the Han part belongs to Japanese too
    if "ja" in ordered and "zh" in ordered:
        ordered.remove("zh")
    return ordered


def is_mixed(text: str) -> bool:
    """True when more than one language hint is present (e.g. zh + en)."""
    hints = detect_languages(text)
    return len(hints) > 1


def mixed_language_hint(text: str) -> str:
    """'zh+en' style tag, for logging and for prompting the LLM path."""
    return "+".join(detect_languages(text)) or "und"


# --------------------------------------------------------------------- #
# output localization
# --------------------------------------------------------------------- #
def resolve_text(value: Any, lang: Optional[str],
                 extra_fallback=()) -> Any:
    """Resolve a value that may be a plain string or a {lang: text} dict.

    - plain string -> returned as is (language-neutral / already localized)
    - {lang: text} -> best match via `language_chain`, else any 'en'/'en_US',
      else the first available value (never raises on missing translation,
      the user must still see *something* — PRD §9.2 explicit feedback).
    """
    if not isinstance(value, dict):
        return value
    if not value:
        return ""
    # allow a nested 'default' key as an explicit authoring fallback
    candidates = dict(value)
    default = candidates.pop("default", None)
    for code in language_chain(lang, extra_fallback):
        if code in candidates:
            return candidates[code]
    for code in ("en_US", "en", "zh_CN", "zh"):
        if code in candidates:
            return candidates[code]
    if default is not None:
        return default
    return next(iter(candidates.values()))


def resolve_dict(data: Dict[str, Any], lang: Optional[str],
                 keys: Iterable[str] = ("voice_prompt", "title", "hint",
                                        "expected", "description", "label"),
                 extra_fallback=()) -> Dict[str, Any]:
    """Localize the well-known text fields of a step/capability dict."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for key in keys:
        if key in out and isinstance(out[key], dict):
            out[key] = resolve_text(out[key], lang, extra_fallback)
    return out
