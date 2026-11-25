# 交接 TODO（2026-11-25 · 切换工作电脑）

> 本文件为跨机器交接快照。新环境 clone 本仓库后，以本文档 + `ITERATION_BACKLOG.md` §5（迭代记录）+ `R2-ACCEPTANCE-SUMMARY.md` §6（保留项）为权威进度源，无需其他会话上下文。
> **当前 HEAD**：`676ffe3`（R2.8 收官）。每完成一项，请更新本文档勾选状态并按惯例提交。

## 环境基线（新电脑需重建）

- Python ≥ 3.13，**零第三方依赖**（全部测试只用标准库 `unittest`）
- Git（`.gitattributes` 已统一 LF，Windows 下无需额外配置）
- 可选：Docker（保留项 2 用）；无 Docker 时的备选见保留项 2

## 测试基线（切换前最后一次全量回归，120 项全过）

| 套件 | 数量 | 运行方式（注意目录差异） |
|---|---|---|
| lifecycle | 42 | 仓库根：`python -m unittest discover -s lifecycle -q` |
| skeleton | 21 | 仓库根：`python -m unittest discover -s skeleton -q` |
| canary | 16 | **必须在 `canary/` 目录内**：`python -m unittest test_canary -q` |
| features | 13 | 仓库根：`python -m unittest discover -s features -q` |
| replay | 10 | 仓库根：`python -m unittest discover -s tests/replay -q`（若 discover 失败则进目录直跑 `test_replay`） |
| contracts | 18 | **必须在 `packages/contracts/tools/` 目录内**：`python -m unittest test_contracts -q`；校验器：`python tools/validate.py`（同目录） |

## TODO

### 1. [ ] 保留项 2：Docker 执行验证 001/002 迁移（接续点，已开工）

原电脑无 Docker，已确认网络可达；**备选方案**（新电脑也无 Docker 时）：`theseus-rs/postgresql-binaries` GitHub release 的 `postgresql-<ver>-x86_64-pc-windows-msvc.zip`，免安装解压即用（`initdb` + `pg_ctl`）。

步骤（建议 `postgres:16`，与 DDL 目标口径一致）：

1. `docker run -d --name adheform-pg -e POSTGRES_PASSWORD=devonly -p 54329:5432 postgres:16`
2. **001**：`psql -f db/migrations/001_init_schema.sql` → `001_init_schema.down.sql` → 再 up。验证：表/触发器/RLS 建立、干净回滚、幂等重建
3. **002**：`002_model_stage_guard.sql` + down，同样三段
4. **行为验证**（触发器真实生效才算过，静态校验不能替代）：
   - `model_registry` INSERT 非法 `model_stage` 被拒
   - INSERT 不从 `registered` 起步被拒（002 触发器，门禁失败=不入册语义）
   - `experiment_transition_guard` 迁移白名单行为（001）
   - RLS 策略生效
5. 结果写回 `db/migrations/README.md` 的「Docker 执行验证」状态段
6. 按惯例 docs 提交 + 推送；勾选本项

### 2. [ ] 保留项 3：CI 接入（GitHub Actions）

- Workflow 覆盖六套件 + 契约校验器 `tools/validate.py`
- **运行目录差异照抄上表**（canary、contracts 必须在各自目录内跑）
- Python 3.13、零依赖，workflow 极简
- 完成后 `R2-ACCEPTANCE-SUMMARY.md` §6 保留项 3 标记闭环

### 3. [ ] 保留项 4：骨架 step8 改造，接入 lifecycle 正式门禁

- `skeleton/pipeline.py` 步骤 8 现为简化门禁（双口径阈值 skeleton-abs ≤0.5×噪声），与 `lifecycle/gates.py` 的 G0–G5 正式门禁并存
- 改接 `run_register_gate`（或至少对齐 production-rel 口径），消除两套门禁并存歧义
- 注意保持 R2.8 已固化的 `exclude_from_train` 跨代逻辑；回归须 120 项全过

### 4. [ ] 保留项 6：model_registry 双 schema 暂存合并（依赖上一项）

- `skeleton/store.py` 骨架行（简化字段）与 `lifecycle/store.py` 生命周期行（DDL 全字段）并存
- 合并为单一 schema：骨架管线统一走 lifecycle store 建行
- 与保留项 4 联动，宜同批处理

### 5. [ ] 保留项 5：替身口径清单替换为生产实现（长期项，一期不实现）

五项替身的生产替换路径（仅在验收汇总 §6 维护清单）：①artifact 校验和（现=指标指纹 sha256 → MinIO 字节流哈希）；②影子观察量（重复探测计 prediction_log → 真实请求节拍）；③灰度分流（阶梯替身 → 真实流量分片）；④C2/T2 中位偏差（合成对拍 → 真实双版本在线对拍）；⑤OOD 两级 severe 拒答替身。

### 6. [ ] R3 启动：B05 数据导入 + QC 阻断级规则引擎（下一轮主矛盾）

- 一期真正瓶颈，决定可用样本量；备选 B08-CI（若 CI 提前完成）、B10
- 迭代规则：立场稳定（§1 排序 / ADR-01/02/03/12 变更需评审）、先证据后实现、文档与代码同批提交、每轮只解一个主矛盾、数据红线不可协商
- **R3 全程不做**：真实数据接入、多租户 UI、LIMS 对接、知识图谱、生成模型、GNN、私有 LLM

## 提交与协作惯例（不可协商）

- 三条一组：代码一条 + 文档同步一条 + 提交号回填一条；一组一推
- 推送前向用户展示改动总览（用户惯例）
- 数据红线：真实配方 / 实测数据 / 模型权重 / 密钥一律不入库（`.gitignore` 与 README「数据红线」）
- 回归不过不算完成；文档不同步不算完成
