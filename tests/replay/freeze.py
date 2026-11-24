# -*- coding: utf-8 -*-
"""T0 point-in-time 冻结（§17.2 步骤 1：冻结「当时可见」的数据）。

标签泄漏（陷阱二）的对策：历史模型只能用 T0 之前记录的数据训练；
合成模式下同样按 day 切分，保证回放逻辑与真实历史口径一致。
"""
from __future__ import annotations


def asof_freeze(history: list[dict], t0_day: int) -> list[dict]:
    """返回 day <= t0_day 的记录（含 failed/cancelled：负结果一等数据）。"""
    return [r for r in history if r["day"] <= t0_day]


def measured_before(history: list[dict], t0_day: int) -> list[dict]:
    """T0 之前「已测得数值」的记录（可作训练标签；失败记录无标签不入训练）。"""
    return [r for r in history if r["day"] <= t0_day and r["state"] == "measured"]


def build_training_set(history: list[dict], t0_day: int,
                       candidates: list[dict]) -> tuple[list, list]:
    """由冻结序列构建 (X, y)。candidate_id -> 特征向量由候选池提供。

    注意：不使用最新批次物性回填（§3.2 material_batch 的 point-in-time 红线）；
    合成模式下特征即候选定义本身，历史模式的特征须由上游预先 point-in-time 化。
    """
    by_id = {c["id"]: c for c in candidates}
    X, y = [], []
    for r in measured_before(history, t0_day):
        c = by_id[r["candidate_id"]]
        X.append(c["x"])
        y.append(r["value"])
    return X, y
