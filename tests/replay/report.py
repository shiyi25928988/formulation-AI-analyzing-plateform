# -*- coding: utf-8 -*-
"""回放报告：轮次下降分布 + bootstrap 置信区间（§17.2 步骤 6）。

门禁语义（§17.1 闭环层）：相对基线轮次下降 ≥30% 且置信区间下界 >0，
才可判定「策略有效」。保守模式下 external_citation=False，
报告自动写入禁止对外引用的标注（附录 B.1）。
"""
from __future__ import annotations
import json
import math
import random
import statistics
from dataclasses import dataclass

from .diagnose import Diagnosis


@dataclass
class StrategySummary:
    strategy: str
    rounds: list[int | None]        # None 表示未达标
    median_rounds: float
    p25: float
    p75: float
    n_hit: int


def _eff_rounds(r):
    return r if r is not None else float("inf")


def summarize(strategy: str, rounds: list) -> StrategySummary:
    hit = [r for r in rounds if r is not None]
    if hit:
        hit_sorted = sorted(hit)
        med = statistics.median(hit)
        p25 = hit_sorted[max(0, int(len(hit_sorted) * 0.25) - 1)]
        p75 = hit_sorted[min(len(hit_sorted) - 1, int(len(hit_sorted) * 0.75))]
    else:
        med = p25 = p75 = float("inf")
    return StrategySummary(strategy, rounds, med, p25, p75, len(hit))


def bootstrap_reduction_ci(treated: list, baseline: list, n_boot: int = 2000,
                           seed: int = 0) -> tuple[float, float]:
    """轮次下降百分比（相对基线）的 95% bootstrap 置信区间。

    inf 处理：未达标按 max_rounds*1.5 计罚（保证「经常不达标」的策略被惩罚），
    惩罚口径在报告中显式说明，避免美化。
    """
    PENALTY = 1.5
    def vec(rs, cap):
        return [(r if r is not None else cap * PENALTY) for r in rs]
    cap = max([r for r in treated + baseline if r is not None] or [1])
    a, b = vec(treated, cap), vec(baseline, cap)
    rng = random.Random(seed)
    reds = []
    for _ in range(n_boot):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        ma, mb = statistics.mean(sa), statistics.mean(sb)
        if mb <= 0:
            continue
        reds.append((mb - ma) / mb * 100.0)
    if not reds:
        return (float("nan"), float("nan"))
    reds.sort()
    return (reds[int(0.025 * len(reds))], reds[int(0.975 * len(reds))])


def render_markdown(mode: str, diagnosis: Diagnosis, summaries: list[StrategySummary],
                    baselines: list[str], seeds: int, max_rounds: int) -> str:
    lines = []
    cite_note = ("允许对外引用" if diagnosis.verdict == "FULL"
                 else "⚠ **保守回放：幸存者偏差风险未排除，轮次下降数字不得对外引用**（附录 B.1）")
    lines.append(f"# 离线回放基线报告（{mode} 模式）")
    lines.append("")
    lines.append(f"- 回放模式：`{mode}` | 对外引用资格：{cite_note}")
    lines.append(f"- 幸存者偏差诊断：失败/取消占比 **{diagnosis.failure_ratio:.1%}**，"
                 f"原因完整度 **{diagnosis.reason_completeness:.0%}**，"
                 f"判定 **{diagnosis.verdict}**（n={diagnosis.n_total}）")
    for w in diagnosis.warnings:
        lines.append(f"  - {w}")
    lines.append(f"- 样本：{seeds} 个独立种子；每轮批次大小 4；轮次上限 {max_rounds}")
    lines.append("- 未达标按 `max_rounds × 1.5` 计罚（口径显式声明，防止美化）")
    lines.append("")
    lines.append("| 策略 | 中位轮次 | P25 | P75 | 达标率 |")
    lines.append("|---|---|---|---|---|")
    for s in summaries:
        med = "—" if s.median_rounds == float("inf") else f"{s.median_rounds:.0f}"
        p25 = "—" if s.p25 == float("inf") else f"{s.p25:.0f}"
        p75 = "—" if s.p75 == float("inf") else f"{s.p75:.0f}"
        lines.append(f"| {s.strategy} | {med} | {p25} | {p75} | "
                     f"{s.n_hit}/{len(s.rounds)} |")
    lines.append("")
    lines.append("## 相对基线的轮次下降（bootstrap 95% CI）")
    lines.append("")
    lines.append("| 策略 | 基线 | 下降中位 | 95% CI | 门禁（≥30% 且下界>0） |")
    lines.append("|---|---|---|---|---|")
    base_sum = {s.strategy: s for s in summaries if s.strategy in baselines}
    for s in summaries:
        if s.strategy in base_sum:
            continue
        for b in baselines:
            bs = base_sum[b]
            med_red = ((bs.median_rounds - s.median_rounds) / bs.median_rounds * 100.0
                       if s.median_rounds > 0 and bs.median_rounds > 0
                       and s.median_rounds != float("inf") and bs.median_rounds != float("inf")
                       else float("nan"))
            lo, hi = bootstrap_reduction_ci(s.rounds, bs.rounds)
            gate = "✅" if (lo > 0 and med_red >= 30.0) else "❌"
            lines.append(f"| {s.strategy} | {b} | {med_red:.1f}% | "
                         f"[{lo:.1f}%, {hi:.1f}%] | {gate} |")
    lines.append("")
    lines.append("> 判定规则（§17.1 闭环层门禁）：相对**任一**基线的下降 ≥30% 且 "
                 "CI 下界 >0 即判有效；两基线均未过则先改策略与特征，不进平台开发。")
    if diagnosis.verdict != "FULL":
        lines.append("")
        lines.append("> **本报告为保守回放产物：合成数据 + 受控口径，历史库失败记录"
                     "完整性未通过诊断。轮次下降数字仅用于内部方向判断，禁止写入任何"
                     "对外材料。**")
    return "\n".join(lines)


def render_json(mode: str, diagnosis: Diagnosis, summaries: list[StrategySummary]) -> str:
    return json.dumps({
        "mode": mode,
        "diagnosis": {
            "verdict": diagnosis.verdict,
            "failure_ratio": round(diagnosis.failure_ratio, 4),
            "reason_completeness": round(diagnosis.reason_completeness, 4),
            "n_total": diagnosis.n_total,
            "warnings": diagnosis.warnings,
        },
        "external_citation_allowed": diagnosis.verdict == "FULL",
        "strategies": [
            {"name": s.strategy,
             "rounds": s.rounds,
             "median": None if math.isinf(s.median_rounds) else s.median_rounds,
             "hit_rate": s.n_hit / len(s.rounds)}
            for s in summaries
        ],
    }, ensure_ascii=False, indent=2)
