#!/usr/bin/env python3
"""契约包测试（零第三方依赖）：用最小 JSON Schema 校验器走样例 payload。

覆盖：§4.3 canonical 预测样例、severe OOD 拒答红线、advisory 标记红线、
三档区间数学自洽样例、事件信封与 payload、任务视图、错误 Problem Details、
非法样例必须被拒绝（防契约失效）。

运行：python packages/contracts/tools/test_contracts.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from validate import RefResolver, Violations, validate_schema_instance  # noqa: E402

CONTRACTS = TOOLS.parent
SCHEMAS = CONTRACTS / "schemas"
EVENTS = CONTRACTS / "events"

UUID = "1b2f9a6c-4d3e-4a5b-8c7d-0e1f2a3b4c5d"
TS = "2026-11-24T05:00:00Z"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(schema_doc: dict, schema_dir: Path, schema: dict, instance: object) -> list[str]:
    v = Violations()
    resolver = RefResolver(schema_doc, schema_dir)
    validate_schema_instance(schema, instance, resolver, "root", v)
    return v.items


def schema_of(doc: dict, dir_path: Path, ref: str) -> tuple[dict, dict]:
    resolver = RefResolver(doc, dir_path)
    return doc, resolver.resolve(ref)


class TestPredictionContract(unittest.TestCase):
    """§4.3 canonical 样例与 OOD 红线。"""

    PRED = load(SCHEMAS / "prediction-result.schema.json")
    PROV = load(SCHEMAS / "provenance.schema.json")
    COMMON = load(SCHEMAS / "common.schema.json")

    def interval(self, confidence: float) -> dict:
        lo, hi = {0.5: (2.25, 2.75), 0.7: (2.1, 2.9), 0.9: (1.95, 3.05)}[confidence]
        return {"confidence": confidence, "p_low": lo, "p_high": hi, "calibrated": True}

    def provenance(self) -> dict:
        return {
            "model_version": "M-v1.0.0",
            "feature_set_version": "FS-v1.0",
            "dataset_snapshot_id": "SNP-20261124-1a2b3c4d",
            "served_at": TS,
        }

    def result_ok(self) -> dict:
        return {
            "metric": "tg_like",
            "unit": "degC",
            "value": 96.2,
            "ood_level": "none",
            "intervals": [self.interval(c) for c in (0.5, 0.7, 0.9)],
            "explanation": {"method": "shap-group", "top_factors": [
                {"feature": "curing_agent_phr", "contribution": 3.1}],
                "status": "inline"},
        }

    def test_canonical_result_passes(self):
        errs = validate(self.PRED, SCHEMAS, self.PRED, self.result_ok())
        self.assertEqual(errs, [])

    def test_severe_ood_must_not_return_value(self):
        """红线：severe 拒答不返回数值（§4.3）。由语义条件校验强制。"""
        from validate import semantic_check_prediction
        bad = self.result_ok() | {"ood_level": "severe"}
        errs = semantic_check_prediction(bad)
        self.assertTrue(any("severe" in e for e in errs), "severe 携带数值必须被拦截")
        good_severe = {"metric": "tg_like", "unit": "degC", "ood_level": "severe"}
        self.assertEqual(semantic_check_prediction(good_severe), [])

    def test_advisory_requires_advisory_only_flag(self):
        """红线：advisory 必须强制 advisory_only 标记。"""
        from validate import semantic_check_prediction
        missing = self.result_ok() | {"ood_level": "advisory"}
        errs = semantic_check_prediction(missing)
        self.assertTrue(any("advisory_only" in e for e in errs))
        ok = self.result_ok() | {"ood_level": "advisory", "advisory_only": True}
        self.assertEqual(semantic_check_prediction(ok), [])

    def test_value_requires_three_intervals(self):
        from validate import semantic_check_prediction
        bad = self.result_ok()
        bad["intervals"] = [self.interval(0.9)]
        errs = semantic_check_prediction(bad)
        self.assertTrue(any("三档" in e for e in errs))

    def test_intervals_must_be_exactly_three_levels(self):
        bad = self.result_ok()
        bad["intervals"] = [self.interval(0.9)]
        errs = validate(self.PRED, SCHEMAS, self.PRED, bad)
        self.assertTrue(any("minItems" in e or "数组元素" in e for e in errs))

    def test_noncalibrated_confidence_rejected(self):
        bad = self.result_ok()
        bad["intervals"][0]["confidence"] = 0.95
        errs = validate(self.PRED, SCHEMAS, self.PRED, bad)
        self.assertTrue(any("枚举" in e for e in errs))

    def test_provenance_triple_required(self):
        missing = {"model_version": "M-v1.0.0", "served_at": TS}
        errs = validate(self.PROV, SCHEMAS, self.PROV, missing)
        self.assertTrue(any("feature_set_version" in e for e in errs))
        self.assertTrue(any("dataset_snapshot_id" in e for e in errs))


class TestEnvelopeContract(unittest.TestCase):
    """§16.4 信封与事件 payload。"""

    ENV = load(SCHEMAS / "envelope.schema.json")
    EV = load(EVENTS / "events.schema.json")
    CATALOG = load(EVENTS / "catalog.json")

    def envelope(self, etype: str, data: dict) -> dict:
        return {
            "id": UUID, "source": "experiment-svc", "type": etype,
            "spec_version": "1.0", "time": TS, "tenant_id": "T-0001",
            "correlation_id": UUID,
            "idempotency_key": "T-0001:ExperimentCompleted:EXP-1:v1",
            "schema_version": "1.0", "data": data,
        }

    def test_experiment_completed_envelope_passes(self):
        errs = validate(self.ENV, SCHEMAS, self.ENV,
                        self.envelope("ExperimentCompleted", {
                            "experiment_id": "EXP-1",
                            "formula_version_id": "FV-1",
                            "completed_at": TS,
                        }))
        self.assertEqual(errs, [])

    def test_short_idempotency_key_rejected(self):
        env = self.envelope("ExperimentCompleted", {
            "experiment_id": "EXP-1", "formula_version_id": "FV-1", "completed_at": TS})
        env["idempotency_key"] = "short"
        errs = validate(self.ENV, SCHEMAS, self.ENV, env)
        self.assertTrue(any("长度" in e for e in errs))

    def test_spec_version_is_pinned(self):
        env = self.envelope("ExperimentCompleted", {
            "experiment_id": "EXP-1", "formula_version_id": "FV-1", "completed_at": TS})
        env["spec_version"] = "2.0"
        errs = validate(self.ENV, SCHEMAS, self.ENV, env)
        self.assertTrue(any("const" in e for e in errs))

    def test_catalog_covers_every_event_with_payload(self):
        types = [e["type"] for e in self.CATALOG["events"]]
        defs = set(self.EV["$defs"])
        self.assertEqual(set(types), defs, "目录与 $defs 双向覆盖")
        self.assertIn("RecommendationIssued", types)

    def test_recommendation_issued_payload_passes(self):
        schema = self.EV["$defs"]["RecommendationIssued"]
        payload = {
            "recommendation_batch_id": "REC-1",
            "candidates": [{"candidate_id": "C-0006", "x": [0.1, 8, 40, 0.02, 150, 240, 1, 45],
                            "expected_gain": 1.7}],
            "strategy": "active_ucb",
            "model_version": "M-v1.0.0",
        }
        errs = validate(self.EV, EVENTS, schema, payload)
        self.assertEqual(errs, [])


class TestTaskAndErrorContract(unittest.TestCase):
    TASK = load(SCHEMAS / "task.schema.json")
    ERR = load(SCHEMAS / "error.schema.json")

    def test_task_view_passes(self):
        task = {
            "task_id": "TASK-20261124-1a2b3c4d", "status": "queued", "queue": "batch",
            "created_at": TS,
        }
        self.assertEqual(validate(self.TASK, SCHEMAS, self.TASK, task), [])

    def test_invalid_task_status_rejected(self):
        task = {"task_id": "TASK-20261124-1a2b3c4d", "status": "pending", "queue": "batch",
                "created_at": TS}
        errs = validate(self.TASK, SCHEMAS, self.TASK, task)
        self.assertTrue(any("枚举" in e for e in errs))

    def test_invalid_queue_rejected(self):
        task = {"task_id": "TASK-20261124-1a2b3c4d", "status": "queued", "queue": "default",
                "created_at": TS}
        errs = validate(self.TASK, SCHEMAS, self.TASK, task)
        self.assertTrue(any("枚举" in e for e in errs))

    def test_problem_details_passes(self):
        err = {
            "type": "/errors/invalid-transition", "title": "Invalid transition",
            "status": 409, "detail": "RECOMMENDED → ACCEPTED is not allowed",
            "instance": "/api/v1/experiment/experiments/EXP-1/transitions",
            "trace_id": UUID,
        }
        self.assertEqual(validate(self.ERR, SCHEMAS, self.ERR, err), [])


class TestOpenApiStructures(unittest.TestCase):
    """OpenAPI 层：九服务齐全、POST 幂等头、错误统一 Problem Details。"""

    def test_nine_services_and_idempotency_headers(self):
        import re as _re
        spec_dir = CONTRACTS / "openapi"
        names = {p.stem for p in spec_dir.glob("*.json")}
        expected = {"prediction-svc", "optimization-svc", "active-learning-svc",
                    "generation-svc", "experiment-svc", "data-hub-svc",
                    "knowledge-svc", "auth-audit-svc", "model-lifecycle-svc"}
        self.assertEqual(names, expected)
        for path in sorted(spec_dir.glob("*.json")):
            raw = path.read_text(encoding="utf-8")
            self.assertIn('"Idempotency-Key"', raw, f"{path.name}: 缺幂等头")
            self.assertIn('"X-Tenant-Id"', raw, f"{path.name}: 缺租户头")
            self.assertIn('"3.1.0"', raw, f"{path.name}: 非 OpenAPI 3.1")
            if path.stem != "knowledge-svc":
                self.assertNotIn('"501"', raw.split('"info"')[0])

    def test_explain_deferral_documented(self):
        raw = (CONTRACTS / "openapi" / "prediction-svc.json").read_text(encoding="utf-8")
        self.assertIn("deferred", raw, "§15.2 解释可降级口径须写入契约")
        self.assertIn("2s P95", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
