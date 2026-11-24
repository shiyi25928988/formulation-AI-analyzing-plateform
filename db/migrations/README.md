# 数据库迁移

## 约定

1. **命名**：`NNN_<名称>.sql` + 同名 `.down.sql` 回滚脚本，编号递增、不可复用。
2. **风格**：扩展-收缩（架构 §18.4）——迁移只做扩展（加表/加列/加默认值）；破坏性变更（删列/改类型）拆两步，先扩展双写、数据回填后再收缩，每步各有回滚。
3. **事务**：每个迁移单事务执行，失败整体回滚；PostgreSQL DDL 可事务化。
4. **RLS**：所有业务表带 `tenant_id` 并启用 `FORCE ROW LEVEL SECURITY`；策略由会话变量 `app.tenant_id` 驱动。连接池借出必须 `SET LOCAL app.tenant_id = ...`，归还时 RESET（架构 §18.3）。
5. **离线链路红线**：训练任务使用 `adhe_ml_training` 低权限角色（只读 + RLS），禁止高权限直读全库（§18.3）。
6. **审计**：`audit_event` 只追加、按月分区；运维需提前建下月分区，`audit_event_default` 兜底。
7. **数据红线**：迁移文件与 seed 数据不得包含真实配方、实测数据、密钥。

## 验证（DoD：可无错执行并回滚）

任选其一：

```bash
# 方式一：Docker 一次性验证（推荐，CI 可直接内嵌）
docker run --rm -v "$PWD/db/migrations:/m" postgres:16-alpine \
  sh -c 'until pg_isready -U postgres >/dev/null 2>&1; do sleep 0.3; done;
         psql -U postgres -v ON_ERROR_STOP=1 -f /m/001_init_schema.sql &&
         psql -U postgres -v ON_ERROR_STOP=1 -f /m/001_init_schema.down.sql &&
         psql -U postgres -v ON_ERROR_STOP=1 -f /m/001_init_schema.sql &&
         echo "MIGRATION OK (up -> down -> up)"'

# 方式二：已有 PG 实例
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 001_init_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 001_init_schema.down.sql
```

**状态**：2026-11-24 通过静态机械校验（事务平衡、创建顺序拓扑检查、RLS 全覆盖、枚举口径比对）；Docker 执行验证待有 Docker/PG 环境时补跑，见 ITERATION_BACKLOG B01 备注。

## 表清单（001）

核心：`tenant` `project` `material` `material_batch` `formula` `formula_version`(append-only) `formula_component` `process_condition` `metric_definition` `doe_design` `experiment`(状态机触发器) `test_result` `quality_rule` `prediction_log` `feature_set` `dataset_snapshot` `model_registry` `recommendation` `calib_stat`(F1 物化) `lineage_edge` `audit_event`(分区 append-only)。

枚举口径与架构对齐：`experiment_state`（§6.1 九态）、`fidelity_level`（experiment/simulation/literature，§3.2/§3.4）、`ood_tier`（none/advisory/severe，评审定版）、`interval_level` 仅 0.50/0.70/0.90（§5.2 三档）。
