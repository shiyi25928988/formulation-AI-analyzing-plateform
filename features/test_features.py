# -*- coding: utf-8 -*-
"""版本化特征工厂验收测试（B07 DoD）。

运行：python features/test_features.py
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from features import factory as F                    # noqa: E402
from canary.runner import build_trained_pipeline     # noqa: E402
from skeleton.pipeline import SkeletonPipeline       # noqa: E402
from skeleton.store import SkeletonStore             # noqa: E402


def _draft_v11():
    """FS-v1.1 草案：新增派生特征 ca_per_eq（固化剂 phr / 当量比）。"""
    defs = [dict(d) for d in F.FEATURE_DEFS_V1]
    defs.append({"name": "ca_per_eq", "kind": "derived", "fn": "ca_per_eq",
                 "unit": "phr/eq", "description": "派生：固化剂 phr / 当量配比"})
    return F.draft("FS-v1.1", defs)


class TestFactory(unittest.TestCase):
    def test_fingerprint_stable(self):
        f1 = F.get_approved()
        f2 = F.draft("FS-v1.0", F.FEATURE_DEFS_V1)
        self.assertEqual(f1.fingerprint(), f2.fingerprint())

    def test_fingerprint_changes_with_definition(self):
        f1, d = F.get_approved(), _draft_v11()
        self.assertNotEqual(f1.fingerprint(), d.fingerprint())

    def test_version_matches_contract_pattern(self):
        import re
        self.assertRegex(F.CURRENT_VERSION, r"^FS-v[0-9]+\.[0-9]+$")

    def test_transform_identity_v1(self):
        x = [1.0, 12.0, 40.0, 1.5, 155.0, 3.0, 120.0, 45.0]
        self.assertEqual(F.get_approved().transform(x), x)

    def test_transform_derived(self):
        out = _draft_v11().transform([1.0, 12.0, 40.0, 1.5, 155, 3, 120, 45])
        self.assertEqual(len(out), 9)
        self.assertAlmostEqual(out[8], 12.0, places=6)

    def test_dimension_mismatch_raises(self):
        with self.assertRaises(F.FeatureMismatchError):
            F.get_approved().transform([1.0, 2.0, 3.0])

    def test_draft_not_usable(self):
        with self.assertRaises(F.FeatureNotApprovedError):
            F.get_approved("FS-v1.1")


class TestSingleSource(unittest.TestCase):
    """DoD：骨架训练与金丝雀预测引用同一 feature_set 实现。"""

    @classmethod
    def setUpClass(cls):
        cls.pipe = build_trained_pipeline()

    def test_pipeline_uses_factory(self):
        self.assertIs(self.pipe.feature_factory, F.get_approved())
        self.assertEqual(self.pipe.feature_set_version, F.CURRENT_VERSION)

    def test_provenance_carries_factory_version(self):
        resp = self.pipe.step5_predict([1.2, 18, 40, 1.5, 155, 4, 110, 45])
        self.assertEqual(resp["provenance"]["feature_set_version"],
                         F.CURRENT_VERSION)

    def test_registry_row_has_fingerprint(self):
        rows = self.pipe.store.where("feature_set")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fingerprint"], F.get_approved().fingerprint())
        self.assertEqual(rows[0]["version"], F.CURRENT_VERSION)

    def test_registration_audited(self):
        evs = [e for e in self.pipe.store.where("audit_event")
               if e.get("type") == "FeatureSetRegistered"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["payload"]["feature_set_version"],
                         F.CURRENT_VERSION)

    def test_reproducible_across_pipelines(self):
        store = SkeletonStore()
        p2 = SkeletonPipeline(store, seed=99)
        p2.step1_import([]) if hasattr(p2, "step1_import") else None
        p2.step3_feature_set()
        self.assertEqual(p2.feature_set_version, self.pipe.feature_set_version)

    def test_approved_change_rolls_new_version(self):
        """变更走审批：draft 不可用 → approve 后新版本可用，旧版本指纹不变。"""
        old_fp = F.get_approved().fingerprint()
        with self.assertRaises(F.FeatureNotApprovedError):
            F.get_approved("FS-v1.1")
        F.approve(_draft_v11())
        f11 = F.get_approved("FS-v1.1")
        self.assertNotEqual(f11.fingerprint(), old_fp)
        self.assertEqual(F.get_approved().fingerprint(), old_fp)  # 旧版不受影响
        # 收尾：移除测试注册，避免污染其他用例
        del F._APPROVED["FS-v1.1"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
