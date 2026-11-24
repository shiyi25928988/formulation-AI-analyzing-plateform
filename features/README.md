# 版本化特征工厂（B07 / §5.1 / ADR-04）

特征定义的**单一事实来源**，防 §16.2 静默失败之首「training-serving skew」：训练与服务共用同一特征实现，定义版本化、变更走审批。

## 运行

```bash
python features/test_features.py    # 13 项验收测试
```

## 口径

| 项 | 实现 |
|---|---|
| 版本号 | `FS-vX.Y`（对齐 B04 契约 `common.FeatureSetVersion` pattern；B03 时代的 `fs-<hash>` 已废止） |
| 指纹 | 定义规范化 JSON 的 sha256（内容寻址：任何定义变更即变，写入 feature_set 注册行 + `FeatureSetRegistered` 审计事件） |
| 变换 | `FeatureFactory.transform()` 是训练（步骤 4/8）与服务（步骤 5/6/OOD）的唯一入口；FS-v1.0 为恒等（as-recorded，与 B01/B03 口径一致） |
| 派生特征 | 以命名函数注册表（`DERIVED_FNS`）承载，定义本身可序列化、可入审计 |
| 审批 | `_APPROVED` 注册表之外（draft）不可用于训练与服务（`FeatureNotApprovedError`）——特征集变更须先审批注册（§18.1） |

## 验收测试覆盖（13 项）

- 指纹稳定性（同定义两次构建同指纹）与变更敏感性（新增派生特征即变）
- 版本号契约合规、恒等变换正确、派生变换正确、维度不符即时暴露
- draft 不可用（审批门）
- **单一来源**：骨架管线 `feature_factory` 即工厂单例、provenance 携带工厂版本、注册行指纹一致、注册事件入审计、跨管线可复现
- **变更走审批**：draft → approve 后新版本可用、旧版本指纹不受影响

## 集成路径（生产）

- 特征定义从本模块映射为 `feature_set` 表 + 定义仓库（git 版本化），审批映射为评审流水线
- `transform()` 映射为 prediction-svc 与训练作业共享的特征服务库（同进程实现，杜绝双实现漂移）
- 指纹入 provenance 三元组与 DataLineage，F2 版本对比可按特征集维度切片
