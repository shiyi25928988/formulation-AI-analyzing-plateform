# Walking Skeleton（B03 / R2.2）

§18.2 十步端到端链路的可运行最小实现。目标：先把三条主线各打通一次，
暴露集成问题而非算法问题——验收要点是**下一轮推荐确实发生变化**且**审计可反查**。

## 快速开始

```bash
python skeleton/run_skeleton.py      # 十步演示（退出码 0 = 闭环打通）
python skeleton/test_skeleton.py -v  # 21 项十步验收断言
```

零第三方依赖；合成数据全部复用 `tests/replay` 的合成材料世界（单一事实来源，
协同窄脊 + 实验噪声），数据红线合规。

## 与 §18.2 十步的逐条对照

| 步骤 | 架构要求 | 骨架落点 | 依据条款 |
|---|---|---|---|
| 1 | 脱敏配方 CSV → 导入 + 阻断级 QC | `step1_import` + `qc_check_row`（缺失/负值/活化窗口外即拒收，阻断明细进审计） | §7.1 |
| 2 | 版本化快照 + 单位归一 | `step2_snapshot`（快照 append-only，归一声明入元数据） | §3.5 |
| 3 | 特征工厂固定 feature_set_version | `step3_feature_set`（清单+归一参数的稳定哈希，可复现） | §5.1 / ADR-04 |
| 4 | 双指标训练 + conformal 校准 | `step4_train`（tg_like + lap_shear_like；split-conformal 三档 0.50/0.70/0.90） | §5.2 |
| 5 | 单点预测：数值+区间+溯源+OOD | `step5_predict`（provenance 三元组必返；advisory 标记不直接采纳；severe 拒答无数值；预测落 append-only 日志） | §4.3 |
| 6 | 约束过滤下的批次推荐 | `step6_recommend`（可行性+原材料可得+未做过过滤；不确定性采样；推荐带 provenance） | §5.4 |
| 7 | 录入实验结果 → QC → 新快照 | `step7_writeback`（九态状态机逐态迁移；QC 不过→REJECTED 进失败样本库，原因留档不回退） | §6.1 |
| 8 | 触发重训，门禁通过 → 新版本 | `step8_retrain`（固定评估集可比；门禁不过→`ModelGateFailed` 拒绝晋升） | §5.5 |
| 9 | 下一轮推荐与上一轮不同 | `step9_compare`（同点位预测更新 + top1 候选变化 + 模型版本递进） | B03 验收 |
| 10 | 全过程审计可反查 | `step10_audit`（sha256 哈希链逐条可验、可发现篡改；按推荐 id 反查时间线） | §16 / §18.2 |

## 镜像的不变量（store.py ↔ 001_init_schema.sql）

1. **追加写**：`formula_version` / `audit_event`（DDL 触发器）+ `prediction_log` / `dataset_snapshot`（§3.5 语义）——UPDATE/DELETE 直接拒绝
2. **状态机白名单**：`app.experiment_transition_guard` 逐条镜像；起始必须 RECOMMENDED
3. **审计哈希链**：逐条 sha256 链接，篡改必被发现（静默失败第三层防护的替身）
4. **事件信封**：至少一次 + 幂等（`event_id` 去重），事件名与 §16.4 目录一致
5. **租户上下文**：跨租户读写直接拒绝（RLS 的内存替身）

## 已知简化（升级路径，接口不变）

| 简化 | 生产替代 |
|---|---|
| 内存存储（仅验语义） | `db/migrations` PostgreSQL + RLS（B01 DDL 为准） |
| bagged CART 代理 | LightGBM/XGBoost 多任务 + 生产 conformal（B07 特征工厂） |
| OOD 包络替身 | §5.3 生产 OOD 检测器 |
| 门禁 = 固定评估集 RMSE 漂移 ≤ 0.5×噪声底噪 | §5.5 多指标 + 历史留出 + 充分样本相对比较 |
| 单条实验回写 | DoE 批次 + LIMS/ELN 对接（experiment-svc） |

> 门禁简化说明：小样本（train_n≈10）下单样本重训的评估噪声大于模型收益，
> 相对阈值（×1.1）会把重训噪声误判为劣化；骨架采用「漂移 ≤ 0.5×测量噪声底噪」
> 的绝对容忍带，并如实记录 RMSE 前后值。
