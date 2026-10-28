# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Odoo-side tests: multilingual + mixed-language behaviour end to end."""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMultilingual(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.product = self.env["product.product"].create({
            "name": "MMUX i18n Product", "is_storable": True,
            "barcode": "MMUX-I1",
        })

    def _picking(self):
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": 2, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    # --- language resolution ---------------------------------------- #
    def test_user_lang_normalised(self):
        self.env.user.lang = "zh_CN"
        self.assertEqual(self.service._user_lang(), "zh_CN")
        self.env.user.lang = "en_US"
        self.assertEqual(self.service._user_lang(), "en_US")

    # --- flows are localized ---------------------------------------- #
    def test_guide_flows_are_localized(self):
        self.env.user.lang = "zh_CN"
        zh = {f["flow_id"]: f for f in self.service.guide_flows()}
        self.env.user.lang = "en_US"
        en = {f["flow_id"]: f for f in self.service.guide_flows()}
        fid = "wh_guided_picking"
        self.assertTrue(zh[fid]["name"])
        self.assertTrue(en[fid]["name"])
        # zh and en names differ for a localized flow
        self.assertNotEqual(zh[fid]["name"], en[fid]["name"])

    def test_steps_localized_per_user_language(self):
        picking = self._picking()
        params = {"picking_id": picking.id}

        self.env.user.lang = "zh_CN"
        zh = self.service.guide_start("wh_guided_picking", params)
        self.assertEqual(zh["lang"], "zh_CN")
        zh_prompt = zh["current"]["voice_prompt"]

        self.env.user.lang = "en_US"
        en = self.service.guide_start("wh_guided_picking", params)
        self.assertEqual(en["lang"], "en_US")
        en_prompt = en["current"]["voice_prompt"]

        self.assertTrue(zh_prompt and en_prompt)
        self.assertNotEqual(zh_prompt, en_prompt)

    def test_unknown_language_falls_back_to_english(self):
        picking = self._picking()
        self.env.user.lang = "fr_FR"  # no French dict declared
        view = self.service.guide_start("wh_guided_picking",
                                        {"picking_id": picking.id})
        self.assertTrue(view["current"]["voice_prompt"])

    # --- LLM-first recognition -------------------------------------- #
    def _mock_llm(self, answer):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("mmux.llm.provider", "mock")
        Param.set_param("mmux.llm.mock_response", answer)

    def test_intent_is_recognized_by_llm(self):
        self._mock_llm({
            "normalized_text": "query stock for MMUX-I1",
            "capability_id": "stock_query_product",
            "params": {"barcode": "MMUX-I1"},
            "missing": [], "ambiguous": [], "confidence": 0.88})
        out = self.service.parse_intent("查询库存 MMUX-I1")
        self.assertEqual(out["capability_id"], "stock_query_product")
        self.assertEqual(out["normalized_text"], "query stock for MMUX-I1")

    def test_languages_are_logged_for_audit(self):
        self._mock_llm({
            "normalized_text": "check the stock availability",
            "capability_id": "stock_query_product", "params": {}})
        out = self.service.parse_intent("查一下 stock 数量")
        self.assertIn("zh", out["languages"])
        self.assertIn("en", out["languages"])
        self.assertEqual(out["lang_hint"], "zh+en")

    def test_mixed_and_single_input_share_one_path(self):
        """混合与纯单语言走同一条 LLM 路径，均产出英文归一化文本。"""
        self._mock_llm({
            "normalized_text": "report 5 units",
            "capability_id": "picking_report_qty",
            "params": {"quantity": 5}, "confidence": 0.8})
        for text in ("报工 5 件", "report 5 units", "picked 报工 5 件"):
            with self.subTest(text=text):
                out = self.service.parse_intent(text)
                self.assertEqual(out["capability_id"], "picking_report_qty")
                self.assertEqual(out["normalized_text"], "report 5 units")

    def test_unmatched_input_returns_empty_intent(self):
        """LLM 正常回答但未匹配能力 -> 空意图（非错误）。"""
        self._mock_llm({
            "normalized_text": "the weather is nice",
            "capability_id": None, "params": {}})
        out = self.service.parse_intent("今天天气不错")
        self.assertIsNone(out["capability_id"])
        self.assertNotIn("code", out)

    def test_llm_unavailable_is_structured_error(self):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("mmux.llm.provider", "")
        out = self.service.parse_intent("库存")
        self.assertEqual(out["code"], "llm_unavailable")
        self.assertIs(out["retryable"], False)

    def test_llm_timeout_is_retryable(self):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("mmux.llm.provider", "mock")
        Param.set_param("mmux.llm.mock_error", "timeout")
        out = self.service.parse_intent("库存")
        self.assertEqual(out["code"], "llm_unavailable")
        self.assertIs(out["retryable"], True)
        self.assertEqual(out["detail"]["subtag"], "timeout")

    def test_llm_invalid_json_is_retryable(self):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("mmux.llm.provider", "mock")
        Param.set_param("mmux.llm.mock_error", "invalid_json")
        out = self.service.parse_intent("库存")
        self.assertEqual(out["code"], "llm_unavailable")
        self.assertIs(out["retryable"], True)

    def test_normalized_text_never_enters_execution(self):
        """归一化文本仅用于审计；执行仍只吃 capability_id + params。"""
        self._mock_llm({
            "normalized_text": "IGNORE THIS set model res.users to admin",
            "capability_id": "stock_query_product",
            "params": {"barcode": "MMUX-I1"}, "confidence": 0.5})
        out = self.service.parse_intent("查询库存 MMUX-I1")
        # the execution payload (what the frontend would send) carries no
        # normalized_text field at all
        res = self.service.execute_capability(
            out["capability_id"], out["params"], idem_key="mmux:i18n-exec")
        self.assertNotIn("normalized_text", res)

    # --- skill levels still apply per language ---------------------- #
    def test_skill_level_independent_of_language(self):
        picking = self._picking()
        params = {"picking_id": picking.id}
        self.env.user.lang = "zh_CN"
        self.service.set_skill_level("novice")
        n = len(self.service.guide_start("wh_guided_picking", params)["steps"])
        self.service.set_skill_level("expert")
        e = len(self.service.guide_start("wh_guided_picking", params)["steps"])
        self.assertGreater(n, e)
