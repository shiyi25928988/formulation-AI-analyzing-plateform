# -*- coding: utf-8 -*-
"""Walking skeleton 演示入口：跑两轮闭环，逐步打印 §18.2 十步的落点与结果。

运行：python skeleton/run_skeleton.py
退出码 0 = 十步全部落点且闭环可见（推荐变化 + 审计完整）。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from skeleton import pipeline as P          # noqa: E402
from skeleton.store import SkeletonStore    # noqa: E402

SEED = 11
PROBE = [1.2, 18, 40, 1.5, 155, 4, 110, 45]


def main() -> int:
    store = SkeletonStore()
    pipe = P.SkeletonPipeline(store, seed=SEED)
    print("=" * 64)
    print("AdheFormAI walking skeleton — §18.2 十步（合成数据）")
    print("=" * 64)

    # 1 导入 + 阻断级 QC
    imp = pipe.step1_import(_demo_rows())
    print(f"[1] 导入: 接受 {len(imp['accepted'])} 行, 阻断 {len(imp['blocked'])} 行"
          f"（阻断原因: {'; '.join(sorted({p.split(':')[0] for b in imp['blocked'] for p in b['problems']}))}）")

    # 2 快照
    snap1 = pipe.step2_snapshot("initial")
    print(f"[2] 快照冻结: {snap1}（{len(store.where('formula_version'))} 行, 单位归一声明已入快照元数据）")

    # 3 特征工厂
    fs = pipe.step3_feature_set()
    print(f"[3] feature_set_version = {fs}")

    # 4 训练
    tr = pipe.step4_train()
    print(f"[4] 训练: {tr['model_version']}（train_n={tr['train_n']}, eval_n={tr['eval_n']}, 双指标 tg_like/lap_shear_like + conformal 三档）")

    # 5 单点预测
    pred1 = pipe.step5_predict(PROBE)
    tg = pred1["predictions"][0]
    print(f"[5] 预测: tg_like={tg['value']} 区间90%={tg['intervals']['0.90']} ood={tg['ood_level']}"
          f" provenance={pred1['provenance']['model_version']}/{pred1['provenance']['snapshot_id'][:9]}…")

    # 6 推荐
    rec1 = pipe.step6_recommend(n=1)[0]
    print(f"[6] 推荐: 候选 {rec1['candidate_id']}（策略 uncertainty_sampling, score={rec1['score']}）")

    # 7 回写 + QC + 新快照
    wb = pipe.step7_writeback(rec1["id"])
    exp = store.get("experiment", wb["experiment_id"])
    print(f"[7] 回写: 实验 {exp['code']} 状态机走全 → {exp['state']}; 实测 tg_like={wb['measured']['tg_like']}; 新快照 {wb['snapshot_id']}")

    # 8 重训 + 门禁
    rt = pipe.step8_retrain()
    print(f"[8] 重训: {rt['retrain_of']} → {rt['model_version']}（门禁 pass：固定评估集 RMSE 漂移 ≤ 0.5×噪声底噪；tg_like {rt['rmse_before']['tg_like']} → {rt['rmse_after']['tg_like']}，lap_shear {rt['rmse_before']['lap_shear_like']} → {rt['rmse_after']['lap_shear_like']}）")

    # 9 闭环可见
    pred2 = pipe.step5_predict(PROBE)["predictions"][0]
    rec2 = pipe.step6_recommend(n=1)[0]
    changed = rec2["candidate_id"] != rec1["candidate_id"]
    print(f"[9] 闭环可见: 推荐变化 {rec1['candidate_id']} → {rec2['candidate_id']}（{'✓ 变化' if changed else '✗ 未变化'}）; 同点位预测 {pred1['predictions'][0]['value']} → {pred2['value']}")

    # 10 审计反查
    audit = pipe.step10_audit(rec1["id"])
    kinds = " → ".join(ev["type"] for ev in audit["timeline"])
    print(f"[10] 审计: 哈希链完整={audit['chain_ok']}; 按推荐 id 反查时间线: {kinds}")
    print("=" * 64)
    ok = changed and audit["chain_ok"] and pred2["value"] != pred1["predictions"][0]["value"]
    print("结论:", "闭环打通（B03 验收要点全部命中）" if ok else "未达成，见上")
    return 0 if ok else 1


def _demo_rows():
    """演示用合成 CSV（与测试同一生成器、同一世界）。"""
    import random
    cands = P.world.generate_candidates(40, SEED)
    rng = random.Random(SEED + 1)
    rows = []
    for c in cands[:37]:
        measured = {}
        if c["feasible"] and c["raw_available"]:
            measured = {
                "tg_like": round(P.METRICS["tg_like"]["truth"](c["x"]) + rng.gauss(0, 2.5), 3),
                "lap_shear_like": round(P.METRICS["lap_shear_like"]["truth"](c["x"]) + rng.gauss(0, 0.8), 3),
            }
        rows.append({"id": c["id"], "x": c["x"], "measured": measured})
    rows += [{"id": "BAD-01", "x": [-1.0, 10, 30, 1, 150, 3, 100, 40]},
             {"id": "BAD-02", "x": [1.2, 10, 30, 1, 90, 3, 100, 40]},
             {"id": "BAD-03", "x": [1.2, 10, 30, 1, None, 3, 100, 40]}]
    return rows


if __name__ == "__main__":
    sys.exit(main())
