# 变更记录

> 采用 Keep a Changelog 简化格式。**架构版本**（文档演进）与**仓库提交**分开记录，便于立项评审追溯「哪一版架构对应哪次交付」。
> 架构总纲：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · 迭代机制与待办：[`docs/architecture/ITERATION_BACKLOG.md`](docs/architecture/ITERATION_BACKLOG.md)。

---

## [R2.6d 收尾 · B09 migration 002] · 2026-11-25

**主题**：B09 收口——`model_registry` 迁移白名单触发器（详设 §6 清单最后一项）。

- `db/migrations/002_model_stage_guard.sql` + 回滚脚本：五条迁移白名单 T0–T5 与 `experiment_transition_guard` 同构；INSERT 必须从 `registered` 起步（门禁失败=不入册，无表外脏状态）；回滚=路由回切非状态迁移，白名单不含回退迁移；双 production 并行数守卫在服务层（行级触发器不约束跨行数量）
- **DoD 2 机械绑定**：`lifecycle/test_lifecycle.py` 新增镜像断言——002 SQL 白名单 == `ALLOWED_TRANSITIONS`，改动任何一侧必须同批同步
- 静态机械校验（事务平衡）随断言固化；Docker 执行验证沿用 001 待办口径
- B09 全部 DoD 达成：1 模型卡 0.8947 ≥0.80、2 白名单镜像 DDL、3 金丝雀作为 canary 门禁
- 回归：lifecycle 42 项全过

---

## [R2.6c 交付 · B09 编排 pipeline + 模型卡] · 2026-11-25

**主题**：B09 实现轮第二、三件——`LifecyclePipeline` 编排器把训练→门禁→注册→影子→灰度→晋级→回滚串成可执行流水线；模型卡渲染达成 DoD 1。一页汇报见 [`B09-ORCHESTRATION-REPORT.md`](docs/architecture/B09-ORCHESTRATION-REPORT.md)，样例卡见 [`B09-MODEL-CARD-SAMPLE.md`](docs/architecture/B09-MODEL-CARD-SAMPLE.md)。

### 新增

- `lifecycle/pipeline.py`：LifecyclePipeline（T0–T3 全链路编排；影子观察量 ≥30 节拍、灰度阶梯 5%→25%→100% 替身、T1 artifact 校验和 + 指纹守卫、跨代 G2 基线辅助；替身口径如实记录）
- `lifecycle/modelcard.py`：render_model_card（8 节模板 + 字段级 auto/semi 统计，实测 auto_fill_ratio 0.8947 ≥0.80，DoD 1 达成；渲染只读，契约 GET /model-card 响应对齐）
- `lifecycle/test_pipeline.py`（9 项）+ `lifecycle/test_modelcard.py`（8 项）
- 汇报与样例：`B09-ORCHESTRATION-REPORT.md`（编排轮一页汇报）、`B09-MODEL-CARD-SAMPLE.md`（MV-001 渲染样例，合成数据）

### 变更

- `lifecycle/gates.py`：抽出 `median_shift_sigma()`，C2 灰度对照与 T2 影子对拍共用（单一事实来源）
- `skeleton/pipeline.py`：加 `exclude_from_train`——新一代继承上一代固定评估集并排除出训练集（§3.1 G2 跨版本可比性）
- `promote()` 落盘 metrics_json 增补 canary_gate（C1–C3 逐项结果，模型卡 §5 注记单一来源）

### 质量

- 回归 119 项全过：lifecycle 41 + skeleton 21 + canary 16 + features 13 + replay 10 + contracts 18

---

## [R2.6 实现轮 · B09 生命周期 store + 门禁] · 2026-11-24

**主题**：`lifecycle/`——把 B09 详设的状态机与门禁变成可运行代码（实现轮第一件：store + gates）。

### 新增

- `lifecycle/store.py`：ModelLifecycleStore（五态状态机 + 六条迁移白名单 T0–T5；`_check`/`_apply` 分离，守卫先于状态变更，防半迁移；T0 以 gate_report 全过才入册；manual-retire 强制 approver；rollback 无可用 peer 拒绝）
- `lifecycle/gates.py`：`run_register_gate`（G0–G5 短路求值，FAIL 后未执行项标 SKIPPED）+ `run_canary_gate`（C1 直接调 B06 `run_canary` 单一来源；C2 双版本中位偏差 σ 计 + 分布突变阻断）；`THRESHOLDS` 双口径常量集中（production-rel ≤2% vs skeleton-abs ≤0.5×噪声）
- `lifecycle/test_lifecycle.py`（14 项，含契约目录对齐）+ `lifecycle/test_gates.py`（10 项，含 gates→lifecycle T0 端到端，统一 B06 SEED=11）

### 变更

- `skeleton/store.py` emit 兼容扩展 `aggregate_id`（幂等键四元组第 3 元，生命周期事件以 model_version 聚合）
- `events.schema.json` gate_report results 项补可选 `detail`（逐项判定留痕，模型卡引用）
- 测试全过：lifecycle 24 + skeleton 21 + contracts 18 项

---

## [R2.6 进行中 · B09 详设] · 2026-11-24

**主题**：训练→门禁→注册→灰度流水线详设 + 模型卡模板（`docs/design/B09-PIPELINE-DETAIL-DESIGN.md`），实现轮待办。

- 状态机裁定：DDL `model_stage` 枚举（registered/shadow/canary/production/deprecated）为唯一权威；门禁失败=不入册 + `ModelGateFailed` 留痕，不设 REJECTED 终态
- 六条迁移白名单（守卫/事件/失败处置逐条定义）；回滚=路由回切非状态迁移，允许同 type 双 production 并行（双版本对照基础）
- G0–G5 注册门禁（短路求值）+ C1–C3 canary 门禁；C1 直接引用 B06 `run_canary`（DoD：金丝雀作为 canary 阶段门禁）
- 模型卡 8 节模板 + 字段级填充来源表（约 83% 字段骨架可自动填充，DoD 口径 ≥80% 达成）
- 契约扩展清单：6 事件 + 4 API + model_registry 迁移白名单触发器（实现轮补录）

### 契约补录（2026-11-24 当轮完成，DDL 触发器除外）

- **6 事件**（catalog 13→19 + payload $defs）：`ModelRegistered`（T0 全过入册）、`ModelGateFailed`（失败=不入册留痕）、`ModelShadowStarted`（T1，含最低观察量与回写轮数节拍）、`ModelCanaryStarted`（T2，灰度阶梯 5%→25%→100%）、`ModelDeprecated`（T4/T5 终态，reason 枚举）、`ModelRolledBack`（路由回切非状态迁移，回切目标=同 type 仍 production 的上一版本）
- **4 API**（model-lifecycle-svc 5→9 paths）：`POST /models/{v}/gate`（G0–G5 短路求值）、`POST /models/{v}/shadow/start`、`POST /models/{v}/canary/start`（非法迁移 409 = /errors/invalid-transition）、`GET /models/{v}/model-card`（auto_fill_ratio ≥0.80 口径入响应）
- §16.4 表加生命周期事件行；校验器 PASS + 18 项契约测试全过
- **待办**：`model_registry` 迁移白名单触发器（migration 002，与 `experiment_transition_guard` 同构）

---

## [R2.5 交付] · 2026-11-24

**主题**：B07 版本化特征工厂——特征定义单一事实来源，落实 ADR-04 防 training-serving skew。

### 新增

- `features/` 包：FeatureFactory（定义版本化 + 内容指纹 + 统一 transform 入口 + 审批注册表）、13 项验收测试、README
- 骨架管线训练（步骤 4/8）与服务（步骤 5/6/OOD）全部改走工厂 transform；金丝雀与骨架引用同一工厂实例（DoD：单一来源达成）
- 契约包补录 `FeatureSetRegistered` 事件（目录 + payload def + §16.4 表行）

### 变更

- feature_set 版本号由 `fs-<hash>` 改为 `FS-v1.0`，对齐 B04 契约 `common.FeatureSetVersion` pattern；注册行新增 fingerprint 字段

### 修正

- 真实 bug：含派生特征时 transform 的输入维度校验错用定义数（应为原始特征数），已修并有测试覆盖

---

## [R2.4 交付] · 2026-11-24

**主题**：B06 语义断言 + 金丝雀配方集——静默失败两层探测器落成代码，故障拦截率 100%。

### 新增

- `canary/` 包：语义断言（五类，severe/advisory 两级，违反标记降级不静默修正）、金丝雀集 CANARY-SET-v1.0（12 条合成配方，版本化 + 内容哈希指纹入审计，覆盖窄脊/边界/湿度/过载等剖面）、八类故障注入、运行器（判定失败 → 审计事件 CanaryRun + quarantine-recommended）、演示 CLI 与 16 项验收测试
- 验收口径达成：健康基线 12/12 通过；故障拦截率 **8/8 = 100%**，且强口径（每条金丝雀均拦截）成立；VALUE-DRIFT / DIRECTION-FLIP 仅偏差层可抓、结构类矛盾仅断言层可抓，两层互补验证成立

### 修正

- 骨架预测响应补 `unit` 字段（对齐 B04 契约 prediction-result required）

### 依据与取舍

- 金丝雀容差 4.5σ：3σ 被小样本树模型系统偏差误触发（首轮实测最大偏差 7.8 > 3σ=7.5），如实记录；生产按留出集残差分布定容差
- 数据红线合规：金丝雀参考值全部来自合成世界真值，绝不拿真实配方做 fixture（§17.3）

---

## [R2.3 交付] · 2026-11-24

**主题**：B04 契约定义——`packages/contracts/` 固化 §4.3 API 契约与 §16.4 事件目录，九服务接口全部定型，R2 第四件落地。

### 新增

- **九服务 OpenAPI 3.1**（`packages/contracts/openapi/`）：`prediction-svc` 全量（§4.3 canonical 样例入库、批量走 MinIO 引用、explain=deferred 降级对齐 §15.2）；`optimization-svc`（Pareto/约束检查，不直接调模型）；`active-learning-svc`（推荐批次，发布 RecommendationIssued，severe 点强制复核）；`generation-svc`（可执行性过滤一期，生成 501 三期）；`experiment-svc`（九态迁移，非法迁移 409 = `/errors/invalid-transition`）；`data-hub-svc`（导入含失败留痕红线参数、快照冻结强一致）；`model-lifecycle-svc`（训练/晋升门禁/回滚 + `/calibration/*` F1 归口 + 漂移订阅）；`knowledge-svc`（/search 一期，/qa 与 /graph 501 二期，禁 Cypher 直传 ADR-20）；`auth-audit-svc`（authz 判定 / 审计查询 / 哈希链校验 / 导出审批）。
- **共享 schema 7 件**（`schemas/`）：事件统一信封（§16.4 十字段）、统一错误（RFC 9457）、provenance 三元组必返、conformal 三档（0.50/0.70/0.90）、预测结果（OOD 条件语义）、异步任务（四队列）、通用类型（含九态枚举镜像 DDL 白名单）。
- **事件目录 12 事件 + payload**（`events/`）：§16.4 全表补录 `RecommendationIssued`（R2.2 骨架已用）；消费者名按一期服务归位；`DriftDetected` 投递语义收敛为 at-least-once-idempotent。
- **零依赖校验器**（`tools/validate.py`）：五类检查——JSON/关键字子集、OpenAPI 结构（POST 强制 Idempotency-Key + X-Tenant-Id、4xx/5xx 统一 Problem Details）、事件目录与 $defs 双向覆盖 + 最小样例自校验、红线口径（provenance/三档/OOD 两级/九态/四队列）、$ref 相对可达。
- **契约测试 18 项**（`tools/test_contracts.py`）：canonical 样例、severe 拒答拦截、advisory 标记强制、信封 const/幂等键长度、任务枚举、九服务头齐全。

### 修正

- 发现并解决契约自冲突：`intervals` 必返 vs severe 拒答须缺省——`intervals` 移出 required，改为**语义条件校验**（`semantic_check_prediction`）强制：severe 缺省 value/intervals/explanation、advisory 强制 `advisory_only=true`、返值必须三档齐全。
- 校验器自身 3 处 bug（关键字遍历误伤属性名、最小样例不满足 pattern、红线检查未解析 $ref）修复后回归通过。

### 保持不变

架构立场（§1 排序、ADR-01/02/03/12/19/20）、R2 范围；§18.1 开发视图 `packages/contracts/` 路径与本次落位一致。

---

## [ADR-20 定论] · 2026-11-24

**主题**：图谱存储选型定论——Neo4j 社区版 + 四条绕行约束（二期启用），附录 B 开放问题再收一条。

### 决策（ADR-20）

- **§10**：新增 ADR-20 表行（ADR 总数 19 → 20，§0 计数同步）。
- **§7.2**：新增「图谱存储选型口径」详述块——CE 2025.x 功能边界核实、数据边界前提（配方主数据与 `lineage_edge` 强制留在 PostgreSQL + RLS，图谱只承载公共知识与租户私有子图）、四条绕行约束（多租户标签过滤 / 服务账号独占 + auth-audit-svc 判定 / 派生可重建 + 冷备 / 日志探针监控）、三条升级触发条件（企业版 / NebulaGraph / TuGraph 备选）、GPLv3 分发合规要点。
- **交叉引用**：§2.3 数据区标注「Neo4j 社区版（ADR-20）」；§8.2 追加图谱访问判定条目（不依赖图库自身权限）；§16.3 追加 knowledge-svc 按 L2 同级降级处置；§18.4 气隙升级包清单补开源许可文本；附录 B.1 登记第二条已定决策（标题改为「2026-11-23 起陆续定论」）。
- **明确封死**：不因 CE 附带向量索引而动 Qdrant 决策（防选型漂移）；图算法规模不构成采购企业版理由。

### 保持不变

架构立场（§1 排序、ADR-01/02/03/12/19）、一期/R2 范围（知识图谱仍为二期）、版本号仍为 v2.1（新增决策不改立场，沿用 ADR-19 惯例）。

---

## [R2.2 交付] · 2026-11-24

**主题**：B03 Walking skeleton——用合成数据把 B01 表结构语义与 B02 回放纪律串成 §18.2 十步端到端链路，R2 三条 P0 全部落地。

### 新增

- **`skeleton/`（B03）**：`store.py`（内存领域存储，镜像 001_init_schema.sql 关键不变量：九态状态机白名单逐条对应 `experiment_transition_guard`、formula_version/prediction_log/dataset_snapshot/audit_event 追加写、audit_event sha256 哈希链可验可防篡改、事件信封 event_id 幂等、租户上下文强制）；`pipeline.py`（十步链路：阻断级 QC → 快照冻结+单位归一 → 特征版本稳定哈希 → 双指标训练 + conformal 三档 → 单点预测 provenance 必返 + OOD 两级（severe 拒答）→ 约束过滤不确定性采样推荐 → 九态回写 + QC + 新快照 → 门禁重训 → 闭环对比 → 审计反查）；`run_skeleton.py`（十步演示）；`test_skeleton.py`（21 项验收断言）；`README.md`（十步逐条对照表 + 简化升级路径）。
- 合成数据复用 `tests/replay` 合成材料世界（单一事实来源）；新增第二指标 `lap_shear_like` 演示多任务。

### 验收结果（§18.2 要点全命中）

- 21 项测试全过：QC 阻断（缺失/负值/活化窗口）、快照与预测日志 append-only、特征版本可复现、provenance/三档区间必返、severe OOD 拒答、非法状态迁移拒绝、QC 拒收进失败样本库（负结果留档）、跨租户拒绝、审计防篡改、事件幂等。
- 两轮闭环演示：推荐 **C-0010 → C-0006 变化**、同点位预测 83.425 → 84.107 更新、模型版本 M-001 → M-002 门禁晋升、审计哈希链完整且按推荐 id 反查时间线（RecommendationIssued → ExperimentCompleted）。

### 设计说明

- 门禁简化口径如实记录：小样本（train_n≈10）下固定评估集 RMSE 漂移 ≤ 0.5×测量噪声底噪判通过——相对阈值会把重训噪声误判为劣化；生产门禁为 §5.5 多指标 + 历史留出 + 充分样本相对比较。
- 骨架只验证「链路语义正确」，存储/模型/OOD 均为可替换替身（接口与 B01 表名对齐，生产实现按 §18.1 依赖方向替换）。

### 保持不变

架构立场（§1 排序、ADR-01/02/03/12/19）、MVP 范围、数据红线（骨架全合成数据）。

---

## [R2.1 交付] · 2026-11-24

**主题**：R2 前两件 P0——把领域模型落成可执行结构（B01），把 §17.2 回放方法落成可运行框架（B02）。

### 新增

- **`db/migrations/001_init_schema.sql` + 回滚脚本 + `db/migrations/README.md`（B01）**：24 张表（16 核心表 + `doe_design` / `quality_rule` / `prediction_log` / `constraint_set` 语义位 / `dataset_snapshot` / `calib_stat` 物化表 / `tenant` / `project`）；10 个枚举与架构口径逐一对齐（`experiment_state` 九态、`fidelity_level`、`ood_tier` 两级、`interval_level` 三档等）；`formula_version` / `audit_event` 追加写触发器；`experiment` 状态机迁移白名单触发器（QC 不过进失败样本库，不回退）；19 张业务表 RLS 全覆盖（`FORCE` + 会话变量策略）；`adhe_ml_training` 低权限训练角色（§18.3 离线链路红线）。静态机械校验通过（事务平衡、创建顺序拓扑、RLS 覆盖、枚举口径、回滚覆盖五项）；Docker 执行验证待办。
- **`tests/replay/`（B02）**：§17.2 全口径离线回放框架，零第三方依赖（纯标准库，含 bagged CART 代理模型，气隙可运行）。幸存者偏差三步诊断 → FULL/CONSERVATIVE 两档判定（附录 B.1）；T0 point-in-time 冻结；候选池约束库重建；双模式（synthetic 实验室 / historical 只揭示真实做过的标签）；三基线（随机/历史顺序/单目标贪心）+ UCB 主动学习策略；bootstrap 95% CI 报告（未达标按 max_rounds×1.5 计罚，口径显式声明）。10 项单元测试覆盖 §17.2 四个不变量，全过。
- **`tests/replay/reports/synthetic-baseline-2026-11-24.md` + `.json`**：12 种子合成回放基线报告。合成世界含协同增效窄脊（最优区在二维窄带，主动学习验证的经典场景）。结果：active_ucb 对 random 轮次下降 84.6%（CI [44.6%, 89.4%]）✅、对 historical 88.9%（CI [65.4%, 94.5%]）✅、对 greedy 未过门禁（中位持平，CI 宽）——**按真实结果记录不修饰**；判定 FULL 档，结论可对外引用。

### 设计说明

- 代理模型为浅层树集成简化替身，接口 `predict / predict_std` 与生产 LightGBM/XGBoost 适配器一致，替换不改框架。
- 合成世界目标值经校准（可行池中约 1.9% 可达标且全部位于窄脊内），保证回放对策略质量有区分度。
- historical 模式的固有偏差（选到未做过的点即消耗轮次）在 README 中显式文档化，是保守档结论只作内部参考的原因之一。

### 保持不变

架构立场（§1 排序、ADR-01/02/03/12/19）、MVP 范围、数据红线（回放框架只用合成数据，真实历史序列文件不入库）。

---

## [架构 v2.1] · 2026-11-23

**主题**：从「系统应该是什么」深化到「系统跑起来之后会怎样、坏了怎么办、如何证明有效、如何持续演进」。

### 新增

- **§15 运行期视图与关键时序**：4+1 视图覆盖矩阵；进程/队列模型与资源隔离；单配方预测 2s 超时预算分解（含「解释比推理贵 5 倍」的降级结论）；三条关键链路时序与失败点。
- **§16 故障模式、降级与数据一致性**：10 项 FMEA；**AI 静默失败专节**（语义断言 / 金丝雀配方集 / 双版本对照）；L0–L4 降级分级；事件目录、统一信封、幂等键与每日对账；强一致与最终一致边界。
- **§17 可测试性与闭环有效性验证**：六层验证矩阵；**离线回放方法**（不花实验预算证明主动学习有效，含三个陷阱）；golden set 与影子流量适用条件；故障注入清单；go/no-go 上线检查单。
- **§18 开发视图、演进治理与交付保障**：代码结构与单向依赖规则；walking skeleton 十步；多租户 RLS 落地（含离线链路易漏点）；扩展-收缩迁移与气隙三层回滚；8 条 fitness function；迭代节奏与 RACI。
- **ADR-13 ~ ADR-18**：解释可降级、静默失败防护、RLS、事件契约、离线回放、迁移回滚策略。
- 术语表新增 10 个术语；附录 B 新增 4 项开放问题（降级偏好、失败记录完整性、气隙升级窗口、RACI 岗位对应）。
- 文档集补充：`docs/architecture/ITERATION_BACKLOG.md`、`CHANGELOG.md`、`.gitattributes`。

### 变更

- §0 增加「v2.1 增量」对照表、文档集说明与分角色阅读顺序。
- §10 ADR 由 12 条增至 18 条；§0 相应标注。
- 文末明确：v2.1 不改变 v2.0 的任何架构立场。

### 修订（全文校对）

- §6.4 验收指标引用由 §13 更正为 §12；§0 增量表中 ADR-13~18 的说明与实际条目对齐；§2.3 推理延迟与 §15.2 预算表统一为 <200ms。
- §3.2 修正 `ph r` → `phr`，补充被正文引用的 `material_batch`、`recommendation` 表定义及支撑表说明；§3.1 实体名与表名对齐为 `metric_definition`。
- §3.3 「最低成膜温度」修正为更贴合热固性体系的「固化剂活化 / 分解温度窗口」；§16.4 「恰好一次」改为严谨的「有效恰好一次」；§18.3 错别字「水线」→「在线」。
- 附录 A 补充 training-serving skew 术语。

### 已定决策（追加）

- **附录 B 第 9 项定论**：历史实验序列按「失败 / 取消记录大概率不完整」设计，不再作为 B02 前置待办。依据：Raccuglia et al.（*Nature*, 2016, 533: 73–76，dark reactions 项目，失败数据可显著改善模型）与 Precision Chemistry 2026 综述（DOI: 10.1021/prechem.5c00449，失败实验极少被数字化，负结果应为一等记录）。落点：§17.2 增加幸存者偏差三步诊断与全量 / 保守两档回放；新采集数据强制全记录失败与取消（数据红线级）；附录 B 改为「开放问题与已定决策」结构，待确认问题由 11 项减至 10 项；backlog B02 前置依赖解除。
- **ADR-19（LLM 接入口径）**：私有 LLM 按任务分层接入——主力 Qwen 32B INT4（问答/解读/生成）、深度推理 DeepSeek-R1 蒸馏 32B（失败分析/推演）、轻量 Qwen 8B 级（分类/抽取/路由），配套 BGE-M3 + BGE-reranker；限定开源可商用权重（气隙与数据红线排除 API 模型），统一 OpenAI 兼容接口、版本固定可审计、可替换；不做微调定制，领域知识靠 RAG 注入；**LLM 永不生成配方数值**，性能数字只能来自预测服务。§7.2 增加接入口径表，ADR 总数 19 条。

### 修订（评审修正轮，2026-11-23 · software-adhe-review 团队评审后统一修正）

- **OOD 处置统一为两级**（P0 口径矛盾）：§4.3 设计要点、§15.3 链路 A 失败点、§16.1 FMEA F5 统一为 `ood_level ∈ {none, advisory, severe}`——advisory 返回数值 + `advisory_only` 强制标记；severe 拒答该指标不返回数值。下游设计文档与原型同步。
- §4.3 区间示例修正为数学自洽：p10/p90 + confidence 0.9 → **p5/p95 + confidence 0.9**；§4.3/§5.2 明确 conformal 输出统一为 **50%/70%/90% 三档**（90% 为验收档）。
- §11 新增「一期功能细化登记」，与 `FEATURE-DESIGN-v0.1.md`（F1–F6、F10）互相引用，消除两份一期范围口径的漂移。
- §16.4 事件目录补 `CanaryFailed`（model-lifecycle-svc 发布）；§3.2 `test_result.fidelity` 补枚举定义（experiment/F2、simulation/F1、literature/F0）。

### 保持不变

质量属性优先级（§1）、小样本选型（ADR-02）、私有化与气隙优先（ADR-03）、MVP 范围（ADR-12）、三阶段路线、量化验收指标、数据红线。

---

## [架构 v2.0] · 2026-11-23

**主题**：把 v1 分层骨架补成可用于立项与交付的设计文档。

- §1 五条硬约束与质量属性裁决顺序；§2 分层 + 控制面/数据面/实验面三条主线 + 部署视图。
- §3 领域模型、10 余张核心表、化工质量规则、单位量纲、多保真度与数据血缘。
- §4 服务边界、同步/异步切分、带 provenance 的 API 契约、事件溯源一致性。
- §5 特征可复现、小样本分层建模、conformal 校准、OOD、物理一致性、优化退化对策、模型生命周期门禁。
- §6 实验状态机、反馈延迟与样本权重、防遗忘、在线/离线双通道。
- §7 质量规则引擎、RAG 与图谱职责边界；§8 数据分级矩阵与 RBAC+ABAC；§9 部署拓扑、SLO 与容量估算。
- §10 12 条 ADR；§11–§13 三阶段路线、量化验收指标、风险登记册；§14 白皮书衔接建议。

---

## [仓库提交]

| 提交 | 说明 |
|---|---|
| `f081a6b` | `docs: initialize architecture v2.0 and project conventions` —— 初始提交，3 文件 / 815 行，建立并跟踪 `origin/main` |
| `a472307` | `docs: make repository status statement time-independent` —— 修正 README 中随状态漂移的描述 |
| `a6d0ae7` | `docs: architecture v2.1 - runtime views, resilience, validation and evolution governance` —— 架构升级 v2.1、迭代机制文档、变更记录、换行规范与 README 导航更新（5 文件 / +594 −20） |
| `27424ce` | `docs: proofread architecture set - fix cross-refs, missing table defs and terminology` —— 全文校对修正 11 处（引用/表定义/术语） |
| `423b7da` | `docs: resolve open question 9 - survivor-bias-first replay design with cited evidence` —— 附录 B 第 9 项定论（附录 B.1），B02 前置依赖解除 |
| `8c23961` | `docs: add ADR-19 layered LLM access with text-only boundary` —— §7.2 私有 LLM 接入口径表 + ADR-19 |
| `f5648e6` | `docs: add one-page LLM access handoff brief for phase-2 team` —— 二期 LLM 接入一页衔接摘要 |
| `256fea0` | `docs: add UI prototype v0.1 with design tokens and figma handoff notes` —— 界面原型 v0.1（5 界面 + 设计令牌 + Figma 交接） |
| `307cbe5` | `feat(db): add core schema DDL with RLS and state-machine triggers (B01)` —— 核心表 DDL + 回滚脚本 + 迁移约定（24 表 / 10 枚举 / RLS / 状态机触发器） |
| `a624611` | `feat(replay): add offline replay framework with baseline report and sync R2.1 docs (B02)` —— 零依赖离线回放框架 + 12 种子基线报告 + R2.1 文档同步 + B01/B02 验收结论 + 应用架构图 |
| `d20b26b` | `feat(skeleton): add walking skeleton end-to-end pipeline on synthetic data (B03)` —— Walking skeleton 端到端链路（十步全落点，21 项断言全过，两轮闭环演示） |
| `7aa94cf` | `docs: add ADR-20 Neo4j CE selection and sync R2.2 walking-skeleton docs` —— ADR-20 图谱存储选型落档（8 处）+ R2.2 文档同步 |
| `9cba6f9` | `feat(contracts): add service contracts - 9 OpenAPI specs, event catalog, validator (B04)` —— 契约包：九服务 OpenAPI 3.1 + 7 共享 schema + 12 事件目录/payload + 零依赖校验器 + 18 项契约测试（21 文件 / +2783 行） |
| `5419646` | `docs: sync R2.3 contract package into architecture set (B04)` —— §4.3/§16.4 契约包指针 + 迭代记录 R2.3 + CHANGELOG/README 同步 |
| `e74ca5d` | `feat(canary): add semantic assertions and canary formula set with fault injection (B06)` —— 语义断言五类 + 金丝雀集 v1.0（12 条合成配方）+ 八类故障注入拦截 100% + 运行器/审计事件 + 16 项测试（9 文件 / +707 行） |
| `0ac4d47` | `docs: sync R2.4 canary acceptance into iteration docs (B06)` —— 迭代记录 R2.4 + CHANGELOG/README 同步 |
| `318d31e` | `feat(features): add versioned feature factory with approval registry (B07)` —— 特征单一来源：版本化定义 + 内容指纹 + 统一 transform + 审批注册表（7 文件 / +649 行，含契约 FeatureSetRegistered 补录与骨架全链路接线） |
| `c1e1771` | `docs: sync R2.5 feature factory into architecture set (B07)` —— §16.4 加行 + 迭代记录 R2.5 + CHANGELOG/README 同步 |
| `72da194` | `docs: add B09 pipeline detail design (train-gate-register-canary + model card)` —— 训练→门禁→注册→灰度流水线详设 + 模型卡模板（状态机以 DDL 为权威、六条迁移白名单、G0–G5 + C1–C3 门禁、模型卡 8 节） |
| `e573453` | `feat(contracts): add lifecycle events and gate/shadow/canary/model-card APIs (B09)` —— 契约补录：6 生命周期事件（目录 13→19 + payload $defs）+ 4 API（model-lifecycle-svc 5→9 paths）+ gate_report 共享结构 + §16.4 表行 |
| `62e8cc6` | `feat(lifecycle): add model lifecycle store and gate engine (B09)` —— 生命周期实现轮第一件：五态状态机 + 六条迁移白名单（_check/_apply 分离防半迁移）+ G0–G5/C1–C3 门禁引擎 + 双口径阈值常量（lifecycle 24 + skeleton 21 + contracts 18 项测试全过） |
| `817da34` | `docs: sync R2.6 lifecycle store and gates into iteration docs (B09)` —— 迭代记录 R2.6b + B09 状态更新 + CHANGELOG 同步 |
| `e8743da` | `feat(lifecycle): add orchestration pipeline and model card renderer (B09)` —— 实现轮第二、三件：LifecyclePipeline 全链路编排（观察量/灰度阶梯/守卫前置）+ 模型卡渲染（auto_fill_ratio 0.8947，DoD 1 达成）+ median_shift_sigma 共用 + exclude_from_train 固定评估集（回归 119 项全过） |

---

*架构版本升级不改变已发布提交的历史；如需对比，以 Git 历史为准。元数据与校对类收尾提交不逐条列入本表，以 Git 历史为准。*
