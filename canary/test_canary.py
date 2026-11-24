# -*- coding: utf-8 -*-
"""金丝雀与语义断言验收测试（B06 DoD：故障拦截率 100%）。

运行：python canary/test_canary.py
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.replay import synthetic as world          # noqa: E402
from canary import assertions as A                   # noqa: E402
from canary.canary import (build_canary_set, set_fingerprint,  # noqa: E402
                           CANARY_VERSION)
from canary.faults import FAULTS                     # noqa: E402
from canary.runner import (build_trained_pipeline, run_canary,  # noqa: E402
                           run_all)


def _pred(**over) -> dict:
    """合法预测样例（契约 prediction-result 口径）。"""
    p = {"metric": "tg_like", "unit": "degC", "value": 85.0,
         "intervals": {"0.50": [83.0, 87.0], "0.70": [82.0, 88.0],
                       "0.90": [80.5, 89.5]},
         "ood_level": "none", "advisory_only": False}
    p.update(over)
    return p


_X = [1.0, 12.0, 40.0, 1.5, 155.0, 3.0, 120.0, 45.0]


class TestAssertions(unittest.TestCase):
    def test_valid_response_passes_all(self):
        self.assertEqual(A.run_assertions(_X, [_pred()], feasible=True), [])

    def test_unit_scale_caught_severe(self):
        v = A.run_assertions(_X, [_pred(value=850.0,
                                        intervals={"0.50": [830.0, 870.0],
                                                   "0.70": [820.0, 880.0],
                                                   "0.90": [805.0, 895.0]}),
                                _pred()], feasible=True)
        self.assertIn("UNIT-RANGE", {x["code"] for x in v})
        self.assertTrue(all(x["severity"] == "severe" for x in v
                            if x["code"] == "UNIT-RANGE"))

    def test_interval_inversion_caught(self):
        p = _pred(intervals={"0.50": [87.0, 83.0], "0.70": [82.0, 88.0],
                             "0.90": [80.5, 89.5]})
        codes = {x["code"] for x in A.run_assertions(_X, [p], feasible=True)}
        self.assertIn("INTERVAL-INVALID", codes)

    def test_interval_non_monotonic_caught(self):
        p = _pred(intervals={"0.50": [83.0, 87.0], "0.70": [84.0, 86.0],
                             "0.90": [80.5, 89.5]})
        codes = {x["code"] for x in A.run_assertions(_X, [p], feasible=True)}
        self.assertIn("INTERVAL-NON-MONOTONIC", codes)

    def test_ood_bypass_caught(self):
        p = _pred(ood_level="severe")
        codes = {x["code"] for x in A.run_assertions(_X, [p], feasible=True)}
        self.assertIn("OOD-BYPASS", codes)

    def test_negative_component_caught(self):
        codes = {x["code"] for x in
                 A.run_assertions([1.0, -5.0] + _X[2:], [_pred()],
                                  feasible=False)}
        self.assertIn("RATIO-NEGATIVE", codes)
        self.assertIn("PHYS-FEASIBILITY", codes)

    def test_unknown_unit_rejected(self):
        p = _pred(unit="celsius")
        codes = {x["code"] for x in A.run_assertions(_X, [p], feasible=True)}
        self.assertIn("UNIT-UNKNOWN", codes)

    def test_severe_refusal_is_clean(self):
        p = {"metric": "tg_like", "unit": "degC", "value": None,
             "intervals": None, "ood_level": "severe"}
        self.assertEqual(A.run_assertions(_X, [p], feasible=True), [])


class TestCanarySet(unittest.TestCase):
    def test_set_size_and_coverage(self):
        entries = build_canary_set()
        self.assertEqual(len(entries), 12)
        profiles = {e["profile"] for e in entries}
        for expect in ("narrow-ridge", "boundary-lower", "boundary-upper",
                       "humidity-degradation", "outside-ridge"):
            self.assertIn(expect, profiles)
        for e in entries:
            self.assertTrue(world.is_feasible(e["x"]), e["id"])
            self.assertEqual(len(e["reference"]), 2)

    def test_fingerprint_changes_with_content(self):
        fp1 = set_fingerprint()
        entries = build_canary_set()
        entries[0]["tolerance"]["tg_like"] += 0.1
        self.assertNotEqual(fp1, set_fingerprint(entries))

    def test_version_registered(self):
        self.assertTrue(CANARY_VERSION.startswith("CANARY-SET-"))


class TestAcceptance100Percent(unittest.TestCase):
    """B06 验收口径：对故意引入物理矛盾的输出 100% 拦截。"""

    @classmethod
    def setUpClass(cls):
        cls.results = run_all()

    def test_healthy_baseline_all_pass(self):
        h = self.results["healthy"]
        self.assertTrue(h["overall_pass"],
                        f"健康基线失败条目: {[r['id'] for r in h['rows'] if not r['ok']]}")
        self.assertEqual(h["failed"], 0)

    def test_all_faults_intercepted(self):
        self.assertEqual(self.results["interception_rate"], "8/8")
        for f in FAULTS:
            self.assertTrue(self.results["interception"][f["code"]], f["code"])

    def test_each_fault_detected_on_every_entry(self):
        """强口径：故障作用于每条金丝雀，每条都必须被拦截。"""
        for f in FAULTS:
            bad = [r["id"] for r in self.results[f["code"]]["rows"] if r["ok"]]
            self.assertEqual(bad, [], f"{f['code']} 未拦截条目: {bad}")

    def test_detector_attribution(self):
        """输出类故障至少一条被语义断言（而非仅偏差层）捕获；VALUE-DRIFT/
        DIRECTION-FLIP 由金丝雀偏差层捕获——两层互补，缺一不可。"""
        assertion_codes = {"UNIT-RANGE", "UNIT-UNKNOWN", "INTERVAL-INVALID",
                           "INTERVAL-NON-MONOTONIC", "INTERVAL-EXCLUDES-VALUE",
                           "OOD-BYPASS", "RATIO-NEGATIVE", "PHYS-FEASIBILITY"}
        for code in ("UNIT-SCALE", "INTERVAL-INVERSION",
                     "INTERVAL-NON-MONOTONIC", "OOD-BYPASS"):
            dets = set().union(*(r["detectors"]
                                 for r in self.results[code]["rows"]))
            self.assertTrue(dets & assertion_codes, code)
        for code in ("VALUE-DRIFT", "DIRECTION-FLIP"):
            dets = set().union(*(r["detectors"]
                                 for r in self.results[code]["rows"]))
            self.assertIn("CANARY-DEVIATION", dets, code)

    def test_audit_event_written(self):
        pipe = build_trained_pipeline()
        run_canary(pipe)
        events = [e for e in pipe.store.where("audit_event")
                  if e.get("type") == "CanaryRun"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["action"], "none")
        run_canary(pipe, fault=FAULTS[0])
        events = [e for e in pipe.store.where("audit_event")
                  if e.get("type") == "CanaryRun"]
        self.assertEqual(events[-1]["payload"]["action"],
                         "quarantine-recommended")


if __name__ == "__main__":
    unittest.main(verbosity=2)
