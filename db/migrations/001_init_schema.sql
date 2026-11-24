-- ============================================================================
-- AdheFormAI 核心表初始化 Schema（B01 / R2）
-- 架构依据：docs/ARCHITECTURE.md v2.1 —— §3.2 核心表、§3.3 质量规则、
--           §6.1 实验状态机、§8.2 禁止跨密级训练、§18.3 RLS 落地
-- 版本：001 | 2026-11-24
--
-- 执行约定：
--   * 单事务执行（本文件自带 BEGIN/COMMIT），失败即整体回滚；
--   * 回滚脚本见 001_init_schema.down.sql；
--   * 迁移风格为「扩展-收缩」（§18.4）：本文件只做扩展（CREATE），
--     破坏性变更（DROP/改列）必须拆分到独立迁移并配套回滚；
--   * 密钥/真实数据不得写入本文件（数据红线）。
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ----------------------------------------------------------------------------
-- 0. 角色与辅助函数（§18.3：RLS 由会话变量驱动；离线训练用专用低权限角色）
-- ----------------------------------------------------------------------------

-- 应用读写 / 只读 / 训练专用三组组角色（登录角色由交付时按环境创建并归入组）
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adhe_app_rw') THEN
    CREATE ROLE adhe_app_rw NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adhe_app_ro') THEN
    CREATE ROLE adhe_app_ro NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adhe_ml_training') THEN
    CREATE ROLE adhe_ml_training NOLOGIN;   -- 专用低权限：只读，且必须经 RLS 上下文
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS app;

-- 读取当前租户上下文；未设置时返回 NULL（RLS 策略据此拒绝一切行）
-- 连接池每次借出必须 SET LOCAL app.tenant_id（§18.3），借还时 RESET。
CREATE OR REPLACE FUNCTION app.current_tenant() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE v text;
BEGIN
  BEGIN
    v := current_setting('app.tenant_id', true);
  EXCEPTION WHEN OTHERS THEN
    v := NULL;
  END;
  RETURN NULLIF(v, '')::uuid;
END
$$;

-- ----------------------------------------------------------------------------
-- 1. 枚举类型（口径与架构一致，禁止应用层另设）
-- ----------------------------------------------------------------------------

CREATE TYPE experiment_state AS ENUM (
  'RECOMMENDED',   -- 推荐（§6.1 状态机起点）
  'REVIEWED',      -- 工程师确认/调整
  'SCHEDULED',
  'IN_PROGRESS',
  'COMPLETED',
  'QC_PENDING',
  'ACCEPTED',      -- 进入训练集，触发重训评估
  'REJECTED',      -- 数据不合格，记录原因，进入失败样本库
  'CANCELLED'      -- 记录取消原因，用于分析推荐可执行性
);

CREATE TYPE fidelity_level AS ENUM (
  'literature',    -- F0 低保真：文献/专利抽取，仅作先验/排序
  'simulation',    -- F1 中保真：仿真/经验公式，辅助任务或偏差校正
  'experiment'     -- F2 高保真：实测，唯一可作训练标签的默认档
);

CREATE TYPE qc_flag_level AS ENUM ('pass', 'warn', 'fail');  -- §3.3：告警级入库打 warn

CREATE TYPE metric_direction AS ENUM ('higher_better', 'lower_better', 'target');

CREATE TYPE approval_status AS ENUM ('draft', 'pending_approval', 'approved', 'rejected');

CREATE TYPE recommendation_state AS ENUM ('proposed', 'accepted', 'rejected', 'cancelled');

CREATE TYPE model_stage AS ENUM ('registered', 'shadow', 'canary', 'production', 'deprecated');

CREATE TYPE confidentiality_level AS ENUM ('L0_public', 'L1_internal', 'L2_confidential', 'L3_core');

CREATE TYPE ood_tier AS ENUM ('none', 'advisory', 'severe');  -- 评审定版：两级 OOD 处置

CREATE TYPE quality_rule_level AS ENUM ('blocking', 'warning', 'hint');  -- §3.3 三级

-- ----------------------------------------------------------------------------
-- 2. 租户与项目（RLS 隔离边界）
-- ----------------------------------------------------------------------------

CREATE TABLE tenant (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code          text NOT NULL UNIQUE,
  name          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE project (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant(id),
  code          text NOT NULL,
  name          text NOT NULL,
  material_system text,                       -- 材料体系（如环氧/硅胶），演进按 §18.4 收缩-扩展
  UNIQUE (tenant_id, code)
);

-- ----------------------------------------------------------------------------
-- 3. 原材料与批次（§3.2；批次物性 point-in-time，禁止回填）
-- ----------------------------------------------------------------------------

CREATE TABLE material (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant(id),
  code          text NOT NULL,
  name          text NOT NULL,
  cas_no        text,
  smiles        text,                          -- 分子描述符来源（RDKit/Mordred）
  supplier      text,
  func_group    text,
  functionality numeric(6,2),
  density       numeric(10,4),
  price         numeric(12,4),
  availability  boolean NOT NULL DEFAULT true, -- 约束库输入（§2.3 生成约束检查）
  UNIQUE (tenant_id, code)
);

CREATE TABLE material_batch (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant(id),
  material_id   uuid NOT NULL REFERENCES material(id),
  supplier      text NOT NULL,
  lot_no        text NOT NULL,
  coa_ref       text,                          -- MinIO 引用：COA 文档
  received_at   date NOT NULL,
  expiry        date,
  props_json    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 当批实测物性（point-in-time 特征来源）
  UNIQUE (tenant_id, material_id, supplier, lot_no)
);

-- ----------------------------------------------------------------------------
-- 4. 配方聚合根（§3.2：配方-版本分离，版本追加写不可变）
-- ----------------------------------------------------------------------------

CREATE TABLE formula (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenant(id),
  project_id            uuid NOT NULL REFERENCES project(id),
  code                  text NOT NULL,
  current_version_id    uuid,                  -- 指针；历史版本不可变
  confidentiality_level confidentiality_level NOT NULL DEFAULT 'L1_internal',
  created_by            text NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, code)
);

CREATE TABLE formula_version (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  formula_id      uuid NOT NULL REFERENCES formula(id),
  version_no      integer NOT NULL,
  parent_id       uuid REFERENCES formula_version(id),
  change_reason   text,
  approval_status approval_status NOT NULL DEFAULT 'draft',
  snapshot_json   jsonb NOT NULL,              -- 固化当次完整配方，避免 JOIN 还原出错
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, formula_id, version_no)
);

-- 追加写守护：任何 UPDATE/DELETE 一律拒绝（含表属主，FORCE RLS 同理）
CREATE FUNCTION app.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %: % not allowed', TG_TABLE_NAME, TG_OP;
END
$$;

CREATE TRIGGER formula_version_append_only
  BEFORE UPDATE OR DELETE ON formula_version
  FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

CREATE TABLE formula_component (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  version_id      uuid NOT NULL REFERENCES formula_version(id),
  material_id     uuid NOT NULL REFERENCES material(id),
  batch_id        uuid REFERENCES material_batch(id),  -- 可空：设计期未定批次
  role            text NOT NULL,               -- 主体树脂/固化剂/填料/助剂/溶剂 …
  mass_part       numeric(12,4) NOT NULL CHECK (mass_part >= 0),  -- phr/质量份，DECIMAL 禁浮点（§3.2）
  equivalent_ratio numeric(10,4) CHECK (equivalent_ratio > 0),
  note            text
);

CREATE TABLE process_condition (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  version_id      uuid NOT NULL REFERENCES formula_version(id),
  step_seq        integer NOT NULL,
  param_key       text NOT NULL,               -- mix_temp / cure_temp / cure_time / post_cure …
  value           numeric(14,6),
  unit            text NOT NULL,
  normalized_value numeric(14,6),              -- 单位换算服务写入，特征层直接消费（§3.3 量纲归一）
  UNIQUE (tenant_id, version_id, step_seq, param_key)
);

-- ----------------------------------------------------------------------------
-- 5. 指标定义与 DoE（目标与区间由业务在此定义，禁止硬编码在模型代码）
-- ----------------------------------------------------------------------------

CREATE TABLE metric_definition (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  code            text NOT NULL,               -- thermal_conductivity / volume_resistivity / tg / shear_strength …
  name            text NOT NULL,
  unit            text NOT NULL,               -- 基准单位：W/(m·K)、Ω·cm、mPa·s、°C、MPa
  direction       metric_direction NOT NULL,
  acceptable_range numrange,                   -- target 型指标的达标区间
  UNIQUE (tenant_id, code)
);

CREATE TABLE doe_design (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  project_id      uuid NOT NULL REFERENCES project(id),
  code            text NOT NULL,
  method          text NOT NULL,               -- LHS / full_factorial / bo …
  params_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, code)
);

-- ----------------------------------------------------------------------------
-- 6. 实验与测试结果（§6.1 状态机 + §3.3 QC）
-- ----------------------------------------------------------------------------

CREATE TABLE experiment (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenant(id),
  project_id          uuid NOT NULL REFERENCES project(id),
  code                text NOT NULL,
  batch_id            uuid REFERENCES doe_design(id),       -- 所属实验批次
  formula_version_id  uuid NOT NULL REFERENCES formula_version(id),
  recommendation_id   uuid,                                 -- 由推荐产生时回链（后置 FK，见文末）
  state               experiment_state NOT NULL DEFAULT 'RECOMMENDED',
  state_reason        text,                   -- REJECTED/CANCELLED 原因（负结果一等数据）
  planned_at          timestamptz,
  finished_at         timestamptz,
  operator_id         text,
  device_id           text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, code)
);

-- 状态迁移白名单（§6.1）；非法迁移直接报错，迁移必须经应用服务并写审计
CREATE FUNCTION app.experiment_transition_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' AND NEW.state <> 'RECOMMENDED' THEN
    RAISE EXCEPTION 'experiment must start at RECOMMENDED';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.state = NEW.state THEN RETURN NEW; END IF;
    IF NOT (
         (OLD.state = 'RECOMMENDED' AND NEW.state IN ('REVIEWED','CANCELLED')) OR
         (OLD.state = 'REVIEWED'    AND NEW.state IN ('SCHEDULED','CANCELLED')) OR
         (OLD.state = 'SCHEDULED'   AND NEW.state IN ('IN_PROGRESS','CANCELLED')) OR
         (OLD.state = 'IN_PROGRESS' AND NEW.state IN ('COMPLETED','CANCELLED')) OR
         (OLD.state = 'COMPLETED'   AND NEW.state IN ('QC_PENDING','CANCELLED')) OR
         (OLD.state = 'QC_PENDING'  AND NEW.state IN ('ACCEPTED','REJECTED'))    -- QC 不过进失败样本库，不回退
       ) THEN
      RAISE EXCEPTION 'illegal experiment transition % -> %', OLD.state, NEW.state;
    END IF;
    NEW.updated_at := now();
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER experiment_state_guard
  BEFORE INSERT OR UPDATE OF state ON experiment
  FOR EACH ROW EXECUTE FUNCTION app.experiment_transition_guard();

CREATE TABLE test_result (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant(id),
  experiment_id uuid NOT NULL REFERENCES experiment(id),
  metric_id     uuid NOT NULL REFERENCES metric_definition(id),
  raw_ref       text,                          -- MinIO 原始曲线引用
  value         numeric(16,6),                 -- 基准单位下的提取指标
  raw_value     numeric(16,6),                 -- 原始值 + 原始单位（审计保留，§3.3）
  raw_unit      text,
  test_standard text NOT NULL,                 -- 测试标准（GB/ISO/企业标准）
  test_condition_json jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 温度/湿度/试样厚度/固化制度：特征而非元数据
  fidelity      fidelity_level NOT NULL DEFAULT 'experiment',
  qc_flag       qc_flag_level NOT NULL DEFAULT 'pass',
  qc_report_ref text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, experiment_id, metric_id, test_standard)
);

-- ----------------------------------------------------------------------------
-- 7. 特征与数据快照（三元组绑定的前两元，§5.5、§18.5；先于 prediction_log 创建）
-- ----------------------------------------------------------------------------

CREATE TABLE feature_set (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenant(id),
  version           text NOT NULL,
  definition_hash   text NOT NULL,             -- 特征定义可复现（ADR-04）
  generator_version text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, version)
);

CREATE TABLE dataset_snapshot (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  code            text NOT NULL,
  scope_json      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 可见数据范围写入快照元数据（§18.3 审计）
  storage_ref     text NOT NULL,               -- MinIO 快照文件
  row_count       integer,
  content_hash    text NOT NULL,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, code)
);

-- ----------------------------------------------------------------------------
-- 8. 质量规则与预测日志（§3.3 / §4.3）
-- ----------------------------------------------------------------------------

CREATE TABLE quality_rule (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant(id),
  code          text NOT NULL,                 -- 如 R-01 配比总和、R-07 固化温度窗口
  level         quality_rule_level NOT NULL,
  description   text NOT NULL,
  params_json   jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled       boolean NOT NULL DEFAULT true,
  UNIQUE (tenant_id, code)
);

CREATE TABLE prediction_log (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenant(id),
  formula_version_id uuid NOT NULL REFERENCES formula_version(id),
  metric_id         uuid NOT NULL REFERENCES metric_definition(id),
  model_version     text NOT NULL,             -- model_registry.version 引用（弱外键，允许历史模型归档）
  feature_set_id    uuid NOT NULL REFERENCES feature_set(id),
  snapshot_id       uuid NOT NULL,             -- dataset_snapshot.id（后置 FK，见文末）
  point_estimate    numeric(16,6),
  interval_low      numeric(16,6),             -- conformal 校准后区间（§5.2）
  interval_high     numeric(16,6),
  interval_level    numeric(4,2) CHECK (interval_level IN (0.50, 0.70, 0.90)),
  ood_tier          ood_tier NOT NULL DEFAULT 'none',
  advisory_note     text,
  requested_by      text,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 9. 模型注册（三元组最后一元）
-- ----------------------------------------------------------------------------

CREATE TABLE model_registry (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenant(id),
  model_type        text NOT NULL,             -- lgbm_multitask / xgb_quantile …
  version           text NOT NULL,
  feature_set_id    uuid NOT NULL REFERENCES feature_set(id),
  dataset_snapshot_id uuid NOT NULL REFERENCES dataset_snapshot(id),
  metrics_json      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 含覆盖率/违规率门禁结果（§5.5）
  stage             model_stage NOT NULL DEFAULT 'registered',
  artifact_ref      text NOT NULL,             -- MinIO 模型产物
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, model_type, version)
);

-- ----------------------------------------------------------------------------
-- 9. 推荐（采纳率与约束库修正依据，§6.1 / §15.3 / §18.5）
-- ----------------------------------------------------------------------------

CREATE TABLE recommendation (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenant(id),
  project_id            uuid NOT NULL REFERENCES project(id),
  batch_id              uuid REFERENCES doe_design(id),
  candidates_json       jsonb NOT NULL,       -- 候选配方（约束库在 T0 重建的池，§17.2）
  scores_json           jsonb NOT NULL,
  model_version         text NOT NULL,
  constraint_set_version text NOT NULL,
  input_snapshot_id     uuid NOT NULL REFERENCES dataset_snapshot(id),
  state                 recommendation_state NOT NULL DEFAULT 'proposed',
  decided_by            text,
  decided_at            timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 10. 校准统计物化表（F1 详设：夜间 Celery 物化，界面只读）
-- ----------------------------------------------------------------------------

CREATE TABLE calib_stat (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  metric_code     text NOT NULL,
  coverage_level  numeric(4,2) NOT NULL CHECK (coverage_level IN (0.50, 0.70, 0.90)),
  window          text NOT NULL,               -- 如 '90d' / 'all'
  n               integer NOT NULL CHECK (n >= 0),
  empirical       numeric(6,4),                -- 经验覆盖率（n=0/不足时为 NULL）
  model_version   text NOT NULL,
  snapshot_id     uuid NOT NULL REFERENCES dataset_snapshot(id),
  computed_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, metric_code, coverage_level, window, model_version, snapshot_id)
);

-- ----------------------------------------------------------------------------
-- 11. 血缘与审计（只追加；审计按月分区）
-- ----------------------------------------------------------------------------

CREATE TABLE lineage_edge (
  id         bigserial PRIMARY KEY,
  tenant_id  uuid NOT NULL REFERENCES tenant(id),
  src_type   text NOT NULL,                    -- formula_version / experiment / model / feature_set / snapshot …
  src_id     text NOT NULL,
  dst_type   text NOT NULL,
  dst_id     text NOT NULL,
  relation   text NOT NULL,                    -- trained_on / predicted_by / derived_from …
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_event (
  id          bigserial NOT NULL,
  tenant_id   uuid NOT NULL,
  actor       text NOT NULL,
  action      text NOT NULL,
  object_ref  text NOT NULL,
  before_hash text,
  after_hash  text,
  ts          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

-- 初始分区 + 默认分区（运维按月预建，§18.4）
CREATE TABLE audit_event_2026_11 PARTITION OF audit_event
  FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE audit_event_2026_12 PARTITION OF audit_event
  FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE audit_event_default PARTITION OF audit_event DEFAULT;

CREATE TRIGGER audit_event_append_only
  BEFORE UPDATE OR DELETE ON audit_event
  FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

-- ----------------------------------------------------------------------------
-- 12. 后置外键（解决表间循环引用）
-- ----------------------------------------------------------------------------

ALTER TABLE formula
  ADD CONSTRAINT fk_formula_current_version
  FOREIGN KEY (current_version_id) REFERENCES formula_version(id);
ALTER TABLE experiment
  ADD CONSTRAINT fk_experiment_recommendation
  FOREIGN KEY (recommendation_id) REFERENCES recommendation(id);
ALTER TABLE prediction_log
  ADD CONSTRAINT fk_prediction_snapshot
  FOREIGN KEY (snapshot_id) REFERENCES dataset_snapshot(id);

-- ----------------------------------------------------------------------------
-- 13. 索引（在线链路 P95 预算 §15.2 与血缘反查 §7.3）
-- ----------------------------------------------------------------------------

CREATE INDEX idx_formula_component_version  ON formula_component (tenant_id, version_id);
CREATE INDEX idx_process_condition_version  ON process_condition (tenant_id, version_id);
CREATE INDEX idx_experiment_version         ON experiment (tenant_id, formula_version_id);
CREATE INDEX idx_experiment_state_open      ON experiment (tenant_id, state)
  WHERE state NOT IN ('ACCEPTED', 'REJECTED', 'CANCELLED');   -- 待回写积压看板
CREATE INDEX idx_test_result_experiment     ON test_result (tenant_id, experiment_id);
CREATE INDEX idx_prediction_version_time    ON prediction_log (tenant_id, formula_version_id, created_at);
CREATE INDEX idx_prediction_model_time      ON prediction_log (tenant_id, model_version, created_at);  -- F1 校准统计扫描
CREATE INDEX idx_recommendation_batch       ON recommendation (tenant_id, batch_id, created_at);
CREATE INDEX idx_lineage_src                ON lineage_edge (tenant_id, src_type, src_id);
CREATE INDEX idx_lineage_dst                ON lineage_edge (tenant_id, dst_type, dst_id);  -- 反向追溯
CREATE INDEX idx_audit_ts                   ON audit_event USING brin (ts);
CREATE INDEX idx_calib_stat_lookup          ON calib_stat (tenant_id, metric_code, model_version, snapshot_id);

-- ----------------------------------------------------------------------------
-- 14. Row-Level Security（§18.3：策略由会话变量驱动，全表启用并 FORCE）
-- ----------------------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'project','material','material_batch','formula','formula_version','formula_component',
    'process_condition','metric_definition','doe_design','experiment','test_result',
    'quality_rule','prediction_log','feature_set','dataset_snapshot','model_registry',
    'recommendation','calib_stat','lineage_edge'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($f$
      CREATE POLICY tenant_isolation ON %I
        USING (tenant_id = app.current_tenant())
        WITH CHECK (tenant_id = app.current_tenant())
    $f$, t);
  END LOOP;
END
$$;

-- audit_event 只追加策略（无 WITH CHECK，读路径同租户隔离）
ALTER TABLE audit_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_read ON audit_event
  FOR SELECT USING (tenant_id = app.current_tenant());
CREATE POLICY tenant_isolation_insert ON audit_event
  FOR INSERT WITH CHECK (tenant_id = app.current_tenant());

-- ----------------------------------------------------------------------------
-- 15. 授权（最小权限；adhe_ml_training 只读且同样受 RLS 约束——§18.3 红线）
-- ----------------------------------------------------------------------------

GRANT USAGE ON SCHEMA app, public TO adhe_app_rw, adhe_app_ro, adhe_ml_training;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO adhe_app_ro, adhe_ml_training;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO adhe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO adhe_app_rw;
-- 离线训练角色不得写库、不得绕过 RLS（默认即受 FORCE RLS 约束）
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO adhe_ml_training;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO adhe_app_rw;

COMMIT;
