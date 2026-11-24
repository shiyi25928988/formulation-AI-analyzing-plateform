-- ============================================================================
-- 001_init_schema 的回滚脚本（B01 / R2）
-- 约定：迁移采用「扩展-收缩」（§18.4），本脚本仅用于 001 整体回滚；
--       生产环境的破坏性变更不走本文件，须使用后续迁移的配套收缩脚本。
-- ============================================================================

BEGIN;

-- 先撤触发器与函数依赖，再按依赖逆序 DROP
DROP TABLE IF EXISTS audit_event CASCADE;          -- 分区表：级联删除全部分区
DROP TABLE IF EXISTS lineage_edge CASCADE;
DROP TABLE IF EXISTS calib_stat CASCADE;
DROP TABLE IF EXISTS recommendation CASCADE;
DROP TABLE IF EXISTS model_registry CASCADE;
DROP TABLE IF EXISTS dataset_snapshot CASCADE;
DROP TABLE IF EXISTS feature_set CASCADE;
DROP TABLE IF EXISTS prediction_log CASCADE;
DROP TABLE IF EXISTS quality_rule CASCADE;
DROP TABLE IF EXISTS test_result CASCADE;
DROP TABLE IF EXISTS experiment CASCADE;
DROP TABLE IF EXISTS doe_design CASCADE;
DROP TABLE IF EXISTS metric_definition CASCADE;
DROP TABLE IF EXISTS process_condition CASCADE;
DROP TABLE IF EXISTS formula_component CASCADE;
DROP TABLE IF EXISTS formula_version CASCADE;
DROP TABLE IF EXISTS formula CASCADE;
DROP TABLE IF EXISTS material_batch CASCADE;
DROP TABLE IF EXISTS material CASCADE;
DROP TABLE IF EXISTS project CASCADE;
DROP TABLE IF EXISTS tenant CASCADE;

DROP FUNCTION IF EXISTS app.experiment_transition_guard() CASCADE;
DROP FUNCTION IF EXISTS app.reject_mutation() CASCADE;
DROP FUNCTION IF EXISTS app.current_tenant() CASCADE;
DROP SCHEMA IF EXISTS app CASCADE;

DROP TYPE IF EXISTS quality_rule_level CASCADE;
DROP TYPE IF EXISTS ood_tier CASCADE;
DROP TYPE IF EXISTS confidentiality_level CASCADE;
DROP TYPE IF EXISTS model_stage CASCADE;
DROP TYPE IF EXISTS recommendation_state CASCADE;
DROP TYPE IF EXISTS approval_status CASCADE;
DROP TYPE IF EXISTS metric_direction CASCADE;
DROP TYPE IF EXISTS qc_flag_level CASCADE;
DROP TYPE IF EXISTS fidelity_level CASCADE;
DROP TYPE IF EXISTS experiment_state CASCADE;

-- 撤销默认授权
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM adhe_ml_training;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM adhe_app_rw;
REVOKE ALL ON SCHEMA app, public FROM adhe_app_rw, adhe_app_ro, adhe_ml_training;

COMMIT;
