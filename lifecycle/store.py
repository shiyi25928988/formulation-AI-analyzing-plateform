"""lifecycle/store.py —— model_registry 内存表 + 生命周期迁移白名单（B09 §2.3）。

镜像 B01 DDL 的 ``model_stage`` 枚举（唯一权威口径）::

    registered → shadow → canary → production → deprecated

关键裁定（docs/design/B09-PIPELINE-DETAIL-DESIGN.md §2.1–§2.4）：

- 门禁失败 = **不入册**（§5.5），不设 REJECTED 终态污染注册表；
  失败证据走 ``ModelGateFailed`` 审计事件留痕，旧版本继续服务。
- 六条迁移白名单逐条守卫，非法迁移一律 ``IllegalTransitionError``（409 同径）。
- 回滚 = **路由回切非状态迁移**：当前 production 转 deprecated，
  路由切回同 ``model_type`` 仍处 production 的上一版本；
  因此白名单允许同 type 至多两个 production 并行（§16.2 双版本对照的基础）。
- 所有迁移写 ``audit_event``（复用骨架哈希链），事件幂等键四元组，
  ``aggregate_id = model_version``。

本模块只管状态机；门禁检查序列（G0–G5 / C1–C3）在 ``gates.py``，
模型卡渲染在 ``modelcard.py``。
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skeleton.store import (                         # noqa: E402
    IllegalTransitionError, SkeletonStore, StoreError, _now,
)

# DDL model_stage 枚举（唯一权威，迁移白名单以此为准）
STAGES = ("registered", "shadow", "canary", "production", "deprecated")

# 六条迁移白名单（B09 §2.3；与 model_registry 触发器 migration 002 同构）
ALLOWED_TRANSITIONS: set[tuple[str, str]] = {
    ("registered", "shadow"),        # T1
    ("shadow", "canary"),            # T2
    ("canary", "production"),        # T3
    ("production", "deprecated"),    # T4
    ("canary", "deprecated"),        # T5
}

# 同 type 允许并行 production 的上限（B09 §2.4：新旧双版本对照）
MAX_PRODUCTION_PER_TYPE = 2

SOURCE = "model-lifecycle-svc"


class LifecycleError(StoreError):
    """生命周期业务约束违规（守卫不满足等）。"""


class ModelLifecycleStore:
    """model_registry 语义层：在 SkeletonStore 之上加状态机与迁移守卫。

    守卫条件由调用方（gates 模块 / 测试）以显式参数传入——状态机只认
    「证据已满足」的布尔结论，不重复实现门禁计算（单一职责）。
    """

    def __init__(self, store: SkeletonStore):
        self.store = store

    # ------------------------------------------------------------------ 查询

    def get_by_version(self, model_version: str) -> dict | None:
        rows = self.store.where("model_registry",
                                model_version=model_version)
        return rows[0] if rows else None

    def production_of(self, model_type: str) -> list[dict]:
        """当前承接推理流量的版本（路由视图；B09 §2.4 允许 1–2 个并行）。"""
        return self.store.where("model_registry", model_type=model_type,
                                model_stage="production")

    def require(self, model_version: str) -> dict:
        row = self.get_by_version(model_version)
        if row is None:
            raise StoreError(f"model_registry/{model_version} not found")
        return row

    # ------------------------------------------------------------------ T0

    def register(self, *, model_version: str, model_type: str,
                 artifact_uri: str, artifact_checksum: str,
                 feature_set_version: str, feature_fingerprint: str,
                 dataset_snapshot_id: str, gate_report: dict) -> dict | None:
        """T0：（表外）→ registered。

        G0–G5 全过（``gate_report["passed"] is True``）才入册并发布
        ``ModelRegistered``；任一失败 = **不入册**，发布 ``ModelGateFailed``
        并返回 None（证据已在 gate_report，模型卡留痕由上层负责）。
        重复注册同一 model_version 拒绝（409 同径）。
        """
        if self.get_by_version(model_version) is not None:
            raise IllegalTransitionError(
                f"{model_version} already registered (duplicate)")
        if gate_report.get("passed") is not True:
            # failed_rule 从逐项结果推导（gate_report 本体保持契约纯净：
            # additionalProperties=false，无顶层 failed_rule 字段）
            failed = next((r["rule_id"] for r in gate_report.get("results", [])
                           if r.get("status") == "FAIL"), "UNKNOWN")
            self.store.emit(SOURCE, "ModelGateFailed", {
                "model_version": model_version,
                "model_version_of": model_type,
                "failed_rule": failed,
                "gate_report": gate_report,
            }, aggregate_id=model_version)
            return None

        row = self.store.insert("model_registry", {
            "tenant_id": self.store.tenant_id,
            "model_version": model_version,
            "model_type": model_type,
            "model_stage": "registered",
            "artifact_uri": artifact_uri,
            "artifact_checksum": artifact_checksum,
            "feature_set_version": feature_set_version,
            "feature_fingerprint": feature_fingerprint,
            "dataset_snapshot_id": dataset_snapshot_id,
            "gate_report": gate_report,
            "metrics_json": {},
        })
        self.store.emit(SOURCE, "ModelRegistered", {
            "model_version": model_version,
            "model_version_of": model_type,
            "feature_set_version": feature_set_version,
            "dataset_snapshot_id": dataset_snapshot_id,
            "gate_report": gate_report,
        }, aggregate_id=model_version)
        return row

    # ------------------------------------------------------------------ T1

    def to_shadow(self, model_version: str, *, artifact_ok: bool,
                  fingerprint_matches: bool, min_requests: int = 30,
                  writeback_rounds: int = 1) -> dict:
        """T1：registered → shadow。守卫：artifact 校验和通过且特征指纹
        与 G0 记录一致（B09 §2.3）；观察量节拍随事件落审计。"""
        row = self._check(model_version, "shadow")
        if not artifact_ok:
            raise LifecycleError("T1 guard failed: artifact checksum mismatch")
        if not fingerprint_matches:
            raise LifecycleError(
                "T1 guard failed: feature fingerprint differs from G0 record")
        row = self._apply(row, "shadow")
        self.store.emit(SOURCE, "ModelShadowStarted", {
            "model_version": model_version,
            "model_version_of": row["model_type"],
            "min_requests": min_requests,
            "writeback_rounds": writeback_rounds,
        }, aggregate_id=model_version)
        return row

    # ------------------------------------------------------------------ T2

    def to_canary(self, model_version: str, *, comparison_stable: bool,
                  traffic_percent: int = 5) -> dict:
        """T2：shadow → canary。守卫：影子对拍差异分布稳定
        （§16.2 双版本对照：分布突变即阻断，不只看聚合指标）。"""
        row = self._check(model_version, "canary")
        if not comparison_stable:
            raise LifecycleError(
                "T2 guard failed: shadow comparison distribution shifted")
        row = self._apply(row, "canary")
        self.store.emit(SOURCE, "ModelCanaryStarted", {
            "model_version": model_version,
            "model_version_of": row["model_type"],
            "traffic_percent": traffic_percent,
        }, aggregate_id=model_version)
        return row

    # ------------------------------------------------------------------ T3

    def to_production(self, model_version: str, *, canary_pass: bool,
                      online_within_thresholds: bool,
                      checked_rules: list[str]) -> dict:
        """T3：canary → production。守卫：B06 金丝雀
        ``run_canary().overall_pass``（C1）+ 灰度期无回滚触发 +
        在线覆盖率/违规率不越界（C3）。

        发布契约既有事件 ``ModelPromoted``（payload 对齐
        events.schema.json#/$defs/ModelPromoted）。同 type production
        并行数达到 ``MAX_PRODUCTION_PER_TYPE`` 时拒绝（应为回滚腾位）。

        守卫先于状态变更：任一守卫失败停在 canary，不产生半迁移。
        """
        row = self._check(model_version, "production")
        if not canary_pass:
            raise LifecycleError(
                "T3 guard failed: canary suite not fully passed (C1)")
        if not online_within_thresholds:
            raise LifecycleError(
                "T3 guard failed: online coverage/violation out of bounds (C3)")
        if len(self.production_of(row["model_type"])) >= MAX_PRODUCTION_PER_TYPE:
            raise LifecycleError(
                f"already {MAX_PRODUCTION_PER_TYPE} production rows for type "
                f"{row['model_type']!r}; roll back one first")
        row = self._apply(row, "production")
        self.store.emit(SOURCE, "ModelPromoted", {
            "model_version": model_version,
            "model_version_of": row["model_type"],
            "gate_result": {"passed": True, "checked_rules": checked_rules},
        }, aggregate_id=model_version)
        return row

    # ------------------------------------------------------------------ T4/T5

    def deprecate(self, model_version: str, *, reason: str,
                  approver: str | None = None) -> dict:
        """T4（production → deprecated）/ T5（canary → deprecated）。

        ``reason ∈ {drift-auto, manual-retire, canary-terminated}``；
        人工路径（manual-retire）必须提供 approver（B09 §2.3）。
        """
        if reason not in ("drift-auto", "manual-retire", "canary-terminated"):
            raise LifecycleError(f"unknown deprecation reason {reason!r}")
        if reason == "manual-retire" and not approver:
            raise LifecycleError("manual-retire requires approver")
        row = self._check(model_version, "deprecated")
        row = self._apply(row, "deprecated")
        self.store.emit(SOURCE, "ModelDeprecated", {
            "model_version": model_version,
            "model_version_of": row["model_type"],
            "reason": reason, "approver": approver,
        }, aggregate_id=model_version)
        return row

    # ------------------------------------------------------------------ 回滚

    def rollback(self, model_version: str, *, trigger: str) -> dict:
        """回滚 = 路由回切（B09 §2.4，明确非状态迁移）：

        1. 当前 production 版本走 T4 转 deprecated（reason=漂移或金丝雀失败）；
        2. 发布 ``ModelRolledBack``，``rolled_back_to`` 指向同 type 仍处
           production 的上一版本（必须存在，否则拒绝——没有可回切的版本）。

        ``trigger ∈ {drift-auto, canary-failure, manual}``。
        """
        if trigger not in ("drift-auto", "canary-failure", "manual"):
            raise LifecycleError(f"unknown rollback trigger {trigger!r}")
        row = self.require(model_version)
        if row["model_stage"] != "production":
            raise IllegalTransitionError(
                f"rollback requires production stage, got {row['model_stage']}")
        peers = [r for r in self.production_of(row["model_type"])
                 if r["model_version"] != model_version]
        if not peers:
            raise LifecycleError(
                f"no production peer of type {row['model_type']!r} to "
                f"route back to; manual intervention required")
        peer = sorted(peers, key=lambda r: r.get("registered_at", ""))[-1]
        reason = "drift-auto" if trigger == "drift-auto" else "canary-terminated"
        self.deprecate(model_version, reason=reason)
        self.store.emit(SOURCE, "ModelRolledBack", {
            "model_version": model_version,
            "rolled_back_to": peer["model_version"],
            "trigger": trigger,
        }, aggregate_id=model_version)
        return peer

    # ------------------------------------------------------------------ 内部

    def _check(self, model_version: str, target: str) -> dict:
        """白名单校验（不变更状态）；返回当前行。"""
        row = self.require(model_version)
        old = row["model_stage"]
        if old != target and (old, target) not in ALLOWED_TRANSITIONS:
            raise IllegalTransitionError(
                f"illegal model transition {old} -> {target} "
                f"for {model_version} (B09 §2.3 whitelist)")
        return row

    def _apply(self, row: dict, target: str) -> dict:
        """应用状态变更（仅在全部守卫通过后调用，避免半迁移）。"""
        return self.store.update("model_registry", row["id"],
                                 {"model_stage": target,
                                  "updated_at": _now()})
