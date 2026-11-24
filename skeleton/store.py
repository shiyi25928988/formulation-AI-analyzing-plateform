# -*- coding: utf-8 -*-
"""内存版领域存储：镜像 001_init_schema.sql（B01）的关键不变量。

镜像范围（walking skeleton 够用的最小表集）：
  tenant / project / metric_definition / formula / formula_version /
  formula_component / process_condition / dataset_snapshot / feature_set /
  model_registry / prediction_log / recommendation / experiment /
  test_result / audit_event

镜像的不变量（与 DDL / 架构条款一一对应）：
  1. 追加写：formula_version / audit_event（DDL 触发器强制）+
     prediction_log / dataset_snapshot（§3.5 语义，快照与预测日志不可变）
  2. 实验状态机：九态白名单迁移（app.experiment_transition_guard 的镜像）；
     起始必须 RECOMMENDED；QC 不过 → REJECTED 进失败样本库，不回退（§6.1）
  3. 审计哈希链：audit_event 逐条 sha256(prev_hash+payload) 链接，
     可反查（§18.2 第 10 步）、可发现篡改（防静默失败第三层）
  4. 事件信封：至少一次 + 幂等——event_id 去重（§16.4）
  5. 租户上下文：所有行强制 tenant_id，跨租户读写直接拒绝
     （RLS 的内存替身；生产实现仍以 db/migrations 为准）

简化声明：本模块只验证「链路语义正确」，不验证 SQL / 索引 / 并发 / 性能；
存储接口与表名对齐 B01，生产替换存储实现时链路代码不动（§18.1 依赖方向）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# ---- 枚举与白名单（与 001_init_schema.sql / 架构 §6.1 对齐） -----------------

EXPERIMENT_STATES = [
    "RECOMMENDED", "REVIEWED", "SCHEDULED", "IN_PROGRESS", "COMPLETED",
    "QC_PENDING", "ACCEPTED", "REJECTED", "CANCELLED",
]

# app.experiment_transition_guard 的镜像（逐条对应，QC 不过不回退）
ALLOWED_TRANSITIONS = {
    ("RECOMMENDED", "REVIEWED"), ("RECOMMENDED", "CANCELLED"),
    ("REVIEWED", "SCHEDULED"), ("REVIEWED", "CANCELLED"),
    ("SCHEDULED", "IN_PROGRESS"), ("SCHEDULED", "CANCELLED"),
    ("IN_PROGRESS", "COMPLETED"), ("IN_PROGRESS", "CANCELLED"),
    ("COMPLETED", "QC_PENDING"), ("COMPLETED", "CANCELLED"),
    ("QC_PENDING", "ACCEPTED"), ("QC_PENDING", "REJECTED"),
}

APPEND_ONLY_TABLES = {
    "formula_version", "audit_event",           # DDL 触发器强制
    "prediction_log", "dataset_snapshot",       # 架构 §3.5 语义
}

INTERVAL_LEVELS = ("0.50", "0.70", "0.90")      # §5.2 三档，仅此三档
OOD_LEVELS = ("none", "advisory", "severe")     # 评审定版：两级处置


class StoreError(Exception):
    pass


class AppendOnlyError(StoreError):
    pass


class IllegalTransitionError(StoreError):
    pass


class CrossTenantError(StoreError):
    pass


class DuplicateEventError(StoreError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rid(prefix: str) -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class SkeletonStore:
    """单租户上下文的内存存储；所有读写经此入口以强制不变量。"""

    def __init__(self, tenant_id: str = "T-DEMO", project_id: str = "P-DEMO"):
        self.tables: dict[str, list[dict]] = {}
        self.tenant_id = tenant_id
        self.project_id = project_id
        self._event_ids: set[str] = set()
        self._seq = 0
        self._prev_hash = "GENESIS"
        # 初始化租户与项目（RLS 上下文锚点）
        self.insert("tenant", {"id": tenant_id, "name": "demo-tenant",
                               "data_level": "L3"})
        self.insert("project", {"id": project_id, "tenant_id": tenant_id,
                                "name": "demo-project"})

    # ---- 基础 CRUD（含不变量检查） ----------------------------------------

    def insert(self, table: str, row: dict) -> dict:
        row = dict(row)
        row.setdefault("id", _rid(table[:4].upper()))
        row.setdefault("created_at", _now())
        tid = row.get("tenant_id", self.tenant_id)
        if tid != self.tenant_id:
            raise CrossTenantError(
                f"cross-tenant write denied: row.tenant_id={tid!r} "
                f"context={self.tenant_id!r}")
        if table == "experiment":
            if row.get("state", "RECOMMENDED") != "RECOMMENDED":
                raise IllegalTransitionError("experiment must start at RECOMMENDED")
            row["state"] = "RECOMMENDED"
        self.tables.setdefault(table, []).append(row)
        return row

    def update(self, table: str, row_id: str, patch: dict) -> dict:
        if table in APPEND_ONLY_TABLES:
            raise AppendOnlyError(
                f"append-only table {table}: UPDATE not allowed "
                f"(mirror of DDL trigger)")
        row = self.get(table, row_id)
        if row is None:
            raise StoreError(f"{table}/{row_id} not found")
        if table == "experiment" and "state" in patch:
            old, new = row["state"], patch["state"]
            if old != new and (old, new) not in ALLOWED_TRANSITIONS:
                raise IllegalTransitionError(
                    f"illegal experiment transition {old} -> {new}")
            patch = {**patch, "updated_at": _now()}
        row.update(patch)
        return row

    def get(self, table: str, row_id: str) -> dict | None:
        for row in self.tables.get(table, []):
            if row["id"] == row_id:
                return row
        return None

    def where(self, table: str, **match) -> list[dict]:
        out = []
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in match.items()):
                out.append(row)
        return out

    # ---- 事件信封 + 审计哈希链（§16.4 + §18.2 第 10 步） -------------------

    def emit(self, source: str, event_type: str, payload: dict,
             event_id: str | None = None,
             aggregate_id: str | None = None) -> dict:
        """发布事件并写入审计哈希链；event_id 幂等（至少一次投递的重放安全）。

        ``aggregate_id`` 为幂等键四元组 (tenant_id, event_type,
        aggregate_id, aggregate_version) 的聚合标识（§16.4）；
        B09 生命周期事件以 model_version 聚合。
        """
        event_id = event_id or _rid("EVT")
        if event_id in self._event_ids:
            raise DuplicateEventError(f"duplicate event_id {event_id}")
        self._event_ids.add(event_id)
        self._seq += 1
        ts = _now()
        pre = json.dumps({"seq": self._seq, "ts": ts, "source": source,
                          "type": event_type, "payload": payload,
                          "prev": self._prev_hash}, sort_keys=True,
                         ensure_ascii=False)
        h = hashlib.sha256(pre.encode("utf-8")).hexdigest()
        row = self.insert("audit_event", {
            "id": event_id, "seq": self._seq, "ts": ts, "source": source,
            "type": event_type, "payload": payload,
            "aggregate_id": aggregate_id,
            "prev_hash": self._prev_hash, "hash": h,
        })
        self._prev_hash = h
        return row

    def verify_audit(self) -> tuple[bool, int | None]:
        """逐条重算哈希链；返回 (是否完整, 首个断裂序号)。"""
        prev = "GENESIS"
        for ev in self.tables.get("audit_event", []):
            pre = json.dumps({"seq": ev["seq"], "ts": ev["ts"],
                              "source": ev["source"], "type": ev["type"],
                              "payload": ev["payload"], "prev": prev},
                             sort_keys=True, ensure_ascii=False)
            if hashlib.sha256(pre.encode("utf-8")).hexdigest() != ev["hash"]:
                return False, ev["seq"]
            if ev["prev_hash"] != prev:
                return False, ev["seq"]
            prev = ev["hash"]
        return True, None

    def trace(self, correlation_id: str) -> list[dict]:
        """审计反查：按关联 id（推荐 / 实验 / 模型版本）拉出完整事件时间线。"""
        out = []
        for ev in self.tables.get("audit_event", []):
            blob = json.dumps(ev["payload"], ensure_ascii=False)
            if correlation_id in blob or ev["id"] == correlation_id:
                out.append(ev)
        return out
