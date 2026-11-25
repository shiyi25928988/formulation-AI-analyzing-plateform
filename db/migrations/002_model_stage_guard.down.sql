-- ============================================================================
-- 002_model_stage_guard 的回滚脚本（B09 / R2.6c）
-- 约定：迁移采用「扩展-收缩」（§18.4）；本迁移为纯扩展（加函数 + 触发器），
-- 回滚即整删，无数据收缩步骤。
-- ============================================================================

BEGIN;

DROP TRIGGER IF EXISTS model_stage_guard ON model_registry;
DROP FUNCTION IF EXISTS app.model_stage_guard CASCADE;

COMMIT;
