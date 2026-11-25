# B09 实现轮汇报：编排 pipeline（第二件）—— 一页纸

> 轮次：R2.6b 续 · 日期：2026-11-25 · 状态：代码完成、全量回归通过，待提交
> 依据：`docs/design/B09-PIPELINE-DETAIL-DESIGN.md` §1/§4/§7 · 上游：B03 骨架 / B06 金丝雀 / B07 特征工厂 / lifecycle store+gates（第一件）

---

## 1. 一句话结论

「训练 → 门禁 → 注册 → 影子 → 灰度 → 晋级 → 回滚」已从文档变成**一条可执行流水线**：每一步都有白名单迁移、门禁证据与审计留痕，任一守卫不满足即停在当前阶段——观察量不足不推进、分布突变不推进、金丝雀失败不晋级，宁可慢，不可带病晋级。

## 2. 流水线全景（实现形态）

```
SkeletonPipeline 训练/预测（B03+B07）
  → run_register_gate   G0–G5 短路求值（§3.1）
  → register (T0)       全过才入册；失败 = 不入册 + ModelGateFailed 留痕，旧版本继续服务
  → start_shadow (T1)   守卫：artifact 校验和复算 + 特征指纹与 G0 记录一致
  → shadow_serve        只记录不返回；观察量 < 30 → start_canary 拒绝（§4）
  → start_canary (T2)   守卫：影子对拍中位偏差 < 3σ（与 C2 同源），灰度阶梯 5%→25%→100%
  → run_canary_gate     C1 = B06 run_canary 单一来源；C2 双版本对照；C3 在线口径
  → promote (T3)        全过晋级 production；失败停 canary（B06 已写 quarantine 留痕）
  → rollback            路由回切非状态迁移：同 type 双 production 并行（上限 2）
```

## 3. 本轮关键设计点（4 条）

| # | 设计点 | 理由 / 出处 |
|---|---|---|
| 1 | 门禁失败统一走状态机 `register`，编排层不短路绕过 | `ModelGateFailed` 留痕 + 短路 SKIPPED 可反查是 T0 失败处置的一部分（§2.3 / §5.5），编排层无权省略 |
| 2 | C2 与 T2 共用同一 `median_shift_sigma()`（从 gates 抽出） | 影子对拍与灰度对照是同一语义（§16.2 分布突变即阻断），不造第二套实现 |
| 3 | 新一代继承上一代固定评估集 + `exclude_from_train` 排除出训练集 | §3.1 G2 的前提是「固定评估集」跨版本可比；评估行混入训练集会虚增覆盖率 |
| 4 | 晋级后 `metrics_json` 合并落 rmse / caliber / canary | 下一代 G2 基线与模型卡 §5 canary 证据的单一来源，update 为整字段替换故须合并不覆盖 |

## 4. 交付物

| 文件 | 规模 | 内容 |
|---|---|---|
| `lifecycle/pipeline.py` | 新增 247 行 | `LifecyclePipeline` 编排器（T0–T3 + 影子/灰度替身 + 运维面 route/rollback + 跨代基线辅助） |
| `lifecycle/test_pipeline.py` | 新增 233 行 | 9 项验收测试（见 §5） |
| `lifecycle/gates.py` | +23 −12 | 抽出 `median_shift_sigma()`，C2 改为调用（行为不变） |
| `skeleton/pipeline.py` | +6 −1 | `exclude_from_train` 属性 + `_training_rows()` 过滤（向后兼容，默认空集） |

## 5. 验收测试（test_pipeline.py，9 项全过）

1. **全链路晋级**：注册→影子→灰度→production，`ModelRegistered/ShadowStarted/CanaryStarted/Promoted` 四事件齐备 + 审计哈希链完整 + metrics_json 落盘
2. **门禁失败不影响路由**：G0 指纹篡改 → 不入册 + `ModelGateFailed`（G5=SKIPPED 可反查），未入任何阶段
3. **观察量不足不得推进**：影子 30 条红线，不足则 `start_canary` 拒绝、状态保持 shadow
4. **T1 校验和篡改阻断**：注册行 artifact_checksum 被改 → 拒绝进影子
5. **T2 分布突变阻断**：影子期服务面漂移 +12.5（5σ 级）→ 中位偏差超阈 → 停在 shadow（漂移注入发生在注册后，与门禁评估走模型本体相区分）
6. **灰度阶梯强制**：50% 不在 {5,25,100} → 拒绝
7. **C1 失败停在 canary**：金丝雀偏差漂移 → promote 返回 failed_rule=C1，状态保持，`CanaryRun` 审计留痕（B06 quarantine 口径）
8. **两代并行 + 回滚回切**：MV-001/MV-002 双 production 并行 → 漂移回滚 → 路由回切 MV-001，MV-002 转 deprecated，`ModelRolledBack.rolled_back_to` 正确
9. **灰度替身**：比例判定函数逐请求分流（候选/上一版本），回切路径可预测

## 6. 回归基线（全量，本轮后）

| 套件 | 数量 | 结果 | 备注 |
|---|---|---|---|
| lifecycle | 33 | ✅ | 14 store + 10 gates + 9 pipeline |
| skeleton | 21 | ✅ | `_training_rows` 改动向后兼容 |
| canary | 16 | ✅ | B06 语义断言 + 金丝雀集 |
| features | 13 | ✅ | B07 特征工厂 |
| tests/replay | 10 | ✅ | B02 回放框架 |
| packages/contracts | 18 | ✅ | B04 契约（校验器 PASS） |

## 7. 骨架替身口径（如实记录，生产替换、接口不变）

- **artifact 校验和** = 模型指标指纹 sha256（生产：MinIO 字节流哈希比对）
- **影子观察量** = prediction_log 该版本预测条数（生产：影子路由器按最低观察量/回写轮数节拍，§17.3）
- **灰度流量** = 比例参数 + 判定函数（生产：网关按请求哈希分流）
- **C2/T2 分布检验** = 中位偏差 σ 计（生产：KS 检验等分布检验）

## 8. 遗留与下一步

1. `lifecycle/modelcard.py`——模型卡渲染（§5 模板，8 节、6 组全自动 ≈83% 字段，DoD 1）
2. `model_registry` 迁移白名单触发器（migration 002，与 `experiment_transition_guard` 同构）
3. 本轮代码待按惯例分组提交推送（代码 / 文档 / 提交号回填）

**DoD 对照**：状态机白名单镜像 DDL（第一件已达成）；金丝雀作为 canary 门禁被引用（C1，本轮端到端验证）；模型卡自动填充 ≥80%（待 modelcard.py）。
