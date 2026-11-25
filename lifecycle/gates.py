"""lifecycle/gates.py —— 门禁检查序列（B09 §3）。

**注册门禁 G0–G5**（T0，短路求值按序执行，失败即停）：

====  ========  ====================================  ==================
编号  检查      输入                                   阈值（§5.5）
====  ========  ====================================  ==================
G0    三元组绑定  model ↔ feature_set(指纹) ↔ snapshot   指纹一致（B07）
G1    可复现性    同代码+同快照+同种子重训                指标差异 < 1%
G2    相对基线    固定评估集 RMSE（前版本为基线）          劣化 ≤ 2% 且 ≥1 指标改进
G3    区间覆盖率  留出集 0.90 区间实测覆盖                ≥ 88%
G4    OOD 召回    留出体系负例                            ≥ 85%
G5    物理一致性  B06 run_assertions 对留出集全量预测      ≤ 1%（severe 计）
====  ========  ====================================  ==================

**canary 阶段门禁 C1–C3**（T3）：

- C1 金丝雀集回归：**直接调用 B06 ``canary.runner.run_canary``**（不造第二套
  金丝雀，单一事实来源），``overall_pass`` 且 action=none；
- C2 双版本对照：新旧版本对同一请求集并行推理，中位偏差 < 3σ（§16.2
  分布突变即阻断；骨架用分位数偏差替身，生产换 KS 检验）；
- C3 在线口径：覆盖率/违规率滑动窗口同 G3/G5 阈值（骨架以探针集合替身）。

**双口径阈值（B09 §3.3，如实记录）**：生产按 §5.5 相对值（caliber=
``production-rel``），骨架替身按绝对容忍带（caliber=``skeleton-abs``，
B03 实证相对 2% 会被小样本重训噪声误判）。阈值常量集中在本模块，
实际使用口径写入模型卡——不做静默二选一。
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skeleton.store import SkeletonStore               # noqa: E402  (类型标注)
from skeleton.pipeline import METRICS                  # noqa: E402
from tests.replay import synthetic as world            # noqa: E402
from tests.replay.surrogate import BaggedEnsemble      # noqa: E402
from canary.assertions import run_assertions           # noqa: E402  G5 单一来源
from canary.runner import run_canary                   # noqa: E402  C1 单一来源

# ---------------------------------------------------------------- 阈值常量
# 双口径集中定义（B09 §3.3）：改动任何阈值必须在本处发生并同步模型卡。

THRESHOLDS = {
    # G1 可复现性：重训评估集 RMSE 相对差异上限（两口径同值）
    "g1_max_rel_diff": 0.01,
    # G2 相对基线：production-rel 口径（§5.5 相对劣化 ≤ 2%）
    "g2_max_rel_degradation": 0.02,
    # G2 相对基线：skeleton-abs 口径（B03 实证：RMSE 漂移 ≤ 0.5×测量噪声）
    "g2_abs_drift_noise_mult": 0.5,
    # G3 区间覆盖率下限（0.90 档）
    "g3_coverage_min": 0.88,
    # G4 OOD 检出召回下限
    "g4_ood_recall_min": 0.85,
    # G5 物理一致性 severe 违规率上限
    "g5_violation_rate_max": 0.01,
    # C2 双版本对照：中位偏差容忍（σ 的倍数；骨架替身，生产换 KS 检验）
    "c2_median_sigma_mult": 3.0,
}

CALIBERS = ("production-rel", "skeleton-abs")

RULES_REGISTER = ["G0", "G1", "G2", "G3", "G4", "G5"]
RULES_CANARY = ["C1", "C2", "C3"]


class GateResult(dict):
    """门禁报告，结构与契约 ``ModelRegistered.gate_report`` 对齐：
    ``{passed, checked_rules, results:[{rule_id,status,detail}], metrics}``。
    额外指标挂在 ``metrics``（写 model_registry.metrics_json 与模型卡），
    不污染契约字段（additionalProperties=false）。"""

    @property
    def failed_rule(self) -> str | None:
        for r in self["results"]:
            if r["status"] == "FAIL":
                return r["rule_id"]
        return None


def _mk_report(results: list[dict], metrics: dict) -> GateResult:
    failed = any(r["status"] == "FAIL" for r in results)
    return GateResult({
        "passed": not failed,
        "checked_rules": [r["rule_id"] for r in results],
        "results": results,
        "metrics": metrics,
    })


# ---------------------------------------------------------------- 注册门禁

def run_register_gate(pipe, *, prev_rmse: dict | None = None,
                      prev_pipe=None,
                      caliber: str = "skeleton-abs") -> GateResult:
    """G0–G5 短路求值。``prev_rmse`` 为上一版本固定评估集 RMSE（G2 基线）；
    ``prev_pipe`` 为上一版本管线（C2 双版本对照用，注册门禁不使用）。

    失败即停：已执行项记 PASS/FAIL，未执行项记 SKIPPED（模型卡可见）。
    """
    if caliber not in CALIBERS:
        raise ValueError(f"unknown caliber {caliber!r}")
    results: list[dict] = []
    metrics: dict = {"caliber": caliber}

    def _stop(rule_id, detail):
        results.append({"rule_id": rule_id, "status": "FAIL",
                        "detail": detail})
        for rest in RULES_REGISTER[RULES_REGISTER.index(rule_id) + 1:]:
            results.append({"rule_id": rest, "status": "SKIPPED",
                            "detail": "short-circuit（短路求值）"})
        return _mk_report(results, metrics)

    # ---- G0 三元组绑定 -------------------------------------------------
    fs_rows = pipe.store.where("feature_set",
                               version=pipe.feature_set_version)
    snap_rows = pipe.store.where("dataset_snapshot",
                                 id=pipe.snapshot_id)
    fp_reg = fs_rows[0].get("fingerprint") if fs_rows else None
    fp_cur = (pipe.feature_factory.fingerprint()
              if pipe.feature_factory is not None else None)
    g0_ok = (bool(fs_rows) and bool(snap_rows)
             and fp_cur is not None and fp_reg == fp_cur)
    metrics["feature_set_version"] = pipe.feature_set_version
    metrics["dataset_snapshot_id"] = pipe.snapshot_id
    metrics["feature_fingerprint"] = fp_cur
    if not g0_ok:
        return _stop("G0", f"三元组绑定失败：fs={bool(fs_rows)} "
                     f"snapshot={bool(snap_rows)} "
                     f"fingerprint {fp_reg} != {fp_cur}")
    results.append({"rule_id": "G0", "status": "PASS",
                    "detail": "三元组互相引用且指纹一致"})

    # ---- G1 可复现性（同代码 + 同快照 + 同种子重训） ---------------------
    train_rows, _ = pipe._split(pipe._training_rows())
    code = "tg_like"
    y = [r["measured"][code] for r in train_rows]
    X = [pipe._feat(r["x"]) for r in train_rows]
    repro = BaggedEnsemble(n_trees=25, seed=pipe.seed)
    repro.fit(X, y)
    n = len(pipe.eval_rows or [])
    if n == 0:
        return _stop("G1", "无固定评估集，可复现性不可评估")
    sse = sum((repro.predict(pipe._feat(r["x"])) - r["measured"][code]) ** 2
              for r in pipe.eval_rows)
    rmse_repro = (sse / n) ** 0.5
    rmse_orig = pipe._eval_rmse(code)
    rel = abs(rmse_repro - rmse_orig) / max(rmse_orig, 1e-9)
    metrics["g1_rel_diff"] = round(rel, 5)
    if rel >= THRESHOLDS["g1_max_rel_diff"]:
        return _stop("G1", f"重训 RMSE 相对差异 {rel:.4f} ≥ "
                     f"{THRESHOLDS['g1_max_rel_diff']}")
    results.append({"rule_id": "G1", "status": "PASS",
                    "detail": f"重训相对差异 {rel:.5f} < 1%"})

    # ---- G2 相对基线不劣化 ----------------------------------------------
    new_rmse = {c: pipe._eval_rmse(c) for c in METRICS}
    metrics["rmse"] = new_rmse
    if prev_rmse is None:
        results.append({"rule_id": "G2", "status": "PASS",
                        "detail": "首版本，无基线（只记录不裁决）"})
    else:
        if caliber == "production-rel":
            degraded = {c: new_rmse[c] / max(prev_rmse[c], 1e-9) - 1
                        for c in METRICS}
            worst = max(degraded.values())
            improved = any(new_rmse[c] < prev_rmse[c] * (1 - 0.01)
                           for c in METRICS)
            ok = worst <= THRESHOLDS["g2_max_rel_degradation"] and improved
            detail = (f"最差相对变化 {worst:+.4f}（阈值 ≤ "
                      f"{THRESHOLDS['g2_max_rel_degradation']}），"
                      f"显著改进={'有' if improved else '无'}")
        else:  # skeleton-abs：绝对容忍带（B03 实证口径）
            drifts = {c: abs(new_rmse[c] - prev_rmse[c])
                      for c in METRICS}
            worst = max(drifts[c] - 0.5 * METRICS[c]["noise"]
                        for c in METRICS)
            ok = worst <= 0
            detail = (f"RMSE 漂移 {({c: round(drifts[c], 3) for c in METRICS})} "
                      f"容忍带 0.5×noise={ {c: round(0.5 * METRICS[c]['noise'], 3) for c in METRICS} }")
            metrics["g2_abs_drift"] = {c: round(drifts[c], 4)
                                       for c in METRICS}
        metrics["g2_worst_margin"] = round(worst, 5)
        if not ok:
            return _stop("G2", detail)
        results.append({"rule_id": "G2", "status": "PASS", "detail": detail})

    # ---- G3 区间覆盖率（0.90 档，固定评估集） ----------------------------
    inside = 0
    total = 0
    for r in pipe.eval_rows:
        resp = pipe.step5_predict(r["x"])
        for p in resp["predictions"]:
            if p["value"] is None:            # severe 拒答不计入覆盖
                continue
            lo, hi = next(iv["range"] for iv in
                          _interval_view(p) if iv["confidence"] == 0.90)
            total += 1
            inside += 1 if lo <= r["measured"][p["metric"]] <= hi else 0
    coverage = inside / total if total else 0.0
    metrics["coverage_090"] = round(coverage, 4)
    if coverage < THRESHOLDS["g3_coverage_min"]:
        return _stop("G3", f"0.90 区间实测覆盖率 {coverage:.3f} < "
                     f"{THRESHOLDS['g3_coverage_min']}")
    results.append({"rule_id": "G3", "status": "PASS",
                    "detail": f"覆盖率 {coverage:.3f} ≥ 0.88"})

    # ---- G4 OOD 检出召回（合成世界不可行点为负例） ------------------------
    cands = [c for c in world.generate_candidates(60, pipe.seed + 99)
             if not c["feasible"]][:20]
    detected = 0
    for c in cands:
        resp = pipe.step5_predict(c["x"])
        detected += 1 if any(p["ood_level"] != "none"
                             for p in resp["predictions"]) else 0
    recall = detected / len(cands) if cands else 1.0
    metrics["ood_recall"] = round(recall, 4)
    if recall < THRESHOLDS["g4_ood_recall_min"]:
        return _stop("G4", f"OOD 检出召回 {recall:.3f} < "
                     f"{THRESHOLDS['g4_ood_recall_min']}（n={len(cands)}）")
    results.append({"rule_id": "G4", "status": "PASS",
                    "detail": f"召回 {recall:.3f} ≥ 0.85（n={len(cands)}）"})

    # ---- G5 物理一致性违规率（B06 run_assertions，severe 计） -------------
    severe_n = 0
    for r in pipe.eval_rows:
        resp = pipe.step5_predict(r["x"])
        violations = run_assertions(r["x"], resp["predictions"],
                                    feasible=world.is_feasible(r["x"]))
        severe_n += sum(1 for v in violations if v["severity"] == "severe")
    rate = severe_n / total if total else 0.0
    metrics["violation_rate_severe"] = round(rate, 5)
    if rate > THRESHOLDS["g5_violation_rate_max"]:
        return _stop("G5", f"severe 违规率 {rate:.4f} > "
                     f"{THRESHOLDS['g5_violation_rate_max']}")
    results.append({"rule_id": "G5", "status": "PASS",
                    "detail": f"违规率 {rate:.4f} ≤ 1%"})

    return _mk_report(results, metrics)


def _interval_view(pred: dict) -> list[dict]:
    """统一区间视图：兼容骨架 intervals dict 与契约 list 两种形态。"""
    ivs = pred.get("intervals") or {}
    if isinstance(ivs, dict):
        return [{"confidence": float(k), "range": v}
                for k, v in ivs.items()]
    return ivs


# ---------------------------------------------------------------- canary 门禁

def median_shift_sigma(pipe, prev_pipe) -> float:
    """双版本对照替身（B09 §3.2 C2 / §2.3 T2 共用，单一事实来源）：
    两版本对固定评估集并行推理，逐指标取中位预测偏差（σ 计），返回最大值。
    生产替换为 KS 检验等分布检验（§16.2：分布突变即阻断，不只看聚合指标）。"""
    probes = [r["x"] for r in (pipe.eval_rows or [])]
    worst = 0.0
    for code in METRICS:
        a = [next(p["value"] for p in pipe.step5_predict(x)["predictions"]
                  if p["metric"] == code) for x in probes]
        b = [next(p["value"] for p in prev_pipe.step5_predict(x)["predictions"]
                  if p["metric"] == code) for x in probes]
        med_a = sorted(a)[len(a) // 2]
        med_b = sorted(b)[len(b) // 2]
        worst = max(worst, abs(med_a - med_b) / METRICS[code]["noise"])
    return worst


def run_canary_gate(pipe, *, prev_pipe=None,
                    caliber: str = "skeleton-abs") -> GateResult:
    """C1–C3（T3 前置）。C1 直接调用 B06 ``run_canary``——单一事实来源。"""
    if caliber not in CALIBERS:
        raise ValueError(f"unknown caliber {caliber!r}")
    results: list[dict] = []
    metrics: dict = {"caliber": caliber}

    # ---- C1 金丝雀集回归（B06） -----------------------------------------
    verdict = run_canary(pipe)
    metrics["canary"] = {"n": verdict["n"], "passed": verdict["passed"],
                         "failed_ids": [r["id"] for r in verdict["rows"]
                                        if not r["ok"]],
                         "set_version": verdict["canary_set_version"]}
    if not verdict["overall_pass"]:
        results.append({"rule_id": "C1", "status": "FAIL",
                        "detail": f"金丝雀 {verdict['passed']}/{verdict['n']}，"
                                  f"失败 {metrics['canary']['failed_ids']}"})
        for rest in RULES_CANARY[1:]:
            results.append({"rule_id": rest, "status": "SKIPPED",
                            "detail": "short-circuit（短路求值）"})
        return _mk_report(results, metrics)
    results.append({"rule_id": "C1", "status": "PASS",
                    "detail": f"金丝雀 {verdict['passed']}/{verdict['n']} 全过"
                              f"（set {verdict['canary_set_version']}）"})

    # ---- C2 双版本对照（中位偏差替身，生产换 KS 检验） ---------------------
    if prev_pipe is None:
        results.append({"rule_id": "C2", "status": "PASS",
                        "detail": "无双版本（首版本），跳过对照"})
    else:
        worst_sigma = median_shift_sigma(pipe, prev_pipe)
        metrics["c2_median_sigma"] = round(worst_sigma, 3)
        if worst_sigma >= THRESHOLDS["c2_median_sigma_mult"]:
            results.append({"rule_id": "C2", "status": "FAIL",
                            "detail": f"中位偏差 {worst_sigma:.2f}σ ≥ "
                                      f"{THRESHOLDS['c2_median_sigma_mult']}σ"
                                      "（分布突变即阻断，§16.2）"})
            results.append({"rule_id": "C3", "status": "SKIPPED",
                            "detail": "short-circuit（短路求值）"})
            return _mk_report(results, metrics)
        results.append({"rule_id": "C2", "status": "PASS",
                        "detail": f"中位偏差 {worst_sigma:.2f}σ < 3σ"})

    # ---- C3 在线口径（骨架以探针集合替身：覆盖率/违规率同 G3/G5 阈值） -----
    inside = total = severe_n = 0
    for r in (pipe.eval_rows or []):
        resp = pipe.step5_predict(r["x"])
        for p in resp["predictions"]:
            if p["value"] is None:
                continue
            lo, hi = next(iv["range"] for iv in
                          _interval_view(p) if iv["confidence"] == 0.90)
            total += 1
            inside += 1 if lo <= r["measured"][p["metric"]] <= hi else 0
        violations = run_assertions(r["x"], resp["predictions"],
                                    feasible=world.is_feasible(r["x"]))
        severe_n += sum(1 for v in violations if v["severity"] == "severe")
    coverage = inside / total if total else 0.0
    vrate = severe_n / total if total else 0.0
    metrics["c3_coverage_090"] = round(coverage, 4)
    metrics["c3_violation_rate"] = round(vrate, 5)
    ok = (coverage >= THRESHOLDS["g3_coverage_min"]
          and vrate <= THRESHOLDS["g5_violation_rate_max"])
    results.append({"rule_id": "C3",
                    "status": "PASS" if ok else "FAIL",
                    "detail": f"覆盖率 {coverage:.3f} / 违规率 {vrate:.4f}"})

    return _mk_report(results, metrics)
