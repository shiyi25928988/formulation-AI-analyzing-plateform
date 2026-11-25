# -*- coding: utf-8 -*-
"""lifecycle/test_modelcard.py —— 模型卡渲染验收（B09 §5，DoD 1）。

夹具与 test_pipeline 同源（gen1 全链路晋级，种子 = B06 标定 SEED=11）。
"""

from __future__ import annotations

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary.runner import SEED                          # noqa: E402
from lifecycle.modelcard import render_model_card       # noqa: E402
from lifecycle.test_pipeline import gen1, gen2          # noqa: E402
from lifecycle.pipeline import MIN_SHADOW_REQUESTS      # noqa: E402


class TestModelCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lp = gen1(seed=SEED)
        cls.lp.promote()
        cls.card = render_model_card(cls.lp)

    def test_auto_fill_ratio_meets_dod(self):
        """DoD 1：自动填充率 ≥ 0.80（字段级，半自动计 0.5 权重）。"""
        self.assertGreaterEqual(self.card["auto_fill_ratio"], 0.80,
                                f"ratio={self.card['auto_fill_ratio']}")

    def test_eight_sections_rendered(self):
        md = self.card["markdown"]
        for i in range(1, 9):
            self.assertIn(f"## {i}. ", md)

    def test_gate_table_all_pass_with_detail(self):
        """§3 门禁表：G0–G5 逐项 PASS 且带实测 detail（短路留痕可见）。"""
        gate_rows = self.card["sections"]["gate_rows"]
        self.assertEqual(len(gate_rows), 6)
        for row in gate_rows:
            self.assertIn("| PASS |", row)
            self.assertNotIn("— | — |", row)      # 无空实测

    def test_metric_rows_cover_both_metrics(self):
        """§4 指标明细：双指标 RMSE + 覆盖率 + 违规率 + conformal 三档。"""
        md = self.card["markdown"]
        self.assertIn("tg_like", md)
        self.assertIn("lap_shear_like", md)
        self.assertIn("0.90", md)                 # conformal 档位列

    def test_canary_evidence_from_b06(self):
        """§5 canary 证据：12/12 全过 + 金丝雀集版本（B06 落盘值）。"""
        md = self.card["markdown"]
        self.assertIn("CANARY-SET", md)
        self.assertIn("12/12", md)

    def test_lifecycle_history_from_audit(self):
        """§7 履历：audit_event 按 aggregate_id=model_version 反查。"""
        rows = self.card["sections"]["lifecycle_rows"]
        types = [r.split("|")[2].strip() for r in rows]
        for t in ("ModelRegistered", "ModelShadowStarted",
                  "ModelCanaryStarted", "ModelPromoted"):
            self.assertIn(t, types)
        # aggregate 隔离：不串入其他版本的事件
        for t in ("ModelGateFailed", "ModelRolledBack"):
            self.assertNotIn(t, types)

    def test_rollback_note_reflects_route_switch(self):
        """§8 回滚记录：回滚后渲染可见触发源与回切目标（审计直取）。"""
        res = gen2(self.lp)                        # 同 store 第二代
        self.assertTrue(res.train_and_register(
            model_version="MV-002")["registered"])
        res.start_shadow()
        probes = [r["x"] for r in res.pipe.eval_rows]
        while res.shadow_observations() < MIN_SHADOW_REQUESTS:
            res.shadow_serve(probes)
        res.start_canary()
        self.assertTrue(res.promote()["promoted"])
        peer = res.rollback(trigger="drift-auto")
        md2 = render_model_card(res, version="MV-002")["markdown"]
        self.assertIn("drift-auto", md2)
        self.assertIn("MV-001", md2)
        self.assertEqual(peer["model_version"], "MV-001")

    def test_render_is_read_only(self):
        """渲染只读：不写任何表与审计事件（§5 契约 GET 语义）。"""
        before = len(self.lp.pipe.store.where("audit_event"))
        render_model_card(self.lp)
        after = len(self.lp.pipe.store.where("audit_event"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
