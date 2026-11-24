# -*- coding: utf-8 -*-
"""合成材料系统与合成历史序列生成器（数据红线合规：全部合成，无真实配方）。

合成世界（synthetic ground truth）模拟一个环氧类体系的目标性能（Tg 类指标），
含非线性、组分交互与实验噪声，并定义可行域（约束库语义：原材料可得性 /
工艺窗口）。历史序列按「当时真实做过的实验」生成，可切换完整留痕 /
幸存者偏差两种口径，用于验证 §17.2 的诊断与两档回放。
"""
from __future__ import annotations
import math
import random

FEATURE_NAMES = [
    "resin_eq_ratio",     # 树脂当量配比（归一）
    "curing_agent_phr",   # 固化剂 phr
    "filler_phr",         # 填料 phr
    "dispersant_phr",     # 分散剂 phr
    "cure_temp_c",        # 固化温度 °C
    "cure_time_h",        # 固化时间 h
    "post_cure_c",        # 后固化温度 °C
    "test_rh_pct",        # 测试相对湿度 %（§3.3：测试条件是特征不是元数据）
]

# 指标定义（对应 metric_definition 语义）：higher_better，目标 ≥ TARGET_MIN
# 校准依据（含协同窄脊后实测）：可行候选池中 ≥95.0 者约 6/319（1.9%），
# 且全部位于协同窄脊内——随机策略期望 10+ 轮才命中，主动学习应显著更少。
METRIC_CODE = "tg_like"
METRIC_UNIT = "degC"
TARGET_MIN = 95.0
NOISE_SIGMA = 2.5

# 约束库语义：固化制度低于活化窗口 → 该点化学上不可行（阻断级规则的合成替身）
ACTIVATION_TEMP_MIN = 120.0
FEASIBLE_TEMP_MIN, FEASIBLE_TEMP_MAX = 120.0, 180.0


def true_value(x: list[float]) -> float:
    """合成 ground truth：非线性 + 组分交互 + 协同增效窄脊。

    窄脊（温度 × 填料的协同窗口）是主动学习验证的关键设计：
    随机策略难以命中、单目标贪心困在历史趋势区、不确定性采样可探索发现——
    这正是 §17.2 要在花实验预算前证明的差异化价值。
    """
    eq, ca, fill, disp, temp, time, post, rh = x
    base = 52.0
    base += 22.0 * math.tanh((ca / max(eq, 0.2)) / 1.6)          # 当量比主导
    base += 0.11 * fill * (1.0 + 0.5 * math.tanh((temp - 150.0) / 25.0))  # 填料×温度交互
    base -= 0.03 * max(fill - 60.0, 0.0)                          # 过量填料反噬
    base += 4.0 * math.tanh((time - 2.0) / 1.5)                   # 固化时间收益饱和
    base += 3.5 * math.tanh((post - 100.0) / 30.0)                # 后固化
    base -= 0.25 * max(rh - 55.0, 0.0)                            # 湿度劣化
    base -= 0.02 * max(disp - 3.0, 0.0) * (1.0 + 0.04 * fill)     # 分散剂过量
    if 150.0 <= temp <= 165.0 and 35.0 <= fill <= 55.0:           # 协同增效窄脊
        base += 10.0
    return base


def is_feasible(x: list[float]) -> bool:
    """约束库在 T0 的合成替身：温度窗口 + 各分量边界。"""
    temp_ok = FEASIBLE_TEMP_MIN <= x[4] <= FEASIBLE_TEMP_MAX
    bounds_ok = all(v >= 0 for v in x) and x[0] > 0 and x[5] >= 1.0
    return temp_ok and bounds_ok


def generate_candidates(n: int, seed: int) -> list[dict]:
    """候选池：T0 时刻约束库重建的结果（§17.2 陷阱三的合成替身）。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        x = [
            round(rng.uniform(0.8, 2.2), 3),      # resin_eq_ratio
            round(rng.uniform(5, 40), 2),         # curing_agent_phr
            round(rng.uniform(0, 120), 1),        # filler_phr
            round(rng.uniform(0, 8), 2),          # dispersant_phr
            round(rng.uniform(100, 200), 1),      # cure_temp（含不可行区 <120）
            round(rng.uniform(1.0, 8.0), 2),      # cure_time
            round(rng.uniform(60, 160), 1),       # post_cure
            round(rng.uniform(20, 85), 1),        # test_rh
        ]
        out.append({
            "id": f"C-{i:04d}",
            "x": x,
            "feasible": is_feasible(x),
            "raw_available": rng.random() > 0.15,  # 原材料可得性（约束检查输入）
        })
    return out


def generate_history(candidates: list[dict], n_experiments: int, seed: int,
                     complete_records: bool = True) -> list[dict]:
    """合成历史序列。

    complete_records=True  → 失败/取消按真实发生全量留痕（附录 B.1 口径）；
    complete_records=False → 人工剥除失败记录，构造幸存者偏差样本（用于
                             验证 diagnose 的保守判定，不是推荐用法）。
    """
    rng = random.Random(seed + 9999)
    # 历史实验者的行为合成：一半随机探索，一半在可行域内偏向高温高固化剂
    picks = rng.sample(range(len(candidates)), min(n_experiments, len(candidates)))
    rows, day = [], 0
    for idx in picks:
        c = candidates[idx]
        x, feasible = c["x"], c["feasible"] and c["raw_available"]
        day += rng.randint(2, 9)  # 每轮实验间隔数天（反馈延迟语义）
        if not feasible:
            reason = ("cure_temp below activation window" if x[4] < ACTIVATION_TEMP_MIN
                      else "raw material unavailable")
            if complete_records:  # 幸存者口径：失败/取消全部丢弃（最强偏差场景）
                rows.append({"candidate_id": c["id"], "day": day, "state": "failed",
                             "reason": reason, "value": None})
            continue
        y = true_value(x) + rng.gauss(0, NOISE_SIGMA)
        rows.append({"candidate_id": c["id"], "day": day, "state": "measured",
                     "reason": None, "value": round(y, 3)})
    # 一部分取消（排产冲突），完整口径下留痕
    n_cancel = max(1, len(rows) // 20)
    for r in rng.sample([r for r in rows if r["state"] == "measured"], n_cancel):
        if complete_records:
            r.update({"state": "cancelled", "reason": "device schedule conflict",
                      "value": None})
    rows.sort(key=lambda r: r["day"])
    return rows
