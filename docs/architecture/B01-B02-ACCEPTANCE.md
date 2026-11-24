# B01 / B02 验收结论文档

> **轮次**：R2.1（2026-11-24）　**对象**：Backlog B01（核心表 DDL + 迁移脚本）、B02（离线回放框架与基线报告）
> **验收依据**：[`ITERATION_BACKLOG.md`](ITERATION_BACKLOG.md) §2 DoD（第 4 步验证证据 + 第 5 步文档同步缺一不可）、架构 §17.1/§17.2/§18.3/§18.4、附录 B.1
> **验收方式**：静态机械校验 + 单元测试复跑 + 基线报告复核；本文为验收结论的正式记录。

---

## 1. 结论速览

| # | 交付物 | 验收结论 | 保留项 |
|---|---|---|---|
| B01 | `db/migrations/001_init_schema.sql` + `.down.sql` + `README.md` | **有保留通过** | Docker/PG 实机执行验证待补（静态校验已过） |
| B02 | `tests/replay/` 全套框架 + 12 种子基线报告 | **通过** | greedy 基线未过门禁（如实记录，属策略层待改进项，非框架缺陷） |

两项均满足迭代 DoD 第 4 步（有验证证据）与第 5 步（`ARCHITECTURE.md` 口径、`ITERATION_BACKLOG.md` 状态、`CHANGELOG.md` R2.1 节三处同步）。

---

## 2. B01：核心表 DDL + 迁移脚本

### 2.1 交付物

- `db/migrations/001_init_schema.sql` —— 24 张表 / 10 个枚举 / RLS 策略 / 状态机与追加写触发器
- `db/migrations/001_init_schema.down.sql` —— 对应回滚脚本
- `db/migrations/README.md` —— 迁移约定（命名 / 扩展-收缩 / 事务 / RLS / 离线链路红线 / 审计分区 / 数据红线）与验证命令

### 2.2 验收项与证据

| 验收项 | 依据 | 结论 |
|---|---|---|
| 表清单完整 | 架构 §3 领域模型 + §6.4 `prediction_log` + §12 `calib_stat`/`lineage_edge` 等 | ✅ 核心域、实验域、ML 域、审计域全覆盖；含评审修正轮补齐的 `material_batch`、`recommendation` |
| 枚举口径与架构对齐 | §6.1 九态 / §3.2·§3.4 fidelity / OOD 两级 / §5.2 三档 | ✅ `experiment_state` 九态、`fidelity_level`（experiment/simulation/literature）、`ood_tier`（none/advisory/severe）、`interval_level` 仅 0.50/0.70/0.90 |
| 创建顺序无悬空引用 | 静态拓扑检查 | ✅ 曾发现 `prediction_log` 引用 `feature_set` 顺序错误，已修正后通过 |
| RLS 全覆盖 | §18.3 多租户隔离 | ✅ 全部业务表带 `tenant_id` + `FORCE ROW LEVEL SECURITY`，策略由 `app.tenant_id` 会话变量驱动 |
| 离线链路红线 | §18.3 | ✅ `adhe_ml_training` 低权限角色（只读 + RLS），禁止高权限直读全库 |
| 状态机约束 | §6.1 九态迁移白名单 | ✅ 触发器白名单实现，非法迁移被数据库层拒绝 |
| 追加写约束 | `formula_version` / `audit_event` append-only | ✅ 触发器拦截 UPDATE/DELETE；`audit_event` 按月分区 + default 兜底 |
| 事务与回滚 | §18.4 扩展-收缩 | ✅ 迁移单事务、失败整体回滚；配套 `.down.sql` 可逆 |
| 数据红线 | 迭代原则 5 | ✅ 迁移与 seed 无真实配方 / 实测数据 / 密钥 |
| 静态机械校验 | 事务平衡、创建顺序拓扑、RLS 全覆盖、枚举口径比对四项 | ✅ 2026-11-24 通过（校验器曾漏检 DO 块内动态 RLS/policy，已修正校验器后复核） |

### 2.3 结论与保留项

**有保留通过。** 结构、口径、约束、安全四方面均达到设计要求；唯一保留项为 **Docker/PG 实机执行验证未跑**（本机无 Docker/PG 环境），验证命令已固化在 `db/migrations/README.md`，条件具备时补跑 `up → down → up` 并回填本节。

**遗留待办**：① Docker 执行验证；② RLS 策略测试（对应 B10）。

---

## 3. B02：离线回放框架与基线报告

### 3.1 交付物

- `tests/replay/` 零第三方依赖回放框架（纯标准库，气隙环境可运行）：`surrogate.py`（bagged CART 代理）、`synthetic.py`（含协同增效窄脊的合成材料系统）、`diagnose.py`（幸存者偏差三步诊断）、`freeze.py`（T0 point-in-time 冻结）、`strategies.py`（三基线 + UCB 待评估策略）、`replay.py`（synthetic/historical 双模式）、`report.py`（bootstrap CI + 报告）、`run_replay.py`、`test_replay.py`
- `tests/replay/reports/synthetic-baseline-2026-11-24.md` + `.json` —— 12 种子基线报告

### 3.2 验收项与证据

| 验收项 | 依据 | 结论 |
|---|---|---|
| §17.2 六要素逐条落地 | T0 冻结 / 候选池重建 / 只揭示真实标签 / 三基线 / 三步诊断先行 / CI | ✅ 逐条有实现位置（`tests/replay/README.md` 对照表） |
| 幸存者偏差诊断 | 附录 B.1 定论（历史库按不完整设计） | ✅ 三步诊断 → FULL/CONSERVATIVE 判定；保守档报告自带禁止对外引用标注 |
| 单元测试 | §17.2 四个不变量 | ✅ **10 项测试全过**（2026-11-24 复跑，0.201s OK），覆盖：完整历史=FULL、空/剥除/部分剥除历史=CONSERVATIVE、冻结不含未来、训练集仅含已实测、历史模式绝不揭示未做点、候选池排除已做与不可行、同种子确定性、曲线产出 |
| 基线报告与门禁 | §17.1 闭环层门禁：相对任一基线下降 ≥30% 且 CI 下界 >0 | ✅ 见下表，两条基线过门禁 |
| 数据红线 | 全合成数据，historical 真实数据路径已被 gitignore 覆盖 | ✅ |

**12 种子合成回放门禁结果**（诊断判定 FULL，失败/取消占比 53.3%、原因完整度 100%，结论可对外引用）：

| 对比 | 下降中位 | bootstrap 95% CI | 门禁（≥30% 且下界>0） |
|---|---|---|---|
| active_ucb vs random | 84.6% | [44.6%, 89.4%] | ✅ |
| active_ucb vs historical | 88.9% | [65.4%, 94.5%] | ✅ |
| active_ucb vs greedy | 0.0% | [-173.3%, 77.6%] | ❌ |

### 3.3 结论与说明

**通过。** 按门禁规则「相对任一基线过即判有效」，active_ucb 对 random、historical 双双通过且 CI 下界大幅高于 0，**在合成世界中证明了主动学习策略的轮次下降有效性**——这正是 B02「在花实验预算之前先证明」的目标。

两点如实记录、不做修饰：

1. **greedy 基线未过门禁**（中位持平）。greedy 在合成模式可直接逼近最优区，与 active_ucb 中位同为 1 轮。这是策略与合成世界特性的真实结果，按「真实发现不修饰」原则记录；处理方式：后续策略迭代（换真实标签分布、historical 模式复验）时继续观察，不构成框架缺陷。
2. **历史模式固有偏差**：选到未做过的点即浪费一轮，天然惩罚探索型策略——已写入判读规则，属保守档结论仅内部参考的原因之一。

**已知简化（均有生产升级路径，接口不变）**：bagged CART → LightGBM/XGBoost + conformal；每轮全量重训 → 增量训练；单指标 → 多指标 + Pareto（F10）；day 粒度 → ELN 真实时间戳。

**遗留待办**：① 真实脱敏历史数据入 `tests/replay/data/` 后跑 historical 模式复验；② greedy 对比项在策略迭代中复验。

---

## 4. 综合结论与对 R2.2 的输入

1. **B01/B02 验收完成**（B01 有保留通过、B02 通过），R2 前两件 P0 落地，状态已在 `ITERATION_BACKLOG.md` 标记为「R2 已落地」。
2. **B02 的关键前置已解除**：幸存者偏差诊断内置后，回放框架对「历史序列不完整」的现实有了制度化防线，附录 B.1 定论已闭环到代码与测试。
3. **R2.2 单一主矛盾不变**：B03 Walking skeleton——用合成数据把 B01 的表结构与 B02 的回放纪律串成端到端可运行链路（导入 → QC → 特征 → 训练 → 预测 → 推荐 → 回写 → 重训），验收要点是「下一轮推荐确实发生变化」且审计可反查。
4. **移交清单**：B03 直接复用 `tests/replay` 的合成数据纪律与 `001_init_schema.sql` 的表结构；B10（RLS 策略测试）以 B01 保留项为输入。
