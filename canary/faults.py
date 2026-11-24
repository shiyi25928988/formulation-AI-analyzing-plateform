# -*- coding: utf-8 -*-
"""故障注入（§17.4 思路在静默失败域的应用）。

八类故意引入物理矛盾的模型输出/输入，验证语义断言 + 金丝雀两层
探测器的**100% 拦截**。每类故障声明期望探测点，拦截判定以
「任一探测器报 severe 违反或偏差越界」为准。
"""
from __future__ import annotations

from canary.assertions import INTERVAL_LEVELS


def _first_live(resp: dict) -> dict | None:
    for p in resp.get("predictions", []):
        if p.get("value") is not None:
            return p
    return None


# ---- 输出层故障（注入到预测响应） ---------------------------------------

def _f_unit_scale(resp: dict) -> dict:
    p = _first_live(resp)
    p["value"] = round(p["value"] * 10, 3)          # 单位换算错误：×10
    p["intervals"] = {lv: [round(itv[0] * 10, 3), round(itv[1] * 10, 3)]
                      for lv, itv in (p.get("intervals") or {}).items()}
    return resp


def _f_interval_inversion(resp: dict) -> dict:
    p = _first_live(resp)
    lo = p["intervals"]["0.50"]
    p["intervals"]["0.50"] = [lo[1], lo[0]]         # p5/p95 倒挂
    return resp


def _f_interval_non_monotonic(resp: dict) -> dict:
    p = _first_live(resp)
    v = p["value"]
    p["intervals"]["0.70"] = [v - 1.0, v + 1.0]     # 0.70 比 0.90 还窄
    p["intervals"]["0.90"] = [v - 0.5, v + 0.5]
    return resp


def _f_ood_bypass(resp: dict) -> dict:
    p = _first_live(resp)
    p["ood_level"] = "severe"                        # 拒答被绕过：仍带数值
    return resp


def _f_value_drift(resp: dict) -> dict:
    p = _first_live(resp)
    p["value"] = round(p["value"] + 25.0, 3)        # 标签口径漂移：+25（10σ）
    return resp


def _f_direction_flip(resp: dict) -> dict:
    p = _first_live(resp)
    v = p["value"]
    p["value"] = round(-v, 3)                        # 方向翻转（符号错误）
    p["intervals"] = {lv: [round(-itv[1], 3), round(-itv[0], 3)]
                      for lv, itv in (p.get("intervals") or {}).items()}
    return resp


# ---- 输入层故障（注入到配方输入） ---------------------------------------

def _f_ratio_negative(x: list[float]) -> list[float]:
    x = list(x)
    x[1] = -5.0                                      # 固化剂 phr 为负：守恒违反
    return x


def _f_feasibility(x: list[float]) -> list[float]:
    x = list(x)
    x[4] = 200.0                                     # 固化温度越窗：约束库违规
    return x


FAULTS: list[dict] = [
    {"code": "UNIT-SCALE", "layer": "output", "transform": _f_unit_scale,
     "description": "单位换算错误（×10），经典 training-serving skew 表现"},
    {"code": "INTERVAL-INVERSION", "layer": "output", "transform": _f_interval_inversion,
     "description": "0.50 档 p5/p95 倒挂"},
    {"code": "INTERVAL-NON-MONOTONIC", "layer": "output", "transform": _f_interval_non_monotonic,
     "description": "区间宽度不随置信水平单调增"},
    {"code": "OOD-BYPASS", "layer": "output", "transform": _f_ood_bypass,
     "description": "severe 拒答被绕过仍返回数值（§4.3 红线）"},
    {"code": "VALUE-DRIFT", "layer": "output", "transform": _f_value_drift,
     "description": "数值整体漂移 +25（标签口径漂移量级），语义上不违反量纲"},
    {"code": "DIRECTION-FLIP", "layer": "output", "transform": _f_direction_flip,
     "description": "预测方向翻转（符号错误），量纲范围内、区间自洽"},
    {"code": "RATIO-NEGATIVE", "layer": "input", "transform": _f_ratio_negative,
     "description": "固化剂 phr 为负（配比守恒违反）"},
    {"code": "FEASIBILITY-VIOLATION", "layer": "input", "transform": _f_feasibility,
     "description": "固化温度越出约束库窗口（T0 口径）"},
]
