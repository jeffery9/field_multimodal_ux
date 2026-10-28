# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Unit tests: language normalisation, script detection, text localisation.

In the LLM-first architecture script detection no longer gates any rule
matching (there are no rules) — it is used for the prompt hint and the
audit tag. These tests cover those helpers plus output localisation,
which is unchanged.
"""
import sys
import unittest

sys.path.insert(0, "mmux_core/services")

from mmux_core.i18n import (  # noqa: E402
    normalize_lang, language_chain, detect_languages, is_mixed,
    mixed_language_hint, resolve_text, resolve_dict,
)


class TestLangNormalization(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_lang("zh-cn"), "zh_CN")
        self.assertEqual(normalize_lang("zh_CN"), "zh_CN")
        self.assertEqual(normalize_lang("zh"), "zh_CN")
        self.assertEqual(normalize_lang("en"), "en_US")
        self.assertEqual(normalize_lang("ja"), "ja_JP")
        self.assertEqual(normalize_lang(None), "en_US")
        self.assertEqual(normalize_lang("xx-YY"), "xx_YY")

    def test_chain_fallback(self):
        chain = language_chain("ja_JP")
        self.assertEqual(chain[0], "ja_JP")
        self.assertIn("ja", chain)
        self.assertIn("en_US", chain)
        # zh user falls back to zh then en
        chain_zh = language_chain("zh_CN")
        self.assertEqual(chain_zh[0], "zh_CN")
        self.assertIn("en_US", chain_zh)


class TestScriptDetection(unittest.TestCase):
    """Detection feeds the LLM prompt hint and the audit tag only."""

    def test_pure_chinese(self):
        self.assertEqual(detect_languages("完成 5 件"), ["zh"])

    def test_pure_english(self):
        self.assertEqual(detect_languages("report 5 units"), ["en"])

    def test_mixed_zh_en(self):
        hints = detect_languages("把 MMUX-0001 的 stock 数量改成 5 个")
        self.assertIn("zh", hints)
        self.assertIn("en", hints)
        self.assertTrue(is_mixed("把 MMUX-0001 的 stock 数量改成 5 个"))

    def test_kana_wins_over_han(self):
        # Japanese sentence with kanji must not be reported as Chinese
        hints = detect_languages("数量を報告 してください")
        self.assertEqual(hints, ["ja"])

    def test_no_script(self):
        self.assertEqual(detect_languages("12345"), [])
        self.assertFalse(is_mixed("12345"))

    def test_hint_tag(self):
        self.assertEqual(mixed_language_hint("abc 中文"), "en+zh")
        self.assertEqual(mixed_language_hint("456"), "und")

    def test_model_code_is_not_a_language(self):
        """型号编码不应被当成语言；检测只反映真实脚本。"""
        hints = detect_languages("查询 MMUX-0001 库存")
        self.assertIn("zh", hints)
        self.assertTrue(all(h in ("zh", "en") for h in hints))


class TestResolveText(unittest.TestCase):
    def test_plain_string_passthrough(self):
        self.assertEqual(resolve_text("hello", "zh_CN"), "hello")

    def test_exact_language(self):
        val = {"zh_CN": "你好", "en_US": "hello"}
        self.assertEqual(resolve_text(val, "zh_CN"), "你好")
        self.assertEqual(resolve_text(val, "en_US"), "hello")

    def test_fallback_to_en(self):
        val = {"en_US": "hello"}
        self.assertEqual(resolve_text(val, "ja_JP"), "hello")
        self.assertEqual(resolve_text(val, "zh_CN"), "hello")

    def test_base_language_match(self):
        val = {"zh": "你好"}
        self.assertEqual(resolve_text(val, "zh_CN"), "你好")

    def test_default_key(self):
        val = {"default": "fallback", "de_DE": "hallo"}
        self.assertEqual(resolve_text(val, "fr_FR"), "fallback")

    def test_never_raises(self):
        val = {"ko_KR": "안녕"}
        self.assertEqual(resolve_text(val, "zh_CN"), "안녕")  # last resort
        self.assertEqual(resolve_text({}, "zh_CN"), "")

    def test_resolve_dict_fields(self):
        data = {"step_id": "s1",
                "voice_prompt": {"zh_CN": "去库位", "en_US": "go to location"},
                "title": {"zh_CN": "到达", "en_US": "arrive"},
                "hint": {"en_US": "only english"}}
        out = resolve_dict(data, "zh_CN")
        self.assertEqual(out["voice_prompt"], "去库位")
        self.assertEqual(out["title"], "到达")
        self.assertEqual(out["hint"], "only english")  # fallback


if __name__ == "__main__":
    unittest.main()
