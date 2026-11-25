-- ============================================================================
-- 002_model_stage_guard.sql —— model_registry 迁移白名单触发器（B09 §2.3 / R2.6c）
--
-- 与 001 的 experiment_transition_guard 同构（B09 §6 清单最后一项）。
-- 白名单唯一权威 = lifecycle/store.py 的 ALLOWED_TRANSITIONS（实现 DoD 2
-- 「状态机白名单镜像 DDL」）：两处由 lifecycle/test_lifecycle.py 的镜像
-- 断言机械绑定，改动任何一侧必须同批同步另一侧并过测试。
--
-- 语义对齐（B09 详设裁定）：
--   * INSERT 必须从 registered 起步——门禁失败 = 不入册（§5.5），
--     不存在 REJECTED 终态，model_registry 不接受表外脏状态；
--   * 回滚 = 路由回切非状态迁移（§2.4）：当前 production 行走
--     production→deprecated（T4），路由由服务层切到同 type 仍处
--     production 的上一版本，故白名单不含任何「回退」迁移；
--   * 同 type 允许至多两个 production 并行（新旧双版本对照 §16.2），
--     行级触发器不约束跨行数量，该守卫在服务层
--     （ModelLifecycleStore.to_production 的 MAX_PRODUCTION_PER_TYPE）。
-- ============================================================================

BEGIN;

CREATE FUNCTION app.model_stage_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' AND NEW.stage <> 'registered' THEN
    RAISE EXCEPTION 'model must start at registered (T0 gate passed only)';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.stage = NEW.stage THEN RETURN NEW; END IF;
    IF NOT (
         (OLD.stage = 'registered' AND NEW.stage = 'shadow')       OR  -- T1
         (OLD.stage = 'shadow'     AND NEW.stage = 'canary')       OR  -- T2
         (OLD.stage = 'canary'     AND NEW.stage = 'production')   OR  -- T3
         (OLD.stage = 'production' AND NEW.stage = 'deprecated')   OR  -- T4
         (OLD.stage = 'canary'     AND NEW.stage = 'deprecated')       -- T5
       ) THEN
      RAISE EXCEPTION 'illegal model transition % -> %', OLD.stage, NEW.stage;
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER model_stage_guard
  BEFORE INSERT OR UPDATE OF stage ON model_registry
  FOR EACH ROW EXECUTE FUNCTION app.model_stage_guard();

COMMIT;
