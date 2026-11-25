# -*- coding: utf-8 -*-
"""lifecycle/test_pipeline.py —— 编排器验收（B09 §1/§4/§7）：
训练 → 门禁 → 注册 → 影子 → 灰度 → 晋级 → 回滚全链路。

种子沿用 B06 标定值（SEED=11）：金丝雀配方健康基线与该种子绑定。
"""

from __future__ import annotations

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skeleton.pipeline import SkeletonPipeline           # noqa: E402
from features.factory import get_approved                # noqa: E402
from canary.runner import build_trained_pipeline, SEED   # noqa: E402
from lifecycle.pipeline import (                         # noqa: E402
    CANARY_LADDER, LifecyclePipeline, MIN_SHADOW_REQUESTS, OrchestrationError,
)
from lifecycle.store import STAGES                       # noqa: E402


def make_pipe(seed: int = SEED) -> SkeletonPipeline:
    """与金丝雀/门禁测试同源的训练管线（单一事实来源夹具）。"""
    pipe = build_trained_pipeline(seed=seed)
    if pipe.feature_factory is None:
        pipe.feature_factory = get_approved()
        pipe.step3_feature_set()
    return pipe


def gen1(*, seed: int = SEED) -> LifecyclePipeline:
    """代际一：训练 → 注册 → 影子 → 灰度（供晋级/回滚用例复用）。"""
    lp = LifecyclePipeline(make_pipe(seed))
    res = lp.train_and_register(model_version="MV-001")
    assert res["registered"], res["gate_report"]["results"]
    lp.start_shadow()
    probes = [r["x"] for r in lp.pipe.eval_rows]
    # 影子观察量达到最低量（合成替身：重复探测计prediction_log）
    while lp.shadow_observations() < MIN_SHADOW_REQUESTS:
        lp.shadow_serve(probes)
    lp.start_canary()
    return lp


def gen2(p1: LifecyclePipeline, *, seed: int = SEED + 1,
         version: str = "MV-002") -> LifecyclePipeline:
    """代际二：与代际一**共享同一 store**（双 production 并行的前提）。

    快照/事件 id 按代际区分（幂等键不冲突）；特征工厂复用已注册实例
    （step3 幂等：feature_set 已存在则不重复发事件）。"""
    from canary.canary import build_canary_set
    from canary import runner as R
    store = p1.pipe.store
    pipe = SkeletonPipeline(store, seed=seed)
    rows = R._demo_rows(seed)
    for e in build_canary_set():                 # 金丝雀点位入库口径与 B06 一致
        rows.append({"id": e["id"] + f"-g{seed}", "x": e["x"],
                     "measured": dict(e["reference"])})
    pipe.step1_import(rows)
    pipe.step2_snapshot(f"canary-baseline-g{seed}")
    pipe.feature_factory = p1.pipe.feature_factory
    pipe.step3_feature_set()
    # 固定评估集跨代继承（§3.1 G2 可比性）：评估行排除出训练集
    pipe.eval_rows = p1.pipe.eval_rows
    pipe.exclude_from_train = {r["id"] for r in p1.pipe.eval_rows}
    pipe.step4_train()
    return LifecyclePipeline(pipe, baseline_rmse=p1.latest_production_rmse(),
                             prev_pipe=p1.pipe)


class TestHappyPath(unittest.TestCase):
    def test_full_pipeline_to_production(self):
        """全链路：注册→影子→灰度→晋级，五事件齐备、审计链完整。"""
        lp = gen1()
        res = lp.promote()
        self.assertTrue(res["promoted"], res["gate_report"]["results"])
        self.assertEqual(lp.lc.require("MV-001")["model_stage"], "production")
        types = [e["type"] for e in lp.pipe.store.where("audit_event")]
        for t in ("ModelRegistered", "ModelShadowStarted",
                  "ModelCanaryStarted", "ModelPromoted"):
            self.assertIn(t, types)
        ok, broken = lp.pipe.store.verify_audit()
        self.assertTrue(ok, f"audit chain broken at {broken}")
        # 晋级后 metrics_json 落盘（下一代 G2 基线 + 模型卡 canary 证据）
        mj = lp.lc.require("MV-001")["metrics_json"]
        self.assertIn("rmse", mj)
        self.assertIn("canary", mj)
        self.assertIsNotNone(lp.latest_production_rmse())

    def test_stage_ladder_constants(self):
        self.assertEqual(CANARY_LADDER, (5, 25, 100))
        self.assertGreaterEqual(MIN_SHADOW_REQUESTS, 30)
        self.assertEqual(STAGES,
                         ("registered", "shadow", "canary", "production",
                          "deprecated"))


class TestGateFailureKeepsRouting(unittest.TestCase):
    def test_gate_failure_not_registered_old_version_serves(self):
        """门禁失败=不入册：G0 指纹被篡改 → ModelGateFailed，路由不受影响。"""
        p1 = gen1()
        p1.promote()
        # 代际二：篡改注册表指纹 → G0 失败
        pipe2 = make_pipe()
        row = pipe2.store.where("feature_set",
                                version=pipe2.feature_set_version)[0]
        row["fingerprint"] = "sha256:tampered"
        lp2 = LifecyclePipeline(pipe2, baseline_rmse=p1.latest_production_rmse(),
                                prev_pipe=p1.pipe)
        res = lp2.train_and_register(model_version="MV-002")
        self.assertFalse(res["registered"])
        self.assertEqual(res["gate_report"].failed_rule, "G0")
        self.assertIsNone(lp2.lc.get_by_version("MV-002"))
        types = [e["type"] for e in pipe2.store.where("audit_event")]
        self.assertIn("ModelGateFailed", types)
        # 失败留痕含短路 SKIPPED（模型卡可反查）
        statuses = {r["rule_id"]: r["status"]
                    for r in res["gate_report"]["results"]}
        self.assertEqual(statuses["G5"], "SKIPPED")
        # p1 的 production 与本 store 无关（各自 store）；此处断言编排器
        # 未把候选推入任何阶段
        with self.assertRaises(OrchestrationError):
            lp2.start_shadow()


class TestShadowStage(unittest.TestCase):
    def test_observations_below_minimum_block_canary(self):
        """§4：影子观察量不足不得推进（宁可慢，不可带病晋级）。"""
        lp = LifecyclePipeline(make_pipe())
        lp.train_and_register(model_version="MV-001")
        lp.start_shadow()
        lp.shadow_serve([r["x"] for r in lp.pipe.eval_rows])   # 远低于 30
        with self.assertRaises(OrchestrationError):
            lp.start_canary()
        self.assertEqual(lp.lc.require("MV-001")["model_stage"], "shadow")

    def test_t1_checksum_tamper_blocks_shadow(self):
        """T1 守卫①：注册行校验和被篡改 → 拒绝进影子，状态保持。"""
        lp = LifecyclePipeline(make_pipe())
        lp.train_and_register(model_version="MV-001")
        row = lp.lc.require("MV-001")
        row["artifact_checksum"] = "sha256:" + "f" * 64
        with self.assertRaises(Exception):
            lp.start_shadow()
        self.assertEqual(lp.lc.require("MV-001")["model_stage"], "registered")


class TestCanaryStage(unittest.TestCase):
    def test_t2_distribution_shift_blocks(self):
        """T2 守卫：影子对拍分布突变即阻断（与 C2 同源阈值/函数）。
        漂移注入发生在注册之后——门禁评估走的是模型本身，影子期走服务面。"""
        p1 = gen1()
        p1.promote()
        lp2 = LifecyclePipeline(make_pipe(),
                                baseline_rmse=p1.latest_production_rmse(),
                                prev_pipe=p1.pipe)
        res = lp2.train_and_register(model_version="MV-002")
        self.assertTrue(res["registered"], res["gate_report"]["results"])
        orig = lp2.pipe.step5_predict

        def shifted_fn(x):
            resp = orig(x)
            for p in resp["predictions"]:
                if p["value"] is not None:
                    p["value"] = round(p["value"] + 5 * 2.5, 3)
            return resp

        lp2.pipe.step5_predict = shifted_fn
        lp2.start_shadow()
        probes = [r["x"] for r in lp2.pipe.eval_rows]
        while lp2.shadow_observations() < MIN_SHADOW_REQUESTS:
            lp2.shadow_serve(probes)
        with self.assertRaises(Exception):
            lp2.start_canary()
        self.assertEqual(lp2.lc.require("MV-002")["model_stage"], "shadow")

    def test_canary_ladder_enforced(self):
        """灰度比例只允许阶梯值（5/25/100），越阶梯即拒绝。"""
        lp = gen1()
        probes = [r["x"] for r in lp.pipe.eval_rows]
        with self.assertRaises(OrchestrationError):
            lp.canary_serve(probes, percent=50)


class TestPromoteAndRollback(unittest.TestCase):
    def test_c1_failure_stays_in_canary(self):
        """C1 金丝雀失败 → 停在 canary（quarantine 已由 B06 留痕）。"""
        lp = gen1()
        shifted = lp.pipe.step5_predict

        def drifted(x):
            resp = shifted(x)
            for p in resp["predictions"]:
                if p["value"] is not None:
                    p["value"] = round(p["value"] * 3 + 50, 3)
            return resp

        lp.pipe.step5_predict = drifted
        res = lp.promote()
        self.assertFalse(res["promoted"])
        self.assertEqual(res["gate_report"].failed_rule, "C1")
        self.assertEqual(lp.lc.require("MV-001")["model_stage"], "canary")
        types = [e["type"] for e in lp.pipe.store.where("audit_event")]
        self.assertIn("CanaryRun", types)

    def test_dual_generation_rollback_routes_back(self):
        """两代并行晋级 → 漂移回滚：路由回切上一版本（B09 §2.4）。"""
        p1 = gen1()
        p1.promote()
        lp2 = gen2(p1)
        res = lp2.train_and_register(model_version="MV-002")
        self.assertTrue(res["registered"], res["gate_report"]["results"])
        lp2.start_shadow()
        probes = [r["x"] for r in lp2.pipe.eval_rows]
        while lp2.shadow_observations() < MIN_SHADOW_REQUESTS:
            lp2.shadow_serve(probes)
        lp2.start_canary()
        res = lp2.promote()
        self.assertTrue(res["promoted"], res["gate_report"]["results"])
        self.assertEqual(len(lp2.route()), 2)
        peer = lp2.rollback(trigger="drift-auto")
        self.assertEqual(peer["model_version"], "MV-001")
        self.assertEqual(lp2.lc.require("MV-002")["model_stage"], "deprecated")
        self.assertEqual(len(lp2.route()), 1)
        types = [e["type"] for e in lp2.pipe.store.where("audit_event")]
        self.assertIn("ModelRolledBack", types)


if __name__ == "__main__":
    unittest.main()
