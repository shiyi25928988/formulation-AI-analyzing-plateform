"""lifecycle/test_gates.py —— G0–G5 与 C1–C3 门禁序列验收（B09 §3）。"""

from __future__ import annotations

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skeleton.store import SkeletonStore               # noqa: E402
from skeleton.pipeline import SkeletonPipeline         # noqa: E402
from features.factory import get_approved              # noqa: E402
from canary.runner import build_trained_pipeline, SEED   # noqa: E402
from lifecycle.gates import (                          # noqa: E402
    RULES_REGISTER, RULES_CANARY, THRESHOLDS, run_canary_gate,
    run_register_gate,
)


def make_pipe(seed: int = SEED, with_factory: bool = True) -> SkeletonPipeline:
    """与金丝雀同源的训练管线（金丝雀点位 = 已知实验；种子用 B06 标定值）。"""
    pipe = build_trained_pipeline(seed=seed)
    if with_factory and pipe.feature_factory is None:
        pipe.feature_factory = get_approved()
        pipe.step3_feature_set()
    return pipe


class TestRegisterGate(unittest.TestCase):
    def test_healthy_pipeline_passes_all_gates(self):
        pipe = make_pipe()
        rep = run_register_gate(pipe)
        self.assertTrue(rep["passed"], rep["results"])
        self.assertEqual([r["status"] for r in rep["results"]],
                         ["PASS"] * len(RULES_REGISTER))
        self.assertIsNone(rep.failed_rule)
        # 指标入 metrics（模型卡来源）
        for k in ("coverage_090", "ood_recall", "violation_rate_severe",
                  "feature_fingerprint", "caliber"):
            self.assertIn(k, rep["metrics"])

    def test_short_circuit_on_g0_mismatch(self):
        """G0 失败即停：后续全 SKIPPED，不可复现一切免谈。"""
        pipe = make_pipe()
        # 篡改注册表指纹 → G0 指纹不一致
        row = pipe.store.where("feature_set",
                               version=pipe.feature_set_version)[0]
        row["fingerprint"] = "sha256:tampered"
        rep = run_register_gate(pipe)
        self.assertFalse(rep["passed"])
        self.assertEqual(rep.failed_rule, "G0")
        statuses = {r["rule_id"]: r["status"] for r in rep["results"]}
        self.assertEqual(statuses["G0"], "FAIL")
        for rid in RULES_REGISTER[1:]:
            self.assertEqual(statuses[rid], "SKIPPED")

    def test_g2_baseline_degradation_blocked(self):
        """G2：劣化超过容忍带即失败（skeleton-abs 口径）。"""
        pipe = make_pipe()
        prev = {c: pipe._eval_rmse(c) - 3.0 for c in ("tg_like",
                                                      "lap_shear_like")}
        rep = run_register_gate(pipe, prev_rmse=prev)
        self.assertFalse(rep["passed"])
        self.assertEqual(rep.failed_rule, "G2")

    def test_g2_first_version_no_baseline(self):
        pipe = make_pipe()
        rep = run_register_gate(pipe, prev_rmse=None)
        g2 = [r for r in rep["results"] if r["rule_id"] == "G2"][0]
        self.assertEqual(g2["status"], "PASS")
        self.assertIn("首版本", g2["detail"])

    def test_dual_caliber_constants_documented(self):
        """双口径常量集中定义（B09 §3.3）：改动必须在此发生。"""
        self.assertEqual(set(THRESHOLDS) >= {
            "g1_max_rel_diff", "g2_max_rel_degradation",
            "g2_abs_drift_noise_mult", "g3_coverage_min",
            "g4_ood_recall_min", "g5_violation_rate_max",
            "c2_median_sigma_mult"}, True)

    def test_gate_report_matches_contract_shape(self):
        """gate_report 结构与契约 ModelRegistered.gate_report 对齐：
        required 三字段 + results 逐项 (rule_id, status[, detail])。"""
        pipe = make_pipe()
        rep = run_register_gate(pipe)
        self.assertEqual(set(rep.keys()) - {"metrics"},
                         {"passed", "checked_rules", "results"})
        for r in rep["results"]:
            self.assertIn(set(r), ({"rule_id", "status"},
                                   {"rule_id", "status", "detail"}))
            self.assertIn(r["status"], ("PASS", "FAIL", "SKIPPED"))


class TestCanaryGate(unittest.TestCase):
    def test_c1_reuses_b06_run_canary(self):
        """C1 直接引用 B06 run_canary（DoD 3：不造第二套金丝雀）。"""
        pipe = make_pipe()
        rep = run_canary_gate(pipe)
        self.assertTrue(rep["passed"], rep["results"])
        c1 = [r for r in rep["results"] if r["rule_id"] == "C1"][0]
        self.assertEqual(c1["status"], "PASS")
        self.assertIn("全过", c1["detail"])
        # 审计中有 CanaryRun 证据（B06 写入）
        types = [e["type"] for e in pipe.store.where("audit_event")]
        self.assertIn("CanaryRun", types)

    def test_c2_dual_version_comparison(self):
        """C2：新旧版本对同一请求集并行推理，中位偏差以 σ 计。"""
        pipe = make_pipe()
        prev = make_pipe()
        rep = run_canary_gate(pipe, prev_pipe=prev)
        c2 = [r for r in rep["results"] if r["rule_id"] == "C2"][0]
        self.assertEqual(c2["status"], "PASS", c2["detail"])
        self.assertIn("σ", c2["detail"])

    def test_c2_distribution_shift_blocks(self):
        """C2 守卫：分布突变即阻断——用退化基线模拟突变。"""
        pipe = make_pipe()
        prev = make_pipe()
        # 人为把基线版本的中位预测压到远偏离（+5σ 级）
        orig = prev.step5_predict
        def shifted(x):
            resp = orig(x)
            for p in resp["predictions"]:
                if p["value"] is not None:
                    p["value"] = round(p["value"] + 5 * 2.5, 3)
            return resp
        prev.step5_predict = shifted
        rep = run_canary_gate(pipe, prev_pipe=prev)
        self.assertFalse(rep["passed"])
        self.assertEqual(rep.failed_rule, "C2")
        statuses = {r["rule_id"]: r["status"] for r in rep["results"]}
        self.assertEqual(statuses["C3"], "SKIPPED")


class TestGateStoreIntegration(unittest.TestCase):
    def test_gate_report_feeds_lifecycle_register(self):
        """端到端：gates 报告 → lifecycle.register 入册（T0 全链路）。"""
        from lifecycle.store import ModelLifecycleStore
        pipe = make_pipe()
        rep = run_register_gate(pipe)
        s = ModelLifecycleStore(pipe.store)
        row = s.register(
            model_version="M-v9.9.9", model_type="lgbm-reg",
            artifact_uri="minio://artifacts/M-v9.9.9.pkl",
            artifact_checksum="sha256:" + "b" * 64,
            feature_set_version=rep["metrics"]["feature_set_version"],
            feature_fingerprint=rep["metrics"]["feature_fingerprint"],
            dataset_snapshot_id=rep["metrics"]["dataset_snapshot_id"],
            gate_report={k: rep[k] for k in
                         ("passed", "checked_rules", "results")})
        self.assertIsNotNone(row)
        self.assertEqual(row["model_stage"], "registered")
        types = [e["type"] for e in pipe.store.where("audit_event")]
        self.assertIn("ModelRegistered", types)
        # 注册行 metrics_json 承载门禁指标（模型卡来源）
        reg = s.get_by_version("M-v9.9.9")
        self.assertEqual(reg["gate_report"]["passed"], True)


if __name__ == "__main__":
    unittest.main()
