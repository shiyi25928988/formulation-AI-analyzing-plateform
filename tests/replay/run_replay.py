#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线回放 CLI（B02 / R2）。

用法（在仓库根目录）：
    python3 tests/replay/run_replay.py --seeds 12 --output tests/replay/reports/report.md

流程 = §17.2 全口径：
  诊断 →（按判定选档）→ 回放（待评估策略 + 三条基线）→ 分布 + bootstrap CI 报告。
数据红线：默认合成模式；真实历史序列仅可经脱敏后由 --history-file 注入，
文件本身不得入库（.gitignore 已覆盖 tests/replay/data/）。
"""
from __future__ import annotations
import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.replay import synthetic as world
from tests.replay.diagnose import survivor_bias_diagnosis
from tests.replay.replay import ReplayConfig, run_replay
from tests.replay.strategies import (RandomStrategy, HistoricalOrderStrategy,
                                     GreedyStrategy, UncertaintySamplingStrategy)
from tests.replay.surrogate import BaggedEnsemble
from tests.replay.report import summarize, render_markdown, render_json

BASELINES = ["random", "historical", "greedy"]


def surrogate_factory(seed: int) -> BaggedEnsemble:
    # 纯 Python 实现的性能档位：浅层小森林 + 低频重训；生产替换 LightGBM 时可调回
    return BaggedEnsemble(n_trees=24, max_depth=3, min_samples_leaf=6, seed=seed)


def run_seed(seed: int, mode: str, n_candidates: int, batch_size: int,
             max_rounds: int, refit_every: int = 1) -> dict:
    rng = random.Random(seed)
    candidates = world.generate_candidates(n_candidates, seed)
    history = world.generate_history(candidates, n_experiments=120, seed=seed)
    order = [r["candidate_id"] for r in history if r["state"] in ("measured", "failed")]
    cfg = ReplayConfig(batch_size=batch_size, max_rounds=max_rounds,
                       refit_every=refit_every)

    results = {}
    specs = {
        "random": RandomStrategy(),
        "historical": HistoricalOrderStrategy(order),
        "greedy": None,          # 代理模型在首轮回放前先冷启动训练
        "active_ucb": None,
    }
    # 冷启动：用历史已测数据训练初始代理模型，各策略共享同一模型保证公平
    from tests.replay.freeze import build_training_set
    X0, y0 = build_training_set(history, t0_day=history[-1]["day"], candidates=candidates)
    init = surrogate_factory(seed)
    init.fit(X0, y0)
    specs["greedy"] = GreedyStrategy(init)
    specs["active_ucb"] = UncertaintySamplingStrategy(init)  # β 默认 0.8

    for name, strat in specs.items():
        results[name] = run_replay(mode, history, candidates, strat,
                                   surrogate_factory, cfg, seed=seed,
                                   init_X=X0, init_y=y0)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="AdheFormAI offline replay (B02)")
    ap.add_argument("--seeds", type=int, default=12)
    ap.add_argument("--candidates", type=int, default=600)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--max-rounds", type=int, default=40)
    ap.add_argument("--refit-every", type=int, default=3,
                    help="每 N 轮重训一次历史模型（1=每轮）")
    ap.add_argument("--mode", choices=["synthetic", "historical"], default="synthetic",
                    help="synthetic=合成实验室；historical=只揭示历史真实做过的标签")
    ap.add_argument("--output", default="tests/replay/reports/report.md")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    # ---- 第一步：幸存者偏差诊断（§17.2 + 附录 B.1；与回放同规格的历史序列）----
    diag_cands = world.generate_candidates(args.candidates, seed=0)
    diag_hist_complete = world.generate_history(diag_cands, 120, seed=0, complete_records=True)
    diag_hist_biased = world.generate_history(diag_cands, 120, seed=0, complete_records=False)
    diag = survivor_bias_diagnosis(diag_hist_complete)
    diag_biased = survivor_bias_diagnosis(diag_hist_biased)
    print(f"[diagnose] complete history -> {diag.verdict} "
          f"(failure {diag.failure_ratio:.1%}, reason {diag.reason_completeness:.0%})")
    print(f"[diagnose] biased history   -> {diag_biased.verdict} "
          f"(failure {diag_biased.failure_ratio:.1%}) — 保守档判定自检")

    # ---- 第二步：回放（多种子 → 分布）----
    all_rounds = {k: [] for k in ["random", "historical", "greedy", "active_ucb"]}
    for s in range(args.seeds):
        res = run_seed(s, args.mode, args.candidates, args.batch_size,
                       args.max_rounds, args.refit_every)
        for name, r in res.items():
            all_rounds[name].append(r.rounds_to_target)
        print(f"[replay] seed {s}: " +
              ", ".join(f"{n}={res[n].rounds_to_target}" for n in all_rounds))

    # ---- 第三步：报告（分布 + CI）----
    summaries = [summarize(name, all_rounds[name]) for name in all_rounds]
    md = render_markdown(args.mode, diag, summaries, BASELINES, args.seeds, args.max_rounds)
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(render_json(args.mode, diag, summaries) + "\n")
    print(f"[report] written -> {args.output}"
          + (f" / {args.json_out}" if args.json_out else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
