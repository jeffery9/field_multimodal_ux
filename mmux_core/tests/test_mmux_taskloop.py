# -*- coding: utf-8 -*-
"""Odoo-side tests for the task loop: get a task -> do it -> report done ->
the system marks it complete and publishes the next one.

The loop is exercised end to end against real records (stock.picking),
including the LLM-first completion report and the L2 confirmation pause.
"""
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMmuxTaskLoop(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.Param.set_param("mmux.llm.provider", "mock")
        self.product = self.env["product.product"].create({
            "name": "MMUX Loop Product", "is_storable": True,
            "barcode": "MMUX-L1",
        })

    # ---------------------------------------------------------------- #
    # helpers
    # ---------------------------------------------------------------- #
    def _mock(self, answer=None):
        self.Param.set_param(
            "mmux.llm.mock_response", json.dumps(answer) if answer else False)
        self.Param.set_param("mmux.llm.mock_error", False)

    def _picking(self, name_suffix="LOOP", qty=2):
        customer = self.env.ref("stock.stock_location_customers")
        stock = self.env.ref("stock.stock_location_stock")
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": stock.id, "location_dest_id": customer.id,
            "user_id": self.env.user.id,
        })
        self.env["stock.move"].create({
            "name": self.product.name, "product_id": self.product.id,
            "product_uom_qty": qty, "picking_id": picking.id,
            "location_id": stock.id, "location_dest_id": customer.id,
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    # ---------------------------------------------------------------- #
    # task sources (the contract)
    # ---------------------------------------------------------------- #
    def test_task_sources_listed(self):
        sources = self.service.task_sources("warehouse")
        ids = [s["source_id"] for s in sources]
        self.assertIn("wh_picking", ids)
        wh = next(s for s in sources if s["source_id"] == "wh_picking")
        self.assertEqual(wh["flow_id"], "wh_guided_picking")
        self.assertEqual(wh["complete_capability"], "picking_task_complete")
        self.assertEqual(wh["task_model"], "stock.picking")

    # ---------------------------------------------------------------- #
    # getting a task (server-driven)
    # ---------------------------------------------------------------- #
    def test_task_next_publishes_assigned_picking(self):
        picking = self._picking()
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNotNone(res["task"])
        self.assertEqual(res["task"]["res_model"], "stock.picking")
        self.assertEqual(res["task"]["res_id"], picking.id)
        self.assertEqual(res["task"]["complete_capability"],
                         "picking_task_complete")
        self.assertEqual(res["loop"]["state"], "task_active")
        self.assertTrue(res["loop_key"])

    def test_task_next_finished_all_when_nothing_pending(self):
        # no assigned picking created for this user in this test
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.assertIsNone(res["task"])
        self.assertEqual(res["loop"]["state"], "finished_all")

    def test_task_next_res_id_targets_exact_record(self):
        p1 = self._picking()
        p2 = self._picking()
        res = self.service.task_next("warehouse", source_id="wh_picking",
                                     res_id=p2.id)
        self.assertEqual(res["task"]["res_id"], p2.id)
        self.assertNotEqual(res["task"]["res_id"], p1.id)

    def test_res_id_for_non_pending_record_is_empty(self):
        picking = self._picking()
        picking.action_cancel()
        res = self.service.task_next("warehouse", source_id="wh_picking",
                                     res_id=picking.id)
        self.assertIsNone(res["task"])

    # ---------------------------------------------------------------- #
    # cursor persistence
    # ---------------------------------------------------------------- #
    def test_task_current_resumes_after_reload(self):
        self._picking()
        res = self.service.task_next("warehouse", source_id="wh_picking")
        key = res["loop_key"]
        again = self.service.task_current(key)
        self.assertEqual(again["task"]["task_id"], res["task"]["task_id"])
        self.assertEqual(again["loop"]["state"], "task_active")

    def test_unknown_loop_key_is_error(self):
        out = self.service.task_current("mmux:does-not-exist")
        self.assertIn("error", out)

    # ---------------------------------------------------------------- #
    # guided help bound to the task
    # ---------------------------------------------------------------- #
    def test_task_guide_starts_flow_for_current_task(self):
        picking = self._picking()
        res = self.service.task_next("warehouse", source_id="wh_picking")
        view = self.service.task_guide(res["loop_key"])
        self.assertNotIn("error", view)
        self.assertEqual(view["flow_id"], "wh_guided_picking")
        self.assertTrue(view["guide_key"])
        self.assertFalse(view["finished"])
        # the flow was bound to THIS picking
        self.assertIn(str(picking.id), view["current"]["step_id"])

    def test_task_help_returns_guidance_not_execution(self):
        self._picking()
        self._mock({"normalized_text": "where is the location",
                    "capability_id": None, "params": {}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        helpres = self.service.task_help(res["loop_key"], "这个库位在哪")
        self.assertEqual(helpres["action"], "guidance")
        self.assertIn("normalized_text", helpres)

    def test_task_help_offers_execution_when_capability_matched(self):
        self._picking()
        self._mock({"normalized_text": "query the product",
                    "capability_id": "stock_query_product",
                    "params": {"barcode": "MMUX-L1"}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        helpres = self.service.task_help(res["loop_key"], "查一下这个商品")
        self.assertEqual(helpres["action"], "execute")
        self.assertEqual(helpres["intent"]["capability_id"],
                         "stock_query_product")

    # ---------------------------------------------------------------- #
    # reporting completion (the loop's closing half)
    # ---------------------------------------------------------------- #
    def test_report_mismatched_capability_is_rejected(self):
        self._picking()
        self._mock({"normalized_text": "query product",
                    "capability_id": "stock_query_product",
                    "params": {"barcode": "MMUX-L1"}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        out = self.service.task_report(res["loop_key"], text="查商品")
        self.assertEqual(out["state"], "task_mismatch")
        self.assertEqual(out["expected_capability"], "picking_task_complete")

    def test_report_unrecognized_text_asks_to_rephrase(self):
        self._picking()
        self._mock({"normalized_text": "hello", "capability_id": None,
                    "params": {}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        out = self.service.task_report(res["loop_key"], text="你好啊")
        self.assertEqual(out["state"], "not_understood")

    def test_report_llm_unavailable_is_structured_error(self):
        self._picking()
        self.Param.set_param("mmux.llm.provider", False)
        res = self.service.task_next("warehouse", source_id="wh_picking")
        out = self.service.task_report(res["loop_key"], text="完成")
        self.assertEqual(out.get("code"), "llm_unavailable")
        self.assertIn("retryable", out)

    def test_report_parks_on_L2_confirmation(self):
        """汇报完成走 L2 能力 -> 必须先人工确认，循环不前进。"""
        picking = self._picking()
        picking.move_ids.move_line_ids.write({"quantity": 2})
        self._mock({"normalized_text": "task done",
                    "capability_id": "picking_task_complete",
                    "params": {}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        out = self.service.task_report(res["loop_key"], text="完成",
                                       confirmed=False)
        self.assertEqual(out["state"], "awaiting_confirmation")
        self.assertIsNotNone(out["confirmation"])
        self.assertEqual(out["loop"]["state"], "awaiting_confirmation")
        # nothing posted yet — the picking is still open
        self.assertNotEqual(picking.state, "done")

    def test_report_completes_task_and_publishes_next(self):
        """完整闭环：汇报 -> 确认 -> 标记完成 -> 自动发布下一个任务。"""
        p1 = self._picking()
        p2 = self._picking()
        p1.move_ids.move_line_ids.write({"quantity": 2})
        p2.move_ids.move_line_ids.write({"quantity": 2})
        self._mock({"normalized_text": "task done",
                    "capability_id": "picking_task_complete",
                    "params": {}})

        res = self.service.task_next("warehouse", source_id="wh_picking")
        first_id = res["task"]["res_id"]
        out = self.service.task_report(res["loop_key"], text="完成",
                                       confirmed=True)
        self.assertEqual(out["state"], "completed")
        self.assertEqual(out["done_count"], 1)
        # the completed record really moved to done
        self.assertEqual(self.env["stock.picking"].browse(first_id).state,
                         "done")
        # and the loop handed back the NEXT task (not the finished one)
        self.assertIsNotNone(out["task"])
        self.assertNotEqual(out["task"]["res_id"], first_id)
        self.assertTrue(out["next_loop_key"])

    def test_loop_ends_when_last_task_completed(self):
        picking = self._picking()
        picking.move_ids.move_line_ids.write({"quantity": 2})
        self._mock({"normalized_text": "task done",
                    "capability_id": "picking_task_complete",
                    "params": {}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        out = self.service.task_report(res["loop_key"], text="完成",
                                       confirmed=True)
        self.assertEqual(out["state"], "completed")
        self.assertIsNone(out["task"])
        self.assertEqual(out["loop"]["state"], "finished_all")

    def test_completed_task_is_not_republished(self):
        """完成的任务不会再被优先队列选中。"""
        p1 = self._picking()
        p1.move_ids.move_line_ids.write({"quantity": 2})
        self._mock({"normalized_text": "task done",
                    "capability_id": "picking_task_complete",
                    "params": {}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        first_id = res["task"]["res_id"]
        self.service.task_report(res["loop_key"], text="完成", confirmed=True)
        # a fresh fetch must not return the finished picking
        again = self.service.task_next("warehouse", source_id="wh_picking")
        if again["task"]:
            self.assertNotEqual(again["task"]["res_id"], first_id)
        else:
            self.assertEqual(again["loop"]["state"], "finished_all")

    def test_done_count_accumulates_across_the_loop(self):
        p1 = self._picking()
        p2 = self._picking()
        p1.move_ids.move_line_ids.write({"quantity": 2})
        p2.move_ids.move_line_ids.write({"quantity": 2})
        self._mock({"normalized_text": "task done",
                    "capability_id": "picking_task_complete",
                    "params": {}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        out1 = self.service.task_report(res["loop_key"], text="完成",
                                        confirmed=True)
        self.assertEqual(out1["done_count"], 1)
        out2 = self.service.task_report(out1["next_loop_key"], text="完成",
                                        confirmed=True)
        self.assertEqual(out2["done_count"], 2)

    def test_report_audited_in_interaction_log(self):
        picking = self._picking()
        picking.move_ids.move_line_ids.write({"quantity": 2})
        self._mock({"normalized_text": "task done",
                    "capability_id": "picking_task_complete",
                    "params": {}})
        res = self.service.task_next("warehouse", source_id="wh_picking")
        self.service.task_report(res["loop_key"], text="完成", confirmed=True)
        Log = self.env["mmux.interaction.log"].sudo()
        rows = Log.search([("capability_id_name", "=", "taskloop:warehouse")])
        self.assertTrue(rows)
        self.assertIn(rows[0].exec_status, ("running", "success"))
