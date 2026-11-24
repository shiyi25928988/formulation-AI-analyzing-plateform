# -*- coding: utf-8 -*-
"""金丝雀运行器：对固定配方集执行预测 + 语义断言 + 偏差检查，聚合判定。

运行口径（§16.2）：
  - 每条金丝雀 = 一次预测响应，过两层探测器：
      L1 语义断言（量纲/可行域/守恒/区间单调/OOD 一致）
      L2 偏差检查（|value − 已知参考值| > 3σ 判越界）
  - 任一 severe 违反或偏差越界 → 该条失败；金丝雀整体失败 →
    建议摘除该模型版本并告警（写入审计事件 CanaryRun）。
  - 金丝雀配方作为已知历史实验入库（参考值即真值），保证预测在域内，
    拒答（severe）本身也判失败——金丝雀点位必须可答。
"""
from __future__ import annotations

from skeleton import pipeline as P
from skeleton.store import SkeletonStore
from tests.replay import synthetic as world

from canary.assertions import run_assertions
from canary.canary import build_canary_set, set_fingerprint, CANARY_VERSION
from canary import faults as F

SEED = 11


def build_trained_pipeline(seed: int = SEED):
    """构建含金丝雀点位（作为已知历史实验）的训练管线。"""
    store = SkeletonStore()
    pipe = P.SkeletonPipeline(store, seed=seed)
    rows = _demo_rows(seed)
    for e in build_canary_set():                       # 金丝雀 = 已知实验
        rows.append({"id": e["id"], "x": e["x"],
                     "measured": dict(e["reference"])})
    pipe.step1_import(rows)
    pipe.step2_snapshot("canary-baseline")
    pipe.step3_feature_set()
    pipe.step4_train()
    return pipe


def _demo_rows(seed: int) -> list[dict]:
    import random
    cands = P.world.generate_candidates(40, seed)
    rng = random.Random(seed + 1)
    rows = []
    for c in cands[:37]:
        measured = {}
        if c["feasible"] and c["raw_available"]:
            measured = {code: round(P.METRICS[code]["truth"](c["x"])
                                    + rng.gauss(0, P.METRICS[code]["noise"]), 3)
                        for code in P.METRICS}
        rows.append({"id": c["id"], "x": c["x"], "measured": measured})
    rows += [{"id": "BAD-01", "x": [-1.0, 10, 30, 1, 150, 3, 100, 40]},
             {"id": "BAD-02", "x": [1.2, 10, 30, 1, 90, 3, 100, 40]},
             {"id": "BAD-03", "x": [1.2, 10, 30, 1, None, 3, 100, 40]}]
    return rows


def run_canary(pipe, fault: dict | None = None) -> dict:
    """对十二条金丝雀逐条预测并判定。fault 非 None 时注入故障。"""
    entries = build_canary_set()
    rows = []
    for e in entries:
        x = list(e["x"])
        if fault and fault["layer"] == "input":
            x = fault["transform"](x)
        resp = pipe.step5_predict(x)
        if fault and fault["layer"] == "output":
            resp = fault["transform"](resp)
        violations = run_assertions(x, resp["predictions"],
                                    feasible=world.is_feasible(x))
        severe = [v for v in violations if v["severity"] == "severe"]
        deviations, breached = [], []
        for p in resp["predictions"]:
            ref = e["reference"][p["metric"]]
            if p["value"] is None:
                deviations.append(None)
                continue
            dev = round(p["value"] - ref, 3)
            deviations.append(dev)
            if abs(dev) > e["tolerance"][p["metric"]]:
                breached.append({"metric": p["metric"], "dev": dev,
                                 "tol": e["tolerance"][p["metric"]]})
        refused = any(p.get("ood_level") == "severe"
                      for p in resp["predictions"])
        ok = not severe and not breached and not refused
        detectors = {v["code"] for v in severe}
        if breached:
            detectors.add("CANARY-DEVIATION")
        if refused and not fault:
            detectors.append("CANARY-REFUSAL")
        rows.append({"id": e["id"], "profile": e["profile"], "ok": ok,
                     "violations": severe, "breaches": breached,
                     "deviations": deviations, "refused": refused,
                     "detectors": detectors})
    failed = [r for r in rows if not r["ok"]]
    verdict = {
        "canary_set_version": CANARY_VERSION,
        "fingerprint": set_fingerprint(),
        "model_version": pipe.model_version,
        "fault": fault["code"] if fault else None,
        "n": len(rows), "passed": len(rows) - len(failed),
        "failed": len(failed), "overall_pass": not failed,
        "rows": rows,
    }
    # §16.2：金丝雀结果写入审计，形成「该模型当前是否可信」的持续证据链
    pipe.store.emit("model-lifecycle-svc", "CanaryRun", {
        "model_version": pipe.model_version,
        "canary_set_version": CANARY_VERSION,
        "fingerprint": verdict["fingerprint"],
        "fault": verdict["fault"],
        "overall_pass": verdict["overall_pass"],
        "failed_ids": [r["id"] for r in failed],
        "action": "quarantine-recommended" if failed else "none",
    }, event_id=f"CR-{verdict['fault'] or 'HEALTHY'}-{pipe.model_version}")
    return verdict


def run_all() -> dict:
    """健康基线 + 八类故障注入。验收口径：故障拦截率 100%。"""
    pipe = build_trained_pipeline()
    results = {"healthy": run_canary(pipe)}
    for f in F.FAULTS:
        results[f["code"]] = run_canary(pipe, fault=f)
    results["interception"] = {
        code: not results[code]["overall_pass"] for code in
        [f["code"] for f in F.FAULTS]}
    results["interception_rate"] = (
        f"{sum(results['interception'].values())}/{len(F.FAULTS)}")
    return results
