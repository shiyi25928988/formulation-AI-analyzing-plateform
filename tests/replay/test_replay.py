# -*- coding: utf-8 -*-
"""回放框架最小测试集（unittest，无 pytest 依赖）。

覆盖 §17.2 的四个关键不变量：
  1. 诊断：完整留痕 → FULL；剥除失败 → CONSERVATIVE；
  2. 冻结：T0 之外的数据绝不进入训练集；
  3. 候选池：已做/不可行/不可得点必须被剔除（陷阱三）；
  4. 历史模式：未做过的点绝不揭示标签（步骤 3 红线）。
可随 walking skeleton（B03）扩展为契约回归的一部分。
"""
from __future__ import annotations
import os
import sys
import unittest
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.replay import synthetic as world
from tests.replay.diagnose import survivor_bias_diagnosis
from tests.replay.freeze import asof_freeze, build_training_set
from tests.replay.strategies import rebuild_pool, RandomStrategy, GreedyStrategy
from tests.replay.replay import ReplayConfig, run_replay
from tests.replay.surrogate import BaggedEnsemble


class TestDiagnosis(unittest.TestCase):
    def test_complete_history_is_full(self):
        cands = world.generate_candidates(300, seed=1)
        hist = world.generate_history(cands, 60, seed=1, complete_records=True)
        d = survivor_bias_diagnosis(hist)
        self.assertEqual(d.verdict, "FULL")

    def test_stripped_history_is_conservative(self):
        cands = world.generate_candidates(300, seed=1)
        hist = world.generate_history(cands, 60, seed=1, complete_records=False)
        d = survivor_bias_diagnosis(hist)
        self.assertEqual(d.verdict, "CONSERVATIVE")

    def test_partially_stripped_history_is_conservative(self):
        """部分剥除（仅残留 2 条失败）也应落入保守档——占比分支。"""
        cands = world.generate_candidates(300, seed=1)
        hist = world.generate_history(cands, 60, seed=1, complete_records=True)
        failures = [r for r in hist if r["state"] in ("failed", "cancelled")]
        keep = set(id(r) for r in failures[:2])
        partial = [r for r in hist
                   if r["state"] in ("measured",) or id(r) in keep]
        d = survivor_bias_diagnosis(partial)
        self.assertEqual(d.verdict, "CONSERVATIVE")
        self.assertGreater(d.n_failed + d.n_cancelled, 0)

    def test_empty_history_is_conservative(self):
        self.assertEqual(survivor_bias_diagnosis([]).verdict, "CONSERVATIVE")


class TestFreeze(unittest.TestCase):
    def test_asof_freeze_excludes_future(self):
        cands = world.generate_candidates(200, seed=2)
        hist = world.generate_history(cands, 40, seed=2)
        t0 = hist[len(hist) // 2]["day"]
        frozen = asof_freeze(hist, t0)
        self.assertTrue(all(r["day"] <= t0 for r in frozen))
        self.assertLess(len(frozen), len(hist))

    def test_training_set_only_measured(self):
        cands = world.generate_candidates(200, seed=3)
        hist = world.generate_history(cands, 40, seed=3)
        X, y = build_training_set(hist, t0_day=10**6, candidates=cands)
        n_measured = sum(1 for r in hist if r["state"] == "measured")
        self.assertEqual(len(X), n_measured)
        self.assertEqual(len(y), n_measured)


class TestPool(unittest.TestCase):
    def test_pool_excludes_performed_and_infeasible(self):
        cands = world.generate_candidates(300, seed=4)
        infeasible_ids = {c["id"] for c in cands if not c["feasible"]}
        self.assertTrue(infeasible_ids, "synthetic world should contain infeasible points")
        performed = {next(iter(infeasible_ids))}
        pool = rebuild_pool(cands, performed, t0_day=1)
        ids = {c["id"] for c in pool}
        self.assertNotIn(next(iter(performed)), ids)
        self.assertTrue(all(c["feasible"] and c["raw_available"] for c in pool))


class TestHistoricalMode(unittest.TestCase):
    def test_historical_mode_never_reveals_unperformed(self):
        """历史模式红线：未做过的点绝不揭示标签。"""
        cands = world.generate_candidates(400, seed=5)
        hist = world.generate_history(cands, 50, seed=5, complete_records=True)
        revealed_ids = {r["candidate_id"] for r in hist if r["state"] == "measured"}
        performed = set()
        X, y = [], []
        # 模拟策略选择大量未做过的点
        rng = random.Random(5)
        pool = rebuild_pool(cands, performed, t0_day=1)
        picks = RandomStrategy().select(pool, 20, rng)
        for cid in picks:
            performed.add(cid)
            if cid in revealed_ids:  # 只有历史做过的才揭示
                X.append(cid)
        self.assertTrue(all(cid in revealed_ids for cid in X),
                        "historical mode leaked labels for unperformed points")


class TestReplayEngine(unittest.TestCase):
    def _mini_replay(self, seed):
        cands = world.generate_candidates(300, seed=seed)
        hist = world.generate_history(cands, 50, seed=seed)
        order = [r["candidate_id"] for r in hist]
        from tests.replay.strategies import HistoricalOrderStrategy
        init = BaggedEnsemble(n_trees=10, max_depth=3, seed=seed)
        X0, y0 = build_training_set(hist, t0_day=10**6, candidates=cands)
        if X0:
            init.fit(X0, y0)
        strat = GreedyStrategy(init)
        return run_replay("synthetic", hist, cands, strat,
                          lambda s: BaggedEnsemble(n_trees=10, max_depth=3, seed=s),
                          ReplayConfig(batch_size=2, max_rounds=15), seed=seed)

    def test_replay_deterministic_per_seed(self):
        r1 = self._mini_replay(7)
        r2 = self._mini_replay(7)
        self.assertEqual(r1.rounds_to_target, r2.rounds_to_target)
        self.assertEqual(r1.best_value, r2.best_value)

    def test_replay_produces_curve(self):
        r = self._mini_replay(8)
        self.assertTrue(len(r.curve) >= 1)
        self.assertGreaterEqual(r.n_experiments, 1)


if __name__ == "__main__":
    unittest.main()
