# -*- coding: utf-8 -*-
"""幸存者偏差三步诊断（§17.2 + 附录 B.1，依据 Raccuglia 2016 / Precision Chem 2026）。

三步：
  1. 核查历史序列中 failed / cancelled 占比与原因字段完整度；
  2. 占比与原因完整度达标 → FULL（全量回放，轮次下降结论可对外引用）；
  3. 明显缺失 → CONSERVATIVE（保守回放，报告显式标注，禁止对外引用数字）。
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Diagnosis:
    n_total: int
    n_failed: int
    n_cancelled: int
    n_measured: int
    failure_ratio: float          # (failed + cancelled) / total
    reason_completeness: float    # 非成功记录中带原因的比例
    verdict: str                  # FULL | CONSERVATIVE
    warnings: list[str] = field(default_factory=list)


def survivor_bias_diagnosis(history: list[dict],
                            min_failure_ratio: float = 0.10,
                            min_reason_completeness: float = 0.80) -> Diagnosis:
    """对历史序列执行三步诊断。

    min_failure_ratio：经验阈值。化工新体系研发的失败率通常显著高于 10%；
    低于该阈值即视为「失败记录大概率未被数字化」（行业默认状态，附录 B.1）。
    """
    n_total = len(history)
    if n_total == 0:
        return Diagnosis(0, 0, 0, 0, 0.0, 0.0, "CONSERVATIVE",
                         ["empty history: cannot verify record completeness"])
    n_failed = sum(1 for r in history if r.get("state") == "failed")
    n_cancelled = sum(1 for r in history if r.get("state") == "cancelled")
    n_measured = sum(1 for r in history if r.get("state") == "measured")
    n_non_success = n_failed + n_cancelled
    failure_ratio = n_non_success / n_total
    with_reason = sum(1 for r in history
                      if r.get("state") in ("failed", "cancelled") and r.get("reason"))
    reason_completeness = with_reason / n_non_success if n_non_success else 0.0

    warnings = []
    if n_non_success == 0:
        verdict = "CONSERVATIVE"
        warnings.append("no failed/cancelled records at all — survivor bias almost certain")
    elif failure_ratio < min_failure_ratio:
        verdict = "CONSERVATIVE"
        warnings.append(f"failure ratio {failure_ratio:.1%} < {min_failure_ratio:.0%} threshold")
    elif reason_completeness < min_reason_completeness:
        verdict = "CONSERVATIVE"
        warnings.append(f"reason completeness {reason_completeness:.0%} < "
                        f"{min_reason_completeness:.0%}; failure taxonomy missing")
    else:
        verdict = "FULL"
        warnings.append("diagnosis passed: full replay conclusions may be cited externally")
    return Diagnosis(n_total, n_failed, n_cancelled, n_measured,
                     failure_ratio, reason_completeness, verdict, warnings)
