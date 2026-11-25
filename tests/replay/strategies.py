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
    """待评估策略：利用 + 外推感知探索（β 可调）。

    score = norm(predict) + beta * norm(novelty)
    * 利用项：点估计 min-max 归一到 [0,1]；
    * 探索项：到最近已标注点的归一化距离（novelty / space-filling），
      再以全池均值定标。

    为什么不用 bagged std 做探索项（R2 验收保留项 1 的实测教训）：
    bagged 树集成对「无数据外推区」confidently wrong——所有树用相似的
    粗叶子外推同一块无数据区，bagging 共识使该区 std 反而低于已覆盖区
    的边界（合成世界实测：窄脊 std 3.26 < 池均值 3.51）。不确定性采样
    因此失去指向性，在 8 维稀疏空间里追着已覆盖区的边界跑。到最近
    已标注点的距离则天然指向整块零覆盖区（正是新机制所在），这是
    space-filling / novelty 采样在主动学习中的标准角色。

    β=0.8：探索项以全池均值定标后，1 个均值距离 ≈ 利用项 0.8 满档差距，
    保证禁忌区深角在 1-2 轮内被压进批次（β 过小则利用项把批次拉回
    plateau 高预测区，实测长尾 15+ 轮）。
    基线对比时所有策略共享同一代理模型，公平。
    """
    name = "active_ucb"

    def __init__(self, surrogate, beta: float = 0.8):
        self.surrogate = surrogate
        self.beta = beta

    def _novelty(self, x: list[float], labeled: list[list[float]]) -> float:
        """到最近已标注点的 L2 距离（特征已按池边界归一）。"""
        best = float("inf")
        for lx in labeled:
            d = sum((a - b) ** 2 for a, b in zip(x, lx))
            if d < best:
                best = d
        return best ** 0.5 if best != float("inf") else 1.0

    def select(self, pool, k, rng):
        preds = [self.surrogate.predict(c["x"]) for c in pool]
        p_lo, p_hi = min(preds), max(preds)
        p_rng = (p_hi - p_lo) or 1.0
        # 特征归一边界：池 ∪ 已标注点的逐维 min-max
        labeled = list(getattr(self.surrogate, "X_fit", []) or [])
        all_pts = [c["x"] for c in pool] + labeled
        lo = [min(col) for col in zip(*all_pts)] if all_pts else [0.0] * 8
        hi = [max(col) for col in zip(*all_pts)] if all_pts else [1.0] * 8
        span = [(h - s) or 1.0 for s, h in zip(lo, hi)]
        norm = lambda x: [(v - s) / t for v, s, t in zip(x, lo, span)]
        labeled_n = [norm(x) for x in labeled]

        novs = [self._novelty(norm(c["x"]), labeled_n) for c in pool]
        n_mean = (sum(novs) / len(novs)) or 1.0

        def score(i: int) -> float:
            pn = (preds[i] - p_lo) / p_rng
            return pn + self.beta * (novs[i] / n_mean)

        order = sorted(range(len(pool)), key=score, reverse=True)
        return [pool[i]["id"] for i in order[:k]]
