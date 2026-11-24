"""lifecycle/test_lifecycle.py —— B09 实现轮验收测试。

store 模块（状态机）用例先行；gates / modelcard 用例随后续模块补入同文件。
"""

from __future__ import annotations

import json
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skeleton.store import (                         # noqa: E402
    IllegalTransitionError, SkeletonStore, StoreError,
)
from lifecycle.store import (                        # noqa: E402
    ALLOWED_TRANSITIONS, LifecycleError, ModelLifecycleStore, STAGES,
)

GATE_PASS = {
    "passed": True,
    "checked_rules": ["G0", "G1", "G2", "G3", "G4", "G5"],
    "results": [{"rule_id": r, "status": "PASS"}
                for r in ["G0", "G1", "G2", "G3", "G4", "G5"]],
}
GATE_FAIL = {
    "passed": False,
    "checked_rules": ["G0", "G1", "G2", "G3"],
    "results": [{"rule_id": r, "status": "PASS"} for r in ["G0", "G1", "G2"]]
    + [{"rule_id": "G3", "status": "FAIL", "detail": "coverage low"},
       {"rule_id": "G4", "status": "SKIPPED", "detail": "short-circuit"},
       {"rule_id": "G5", "status": "SKIPPED", "detail": "short-circuit"}],
}


def fresh() -> tuple[SkeletonStore, ModelLifecycleStore]:
    s = SkeletonStore()
    return s, ModelLifecycleStore(s)


def register_ok(lc: ModelLifecycleStore, version="M-v1.0.0",
                mtype="lgbm-reg", **kw) -> dict:
    kw.setdefault("gate_report", dict(GATE_PASS))
    row = lc.register(model_version=version, model_type=mtype,
                      artifact_uri=f"minio://artifacts/{version}.pkl",
                      artifact_checksum="sha256:" + "a" * 64,
                      feature_set_version="FS-v1.0",
                      feature_fingerprint="sha256:d1a2b3f2766f12d6",
                      dataset_snapshot_id="SNP-20261124-aaaaaaaa",
                      **kw)
    return row


class TestT0Register(unittest.TestCase):
    def test_gate_pass_registers_and_emits(self):
        s, lc = fresh()
        row = register_ok(lc)
        self.assertEqual(row["model_stage"], "registered")
        types = [e["type"] for e in s.where("audit_event")]
        self.assertIn("ModelRegistered", types)
        self.assertNotIn("ModelGateFailed", types)

    def test_gate_failure_does_not_register(self):
        """裁定：门禁失败=不入册，ModelGateFailed 留痕（§5.5 / B09 §2.1）。"""
        s, lc = fresh()
        row = register_ok(lc, gate_report=dict(GATE_FAIL))
        self.assertIsNone(row)
        self.assertIsNone(lc.get_by_version("M-v1.0.0"))
        types = [e["type"] for e in s.where("audit_event")]
        self.assertIn("ModelGateFailed", types)
        self.assertNotIn("ModelRegistered", types)
        ev = [e for e in s.where("audit_event")
              if e["type"] == "ModelGateFailed"][0]
        self.assertEqual(ev["payload"]["failed_rule"], "G3")
        # 失败项留痕：短路求值结果 SKIPPED 可反查
        statuses = {r["rule_id"]: r["status"]
                    for r in ev["payload"]["gate_report"]["results"]}
        self.assertEqual(statuses["G4"], "SKIPPED")

    def test_duplicate_registration_rejected(self):
        _, lc = fresh()
        register_ok(lc)
        with self.assertRaises(IllegalTransitionError):
            register_ok(lc)

    def test_contract_stage_enum_alignment(self):
        """状态枚举与 B04 契约/DDL 口径一致：五态、无 REJECTED。"""
        self.assertEqual(STAGES, ("registered", "shadow", "canary",
                                  "production", "deprecated"))
        self.assertEqual(ALLOWED_TRANSITIONS, {
            ("registered", "shadow"), ("shadow", "canary"),
            ("canary", "production"), ("production", "deprecated"),
            ("canary", "deprecated"),
        })


class TestTransitions(unittest.TestCase):
    def _to_canary(self, lc, version="M-v1.0.0"):
        register_ok(lc, version)
        lc.to_shadow(version, artifact_ok=True, fingerprint_matches=True)
        lc.to_canary(version, comparison_stable=True)

    def test_full_happy_path(self):
        s, lc = fresh()
        self._to_canary(lc)
        lc.to_production("M-v1.0.0", canary_pass=True,
                         online_within_thresholds=True,
                         checked_rules=["C1", "C2", "C3"])
        self.assertEqual(lc.require("M-v1.0.0")["model_stage"], "production")
        lc.deprecate("M-v1.0.0", reason="manual-retire", approver="高见远")
        self.assertEqual(lc.require("M-v1.0.0")["model_stage"], "deprecated")
        types = [e["type"] for e in s.where("audit_event")]
        for t in ("ModelRegistered", "ModelShadowStarted",
                  "ModelCanaryStarted", "ModelPromoted", "ModelDeprecated"):
            self.assertIn(t, types)

    def test_illegal_transitions_rejected(self):
        _, lc = fresh()
        register_ok(lc)
        # registered 直接跳 canary / production 均不在白名单
        with self.assertRaises(IllegalTransitionError):
            lc.to_canary("M-v1.0.0", comparison_stable=True)
        with self.assertRaises(IllegalTransitionError):
            lc.to_production("M-v1.0.0", canary_pass=True,
                             online_within_thresholds=True,
                             checked_rules=["C1"])
        # deprecated 是终态
        lc.to_shadow("M-v1.0.0", artifact_ok=True, fingerprint_matches=True)
        lc.deprecate = lc.deprecate  # noqa: B018
        lc.to_canary("M-v1.0.0", comparison_stable=True)
        lc.deprecate("M-v1.0.0", reason="canary-terminated")
        with self.assertRaises(IllegalTransitionError):
            lc.to_shadow("M-v1.0.0", artifact_ok=True,
                         fingerprint_matches=True)

    def test_t1_guard_fingerprint_mismatch(self):
        _, lc = fresh()
        register_ok(lc)
        with self.assertRaises(LifecycleError):
            lc.to_shadow("M-v1.0.0", artifact_ok=True,
                         fingerprint_matches=False)

    def test_t3_guard_canary_fail_keeps_stage(self):
        _, lc = fresh()
        self._to_canary(lc)
        with self.assertRaises(LifecycleError):
            lc.to_production("M-v1.0.0", canary_pass=False,
                             online_within_thresholds=True,
                             checked_rules=["C1"])
        self.assertEqual(lc.require("M-v1.0.0")["model_stage"], "canary")

    def test_manual_retire_requires_approver(self):
        _, lc = fresh()
        register_ok(lc)
        with self.assertRaises(LifecycleError):
            lc.deprecate("M-v1.0.0", reason="manual-retire")


class TestRolloutAndRollback(unittest.TestCase):
    def test_dual_production_allowed_and_routing_view(self):
        """B09 §2.4：同 type 允许至多两个 production 并行（路由回切基础）。"""
        _, lc = fresh()
        register_ok(lc, "M-v1.0.0")
        lc.to_shadow("M-v1.0.0", artifact_ok=True, fingerprint_matches=True)
        lc.to_canary("M-v1.0.0", comparison_stable=True)
        lc.to_production("M-v1.0.0", canary_pass=True,
                         online_within_thresholds=True,
                         checked_rules=["C1", "C2", "C3"])
        register_ok(lc, "M-v2.0.0")
        lc.to_shadow("M-v2.0.0", artifact_ok=True, fingerprint_matches=True)
        lc.to_canary("M-v2.0.0", comparison_stable=True)
        lc.to_production("M-v2.0.0", canary_pass=True,
                         online_within_thresholds=True,
                         checked_rules=["C1", "C2", "C3"])
        self.assertEqual(len(lc.production_of("lgbm-reg")), 2)

    def test_rollback_is_route_switch_not_state_transition(self):
        """回滚：当前 production 转 deprecated + 路由回切到上一版本。"""
        s, lc = fresh()
        register_ok(lc, "M-v1.0.0")
        lc.to_shadow("M-v1.0.0", artifact_ok=True, fingerprint_matches=True)
        lc.to_canary("M-v1.0.0", comparison_stable=True)
        lc.to_production("M-v1.0.0", canary_pass=True,
                         online_within_thresholds=True,
                         checked_rules=["C1", "C2", "C3"])
        register_ok(lc, "M-v2.0.0")
        lc.to_shadow("M-v2.0.0", artifact_ok=True, fingerprint_matches=True)
        lc.to_canary("M-v2.0.0", comparison_stable=True)
        lc.to_production("M-v2.0.0", canary_pass=True,
                         online_within_thresholds=True,
                         checked_rules=["C1", "C2", "C3"])
        peer = lc.rollback("M-v2.0.0", trigger="drift-auto")
        self.assertEqual(peer["model_version"], "M-v1.0.0")
        self.assertEqual(peer["model_stage"], "production")
        self.assertEqual(lc.require("M-v2.0.0")["model_stage"], "deprecated")
        types = [e["type"] for e in s.where("audit_event")]
        self.assertIn("ModelRolledBack", types)
        ev = [e for e in s.where("audit_event")
              if e["type"] == "ModelRolledBack"][0]
        self.assertEqual(ev["payload"]["rolled_back_to"], "M-v1.0.0")
        self.assertEqual(ev["payload"]["trigger"], "drift-auto")
        # 回切后路由视图只剩一个 production
        self.assertEqual(len(lc.production_of("lgbm-reg")), 1)

    def test_rollback_without_peer_rejected(self):
        _, lc = fresh()
        register_ok(lc, "M-v1.0.0")
        lc.to_shadow("M-v1.0.0", artifact_ok=True, fingerprint_matches=True)
        lc.to_canary("M-v1.0.0", comparison_stable=True)
        lc.to_production("M-v1.0.0", canary_pass=True,
                         online_within_thresholds=True,
                         checked_rules=["C1", "C2", "C3"])
        with self.assertRaises(LifecycleError):
            lc.rollback("M-v1.0.0", trigger="drift-auto")


class TestAuditChainIntegrity(unittest.TestCase):
    def test_all_transitions_on_hash_chain(self):
        s, lc = fresh()
        register_ok(lc)
        lc.to_shadow("M-v1.0.0", artifact_ok=True, fingerprint_matches=True)
        lc.to_canary("M-v1.0.0", comparison_stable=True)
        lc.to_production("M-v1.0.0", canary_pass=True,
                         online_within_thresholds=True,
                         checked_rules=["C1", "C2", "C3"])
        lc.deprecate("M-v1.0.0", reason="drift-auto")
        ok, broken = s.verify_audit()
        self.assertTrue(ok, f"audit chain broken at {broken}")
        # aggregate_id = model_version（幂等键四元组的一部分）
        for e in s.where("audit_event"):
            if e["type"].startswith("Model"):
                self.assertEqual(e["payload"].get("model_version"), "M-v1.0.0")

    def test_lifecycle_events_match_contract_catalog(self):
        """补录的 6 事件 + ModelPromoted 与契约目录一一对应（单一事实来源）。"""
        import json
        cat_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "packages", "contracts", "events",
            "catalog.json")
        cat = json.load(open(cat_path, encoding="utf-8"))
        catalog_types = {e["type"] for e in cat["events"]}
        for t in ("ModelRegistered", "ModelGateFailed", "ModelShadowStarted",
                  "ModelCanaryStarted", "ModelDeprecated", "ModelRolledBack",
                  "ModelPromoted"):
            self.assertIn(t, catalog_types)


if __name__ == "__main__":
    unittest.main()
