# 金丝雀配方集与语义断言（B06 / §16.2 / ADR-14）

防静默失败的两层探测器：**语义断言**（响应返回前）+ **金丝雀配方集**（性能已知的固定配方持续探测）。零第三方依赖，全部合成数据（数据红线：绝不拿真实配方做 fixture）。

## 运行

```bash
python canary/run_canary.py     # 演示：健康基线 → 8 类故障注入 → 拦截率
python canary/test_canary.py    # 16 项验收测试（含 100% 拦截断言）
```

## 语义断言（assertions.py）

| 断言 | 代码 | 级别 | 对应静默失败来源（§16.2） |
|---|---|---|---|
| 量纲范围 | UNIT-RANGE / UNIT-UNKNOWN | severe | 单位/量纲换算错误 |
| 物理可行边界 | PHYS-FEASIBILITY | severe | 约束库过期 |
| 配比守恒 | RATIO-NEGATIVE / RATIO-EOQ | severe | 输入管线损坏 |
| 区间单调与自洽 | INTERVAL-INVALID / INTERVAL-NON-MONOTONIC / INTERVAL-EXCLUDES-VALUE | severe | 校准区间失真 |
| OOD 一致性 | OOD-BYPASS / OOD-FLAG-MISSING | severe | §4.3 红线被绕过 |
| 方向登记 | DIRECTION-UNKNOWN | advisory | 指标定义缺失 |

原则：**断言失败不静默修正，而是标记并降级**——severe 由调用方拒答/摘除，advisory 进人工复核。

## 金丝雀集（canary.py）

- `CANARY-SET-v1.0`：12 条固定配方（内容哈希指纹写入审计，变更需审批）
- 覆盖：窄脊 / 性能高低端 / 温度边界 / 湿度劣化 / 填料过载 / 分散剂过量 / 时间饱和 / 窄脊外
- 参考值 = 合成世界真值（无噪声）；容差 = 4.5σ（3σ 被小样本树模型系统偏差误触发，实测最大 7.8 > 7.5；生产按留出集残差分布定容差）
- 金丝雀点位作为已知历史实验入库 → 预测必在域内；**拒答（severe）本身判失败**
- 判定失败 → 审计事件 `CanaryRun`（action=quarantine-recommended）→ 建议摘除该模型版本

## 故障注入（faults.py）——验收证据

| 故障 | 层 | 探测层 | 说明 |
|---|---|---|---|
| UNIT-SCALE（×10） | 输出 | 语义断言 + 偏差 | 经典 training-serving skew |
| INTERVAL-INVERSION | 输出 | 语义断言 | p5/p95 倒挂 |
| INTERVAL-NON-MONOTONIC | 输出 | 语义断言 | 档位宽度非单调 |
| OOD-BYPASS | 输出 | 语义断言 | severe 拒答被绕过 |
| VALUE-DRIFT（+25≈10σ） | 输出 | **偏差层** | 量纲合法、区间自洽，只有金丝雀能抓 |
| DIRECTION-FLIP | 输出 | **偏差层** | 符号错误，量纲范围内 |
| RATIO-NEGATIVE | 输入 | 语义断言 | 守恒违反 |
| FEASIBILITY-VIOLATION | 输入 | 语义断言 | 约束库越窗 |

两层互补缺一不可：语义断言抓「结构/物理矛盾」，金丝雀偏差抓「数值合理但结论错误」。

## 集成路径（生产）

- `run_assertions` 挂 prediction-svc 响应链路（P95 预算 100ms 档，§5.4）
- `run_canary` 挂 model-lifecycle-svc：模型加载 / 发布 / 每日定时三触发（§16.2）
- 结果写审计，与 §8.4 溯源链合并为「该模型当前是否可信」证据链
