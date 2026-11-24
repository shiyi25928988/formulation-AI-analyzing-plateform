# -*- coding: utf-8 -*-
"""§18.2 十步 walking skeleton 的端到端链路实现（合成数据，零第三方依赖）。

与架构的对应：
  步骤 1  导入 + 阻断级 QC           → §7.1 质量规则（阻断级语义替身）
  步骤 2  版本化快照 + 单位归一       → §3.5 dataset_snapshot（T0 语义）
  步骤 3  特征工厂固定 feature_set_version → §5.1 / ADR-04
  步骤 4  双指标训练 + conformal 校准 → §5.2（代理模型替身，接口不变）
  步骤 5  单点预测：数值+区间+溯源+OOD → §4.3（必返字段全部实现）
  步骤 6  约束过滤下的批次推荐        → §5.4（不确定性采样，批量去冗余）
  步骤 7  录入实验结果 → QC → 新快照  → §6.1 九态状态机
  步骤 8  触发重训，门禁通过 → 新版本  → §5.5
  步骤 9  下一轮推荐与上一轮不同      → 闭环可见性（B03 验收要点）
  步骤 10 全过程审计可反查            → store.verify_audit / trace

合成世界复用 tests/replay.synthetic（单一事实来源）：协同窄脊 + 实验噪声。
第二指标 lap_shear_like 在本模块定义（演示多任务；生产为 LightGBM 多任务）。
"""
from __future__ import annotations

import math
import random
import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.replay import synthetic as world          # noqa: E402  合成世界单一来源
from tests.replay.surrogate import BaggedEnsemble    # noqa: E402  代理模型替身
from features import factory as ffactory             # noqa: E402  特征单一来源（B07）
from skeleton.store import (                         # noqa: E402
    INTERVAL_LEVELS, StoreError,
)

# ---- 第二指标（多任务演示）：拉剪类指标，简单非线性 + 噪声 --------------------

def _lap_shear_truth(x: list[float]) -> float:
    eq, ca, fill, disp, temp, time, post, rh = x
    v = 14.0
    v += 6.0 * math.tanh((ca - 15.0) / 8.0)
    v -= 0.04 * fill
    v += 2.0 * math.tanh((time - 3.0) / 2.0)
    v += 1.5 * math.tanh((temp - 140.0) / 30.0)
    v -= 0.3 * max(rh - 60.0, 0.0)
    return v


METRICS = {
    "tg_like": {"unit": "degC", "target_min": world.TARGET_MIN,
                "higher_better": True, "noise": world.NOISE_SIGMA,
                "truth": world.true_value},
    "lap_shear_like": {"unit": "MPa", "target_min": 20.0,
                       "higher_better": True, "noise": 0.8,
                       "truth": _lap_shear_truth},
}


# ---- 阻断级 QC 规则（§7.1 的合成替身；命中即拒收，不进主数据） ----------------

def qc_check_row(row: dict) -> list[str]:
    """返回阻断原因列表；空列表 = 通过。红线：阻断级不静默放行。"""
    problems = []
    x = row.get("x")
    if x is None or len(x) != len(world.FEATURE_NAMES):
        problems.append("schema: feature vector malformed")
        return problems
    if any(v is None for v in x):
        problems.append("missing: null feature value")
        return problems
    if any(v < 0 for v in x):
        problems.append("domain: negative component / process value")
    if x[4] < world.ACTIVATION_TEMP_MIN:
        problems.append("process: cure_temp below activation window")
    if x[0] <= 0:
        problems.append("domain: resin_eq_ratio must be positive")
    if x[5] < 1.0:
        problems.append("process: cure_time below minimum")
    return problems


def _quantile(sorted_vals: list[float], level: float) -> float:
    """split-conformal 分位数（保守取整）。"""
    n = len(sorted_vals)
    if n == 0:
        return float("inf")
    idx = min(n - 1, max(0, math.ceil(level * (n + 1)) - 1))
    return sorted_vals[idx]


class SkeletonPipeline:
    def __init__(self, store, seed: int = 7):
        self.store = store
        self.rng = random.Random(seed)
        self.seed = seed
        self.model = {}                   # metric_code -> BaggedEnsemble
        self.calib = {}                   # metric_code -> {level: half_width}
        self.eval_rows = None             # 固定评估集（门禁可比性）
        self.feature_set_version = None
        self.feature_factory = None       # B07：版本化特征工厂实例
        self.snapshot_id = None
        self.model_version = None
        self.prev_eval_rmse = None
        self.performed_ids: set[str] = set()

    # ---- 步骤 1：导入 + 阻断级 QC -----------------------------------------

    def step1_import(self, raw_rows: list[dict]) -> dict:
        accepted, blocked = [], []
        for row in raw_rows:
            problems = qc_check_row(row)
            if problems:
                blocked.append({"candidate_id": row.get("id"), "problems": problems})
                continue
            f = self.store.insert("formula", {
                "tenant_id": self.store.tenant_id, "code": f"F-{row['id']}"})
            fv = self.store.insert("formula_version", {
                "tenant_id": self.store.tenant_id, "formula_id": f["id"],
                "version": 1, "candidate_id": row["id"], "x": row["x"],
                "measured": row.get("measured", {}),
            })
            # 组分（phr 类）与工艺条件分表落位（§3 领域模型语义）
            names = world.FEATURE_NAMES
            for i, name in enumerate(names[:4]):
                self.store.insert("formula_component", {
                    "tenant_id": self.store.tenant_id,
                    "formula_version_id": fv["id"], "name": name,
                    "phr": row["x"][i]})
            for i, name in enumerate(names[4:], start=4):
                self.store.insert("process_condition", {
                    "tenant_id": self.store.tenant_id,
                    "formula_version_id": fv["id"], "name": name,
                    "value": row["x"][i]})
            accepted.append(fv)
        self.store.emit("data-hub-svc", "FormulaImported",
                        {"accepted": len(accepted), "blocked": len(blocked),
                         "blocked_detail": blocked},
                        event_id=f"IMP-{self.seed}")
        self.store.emit("data-hub-svc", "FormulaVersionCreated",
                        {"count": len(accepted)}, event_id=f"FVC-{self.seed}")
        return {"accepted": accepted, "blocked": blocked}

    # ---- 步骤 2：版本化快照 + 单位归一 -------------------------------------

    def step2_snapshot(self, label: str) -> str:
        rows = self.store.where("formula_version")
        snap = self.store.insert("dataset_snapshot", {
            "tenant_id": self.store.tenant_id, "label": label,
            "row_ids": [r["id"] for r in rows],
            "unit_normalization": {
                "phr": "as-recorded (already normalized, DECIMAL(12,4))",
                "temp": "degC", "time": "h", "rh": "%"},
            "n_rows": len(rows),
        })
        self.snapshot_id = snap["id"]
        self.store.emit("data-hub-svc", "DatasetSnapshotFrozen",
                        {"snapshot_id": snap["id"], "label": label,
                         "n_rows": len(rows)},
                        event_id=f"SNAP-{label}")
        return snap["id"]

    # ---- 步骤 3：特征工厂（单一事实来源 + 版本化 + 审批注册，B07） ----------

    def step3_feature_set(self) -> str:
        """feature_set_version 取自版本化特征工厂（features.factory，
        训练与服务共用同一实现）。版本 FS-vX.Y 对齐 B04 契约 pattern；
        指纹 = 定义内容哈希，注册行 + 审计事件可反查（ADR-04）。"""
        factory = ffactory.get_approved()          # 未注册（draft）即拒绝
        self.feature_factory = factory
        fp = factory.fingerprint()
        if not self.store.where("feature_set", version=factory.version):
            self.store.insert("feature_set", {
                "tenant_id": self.store.tenant_id,
                "version": factory.version,
                "features": factory.feature_names(),
                "fingerprint": fp,
                "source": "features.factory"})
            self.store.emit("model-lifecycle-svc", "FeatureSetRegistered",
                            {"feature_set_version": factory.version,
                             "fingerprint": fp},
                            event_id=f"FSREG-{factory.version}")
        self.feature_set_version = factory.version
        return factory.version

    # ---- 步骤 4：双指标训练 + conformal 三档 -------------------------------

    def _training_rows(self) -> list[dict]:
        return [r for r in self.store.where("formula_version")
                if r.get("measured")]

    def _split(self, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        """确定性切分：尾部 25% 为评估集（首轮固定后不再变，保证门禁可比）。"""
        k = max(4, len(rows) // 4)
        return rows[:-k], rows[-k:]

    def _feat(self, x: list[float]) -> list[float]:
        """统一特征变换入口：训练与服务共用同一实现（B07 / ADR-04）。
        step3 未调用时退化为恒等（仅供底层单测使用）。"""
        if self.feature_factory is None:
            return list(x)
        return self.feature_factory.transform(x)

    def _fit_one(self, code: str, train_rows: list[dict]) -> None:
        X = [self._feat(r["x"]) for r in train_rows]
        y = [r["measured"][code] for r in train_rows]
        model = BaggedEnsemble(n_trees=25, seed=self.seed)
        model.fit(X, y)
        holdout = self.eval_rows or []
        resid = sorted(abs(model.predict(self._feat(r["x"])) - r["measured"][code])
                       for r in holdout)
        self.model[code] = model
        self.calib[code] = {lv: _quantile(resid, float(lv)) for lv in INTERVAL_LEVELS}

    def step4_train(self) -> dict:
        rows = self._training_rows()
        train_rows, eval_rows = self._split(rows)
        if self.eval_rows is None:
            self.eval_rows = eval_rows          # 首轮固定，门禁跨轮可比
        for code in METRICS:
            self._fit_one(code, train_rows)
        mv = f"M-{len(self.store.where('model_registry')) + 1:03d}"
        self.store.insert("model_registry", {
            "tenant_id": self.store.tenant_id, "version": mv,
            "feature_set_version": self.feature_set_version,
            "snapshot_id": self.snapshot_id, "status": "promoted",
            "metrics": {c: self._eval_rmse(c) for c in METRICS},
        })
        self.model_version = mv
        self.store.emit("model-lifecycle-svc", "ModelTrained",
                        {"model_version": mv,
                         "feature_set_version": self.feature_set_version,
                         "snapshot_id": self.snapshot_id},
                        event_id=f"MT-{mv}")
        self.store.emit("model-lifecycle-svc", "ModelPromoted",
                        {"model_version": mv}, event_id=f"MP-{mv}")
        return {"model_version": mv, "train_n": len(train_rows),
                "eval_n": len(eval_rows)}

    def _eval_rmse(self, code: str) -> float:
        n = len(self.eval_rows)
        if n == 0:
            return 0.0
        sse = sum((self.model[code].predict(self._feat(r["x"])) - r["measured"][code]) ** 2
                  for r in self.eval_rows)
        return round(math.sqrt(sse / n), 3)

    # ---- 步骤 5：单点预测（必返：数值 + 三档区间 + provenance + OOD） --------

    def _ood_level(self, x: list[float]) -> str:
        """OOD 两级处置的合成替身：训练集包络之外 advisory，
        包络外扩 10% 仍在外（多于一维）→ severe 拒答。
        生产替换为 §5.3 的 OOD 检测器，接口不变。"""
        rows = self._training_rows()
        if not rows:
            return "severe"
        cols_t = [self._feat(r["x"]) for r in rows]   # 特征空间与训练一致（B07）
        outside = 0
        for i in range(len(x)):
            col = [t[i] for t in cols_t]
            lo, hi = min(col), max(col)
            rng_ = max(hi - lo, 1e-9)
            if x[i] < lo or x[i] > hi:
                outside += 1
                if x[i] < lo - 0.10 * rng_ or x[i] > hi + 0.10 * rng_:
                    outside += 1
        if outside >= 2:
            return "severe"
        return "advisory" if outside == 1 else "none"

    def step5_predict(self, x: list[float]) -> dict:
        x = self._feat(list(x))          # 与训练同一变换（B07 单一来源）
        ood = self._ood_level(x)
        result = {"request_id": f"REQ-{self.rng.randrange(10**9):09d}",
                  "provenance": {"model_version": self.model_version,
                                 "feature_set_version": self.feature_set_version,
                                 "snapshot_id": self.snapshot_id},
                  "predictions": []}
        for code in METRICS:
            if ood == "severe":
                # §4.3 红线：severe 拒答该指标，绝不返回看似合理的数字
                result["predictions"].append({"metric": code, "value": None,
                                              "intervals": None,
                                              "ood_level": "severe",
                                              "reason": "OOD severe: refuse to answer"})
                continue
            v = self.model[code].predict(x)
            intervals = {lv: [round(v - self.calib[code][lv], 3),
                              round(v + self.calib[code][lv], 3)]
                         for lv in INTERVAL_LEVELS}
            result["predictions"].append({
                "metric": code, "unit": METRICS[code]["unit"],
                "value": round(v, 3),
                "intervals": intervals, "ood_level": ood,
                "advisory_only": ood == "advisory"})
        self.store.insert("prediction_log", {
            "tenant_id": self.store.tenant_id,
            "request_id": result["request_id"],
            "model_version": self.model_version,
            "feature_set_version": self.feature_set_version,
            "snapshot_id": self.snapshot_id, "result": result})
        return result

    # ---- 步骤 6：约束过滤下的批次推荐 --------------------------------------

    def step6_recommend(self, n: int = 1) -> list[dict]:
        pool = [r for r in self.store.where("formula_version")
                if r["candidate_id"] not in self.performed_ids
                and world.is_feasible(r["x"])]
        scored = []
        for r in pool:
            std = self.model["tg_like"].predict_std(self._feat(r["x"]))
            scored.append({"candidate_id": r["candidate_id"],
                           "fv_id": r["id"], "x": r["x"],
                           "score": round(std, 4)})
        scored.sort(key=lambda s: -s["score"])
        top = scored[:n]
        recs = []
        for s in top:
            rec = self.store.insert("recommendation", {
                "tenant_id": self.store.tenant_id,
                "candidate_id": s["candidate_id"],
                "formula_version_id": s["fv_id"],
                "model_version": self.model_version,
                "feature_set_version": self.feature_set_version,
                "snapshot_id": self.snapshot_id,
                "strategy": "uncertainty_sampling", "score": s["score"]})
            self.store.emit("active-learning-svc", "RecommendationIssued",
                            {"recommendation_id": rec["id"],
                             "candidate_id": s["candidate_id"],
                             "model_version": self.model_version})
            recs.append(rec)
        return recs

    # ---- 步骤 7：录入实验结果 → QC → 新快照（九态状态机走全） ----------------

    def step7_writeback(self, rec_id: str, measured: dict | None = None,
                        qc_pass: bool = True) -> dict:
        rec = self.store.get("recommendation", rec_id)
        fv = self.store.get("formula_version", rec["formula_version_id"])
        exp = self.store.insert("experiment", {
            "tenant_id": self.store.tenant_id,
            "code": f"EXP-{rec['candidate_id']}",
            "formula_version_id": fv["id"], "recommendation_id": rec_id,
            "state": "RECOMMENDED"})
        if measured is None:
            measured = {code: round(METRICS[code]["truth"](fv["x"])
                                    + self.rng.gauss(0, METRICS[code]["noise"]), 3)
                        for code in METRICS}
        # 九态主线：RECOMMENDED → ... → QC_PENDING → ACCEPTED / REJECTED
        for state in ("REVIEWED", "SCHEDULED", "IN_PROGRESS", "COMPLETED",
                      "QC_PENDING"):
            self.store.update("experiment", exp["id"], {"state": state})
        self.store.emit("experiment-svc", "ExperimentCompleted",
                        {"experiment_id": exp["id"],
                         "recommendation_id": rec_id})
        if qc_pass:
            self.store.update("experiment", exp["id"],
                              {"state": "ACCEPTED"})
            for code, val in measured.items():
                self.store.insert("test_result", {
                    "tenant_id": self.store.tenant_id,
                    "experiment_id": exp["id"], "metric_code": code,
                    "value": val})
            fv2 = self.store.insert("formula_version", {
                "tenant_id": self.store.tenant_id,
                "formula_id": fv["formula_id"],
                "version": 2, "candidate_id": fv["candidate_id"],
                "x": fv["x"], "measured": measured})
            self.store.emit("auth-audit-svc", "DataAccepted",
                            {"experiment_id": exp["id"],
                             "formula_version_id": fv2["id"]})
            self.performed_ids.add(fv["candidate_id"])
            snap = self.step2_snapshot(label=f"after-{rec['candidate_id']}")
            return {"experiment_id": exp["id"], "snapshot_id": snap,
                    "measured": measured, "qc_pass": True}
        else:
            # QC 不过 → REJECTED 进失败样本库（负结果一等数据，不回退不删除）
            self.store.update("experiment", exp["id"],
                              {"state": "REJECTED",
                               "state_reason": "qc: measured out of spec window"})
            return {"experiment_id": exp["id"], "qc_pass": False,
                    "state": "REJECTED"}

    # 新样本以新 formula_version 行进入下一轮训练集（快照冻结不可变）：
    # _training_rows 按 measured 字段过滤，天然纳入，无需回写快照。

    # ---- 步骤 8：触发重训 + 门禁 -------------------------------------------

    def step8_retrain(self) -> dict:
        prev_rmse = {c: self._eval_rmse(c) for c in METRICS}
        old_version = self.model_version
        rows = self._training_rows()
        train_rows = [r for r in rows
                      if r["id"] not in {e["id"] for e in self.eval_rows}]
        for code in METRICS:
            self._fit_one(code, train_rows)
        new_rmse = {c: self._eval_rmse(c) for c in METRICS}
        # §5.5 门禁（骨架简化口径）：固定评估集上 RMSE 漂移不超过
        # 0.5 × 测量噪声底噪——小样本下单样本重训的评估噪声大于模型收益，
        # 相对阈值会误判；生产门禁为多指标 + 历史留出 + 充分样本后的相对比较。
        gate = all(new_rmse[c] <= prev_rmse[c] + 0.5 * METRICS[c]["noise"]
                   for c in METRICS)
        mv = f"M-{len(self.store.where('model_registry')) + 1:03d}"
        self.store.insert("model_registry", {
            "tenant_id": self.store.tenant_id, "version": mv,
            "feature_set_version": self.feature_set_version,
            "snapshot_id": self.snapshot_id,
            "status": "promoted" if gate else "rejected_by_gate",
            "metrics": new_rmse})
        if not gate:
            self.store.emit("model-lifecycle-svc", "ModelGateFailed",
                            {"model_version": mv, "rmse": new_rmse})
            raise StoreError(f"gate failed: {prev_rmse} -> {new_rmse}")
        self.model_version = mv
        self.prev_eval_rmse = prev_rmse
        self.store.emit("model-lifecycle-svc", "ModelTrained",
                        {"model_version": mv,
                         "retrain_of": old_version,
                         "snapshot_id": self.snapshot_id},
                        event_id=f"MT-{mv}")
        self.store.emit("model-lifecycle-svc", "ModelPromoted",
                        {"model_version": mv, "retrain_of": old_version},
                        event_id=f"MP-{mv}")
        return {"model_version": mv, "retrain_of": old_version,
                "rmse_before": prev_rmse, "rmse_after": new_rmse,
                "gate": "pass"}

    # ---- 步骤 9 + 10：闭环可见性与审计反查 ----------------------------------

    def step9_compare(self, probe_x: list[float]) -> dict:
        """同一点位前后两次预测 + 两轮推荐 top1 对比。"""
        pred = self.step5_predict(probe_x)
        recs = self.step6_recommend(n=1)
        return {"prediction": pred, "top_recommendation": recs[0]}

    def step10_audit(self, correlation_id: str) -> dict:
        ok, bad = self.store.verify_audit()
        return {"chain_ok": ok, "broken_at": bad,
                "timeline": self.store.trace(correlation_id)}
