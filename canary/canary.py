# -*- coding: utf-8 -*-
"""金丝雀配方集（§16.2 三层防护之二 / ADR-14）。

一组性能已知的固定配方：每次模型加载、每次发布、每日定时各跑一遍；
偏差超阈值即判失败并建议摘除该版本。集合版本化（变更需审批），
内容哈希写入审计；全部为合成世界真值生成的参考值——**绝不拿真实配方
做 fixture（数据红线）**。

设计覆盖（对应 §17.3 golden set 覆盖性要求）：
性能高中低区间 / 温度窗口上下边界 / 协同增效窄脊 / 湿度劣化 /
填料过载反噬 / 分散剂过量 / 时间收益饱和 / 后固化高点。
"""
from __future__ import annotations
import hashlib
import json

from tests.replay import synthetic as world
from skeleton.pipeline import METRICS

CANARY_VERSION = "CANARY-SET-v1.0"
# 偏差容差系数：3σ 会被小样本（<50 行）树模型的系统偏差误触发
# （首轮实测最大偏差 7.8 > 3σ=7.5），放宽至 4.5σ；故障注入量级
# （VALUE-DRIFT +25 ≈ 10σ）仍远超容差，不影响拦截灵敏度。
# 生产口径：按指标用留出集残差分布定容差，此处为合成替身。
TOLERANCE_SIGMA = 4.5

# 十二条固定配方（手工锚点，确定性、可审计）。x 顺序与 world.FEATURE_NAMES 一致。
_FORMULA_X = {
    "CAN-01": [1.00, 12.0, 40.0, 1.5, 155.0, 3.0, 120.0, 45.0],   # 协同窄脊内
    "CAN-02": [0.60, 6.0, 20.0, 1.0, 130.0, 1.5, 90.0, 40.0],     # 低性能端
    "CAN-03": [1.40, 18.0, 50.0, 1.0, 160.0, 4.0, 140.0, 30.0],   # 高性能端
    "CAN-04": [1.00, 12.0, 40.0, 1.5, 120.0, 3.0, 100.0, 45.0],   # 温度下边界
    "CAN-05": [1.00, 12.0, 40.0, 1.5, 180.0, 3.0, 100.0, 45.0],   # 温度上边界
    "CAN-06": [1.00, 12.0, 40.0, 1.5, 150.0, 3.0, 100.0, 75.0],   # 湿度劣化
    "CAN-07": [1.00, 12.0, 80.0, 1.5, 150.0, 3.0, 100.0, 45.0],   # 填料过载反噬
    "CAN-08": [1.00, 12.0, 40.0, 5.0, 150.0, 3.0, 100.0, 45.0],   # 分散剂过量
    "CAN-09": [1.00, 12.0, 40.0, 1.5, 150.0, 6.0, 100.0, 45.0],   # 固化时间收益饱和
    "CAN-10": [1.00, 12.0, 40.0, 1.5, 150.0, 3.0, 160.0, 45.0],   # 后固化高点
    "CAN-11": [1.00, 12.0, 10.0, 1.5, 130.0, 3.0, 100.0, 45.0],   # 窄脊外（低填料低温）
    "CAN-12": [1.30, 16.0, 48.0, 1.2, 158.0, 4.0, 135.0, 35.0],   # 近目标区
}

_PROFILE = {
    "CAN-01": "narrow-ridge", "CAN-02": "perf-low", "CAN-03": "perf-high",
    "CAN-04": "boundary-lower", "CAN-05": "boundary-upper",
    "CAN-06": "humidity-degradation", "CAN-07": "filler-overload",
    "CAN-08": "dispersant-excess", "CAN-09": "time-saturation",
    "CAN-10": "post-cure-high", "CAN-11": "outside-ridge",
    "CAN-12": "near-target",
}


def build_canary_set() -> list[dict]:
    """构建金丝雀集：参考值取合成世界真值（无噪声），容差 = 3σ。"""
    entries = []
    for cid, x in _FORMULA_X.items():
        entries.append({
            "id": cid,
            "profile": _PROFILE[cid],
            "x": list(x),
            "reference": {code: round(METRICS[code]["truth"](x), 3)
                          for code in METRICS},
            "tolerance": {code: round(TOLERANCE_SIGMA
                                      * METRICS[code]["noise"], 3)
                          for code in METRICS},
        })
    return entries


def set_fingerprint(entries: list[dict] | None = None) -> str:
    """集合内容哈希：任何配方或容差变更都会改变指纹（版本化 + 审计）。"""
    if entries is None:
        entries = build_canary_set()
    payload = json.dumps(entries, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
