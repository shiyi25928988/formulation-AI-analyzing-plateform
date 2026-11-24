# packages/contracts — 服务接口契约包（B04）

固化 `docs/ARCHITECTURE.md` §4.3（API 契约）与 §16.4（事件信封与目录），支撑**契约测试**与 **OpenAPI diff**（破坏性变更必须在 CI 中被 diff 捕获，旧版本至少并行 2 个迭代周期）。

全部为 OpenAPI 3.1 / JSON Schema 2020-12（JSON 形式，机器可校验）；零第三方依赖，校验器与测试均为纯标准库。

## 目录

```
packages/contracts/
├── schemas/                      # 共享 JSON Schema
│   ├── common.schema.json        # 基础类型：租户/指标/配方/版本标识/状态机枚举
│   ├── envelope.schema.json      # 事件统一信封（§16.4 / ADR-16）
│   ├── error.schema.json         # 统一错误（RFC 9457 Problem Details）
│   ├── provenance.schema.json    # 溯源三元组（必返红线）
│   ├── interval.schema.json      # conformal 三档区间（0.50/0.70/0.90）
│   ├── prediction-result.schema.json  # 单指标预测结果（OOD 条件语义）
│   └── task.schema.json          # 异步任务视图（四队列隔离）
├── openapi/                      # 九服务 OpenAPI 3.1（§4.1 全覆盖）
│   ├── prediction-svc.json       # 全量旗舰：/predict /predict/batch /tasks（§4.3 示例入库）
│   ├── optimization-svc.json     # Pareto / 约束检查（不直接调模型）
│   ├── active-learning-svc.json  # 推荐批次（发布 RecommendationIssued）
│   ├── generation-svc.json       # 可执行性过滤（一期）；生成 501（三期）
│   ├── experiment-svc.json       # 九态迁移白名单（镜像 DDL experiment_transition_guard）
│   ├── data-hub-svc.json         # 导入/QC/快照冻结（强一致边界）
│   ├── knowledge-svc.json        # /search 一期；/qa /graph 501（二期，ADR-19/20）
│   ├── auth-audit-svc.json       # authz 判定 / 审计查询 / 哈希链校验 / 导出审批
│   └── model-lifecycle-svc.json  # 训练/晋升/回滚 /calibration/*（F1）/ 漂移订阅
├── events/
│   ├── catalog.json              # 事件目录：12 事件 × 发布者/消费者/投递语义/幂等键规范
│   └── events.schema.json        # 各事件 payload（$defs，与目录双向覆盖）
└── tools/
    ├── validate.py               # 零依赖契约校验器（5 类检查）
    └── test_contracts.py         # 18 项契约测试（样例 payload 走最小校验器）
```

## 校验

```bash
python packages/contracts/tools/validate.py       # 结构 + 事件 + 红线，PASS 才可提交
python packages/contracts/tools/test_contracts.py # 18 项样例测试
```

## 校验器五类检查

1. **语法**：全部 JSON 可解析；schema 关键字限于已支持子集（不支持的关键字直接报错，防静默失效）。
2. **OpenAPI**：版本 3.1.0、服务清单与 §4.1 九服务一致、每个操作有 operationId/responses、**所有 POST 必带 Idempotency-Key + X-Tenant-Id**（§4.3）、4xx/5xx 统一 `application/problem+json` 引用 error.schema。
3. **事件**：catalog 与 events.$defs **双向覆盖**；publisher/consumer 必为九服务；投递语义仅两种（`at-least-once-idempotent` / `effective-exactly-once`）；信封 required 与 §16.4 十字段一致；每个事件的最小合法样例必须通过自身 payload schema。
4. **红线**：provenance 三元组必返；conformal 档位枚举 = {0.50, 0.70, 0.90}；OOD 两级 = {none, advisory, severe}；任务状态机与四队列枚举；九态与 §6.1 / DDL 白名单一致。
5. **$ref**：相对路径 + JSON Pointer 全部可达。

另含**预测结果条件语义**（`semantic_check_prediction`，测试与运行时可复用）：
`severe` → value/intervals/explanation 必须缺省（拒答不返数值）；`advisory` → `advisory_only` 必须为 true；返回数值 → 三档区间齐全。

## 关键契约口径（速查）

| 契约点 | 值 | 出处 |
|---|---|---|
| 幂等 | 所有 POST 带 `Idempotency-Key`；事件幂等键 `(tenant_id, event_type, aggregate_id, aggregate_version)` | §4.3 / §16.4 |
| 租户 | 所有操作带 `X-Tenant-Id`（网关校验透传，RLS/ABAC 作用域键） | §8.2 |
| 不确定性 | 三档 conformal，分位数学自洽；未校准档 `calibrated=false` | §5.2 |
| OOD | advisory 返回值强制标记；severe 拒答不返数值 | §4.3 / §15.3 |
| 解释 | `explain` 可降级：超时先返回预测+区间+溯源，解释 `deferred` 异步补齐 | §15.2 |
| 任务 | `queued→running→succeeded/failed/cancelled`；四队列隔离；失败留中间产物 | §4.3 / §15.2 |
| 版本 | `/api/v1` 路径版本化；破坏性变更升版本，旧版并行 ≥2 迭代周期 | §4.3 |
| 错误 | RFC 9457 Problem Details；`type` 预留 `/errors/validation` `/errors/invalid-transition` `/errors/gate-failed` `/errors/ood-severe` 等 | §4.3 |
| 事件重试 | 仅幂等操作可重试；指数退避+抖动，5 次后入死信，死信可人工重放 | §16.4 |
| schema 兼容 | 事件 schema 升版本时消费者兼容至少两个版本 | §16.4 |

## 对目录相对 §16.4 原表的补录与澄清

- **`RecommendationIssued`**（active-learning-svc → experiment-svc/audit）：§16.4 原表缺失，R2.2 skeleton 已使用，本轮补录并同步架构文档。
- 消费者名按一期服务归位（如「QC 服务」→ data-hub-svc 质量规则引擎，「在线推理」→ prediction-svc），原表语义不变。
- `DriftDetected` 投递语义由「允许重复」收敛为 `at-least-once-idempotent`（消费端幂等吸收重复，语义等价）。

## 使用方式

- **服务端**：实现以上述 schema 为准（请求校验 + 响应生成）；九态迁移、幂等去重逻辑参考 `skeleton/store.py`（B03 镜像实现）。
- **客户端/前端**：按 OpenAPI 生成类型；severe OOD 与 `deferred` 解释必须在 UI 层显式呈现（界面原型 v0.1 已对齐）。
- **CI**：`validate.py` + `test_contracts.py` 作为门禁；契约变更走 PR diff，破坏性变更（删字段/收紧枚举/改必填）须升 `/api/v` 版本。
