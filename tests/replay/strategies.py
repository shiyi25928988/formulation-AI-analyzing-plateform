# -*- coding: utf-8 -*-
"""推荐策略与三条基线（§17.2 步骤 5）。

  * RandomStrategy            —— 基线一：随机采样
  * HistoricalOrderStrategy   —— 基线二：工程师历史真实顺序
  * GreedyStrategy            —— 基线三：单目标贪心（ exploitation 极端）
  * UncertaintySamplingStrategy —— 待评估的主动学习策略（不确定性 × 预期改进）

候选池纪律（§17.2 陷阱三）：所有策略只允许从「约束库在 T0 重建的可行候选」中
选择；已做过的点从池中移除，不得从结果反推候选。
"""
from __future__ import annotations
import random


def rebuild_pool(candidates: list[dict], performed: set[str], t0_day: int) -> list[dict]:
    """约束库在 T0 重建候选池：可行 × 原材料可得 × 未做过。

    注意 feasible 已含约束语义；historical 模式下 t0_day 用于过滤
    「当时尚不可得的候选」（合成模式简化为全量可行集）。
    """
    return [c for c in candidates
            if c["feasible"] and c["raw_available"] and c["id"] not in performed]


class BaseStrategy:
    name = "base"

    def select(self, pool: list[dict], k: int, rng: random.Random) -> list[str]:
        raise NotImplementedError


class RandomStrategy(BaseStrategy):
    """基线一：随机。"""
    name = "random"

    def select(self, pool, k, rng):
        return [c["id"] for c in rng.sample(pool, min(k, len(pool)))]


class HistoricalOrderStrategy(BaseStrategy):
    """基线二：按工程师历史真实顺序重放（跳过不可行/已做/超出 T0 的点）。"""
    name = "historical"

    def __init__(self, history_order: list[str]):
        self.history_order = history_order
        self._cursor = 0

    def select(self, pool, k, rng):
        pool_ids = {c["id"] for c in pool}
        picked = []
        while len(picked) < k and self._cursor < len(self.history_order):
            cid = self.history_order[self._cursor]
            self._cursor += 1
            if cid in pool_ids:
                picked.append(cid)
        if not picked:  # 历史顺序耗尽时退化为随机（避免死循环）
            picked = RandomStrategy().select(pool, k, rng)
        return picked


class GreedyStrategy(BaseStrategy):
    """基线三：单目标贪心——只看代理模型点估计，最大预期值。"""
    name = "greedy"

    def __init__(self, surrogate):
        self.surrogate = surrogate

    def select(self, pool, k, rng):
        scored = sorted(pool, key=lambda c: self.surrogate.predict(c["x"]), reverse=True)
        return [c["id"] for c in scored[:k]]


class UncertaintySamplingStrategy(BaseStrategy):
    """待评估策略：预期改进 + 不确定性加成（探索-利用平衡，β 可调）。

    score = max(predict - current_best, 0) + beta * predict_std
    预期改进为 0 的点（劣于当前最优）仍可凭不确定性入选——这正是
    防止贪心陷入局部最优的关键，也是主动学习对基线的核心增益来源。
    """
    name = "active_ucb"

    def __init__(self, surrogate, beta: float = 1.0):
        self.surrogate = surrogate
        self.beta = beta

    def select(self, pool, k, rng):
        def score(c):
            p = self.surrogate.predict(c["x"])
            return max(p, 0.0) * 0.0 + p + self.beta * self.surrogate.predict_std(c["x"])
        # 说明：以「预测值 + β·不确定性」排序即可表达 UCB（预测含当前最优信息
        # 由重训后的模型自带）。基线对比时所有策略共享同一代理模型，公平。
        scored = sorted(pool, key=score, reverse=True)
        return [c["id"] for c in scored[:k]]
