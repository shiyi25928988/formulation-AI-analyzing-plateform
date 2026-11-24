# -*- coding: utf-8 -*-
"""版本化特征工厂（B07 / §5.1 / ADR-04）——特征定义的单一事实来源。

解决 §16.2 静默失败之首「training-serving skew」：训练与服务共用
同一特征实现（本模块），特征定义（名称 / 来源 / 变换 / 单位）版本化，
任何定义变更都会改变内容指纹并需审批注册后方可使用。

口径：
  - 版本号 FS-vX.Y，对齐 B04 契约 common.FeatureSetVersion pattern；
  - 指纹 = 定义的规范化 JSON 的 sha256（确定性、可复现）；
  - 变换以「命名派生函数注册表」承载，定义本身可序列化（可入审计）；
  - draft（未注册）工厂不可用于训练与服务——变更走审批（§18.1）；
  - FS-v1.0 为恒等变换（as-recorded），与 B01/B03 已验证口径一致。
"""
from __future__ import annotations
import hashlib
import json

# ---- FS-v1.0：恒等变换（as-recorded，与 B01 DDL / B03 骨架口径一致）--------

FEATURE_DEFS_V1: list[dict] = [
    {"name": "resin_eq_ratio", "kind": "identity", "index": 0,
     "unit": "normalized", "description": "树脂当量配比（归一）"},
    {"name": "curing_agent_phr", "kind": "identity", "index": 1,
     "unit": "phr", "description": "固化剂 phr"},
    {"name": "filler_phr", "kind": "identity", "index": 2,
     "unit": "phr", "description": "填料 phr"},
    {"name": "dispersant_phr", "kind": "identity", "index": 3,
     "unit": "phr", "description": "分散剂 phr"},
    {"name": "cure_temp_c", "kind": "identity", "index": 4,
     "unit": "degC", "description": "固化温度 °C"},
    {"name": "cure_time_h", "kind": "identity", "index": 5,
     "unit": "h", "description": "固化时间 h"},
    {"name": "post_cure_c", "kind": "identity", "index": 6,
     "unit": "degC", "description": "后固化温度 °C"},
    {"name": "test_rh_pct", "kind": "identity", "index": 7,
     "unit": "%", "description": "测试相对湿度 %（§3.3：测试条件是特征不是元数据）"},
]

CURRENT_VERSION = "FS-v1.0"


class FeatureMismatchError(ValueError):
    """输入维度与特征定义不符（训练/服务不一致的即时暴露）。"""


class FeatureNotApprovedError(KeyError):
    """特征集未注册（draft）——变更须先走审批（§18.1）。"""


class FeatureFactory:
    """一个不可变特征集定义：版本 + 定义 + 指纹 + 统一变换入口。"""

    def __init__(self, version: str, definitions: list[dict]):
        self.version = version
        self.definitions = [dict(d) for d in definitions]

    # ---- 指纹（内容寻址，定义变更即变） ------------------------------------
    def fingerprint(self) -> str:
        payload = json.dumps({"version": self.version,
                              "definitions": self.definitions},
                             sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def feature_names(self) -> list[str]:
        return [d["name"] for d in self.definitions]

    def describe(self) -> dict:
        return {"version": self.version, "fingerprint": self.fingerprint(),
                "n_features": len(self.definitions),
                "definitions": self.definitions}

    # ---- 统一变换入口：训练与服务共用（ADR-04 防漂移的关键） ---------------
    def transform(self, x: list[float]) -> list[float]:
        # 输入维度 = 原始特征数（identity 的最大 index+1）；
        # 派生特征在原始输入之上计算，输出维度可以更多
        raw_len = max((d["index"] + 1 for d in self.definitions
                       if d["kind"] == "identity"), default=0)
        if x is None or len(x) != raw_len:
            raise FeatureMismatchError(
                f"输入维度 {len(x) if x else 0} ≠ 原始特征数 "
                f"{raw_len}（{self.version}）")
        out: list[float] = []
        for d in self.definitions:
            if d["kind"] == "identity":
                out.append(x[d["index"]])
            else:
                fn = DERIVED_FNS[d["fn"]]        # 未登记派生函数即失败
                out.append(round(fn(x), 6))
        return out


# ---- 派生函数注册表（按名引用，保证定义可序列化、可审计） -------------------

DERIVED_FNS: dict[str, object] = {
    # 示例（FS-v1.1 草案演示用）：固化剂 phr / 当量配比
    "ca_per_eq": lambda x: x[1] / x[0],
}


# ---- 审批注册表：只有注册过的特征集可用于训练与服务 -------------------------

_APPROVED: dict[str, FeatureFactory] = {
    CURRENT_VERSION: FeatureFactory(CURRENT_VERSION, FEATURE_DEFS_V1),
}


def get_approved(version: str = CURRENT_VERSION) -> FeatureFactory:
    if version not in _APPROVED:
        raise FeatureNotApprovedError(
            f"{version} 未注册——特征集变更须先走审批（§18.1）")
    return _APPROVED[version]


def approve(factory: FeatureFactory) -> None:
    """审批通过并注册（生产映射为人工评审 + 审计留痕）。"""
    _APPROVED[factory.version] = factory


def draft(version: str, definitions: list[dict]) -> FeatureFactory:
    """创建草案工厂（不入注册表，不可用于训练/服务）。"""
    return FeatureFactory(version, definitions)
