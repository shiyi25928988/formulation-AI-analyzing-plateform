# 离线回放框架（B02）

在花实验预算之前，用历史序列验证「推荐策略是否真的比基线少做实验」（架构 §17.2；§17.1 闭环层门禁：相对基线轮次下降 ≥30%）。

## 快速开始

```bash
# 合成模式（默认，数据红线合规：全合成数据，可入库可分享）
python3 tests/replay/run_replay.py --seeds 12 --output tests/replay/reports/report.md

# 运行测试
python3 tests/replay/test_replay.py -v
```

零第三方依赖（纯标准库，含 bagged CART 代理模型），气隙环境可直接运行。

## 与架构的逐条对应

| §17.2 要求 | 实现位置 |
|---|---|
| T0 冻结「当时可见」数据 | `freeze.asof_freeze` / `build_training_set` |
| 候选池由约束库在 T0 重建 | `strategies.rebuild_pool` |
| 只揭示真实做过的实验标签 | `replay.run_replay(mode="historical")` |
| 三条基线 | `RandomStrategy` / `HistoricalOrderStrategy` / `GreedyStrategy` |
| 幸存者偏差三步诊断先行 | `diagnose.survivor_bias_diagnosis` |
| 轮次下降分布 + 置信区间 | `report.bootstrap_reduction_ci`（bootstrap 95% CI） |

## 回放模式

- **synthetic**（默认）：合成实验室。被选中的候选都会「做实验」，标签来自合成 ground truth + 噪声；约束外选择按失败处理并消耗轮次。用于验证策略逻辑与框架正确性。
- **historical**：只揭示历史真实做过的实验标签。选到未做过的点 → 该轮消耗、无信息。真实历史数据经脱敏后放 `tests/replay/data/`（**gitignore 已覆盖，不得入库**）。

## 判读规则

1. **先看诊断，再看数字**：`verdict=FULL` 才允许对外引用轮次下降；`CONSERVATIVE` 的报告自带禁止对外引用标注（附录 B.1 口径）。
2. **门禁**：相对任一基线下降 ≥30% 且 CI 下界 >0 判有效；两条基线都不过 → 先改策略与特征，不进平台开发（§17.2 尾注）。
3. **未达标计罚**：`max_rounds × 1.5`，口径在报告头部显式声明。
4. **历史模式的固有偏差**：策略选到未做过点即「浪费一轮」，天然惩罚探索型策略——这是保守档结论只能内部参考的原因之一，不构成框架缺陷。

## 已知简化（升级路径）

| 简化 | 生产替代 |
|---|---|
| bagged CART 代理模型 | LightGBM/XGBoost 多任务 + conformal 校准（接口 `predict/predict_std` 不变） |
| 每轮全量重训 | 增量训练（§6.3） |
| 合成 ground truth 单指标 | 多指标 + Pareto（F10 / §11） |
| day 粒度时间 | 与 ELN 对接的真实时间戳 |

## 文件结构

```
tests/replay/
├── surrogate.py   # 纯标准库 bagged CART（预测 + 不确定性）
├── synthetic.py   # 合成材料系统与历史序列（完整留痕 / 幸存者偏差两种口径）
├── diagnose.py    # 三步诊断 → FULL / CONSERVATIVE 判定
├── freeze.py      # T0 point-in-time 冻结
├── strategies.py  # 三条基线 + 待评估 UCB 策略
├── replay.py      # 回放引擎（synthetic / historical 双模式）
├── report.py      # 分布 + bootstrap CI + Markdown/JSON 报告
├── run_replay.py  # CLI 入口
└── test_replay.py # §17.2 四个不变量的最小测试
```
