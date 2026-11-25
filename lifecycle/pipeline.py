# -*- coding: utf-8 -*-
"""lifecycle/pipeline.py —— 训练 → 门禁 → 注册 → 影子 → 灰度 → 晋级的编排器
（B09 §1 / §4 / §7「与既有资产的衔接」）。

把三块既有资产串成一条**可执行流水线**（每一步都有白名单迁移、门禁证据与
审计留痕，缺一不可推进——「宁可慢，不可带病晋级」）：

    SkeletonPipeline（训练/预测，B03+B07）
        → gates.run_register_gate（G0–G5 短路求值）
        → ModelLifecycleStore.register（T0：全过才入册，失败=不入册）
        → to_shadow（T1：artifact + 指纹守卫；影子观察量 ≥ 最低量才可推进）
        → to_canary（T2：影子对拍分布稳定；灰度阶梯 5%→25%→100%）
        → gates.run_canary_gate（C1–C3；C1 单一来源 = B06 run_canary）
        → to_production（T3）→ rollback（路由回切，B09 §2.4）

**骨架替身口径（如实记录，生产替换、接口不变）**：

- artifact 校验和 = 模型评估指纹（版本 + RMSE + 特征指纹 + 快照）的
  sha256，非真实模型字节流哈希（生产：MinIO artifact 下载后哈希比对）；
- 影子观察量 = ``prediction_log`` 中该版本已记录预测条数
  （生产：影子路由器按最低观察量/回写轮数节拍推进，§17.3）；
- 灰度流量 = 比例参数 + 判定函数（``CANARY_LADDER`` 5%→25%→100%），
  生产为网关按请求哈希分流；
- 晋级后 ``metrics_json`` 落 RMSE 与 canary 证据，作为下一版本的
  G2 基线与 C2 对照来源（跨代际门禁可比性）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skeleton.store import SkeletonStore                 # noqa: E402  (类型标注)
from skeleton.pipeline import METRICS                   # noqa: E402
from lifecycle.store import ModelLifecycleStore         # noqa: E402
from lifecycle.gates import (                           # noqa: E402
    GateResult, median_shift_sigma, run_canary_gate, run_register_gate,
)

# B09 §4 灰度阶梯与最低观察量（骨架替身；回写轮数节拍生产替换）
CANARY_LADDER = (5, 25, 100)
MIN_SHADOW_REQUESTS = 30

SOURCE = "model-lifecycle-svc"


class OrchestrationError(Exception):
    """编排层守卫违规（观察量不足、阶段错位等）。状态未被变更。"""


class LifecyclePipeline:
    """单代际候选模型的编排视图：一个 SkeletonPipeline + 生命周期注册表。

    跨代际：上一代 ``LifecyclePipeline`` 晋级后，其 RMSE 基线与管线引用
    通过 ``baseline_rmse`` / ``pipe`` 传给下一代构造参数（G2 / C2 的
    对照来源），保证固定评估集上的跨版本可比性。
    """

    def __init__(self, pipe, *, model_type: str = "lgbm-reg",
                 caliber: str = "skeleton-abs",
                 baseline_rmse: dict | None = None,
                 prev_pipe=None):
        self.pipe = pipe
        self.lc = ModelLifecycleStore(pipe.store)
        self.model_type = model_type
        self.caliber = caliber
        self.baseline_rmse = baseline_rmse    # 上一 production 版本 RMSE（G2）
        self.prev_pipe = prev_pipe            # 上一 production 管线（C2）
        self.version: str | None = None       # 本代候选的生命周期版本号

    # ------------------------------------------------------------- T0 训练+门禁+注册

    def train_and_register(self, *, model_version: str | None = None) -> dict:
        """G0–G5 门禁 → T0 注册（全过才入册，失败=不入册）。

        返回 ``{"registered": bool, "gate_report": GateResult,
        "model_version": str}``；失败时旧版本继续服务（§5.5）。
        """
        if self.version is not None:
            raise OrchestrationError(
                f"{self.version} already orchestrated in this generation")
        version = model_version or self._next_version()
        report = run_register_gate(self.pipe, prev_rmse=self.baseline_rmse,
                                   caliber=self.caliber)
        # T0 统一走状态机：全过→入册+ModelRegistered；任一失败→
        # 不入册+ModelGateFailed 留痕（§5.5），旧版本继续服务
        m = report["metrics"]
        row = self.lc.register(
            model_version=version, model_type=self.model_type,
            artifact_uri=f"minio://artifacts/{version}.pkl",
            artifact_checksum=self._artifact_fingerprint(version, m),
            feature_set_version=m.get("feature_set_version"),
            feature_fingerprint=m.get("feature_fingerprint"),
            dataset_snapshot_id=m.get("dataset_snapshot_id"),
            gate_report={k: report[k]
                         for k in ("passed", "checked_rules", "results")})
        if row is not None:
            # 门禁指标落 metrics_json（artifact 复算 + 模型卡来源）
            self.pipe.store.update("model_registry", row["id"],
                                   {"metrics_json": {"gate": m}})
            # 服务身份切到生命周期版本号（provenance / prediction_log 可反查）
            self.pipe.model_version = version
            self.version = version
        return {"registered": report["passed"], "gate_report": report,
                "model_version": version}

    # ------------------------------------------------------------- T1 影子

    def start_shadow(self) -> dict:
        """T1：registered → shadow。守卫：artifact 校验和通过、特征指纹
        与 G0 记录一致（B09 §2.3）——两项均由本层计算后传入状态机。"""
        self._require_stage("registered")
        row = self.lc.require(self.version)
        # T1 守卫①：校验和复算（生产：MinIO 字节流哈希；替身：指标指纹）
        artifact_ok = self._recompute_checksum() == row["artifact_checksum"]
        # T1 守卫②：特征指纹与 G0 记录一致（B07 工厂当前指纹）
        fingerprint_matches = (self.pipe.feature_factory is not None
                               and self.pipe.feature_factory.fingerprint()
                               == row["feature_fingerprint"])
        return self.lc.to_shadow(self.version, artifact_ok=artifact_ok,
                                 fingerprint_matches=fingerprint_matches)

    def shadow_serve(self, xs: list[list[float]]) -> list[dict]:
        """影子流量替身：只记录不返回（预测落 ``prediction_log``）。"""
        self._require_stage("shadow")
        return [self.pipe.step5_predict(list(x)) for x in xs]

    def shadow_observations(self) -> int:
        return len(self.pipe.store.where("prediction_log",
                                         model_version=self.version))

    # ------------------------------------------------------------- T2 灰度

    def start_canary(self, *, traffic_percent: int = CANARY_LADDER[0]) -> dict:
        """T2：shadow → canary。守卫：影子观察量 ≥ 最低量（§4：观察量
        不足不得推进）+ 影子对拍分布稳定（与上一 production 管线对照）。"""
        self._require_stage("shadow")
        n = self.shadow_observations()
        if n < MIN_SHADOW_REQUESTS:
            raise OrchestrationError(
                f"shadow observations {n} < {MIN_SHADOW_REQUESTS} "
                f"（最低观察量，§4：宁可慢不可带病晋级）")
        if self.prev_pipe is None:
            stable = True                    # 首版本无双版本对照
        else:
            sigma = median_shift_sigma(self.pipe, self.prev_pipe)
            stable = sigma < 3.0             # C2 同源阈值（gates.THRESHOLDS）
        return self.lc.to_canary(self.version,
                                 comparison_stable=stable,
                                 traffic_percent=traffic_percent)

    def canary_serve(self, xs: list[list[float]], *, percent: int) -> list[dict]:
        """灰度替身：``percent``% 流量给候选版本（判定函数 = 序号取模），
        其余回切上一 production 管线。返回逐请求实际服务版本。"""
        self._require_stage("canary")
        if percent not in CANARY_LADDER:
            raise OrchestrationError(
                f"traffic {percent}% not on ladder {CANARY_LADDER}")
        served = []
        for i, x in enumerate(xs):
            if i * 100 < percent * len(xs):          # 比例判定替身
                served.append((self.pipe.step5_predict(list(x)),
                               self.version))
            elif self.prev_pipe is not None:
                served.append((self.prev_pipe.step5_predict(list(x)),
                               self.prev_pipe.model_version))
            else:
                served.append((self.pipe.step5_predict(list(x)),
                               self.version))
        return served

    # ------------------------------------------------------------- T3 晋级

    def promote(self) -> dict:
        """T3：canary → production。C1–C3 门禁（C1 = B06 金丝雀单一来源）
        全过才晋级；失败停在 canary（金丝雀失败已由 B06 写
        quarantine-recommended），路由不受影响。"""
        self._require_stage("canary")
        report = run_canary_gate(self.pipe, prev_pipe=self.prev_pipe,
                                 caliber=self.caliber)
        if not report["passed"]:
            return {"promoted": False, "gate_report": report}
        c3 = next((r for r in report["results"] if r["rule_id"] == "C3"), None)
        online_ok = c3 is not None and c3["status"] == "PASS"
        row = self.lc.to_production(self.version, canary_pass=True,
                                    online_within_thresholds=online_ok,
                                    checked_rules=report["checked_rules"])
        # metrics_json 合并落盘：下一版本的 G2 基线 + 模型卡 canary 证据
        rmse = report["metrics"].get("rmse",
                                     {c: self.pipe._eval_rmse(c)
                                      for c in METRICS})
        self.pipe.store.update("model_registry", row["id"], {
            "metrics_json": {**row.get("metrics_json", {}),
                             "rmse": rmse,
                             "caliber": self.caliber,
                             "canary": report["metrics"].get("canary", {}),
                             "canary_gate": report["results"]}})
        return {"promoted": True, "gate_report": report,
                "model_version": self.version}

    # ------------------------------------------------------------- 运维面

    def route(self) -> list[dict]:
        """路由视图：当前承接流量的 production 版本（1–2 个并行）。"""
        return self.lc.production_of(self.model_type)

    def latest_production_rmse(self) -> dict | None:
        """最近晋级的 production 版本 RMSE（下一代 ``baseline_rmse`` 来源）。"""
        prods = self.route()
        if not prods:
            return None
        row = sorted(prods, key=lambda r: r.get("created_at", ""))[-1]
        return (row.get("metrics_json") or {}).get("rmse")

    def rollback(self, *, trigger: str) -> dict:
        """回滚 = 路由回切非状态迁移（B09 §2.4），委托状态机执行。"""
        return self.lc.rollback(self.version, trigger=trigger)

    # ------------------------------------------------------------- 内部

    def _require_stage(self, stage: str) -> None:
        if self.version is None:
            raise OrchestrationError("candidate not registered yet "
                                     "(run train_and_register first)")
        row = self.lc.require(self.version)
        if row["model_stage"] != stage:
            raise OrchestrationError(
                f"{self.version} at {row['model_stage']}, expected {stage}")

    def _next_version(self) -> str:
        reg = self.pipe.store.where("model_registry")
        n = sum(1 for r in reg if "model_version" in r)
        return f"MV-{n + 1:03d}"

    def _recompute_checksum(self) -> str:
        row = self.lc.require(self.version)
        m = row.get("metrics_json", {}).get("gate", {})
        return self._artifact_fingerprint(self.version, m)

    @staticmethod
    def _artifact_fingerprint(version: str, metrics: dict) -> str:
        """artifact 校验和替身（生产：MinIO 字节流 sha256）。"""
        basis = json.dumps({"version": version, "metrics": metrics},
                           sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(basis.encode()).hexdigest()
