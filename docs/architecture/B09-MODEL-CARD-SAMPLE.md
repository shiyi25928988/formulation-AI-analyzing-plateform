# 模型卡：lgbm-reg · MV-001

## 1. 标识与三元组
- 模型版本：MV-001（artifact：minio://artifacts/MV-001.pkl，校验和 sha256:8f5907141fabb2899767714c9fbf9e02358a8ac166be2a26891abefb2306c105）
- 特征集：FS-v1.0（指纹 sha256:0397b11d25573ec5，B07 注册）
- 数据快照：DATA-c8c6b165（label=canary-baseline，n_rows=41，冻结时间 2026-11-25T01:06:59+00:00）
- 训练环境：seed=11，随机性声明：同代码+同快照+同种子重训 RMSE 相对差异 6e-05（G1 实测）

## 2. 训练数据声明
- 数据来源保真度分布：experiment 100%（合成世界，单一保真度；生产按快照元数据统计）
- 失败/取消记录：n/a（骨架首代无回写实验）（幸存者偏差诊断档位：T0 冻结快照 + 全量回放档位（B02 三步诊断口径））
- 数据红线声明：真实配方 / 实测数据 / 模型权重不入仓库 （.gitignore 数据红线，README 口径）

## 3. 评估与门禁（G0–G5 逐项）
| 门禁 | 阈值 | 实测 | 判定 |
|---|---|---|---|
| G0 | 三元组互相引用且指纹一致（B07） | 三元组互相引用且指纹一致 | PASS |
| G1 | 重训指标差异 < 1% | 重训相对差异 0.00006 < 1% | PASS |
| G2 | 劣化 ≤ 2%（production-rel）或漂移 ≤ 0.5×噪声（skeleton-abs，双口径） | 首版本，无基线（只记录不裁决） | PASS |
| G3 | 0.90 区间实测覆盖 ≥ 88% | 覆盖率 1.000 ≥ 0.88 | PASS |
| G4 | OOD 检出召回 ≥ 85% | 召回 1.000 ≥ 0.85（n=20） | PASS |
| G5 | severe 违规率 ≤ 1%（B06 run_assertions） | 违规率 0.0000 ≤ 1% | PASS |

## 4. 指标明细
| 指标 | RMSE | 0.90 覆盖率 | severe 违规率 | conformal 档位 |
|---|---|---|---|---|
| tg_like | 6.016 | 1.0 | 0.0 | 0.50/0.70/0.90 |
| lap_shear_like | 1.724 | 1.0 | 0.0 | 0.50/0.70/0.90 |

## 5. canary 阶段证据（B06）
- 金丝雀集：CANARY-SET-v1.0（指纹 sha256:d1a2b3f2766f12d6）
- 判定：12/12 全过
- 双版本对照：无双版本（首版本），跳过对照

## 6. 已知局限与不适用范围
- greedy 策略基线未过回放门禁（B02 如实记录，未修饰）；双版本对照/影子对拍为中位偏差替身，生产替换 KS 检验（B09 §3.3）；artifact 校验和为指标指纹替身，生产为 MinIO 字节流哈希；金丝雀容差 4.5σ 与模型族绑定（SEED=11 标定），换族须重新标定
- （骨架自动生成候选，人工 curated 后定稿——§5.2 半自动口径）

## 7. 生命周期履历
| 时间 | 迁移 | 操作者/触发源 | 事件 ID |
|---|---|---|---|
| 2026-11-25T01:06:59+00:00 | ModelRegistered | model-lifecycle-svc | EVT-a4d66809 |
| 2026-11-25T01:06:59+00:00 | ModelShadowStarted | model-lifecycle-svc | EVT-d3a2bae4 |
| 2026-11-25T01:06:59+00:00 | ModelCanaryStarted | model-lifecycle-svc | EVT-e98e3ba9 |
| 2026-11-25T01:06:59+00:00 | ModelPromoted | model-lifecycle-svc | EVT-53df73eb |

## 8. 审批与责任
- 注册审批：自动门禁（G0–G5 全过，无人工审批人）；晋级审批：自动晋级（C1–C3 全过）；回滚记录：无回滚记录
- 联系岗位：模型负责人（RACI 见附录 B.2 待定项）


---

> 样例说明：由 lifecycle/modelcard.render_model_card 从骨架合成世界自动渲染（gen1 全链路晋级后的 MV-001），auto_fill_ratio=0.8947（DoD 1 ≥0.80 达成）。非真实数据。
