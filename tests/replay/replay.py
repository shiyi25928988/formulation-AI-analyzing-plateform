# -*- coding: utf-8 -*-
"""回放引擎（§17.2 步骤 3-4）。

模式：
  * synthetic —— 合成实验室：任何被选中的候选都「做实验」，标签来自合成
    ground truth + 噪声；不可行点按概率失败并消耗轮次（体现约束的价值）。
  * historical —— 只揭示历史上真实做过的实验标签（§17.2 步骤 3）；策略选到
    未做过的点时该轮消耗且无信息（known limitation，见 README「历史模式的
    固有偏差」），这正是保守回放不能对外引用数字的原因之一。

每轮结束用 T0 可见数据重训代理模型（增量模拟：全量重训的简化），
直到命中目标区间或达到轮次上限。失败实验计入轮次——负结果同样消耗预算。
"""
from __future__ import annotations
import random
from dataclasses import dataclass, field

from .freeze import asof_freeze, build_training_set
from .strategies import rebuild_pool
from . import synthetic as world


@dataclass
class ReplayConfig:
    batch_size: int = 4          # 每轮实验数（对应 recommendation 批次）
    max_rounds: int = 40
    target_min: float = world.TARGET_MIN
    refit_every: int = 1         # 每轮重训（生产中由增量训练触发，§6.3）
    failure_cost: float = 1.0    # 失败实验消耗轮次系数


@dataclass
class ReplayResult:
    strategy: str
    rounds_to_target: int | None      # None = 未达标（按 max_rounds 计罚）
    total_rounds_used: float
    n_experiments: int
    n_failed: int
    best_value: float
    curve: list[float] = field(default_factory=list)   # 每轮结束时的最优值


def run_replay(mode: str, history: list[dict], candidates: list[dict],
               strategy, surrogate_factory, cfg: ReplayConfig,
               seed: int = 0) -> ReplayResult:
    rng = random.Random(seed)
    by_id = {c["id"]: c for c in candidates}
    performed: set[str] = set()
    X, y = [], []            # 已揭示的训练集
    best = -float("inf")
    curve: list[float] = []
    rounds_used = 0.0
    n_exp = n_failed = 0
    target_hit_at = None

    # 历史模式的可揭示标签：candidate_id -> 真实测得值（point-in-time 最终修订值）
    revealed: dict[str, float] = {}
    if mode == "historical":
        for r in history:
            if r["state"] == "measured":
                revealed[r["candidate_id"]] = r["value"]
        order = [r["candidate_id"] for r in history]
    else:
        order = []

    for round_no in range(1, cfg.max_rounds + 1):
        if mode == "historical":
            if HistoricalOrderCursor(order).done(performed):
                break
        pool = rebuild_pool(candidates, performed, t0_day=round_no)
        if not pool:
            break
        picks = strategy.select(pool, cfg.batch_size, rng)
        if not picks:
            break

        gained_label = False
        for cid in picks:
            performed.add(cid)
            n_exp += 1
            if mode == "synthetic":
                c = by_id[cid]
                if c["feasible"] and c["raw_available"]:
                    v = world.true_value(c["x"]) + rng.gauss(0, world.NOISE_SIGMA)
                    X.append(c["x"]); y.append(v)
                    best = max(best, v)
                    gained_label = True
                else:
                    n_failed += 1          # 约束外选择：失败并消耗预算
                    rounds_used += cfg.failure_cost
            else:  # historical：只揭示真实做过的
                if cid in revealed:
                    v = revealed[cid]
                    X.append(by_id[cid]["x"]); y.append(v)
                    best = max(best, v)
                    gained_label = True
                # 未做过的点：不揭示、不失败——该轮按下方规则计罚
        rounds_used += 1.0
        curve.append(best if best > -float("inf") else float("nan"))

        if best >= cfg.target_min and target_hit_at is None:
            target_hit_at = round_no
            break

        # 重训历史模型（T0 可见数据 = 当前已揭示集；§17.2 步骤 2 的每轮模拟）
        if gained_label and X and (round_no % cfg.refit_every == 0):
            strategy_surrogate = surrogate_factory(seed)
            strategy_surrogate.fit(X, y)
            _attach(strategy, strategy_surrogate)

    return ReplayResult(
        strategy=strategy.name,
        rounds_to_target=target_hit_at,
        total_rounds_used=rounds_used,
        n_experiments=n_exp,
        n_failed=n_failed,
        best_value=(best if best > -float("inf") else float("nan")),
        curve=curve,
    )


class HistoricalOrderCursor:
    """历史模式辅助：判断工程师历史序列是否已耗尽（防止无终止回放）。"""
    def __init__(self, order: list[str]):
        self.order = order

    def done(self, performed: set[str]) -> bool:
        return all(cid in performed for cid in self.order)


def _attach(strategy, surrogate):
    """把重训后的代理模型挂到依赖模型的策略上（随机/历史顺序策略不受影响）。"""
    if hasattr(strategy, "surrogate"):
        strategy.surrogate = surrogate
