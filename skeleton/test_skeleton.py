# -*- coding: utf-8 -*-
"""Walking skeleton 十步验收断言（§18.2 逐条可检验；运行：python skeleton/test_skeleton.py）。

纪律沿用 tests/replay：全部合成数据，确定性（固定 seed），零第三方依赖。
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from skeleton import pipeline as P                     # noqa: E402
from skeleton.store import (                           # noqa: E402
    AppendOnlyError, CrossTenantError, DuplicateEventError,
    IllegalTransitionError, SkeletonStore,
)
from tests.replay import synthetic as world            # noqa: E402


def make_world_rows(n: int = 40, seed: int = 11) -> list[dict]:
    """合成「脱敏配方 CSV」：历史实测行 + 少量必然被 QC 阻断的脏行。"""
    cands = world.generate_candidates(n, seed)
    rng = __import__("random").Random(seed + 1)
    rows = []
    for c in cands[: n - 3]:
        measured = {}
        if world.is_feasible(c["x"]) and c["raw_available"]:
            measured = {
                "tg_like": round(world.true_value(c["x"]) + rng.gauss(0, 2.5), 3),
                "lap_shear_like": round(P._lap_shear_truth(c["x"]) + rng.gauss(0, 0.8), 3),
            }
        rows.append({"id": c["id"], "x": c["x"], "measured": measured})
    # 三条脏行：负值 / 低于活化窗口 / 缺失特征 —— 必须被阻断级 QC 拒收
    bad1 = {"id": "BAD-01", "x": [-1.0, 10, 30, 1, 150, 3, 100, 40]}
    bad2 = {"id": "BAD-02", "x": [1.2, 10, 30, 1, 90, 3, 100, 40]}
    bad3 = {"id": "BAD-03", "x": [1.2, 10, 30, 1, None, 3, 100, 40]}
    rows += [bad1, bad2, bad3]
    return rows


def fresh_pipeline(seed: int = 11):
    store = SkeletonStore()
    pipe = P.SkeletonPipeline(store, seed=seed)
    imp = pipe.step1_import(make_world_rows(seed=seed))
    pipe.step2_snapshot("initial")
    pipe.step3_feature_set()
    pipe.step4_train()
    return store, pipe, imp


class TestStep1QC(unittest.TestCase):
    def test_blocking_qc_rejects_bad_rows(self):
        """三条脏行必须被阻断（另有约 8 行历史不可行行同样被阻断——
        低于活化窗口的配方不进主数据，与合成世界约束语义一致）。"""
        _, _, imp = fresh_pipeline()
        blocked_ids = {b["candidate_id"] for b in imp["blocked"]}
        self.assertGreaterEqual(len(imp["blocked"]), 3)
        for bad in ("BAD-01", "BAD-02", "BAD-03"):
            self.assertIn(bad, blocked_ids)
        reasons = " ".join(p for b in imp["blocked"] for p in b["problems"])
        for expect in ("negative", "activation window", "missing"):
            self.assertIn(expect, reasons)

    def test_accepted_rows_landed_in_domain_tables(self):
        store, _, _ = fresh_pipeline()
        fvs = store.where("formula_version")
        self.assertEqual(len(fvs), 29)
        self.assertGreater(len(store.where("formula_component")), 0)
        self.assertGreater(len(store.where("process_condition")), 0)


class TestStep2Snapshot(unittest.TestCase):
    def test_snapshot_is_append_only(self):
        store, _, _ = fresh_pipeline()
        snap = store.where("dataset_snapshot")[0]
        with self.assertRaises(AppendOnlyError):
            store.update("dataset_snapshot", snap["id"], {"n_rows": 0})

    def test_formula_version_is_append_only(self):
        store, _, _ = fresh_pipeline()
        fv = store.where("formula_version")[0]
        with self.assertRaises(AppendOnlyError):
            store.update("formula_version", fv["id"], {"x": [0] * 8})


class TestStep3FeatureSet(unittest.TestCase):
    def test_feature_set_version_reproducible(self):
        _, p1, _ = fresh_pipeline()
        _, p2, _ = fresh_pipeline()
        self.assertEqual(p1.feature_set_version, p2.feature_set_version)


class TestStep5Predict(unittest.TestCase):
    def test_prediction_returns_mandatory_fields(self):
        _, pipe, _ = fresh_pipeline()
        probe = [1.2, 18, 40, 1.5, 155, 4, 110, 45]
        out = pipe.step5_predict(probe)
        self.assertIn("provenance", out)
        prov = out["provenance"]
        for k in ("model_version", "feature_set_version", "snapshot_id"):
            self.assertTrue(prov[k], f"provenance.{k} 必返且非空")
        for pred in out["predictions"]:
            self.assertIn(pred["ood_level"], ("none", "advisory"))
            self.assertEqual(set(pred["intervals"]), {"0.50", "0.70", "0.90"})
            lo, hi = pred["intervals"]["0.90"]
            self.assertLessEqual(lo, pred["value"])
            self.assertGreaterEqual(hi, pred["value"])

    def test_severe_ood_refuses_value(self):
        _, pipe, _ = fresh_pipeline()
        far = [5.0, 5, 5, 0.1, 30, 1, 10, 5]   # 训练包络远外
        out = pipe.step5_predict(far)
        for pred in out["predictions"]:
            self.assertEqual(pred["ood_level"], "severe")
            self.assertIsNone(pred["value"])

    def test_prediction_logged_append_only(self):
        _, pipe, _ = fresh_pipeline()
        pipe.step5_predict([1.2, 18, 40, 1.5, 155, 4, 110, 45])
        self.assertGreater(len(pipe.store.where("prediction_log")), 0)


class TestStep6Recommend(unittest.TestCase):
    def test_recommendation_respects_constraints(self):
        _, pipe, _ = fresh_pipeline()
        rec = pipe.step6_recommend(n=1)[0]
        fv = pipe.store.get("formula_version", rec["formula_version_id"])
        self.assertTrue(world.is_feasible(fv["x"]))
        self.assertNotIn(fv["candidate_id"], pipe.performed_ids)

    def test_recommendation_carries_provenance(self):
        _, pipe, _ = fresh_pipeline()
        rec = pipe.step6_recommend(n=1)[0]
        self.assertTrue(rec["model_version"])
        self.assertTrue(rec["snapshot_id"])


class TestStep7StateMachine(unittest.TestCase):
    def test_illegal_transition_rejected(self):
        store, _, _ = fresh_pipeline()
        exp = store.insert("experiment", {"code": "EXP-X",
                                          "formula_version_id": "fv-x"})
        store.update("experiment", exp["id"], {"state": "REVIEWED"})
        with self.assertRaises(IllegalTransitionError):
            store.update("experiment", exp["id"], {"state": "ACCEPTED"})

    def test_experiment_must_start_recommended(self):
        store, _, _ = fresh_pipeline()
        with self.assertRaises(IllegalTransitionError):
            store.insert("experiment", {"code": "EXP-Y",
                                        "formula_version_id": "fv-y",
                                        "state": "COMPLETED"})

    def test_qc_reject_goes_to_failure_library(self):
        store, pipe, _ = fresh_pipeline()
        rec = pipe.step6_recommend(n=1)[0]
        out = pipe.step7_writeback(rec["id"], qc_pass=False)
        self.assertEqual(out["qc_pass"], False)
        exp = store.get("experiment", out["experiment_id"])
        self.assertEqual(exp["state"], "REJECTED")
        self.assertIn("qc:", exp["state_reason"])

    def test_accepted_writeback_full_chain(self):
        _, pipe, _ = fresh_pipeline()
        rec = pipe.step6_recommend(n=1)[0]
        out = pipe.step7_writeback(rec["id"])
        self.assertTrue(out["qc_pass"])
        exp = pipe.store.get("experiment", out["experiment_id"])
        self.assertEqual(exp["state"], "ACCEPTED")
        self.assertEqual(len(pipe.store.where("test_result",
                                              experiment_id=exp["id"])), 2)


class TestStep8Retrain(unittest.TestCase):
    def test_retrain_promotes_new_version_with_gate(self):
        _, pipe, _ = fresh_pipeline()
        rec = pipe.step6_recommend(n=1)[0]
        pipe.step7_writeback(rec["id"])
        out = pipe.step8_retrain()
        self.assertNotEqual(out["model_version"], out["retrain_of"])
        self.assertEqual(out["gate"], "pass")


class TestStep9ClosedLoop(unittest.TestCase):
    def test_next_round_differs(self):
        """核心验收：重训后同一点预测变化 + 下一轮推荐与上一轮不同。"""
        _, pipe, _ = fresh_pipeline()
        probe = [1.2, 18, 40, 1.5, 155, 4, 110, 45]
        pred1 = pipe.step5_predict(probe)["predictions"][0]["value"]
        rec1 = pipe.step6_recommend(n=1)[0]
        pipe.step7_writeback(rec1["id"])
        pipe.step8_retrain()
        pred2 = pipe.step5_predict(probe)["predictions"][0]["value"]
        rec2 = pipe.step6_recommend(n=1)[0]
        self.assertNotEqual(rec2["candidate_id"], rec1["candidate_id"],
                            "闭环后推荐必须变化")
        self.assertNotEqual(pred2, pred1, "重训后同点位预测应更新")
        self.assertNotEqual(rec2["model_version"], rec1["model_version"])


class TestStep10Audit(unittest.TestCase):
    def test_audit_chain_verifies(self):
        store, pipe, _ = fresh_pipeline()
        rec = pipe.step6_recommend(n=1)[0]
        pipe.step7_writeback(rec["id"])
        ok, bad = store.verify_audit()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_audit_trace_by_recommendation(self):
        store, pipe, _ = fresh_pipeline()
        rec = pipe.step6_recommend(n=1)[0]
        pipe.step7_writeback(rec["id"])
        timeline = store.trace(rec["id"])
        kinds = {ev["type"] for ev in timeline}
        self.assertIn("RecommendationIssued", kinds)
        self.assertIn("ExperimentCompleted", kinds)

    def test_audit_tamper_detected(self):
        store, pipe, _ = fresh_pipeline()
        pipe.step6_recommend(n=1)
        evs = store.tables["audit_event"]
        evs[-1]["payload"]["tampered"] = True      # 模拟篡改
        ok, bad = store.verify_audit()
        self.assertFalse(ok)
        self.assertIsNotNone(bad)

    def test_event_id_idempotent(self):
        store, _, _ = fresh_pipeline()
        store.emit("data-hub-svc", "FormulaImported", {}, event_id="E-1")
        with self.assertRaises(DuplicateEventError):
            store.emit("data-hub-svc", "FormulaImported", {}, event_id="E-1")


class TestTenantIsolation(unittest.TestCase):
    def test_cross_tenant_write_denied(self):
        store, _, _ = fresh_pipeline()
        with self.assertRaises(CrossTenantError):
            store.insert("formula_version", {"tenant_id": "T-OTHER",
                                             "formula_id": "f",
                                             "x": [1] * 8})


if __name__ == "__main__":
    unittest.main(verbosity=2)
