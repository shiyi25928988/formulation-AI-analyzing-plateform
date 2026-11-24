# R2.3 变更说明：B04 服务契约包（`packages/contracts/`）

> 变更窗口：2026-11-24 · 提交：`9cba6f9`（契约包代码，21 文件 / +2783 行）、`5419646`（架构文档集同步）、`479be59`（提交号回填） · 远端 `origin/main` 已更新至 `479be59`
> 对应 backlog：`ITERATION_BACKLOG.md` B04（R2.3 已落地）· 架构依据：`ARCHITECTURE.md` §4.1–§4.3、§16.4、§15.2、§5.2、§6.1、§8.2

## 1. 本轮目标与结论

**目标**（B04 DoD）：把 §4.3 API 契约示意与 §16.4 事件目录固化为机器可校验的契约包，支撑契约测试与 OpenAPI diff，供 B05–B08 并行开发共用（单一事实来源）。

**结论**：已完成。九服务 OpenAPI 3.1 全部定型、12 事件目录与 payload schema 固化、零依赖校验器五类检查 PASS、18 项契约测试全过。全量回归：契约 18 项 + 回放 10 项 + 骨架 21 项测试全过。

## 2. 交付物清单

```
packages/contracts/
├── schemas/                      # 7 件共享 JSON Schema（2020-12 子集）
│   ├── common.schema.json        # 租户/指标/配方/版本标识/九态与任务枚举
│   ├── envelope.schema.json      # 事件统一信封（§16.4 十字段，spec_version 钉死 1.0）
│   ├── error.schema.json         # 统一错误（RFC 9457 Problem Details）
│   ├── provenance.schema.json    # 溯源三元组必返（model_version + feature_set_version + snapshot_id）
│   ├── interval.schema.json      # conformal 三档（0.50/0.70/0.90，分位数学自洽）
│   ├── prediction-result.schema.json  # 单指标预测结果（OOD 条件语义）
│   └── task.schema.json          # 异步任务视图（四队列隔离：feature/batch/train/export）
├── openapi/                      # 九服务 OpenAPI 3.1（§4.1 全覆盖）
│   ├── prediction-svc.json       # 全量旗舰：/predict 同步 + /predict/batch 异步 + /tasks
│   ├── optimization-svc.json     # Pareto 运行/前沿获取 + 约束检查（不直接调模型）
│   ├── active-learning-svc.json  # 推荐批次（发布 RecommendationIssued，severe 点强制复核）
│   ├── generation-svc.json       # 可执行性过滤（一期）；生成接口 501（三期占位）
│   ├── experiment-svc.json       # 九态迁移白名单 + 状态日志时间线
│   ├── data-hub-svc.json         # 导入（失败留痕红线参数）+ 快照冻结（强一致）
│   ├── knowledge-svc.json        # /search 一期；/qa、/graph 501（二期，ADR-19/20）
│   ├── auth-audit-svc.json       # authz 判定 / 审计查询 / 哈希链校验 / 导出审批
│   └── model-lifecycle-svc.json  # 训练/晋升门禁/回滚 + /calibration/*（F1 归口）+ 漂移订阅
├── events/
│   ├── catalog.json              # 12 事件目录（发布者/消费者/投递语义/幂等键规范）
│   └── events.schema.json        # 各事件 payload（$defs，与目录双向覆盖）
├── tools/
│   ├── validate.py               # 零依赖契约校验器（五类检查）
│   └── test_contracts.py         # 18 项契约测试
└── README.md                     # 目录说明 + 红线口径速查表 + 使用方式
```

## 3. 关键契约决策（对外承诺口径）

| # | 决策 | 依据 |
|---|---|---|
| 1 | **所有 POST 必带 `Idempotency-Key` + `X-Tenant-Id`**，校验器强制；重放返回上次结果并标 `idempotency_replay`，同键不同指纹返回 409 | §4.3 / §8.2 |
| 2 | **事件幂等键 = `(tenant_id, event_type, aggregate_id, aggregate_version)`**；投递语义仅 `at-least-once-idempotent` 与 `effective-exactly-once` 两种；重试指数退避最多 5 次入死信 | §16.4 |
| 3 | **provenance 三元组必返**（model_version / feature_set_version / dataset_snapshot_id），缺一即契约违规 | §4.3 / §3.5 |
| 4 | **conformal 三档钉死 0.50/0.70/0.90**，分位数学自洽（0.9→p5/p95 等）；未校准档 `calibrated=false` 显式标记 | §5.2 |
| 5 | **OOD 两级**：`advisory` 返回数值但强制 `advisory_only=true`；`severe` 拒答——value/intervals/explanation 必须缺省，不返回任何数值 | §4.3 / §15.3 |
| 6 | **解释可降级**：`explain` 超时先返回预测+区间+溯源，解释 `deferred` 异步补齐（解释比推理贵 5 倍以上） | §15.2 |
| 7 | **任务状态机** `queued→running→succeeded/failed/cancelled`；失败保留中间产物；四队列隔离防训练饿死交互任务 | §4.3 / §15.2 |
| 8 | **错误统一 RFC 9457**，`type` 预留 `/errors/invalid-transition`、`/errors/gate-failed`、`/errors/ood-severe` 等稳定分支 | §4.3 |
| 9 | **九态枚举镜像 DDL 白名单**（`experiment_transition_guard`）；非法迁移 409 | §6.1 / B01 |
| 10 | **knowledge-svc 禁止向终端用户暴露 Cypher**（结构化查询模式替代），服务账号独占连接 | ADR-20 |

## 4. 相对架构文档原表的三处补录/澄清（已同步进 §16.4）

1. **补录 `RecommendationIssued`**（active-learning-svc → experiment-svc/审计）：§16.4 原表缺失，但它是闭环状态机起点事件且 R2.2 骨架已实际使用——目录与实现补齐。
2. **消费者名按一期服务归位**：「QC 服务」→ data-hub-svc 质量规则引擎（§7.1）、「在线推理」→ prediction-svc、「训练编排」→ model-lifecycle-svc（§5.5）——原语义不变，落为具体服务。
3. **`DriftDetected` 投递语义收敛**：原表「至少一次，允许重复」→ `at-least-once-idempotent`（重复由消费端幂等吸收，语义等价，消除目录中的第三种语义）。

## 5. 本轮发现并解决的问题

### 5.1 契约自冲突（结构性修正）

`intervals` 必返（§4.3「不确定性是必返字段」）与 severe OOD 拒答须缺省区间（§4.3 红线）在结构层面互相矛盾。**解法**：`intervals` 移出 required，新增**语义条件校验** `semantic_check_prediction()` 强制三条规则——severe 缺省数值结构、advisory 强制标记、返值必须三档齐全（含档位集合精确比对）。校验器对该函数做了正反样例自检（合法样例零误报、非法样例全部拦截），测试覆盖三条红线。

### 5.2 校验器自身 3 处 bug（开发期修复）

- 关键字遍历误伤属性名（把 `properties` 下的字段名当 schema 关键字报错）→ 改为仅对「schema 形状」节点检查；
- 最小样例生成不满足 pattern → pattern 类定义补 `examples`，生成器优先取 examples；
- 红线检查未解析 `$ref`（interval confidence 实际引用 common.ConfidenceLevel）→ 补 ref 解析。

修复后五类检查 PASS，且正向确认了「校验器能拦截自己写错」的价值。

## 6. 验收结果

| 检查 | 结果 |
|---|---|
| `validate.py` 五类检查 | **PASS**（18 个 JSON 文件语法/子集；9 服务清单与 §4.1 一致；目录↔$defs 双向覆盖；红线全过；$ref 全可达） |
| `test_contracts.py` | **18 项全过**（0.05s） |
| `tests/replay` 回归 | 10 项全过 |
| `skeleton` 回归 | 21 项全过 |
| 文档一致性 | §4.3/§16.4 加契约包指针；§18.1 `packages/contracts/` 路径与落位一致；backlog/CHANGELOG/README 同步 |

## 7. 边界与不做清单（本轮未动）

- 不含服务端实现（契约先行，B05+ 按契约实现）；`skeleton/` 仍是链路语义的镜像实现，接口名已与契约对齐。
- `generation-svc` 生成接口、`knowledge-svc` /qa 与 /graph 固化契约形状但返回 501（三期 / 二期范围，ADR-12 / ADR-19 / ADR-20）。
- 未引入任何第三方依赖（校验器为 JSON Schema 2020-12 子集，不支持的关键字显式报错防静默失效）。

## 8. 下一步

**R2.4 / B06 语义断言 + 金丝雀配方集**（依赖 B01、B04 均已满足）：物理一致性断言 + 金丝雀集版本化进 prediction 响应链路与门禁；验收 = 对故意引入物理矛盾的输出 100% 拦截，全部走本契约包。
