# B09 详细设计：训练 → 门禁 → 注册 → 灰度流水线 + 模型卡模板

> 状态：详设评审稿（R2.6）。实现轮按本文档落代码，如实现中发现冲突，回改本文档并同步 CHANGELOG。
> 依据：ARCHITECTURE.md §5.5（生命周期与门禁）、§8.4（溯源）、§16.2（双版本对照）、§17.5（go/no-go）；`db/migrations/001_init_schema.sql`（`model_stage` 枚举）；`packages/contracts/openapi/model-lifecycle-svc.json`；B06 `canary/`、B07 `features/`。

---

## 1. 目标与范围

把 §5.5 中只存在于文档的「训练 → 门禁 → 注册 → 影子 → 灰度 → 生产 → 回滚」落成**可执行的状态机 + 检查序列 + 模型卡模板**，使模型从训练完成到可服务的每一步都有白名单迁移、门禁证据与审计留痕。

**DoD（迭代记录 R2.6 建议口径）**：

1. 模型卡模板可由骨架管线自动填充 **≥80%** 字段（本文 §5 给出字段级填充来源表）；
2. 状态机白名单**镜像 DDL**（`model_stage` 枚举为唯一权威）；
3. 金丝雀（B06 `run_canary`）作为 **canary 阶段的门禁**被显式引用。

**不在本轮范围**：真实训练作业编排（Celery 任务流）、MLflow 对接、流量切分基础设施（灰度百分比在骨架中以「比例参数 + 判定函数」替身）。

---

## 2. 生命周期状态机

### 2.1 与 backlog 建议的口径裁定

R2.6 建议草稿曾写「trained → gate_pending → shadow → canary → promoted / rejected」。**详设裁定：以 DDL `model_stage` 枚举为唯一权威**——

```
registered → shadow → canary → production → deprecated
```

理由：

- B01 DDL 已验收，枚举变更属破坏性迁移，需独立评审而非 backlog 行文顺手改；
- `trained` / `gate_pending` 是**注册前**的产物状态，不进 `model_registry` 表——门禁失败的根本处置是「不入册」（§5.5：不达标不允许注册），无需 REJECTED 终态污染注册表；
- 门禁证据不丢：失败结果写入模型卡（§5）与审计事件 `ModelGateFailed`，模型产物保留在 MinIO 可复查。

### 2.2 状态图

```
                         ┌──────────────────────────────────────────┐
                         │  （表外）训练完成：artifact 落 MinIO      │
                         │  G0–G5 门禁检查序列（§3.1）              │
                         └──────┬───────────────────────┬───────────┘
                          全过  │                       │ 任一失败
                                ▼                       ▼
   ┌────────────┐  影子对拍  ┌────────────┐  金丝雀+灰度 ┌────────────┐
   │ registered │───────────▶│   shadow   │─────────────▶│   canary   │
   │ （staging） │            │  （对拍）   │              │ 5%→25%→100%│
   └────────────┘            └────────────┘              └─────┬──────┘
         ▲                                                     │ 全过
         │            ┌────────────────────────────────────────┤
         │            ▼                                        ▼
         │      ┌────────────┐    漂移触发自动回滚        ┌────────────┐
         │      │ deprecated │◀─────────────────────────│ production │
         │      └────────────┘    人工淘汰              └────────────┘
         └── 上一 production 版本恢复服务（路由回切，非状态迁移）
```

### 2.3 迁移白名单（镜像 DDL，逐条守卫）

| # | 迁移 | 守卫条件（全过才允许） | 事件（成功） | 失败处置 |
|---|---|---|---|---|
| T0 | （表外）→ `registered` | G0–G5 门禁检查序列全过（§3.1）；三元组绑定校验通过 | `ModelRegistered`（新增，需契约补录） | **不入册**；`ModelGateFailed`（新增）+ 模型卡记失败项；旧版本继续服务 |
| T1 | `registered` → `shadow` | artifact 存在且校验和通过；feature_set 指纹与 G0 记录一致 | `ModelShadowStarted`（新增） | 保持 registered，告警 |
| T2 | `shadow` → `canary` | 影子对拍差异分布稳定（§16.2 双版本对照：分布突变即阻断，不只看聚合指标） | `ModelCanaryStarted`（新增） | 保持 shadow，延长对拍或人工裁定 |
| T3 | `canary` → `production` | **B06 金丝雀 `run_canary().overall_pass == True`**（canary 阶段门禁，DoD 3）；灰度比例 5%→25%→100% 期间无回滚触发；覆盖率/违规率在线口径不越界 | `ModelPromoted`（契约已有） | 停在 canary；金丝雀失败已自动写 `CanaryRun`（action=quarantine-recommended） |
| T4 | `production` → `deprecated` | 仅两条路径：① 漂移监控触发**自动回滚**（§5.5 三路漂移任一持续超阈值）；② 人工淘汰（需审批人） | `ModelDeprecated`（新增）+ 回滚时 `ModelRolledBack`（新增） | ——（终态） |
| T5 | `canary` → `deprecated` | canary 期间人工终止或严重事件 | `ModelDeprecated` | ——（终态） |

**非法迁移一律拒绝**（409 Conflict，与 experiment 状态机同口径）；所有迁移写 `audit_event`，事件幂等键 `(tenant_id, event_type, aggregate_id, aggregate_version)`，`aggregate_id = model_version`。

### 2.4 回滚语义（明确非状态迁移）

「自动回滚到上一稳定版本」= 当前 production 版本 T4 转 `deprecated` + **推理路由回切到仍处 `production` 的上一版本**（`model_registry` 中同 `model_type` 的另一行）。因此白名单允许同 type 同时存在至多两个 production 版本（新旧并行），路由由 model-lifecycle-svc 决定——这也是双版本对照（§16.2 第三层）的基础。

---

## 3. 可执行门禁检查序列

### 3.1 注册门禁（T0，按执行顺序）

| 编号 | 检查 | 输入 | 阈值（§5.5） | 失败处置 |
|---|---|---|---|---|
| G0 | 三元组绑定 | model ↔ feature_set(含指纹) ↔ dataset_snapshot | 三者互相引用且指纹一致（B07） | 立即失败，不跑后续（不可复现一切免谈） |
| G1 | 可复现性 | 同代码+同快照+同种子重训 | 指标差异 < 1% | 失败即整序列失败 |
| G2 | 相对基线不劣化 | 固定评估集 RMSE（前版本为基线） | 劣化 ≤ 2%，且至少 1 指标显著改进 | 失败 |
| G3 | 区间覆盖率 | 留出集 0.90 区间实测覆盖 | ≥ 88% | 失败 |
| G4 | OOD 检出召回 | 留出体系负例 | ≥ 85% | 失败 |
| G5 | 物理一致性违规率 | B06 `run_assertions` 对留出集全量预测 | ≤ 1%（severe 计） | 失败 |

执行原则：**短路求值按 G0→G5 顺序**，失败即停，已执行项的结果全部记入模型卡（失败项标 FAIL，其余标 PASS/SKIPPED）；门禁结果整体写 `metrics_json`（DDL 字段已预留「含覆盖率/违规率门禁结果」）。

### 3.2 canary 阶段门禁（T3）

| 编号 | 检查 | 实现 | 阈值 |
|---|---|---|---|
| C1 | 金丝雀集回归 | **B06 `canary.runner.run_canary`**：`overall_pass == True`，且 `CanaryRun` 审计事件 action=none | 12/12 通过 |
| C2 | 双版本对照 | 新旧版本对同一请求集并行推理，差异分布与影子期基线比较 | 分布突变即阻断（KS 检验替身：骨架用分位数偏差 < 3σ） |
| C3 | 在线口径 | 灰度流量上的覆盖率/违规率滑动窗口 | 同 G3/G5 阈值 |

C1 直接复用 B06 交付物，不新造第二套金丝雀——**单一事实来源**。

### 3.3 骨架替身口径（如实记录）

B03 步骤 8 的门禁用了「漂移 ≤ 0.5×噪声底噪」绝对容忍带（小样本下相对 2% 会被重训噪声误判）。实现轮 gates 模块采用**双口径**：生产阈值按 §5.5 相对值，骨架替身按绝对容忍带，阈值常量集中定义并在模型卡标注实际使用口径——不做静默二选一。

---

## 4. 灰度阶段定义

| 阶段 | 流量 | 进入条件 | 退出条件 | 最低观察量 |
|---|---|---|---|---|
| shadow | 0%（只记录不返回） | T1 | 对拍稳定（T2） | ≥ 30 个请求或 1 个实验周期 |
| canary-1 | 5% | T2 | 无回滚触发 | ≥ 1 轮金丝雀全过 |
| canary-2 | 25% | 同上持续 | 同上 | 同上 |
| production | 100% | T3 | 漂移触发 T4 | —— |

- **回写周期优先于固定天数**（§17.3 冠军挑战者口径）：小样本场景实验周期长，阶段推进以「回写实验轮数」为节拍，不以自然日；
- 观察量不足**不得推进**（宁可慢，不可带病晋级）；
- canary 期间任何一次 `CanaryRun` 失败 → 立即冻结比例并回切路由，等人工裁定。

---

## 5. 模型卡模板

### 5.1 模板全文（`templates/model-card.tmpl.md`，实现轮落仓库）

```markdown
# 模型卡：{model_type} · {model_version}

## 1. 标识与三元组
- 模型版本：{model_version}（artifact：{artifact_ref}，校验和 {artifact_sha256}）
- 特征集：{feature_set_version}（指纹 {feature_set_fingerprint}，B07 注册）
- 数据快照：{dataset_snapshot_id}（label={snapshot_label}，n_rows={n_rows}，冻结时间 {frozen_at}）
- 训练环境：seed={seed}，随机性声明：{reproducibility_note}

## 2. 训练数据声明
- 数据来源保真度分布：{fidelity_distribution}（experiment / simulation / literature）
- 失败/取消记录：{failure_share}%（幸存者偏差诊断档位：{replay_mode}）
- 数据红线声明：{data_redline_statement}（真实配方不入测试 fixture）

## 3. 评估与门禁（G0–G5 逐项）
| 门禁 | 阈值 | 实测 | 判定 |
|---|---|---|---|
{gate_table_rows}

## 4. 指标明细
| 指标 | RMSE | 0.90 覆盖率 | severe 违规率 | conformal 档位 |
|---|---|---|---|---|
{metric_rows}

## 5. canary 阶段证据（B06）
- 金丝雀集：{canary_set_version}（指纹 {canary_fingerprint}）
- 判定：{canary_verdict}（{canary_passed}/{canary_total}，失败条目：{canary_failed_ids}）
- 双版本对照：{shadow_compare_note}

## 6. 已知局限与不适用范围
- {known_limitations}   <!-- 如：greedy 策略基线未过门禁（B02 如实记录） -->

## 7. 生命周期履历
| 时间 | 迁移 | 操作者/触发源 | 事件 ID |
|---|---|---|---|
{lifecycle_rows}

## 8. 审批与责任
- 注册审批：{gate_approver}；晋级审批：{promote_approver}；回滚记录：{rollback_note}
- 联系岗位：{owner_role}（RACI 见附录 B.2 待定项）
```

### 5.2 字段填充来源表（DoD 1：≥80% 自动填充）

| 字段组 | 来源 | 自动填充 |
|---|---|---|
| §1 标识与三元组 | `model_registry` 行 + B07 feature_set 指纹 + snapshot 元数据 | ✅ 100% |
| §2 数据声明 | `dataset_snapshot` 元数据 + B02 诊断档位 + `.gitignore` 红线常量 | ✅ 100% |
| §3 门禁表 | G0–G5 检查序列输出 | ✅ 100% |
| §4 指标明细 | `metrics_json` + conformal 档位常量 | ✅ 100% |
| §5 canary 证据 | B06 `run_canary` 返回值（含指纹与失败条目） | ✅ 100% |
| §6 已知局限 | 门禁/基线报告的「如实记录」小节（人工 curated，骨架给出候选列表） | ⚠️ 半自动 |
| §7 生命周期履历 | `audit_event` 按 model_version 反查 | ✅ 100% |
| §8 审批与责任 | 审批人字段 + RACI（附录 B.2 开放问题，暂用角色占位） | ⚠️ 半自动 |

**统计**：8 组字段中 6 组全自动（约 83% 字段级自动填充，DoD 1 达成口径：按字段计 ≥80%）；2 组半自动均需人工判断（curated 局限、审批人），与「自动化测试只用合成数据」不冲突。

---

## 6. 事件与契约扩展清单（实现轮补录）

> **补录状态（2026-11-25 更新）**：6 事件与 4 API 已入 `packages/contracts/`（catalog 19 事件 / payload $defs 19 / model-lifecycle-svc 9 paths），校验器 PASS + 18 项契约测试全过；DDL 迁移白名单触发器已落档 `db/migrations/002_model_stage_guard.sql`（与代码白名单由镜像断言机械绑定）。

| 项 | 类型 | 说明 |
|---|---|---|
| `ModelRegistered` / `ModelShadowStarted` / `ModelCanaryStarted` / `ModelDeprecated` / `ModelRolledBack` / `ModelGateFailed` | 事件补录 | catalog + payload $defs + §16.4 表行，与 B04 流程一致 |
| `POST /models/{v}/gate` 、`POST /models/{v}/shadow/start`、`POST /models/{v}/canary/start`、`GET /models/{v}/model-card` | OpenAPI 扩展 | 现有 `/promote` `/rollback` 保留，语义映射 T3/T4 |
| `model_registry` 迁移白名单触发器 | DDL 扩展 | 与 `experiment_transition_guard` 同构，实现轮出 migration 002 |

---

## 7. 与既有资产的衔接

| 资产 | 衔接点 |
|---|---|
| B01 DDL | `model_stage` 枚举 = 状态机唯一权威；`metrics_json` 承载门禁结果；实现轮补迁移白名单触发器 |
| B04 契约 | model-lifecycle-svc OpenAPI 扩展 + 事件补录（§6 清单） |
| B06 金丝雀 | C1 门禁直接调用 `canary.runner.run_canary`，`CanaryRun` 事件即证据 |
| B07 特征工厂 | G0 绑定校验用 `FeatureFactory.fingerprint()`；模型卡 §1 引用指纹 |
| B03 骨架 | 步骤 8 重训改造为调用 gates 序列；`step8_retrain` 返回值含门禁明细；模型卡由骨架自动填充（§5.2） |

## 8. 实现拆分建议（下一轮 B09 实现）

1. `lifecycle/store.py`：model_registry 内存表 + 迁移白名单（镜像 §2.3，非法迁移拒绝）
2. `lifecycle/gates.py`：G0–G5 + C1–C3 检查序列（短路求值，双口径阈值常量）
3. `lifecycle/modelcard.py`：模板渲染（§5.1），骨架管线自动填充
4. `lifecycle/test_lifecycle.py`：合法迁移全过 / 非法迁移拒绝 / 门禁失败不入册 / 模型卡自动填充率断言 ≥80%
5. 契约补录（§6 清单）+ 文档同步
