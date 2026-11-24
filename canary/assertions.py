# -*- coding: utf-8 -*-
"""语义断言（§16.2 三层防护之一 / ADR-14）。

在响应返回前执行五类领域级校验：
  1. 量纲范围        —— 数值整体偏移一个数量级（单位换算错误）即 severe
  2. 物理可行边界    —— 输入违反约束库（温度窗口等）→ severe；数值超物理量程 → advisory
  3. 配比守恒        —— 组分负值 / 当量非正 → severe
  4. 单调性方向      —— 区间档位宽度必须随置信水平单调增，p5 ≤ value ≤ p95 → 违反即 severe
  5. 指标方向一致性  —— OOD 与数值共存的契约矛盾 → severe（§4.3 红线的纵深防御）

原则：断言失败**不静默修正**，而是标记并降级——由调用方按两级处置
（advisory_only / 拒答）。所有结果写入审计。
"""
from __future__ import annotations

# 量纲物理量程（单位 → (下限, 上限)）。生产由约束库服务提供，此处为合成替身。
UNIT_RANGES: dict[str, tuple[float, float]] = {
    "degC": (-100.0, 400.0),   # Tg 类指标物理量程
    "MPa": (0.0, 80.0),        # 剪切强度物理量程
}

# 指标方向（对应 metric_definition.higher_better）
METRIC_DIRECTION: dict[str, bool] = {"tg_like": True, "lap_shear_like": True}

INTERVAL_LEVELS = ("0.50", "0.70", "0.90")


def run_assertions(x: list[float], predictions: list[dict],
                   feasible: bool | None = None) -> list[dict]:
    """对一次预测响应执行全部语义断言。

    x：输入配方向量；predictions：契约 prediction-result 的 predictions 数组；
    feasible：约束库判定结果（None 时跳过可行域断言）。
    返回违反列表（空 = 全过），每条 {code, severity, detail}。
    """
    violations: list[dict] = []
    violations += _check_ratio_conservation(x)
    if feasible is False:
        violations.append({
            "code": "PHYS-FEASIBILITY", "severity": "severe",
            "detail": "输入违反约束库（T0 冻结口径）：温度窗口或组分边界之外"})
    for p in predictions:
        violations += _check_metric(p)
    return violations


def _check_ratio_conservation(x: list[float]) -> list[dict]:
    """配比守恒：组分与工艺参数非负，当量配比为正。"""
    out: list[dict] = []
    if x is None:
        return [{"code": "RATIO-NULL", "severity": "severe",
                 "detail": "输入向量为空"}]
    for i, v in enumerate(x):
        if v is None or v < 0:
            out.append({"code": "RATIO-NEGATIVE", "severity": "severe",
                        "detail": f"第 {i} 维分量为负或缺失（配比守恒违反）"})
            break
    if x and x[0] <= 0:
        out.append({"code": "RATIO-EOQ", "severity": "severe",
                    "detail": "树脂当量配比非正（守恒违反）"})
    return out


def _check_metric(p: dict) -> list[dict]:
    out: list[dict] = []
    code, value = p.get("metric"), p.get("value")
    unit = p.get("unit", "")
    level = p.get("ood_level")
    intervals = p.get("intervals")

    # ---- OOD 一致性（§4.3 契约红线的响应侧纵深防御）------------------------
    if level == "severe":
        if value is not None or intervals:
            out.append({"code": "OOD-BYPASS", "severity": "severe",
                        "detail": f"{code}: ood_level=severe 仍返回数值/区间（拒答被绕过）"})
        return out
    if level == "advisory" and not p.get("advisory_only"):
        out.append({"code": "OOD-FLAG-MISSING", "severity": "severe",
                    "detail": f"{code}: advisory 结果缺少 advisory_only 标记"})

    if value is None:
        return out

    # ---- 量纲范围 ----------------------------------------------------------
    rng = UNIT_RANGES.get(unit)
    if rng is None:
        out.append({"code": "UNIT-UNKNOWN", "severity": "severe",
                    "detail": f"{code}: 未知单位 '{unit}'（量纲检查不可跳过）"})
    elif not (rng[0] <= value <= rng[1]):
        # 物理量程之外即不可能值（如 Tg=850°C 或负剪切强度）——
        # 这正是单位换算错误的典型表现，一律 severe，不静默降级
        out.append({"code": "UNIT-RANGE", "severity": "severe",
                    "detail": f"{code}: value={value} 超出 {unit} 物理量程 {rng}"})

    # ---- 单调性方向：区间档位随置信水平单调增 ------------------------------
    if intervals and isinstance(intervals, dict):
        widths = []
        for lv in INTERVAL_LEVELS:
            itv = intervals.get(lv)
            if not isinstance(itv, (list, tuple)) or len(itv) != 2 \
                    or itv[1] < itv[0]:
                out.append({"code": "INTERVAL-INVALID", "severity": "severe",
                            "detail": f"{code}: {lv} 档区间缺失或 p5>p95（倒挂）"})
                widths.append(None)
                continue
            widths.append(itv[1] - itv[0])
        legal = [w for w in widths if w is not None]
        if len(legal) == len(INTERVAL_LEVELS) and legal != sorted(legal):
            out.append({"code": "INTERVAL-NON-MONOTONIC", "severity": "severe",
                        "detail": f"{code}: 区间宽度不随置信水平单调增 {widths}"})
        # p5 ≤ value ≤ p95
        lo = intervals.get("0.50")
        if isinstance(lo, (list, tuple)) and len(lo) == 2:
            if not (lo[0] <= value <= lo[1]):
                out.append({"code": "INTERVAL-EXCLUDES-VALUE",
                            "severity": "severe",
                            "detail": f"{code}: value={value} 落在 0.50 区间 {lo} 之外"})

    # ---- 指标方向一致性：值域方向由训练口径约束，此处校验方向字段存在 --------
    if code is not None and code not in METRIC_DIRECTION:
        out.append({"code": "DIRECTION-UNKNOWN", "severity": "advisory",
                    "detail": f"{code}: 未登记指标方向（higher_better），方向断言跳过"})
    return out
