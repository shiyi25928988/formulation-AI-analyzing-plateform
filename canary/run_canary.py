# -*- coding: utf-8 -*-
"""金丝雀配方集演示：健康基线 → 八类故障注入 → 拦截率汇总。

运行：python canary/run_canary.py
退出码 0 = 健康基线全过且故障拦截率 100%（B06 验收口径）。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from canary.runner import run_all                      # noqa: E402
from canary.faults import FAULTS                       # noqa: E402


def main() -> int:
    results = run_all()
    healthy = results["healthy"]
    print("=" * 68)
    print("AdheFormAI 金丝雀配方集（CANARY-SET v1.0，12 条合成配方）")
    print(f"集合指纹 {healthy['fingerprint']}  模型版本 {healthy['model_version']}")
    print("=" * 68)

    # ---- 健康基线 --------------------------------------------------------
    print(f"\n[基线] 健康模型：{healthy['passed']}/{healthy['n']} 条通过")
    for r in healthy["rows"]:
        devs = ", ".join("refused" if d is None else f"{d:+.1f}"
                         for d in r["deviations"])
        print(f"  {r['id']} ({r['profile']:<20}) 偏差[{devs}] "
              f"{'PASS' if r['ok'] else 'FAIL ' + str(r['detectors'])}")

    # ---- 故障注入 --------------------------------------------------------
    print("\n[故障注入] 八类物理矛盾，逐类验证拦截：")
    print(f"  {'故障':<26}{'层':<8}{'拦截':<6}{'探测点（样例）'}")
    for f in FAULTS:
        v = results[f["code"]]
        sample = next((r for r in v["rows"] if not r["ok"]), None)
        dets = ",".join(sample["detectors"]) if sample else "-"
        mark = "✓" if results["interception"][f["code"]] else "✗ 漏检"
        print(f"  {f['code']:<26}{'输入' if f['layer']=='input' else '输出':<8}"
              f"{mark:<6}{dets}")

    rate = results["interception_rate"]
    ok = healthy["overall_pass"] and results["interception_rate"].startswith("8/")
    print("-" * 68)
    print(f"结论: 健康基线{'通过' if healthy['overall_pass'] else '未通过'}；"
          f"故障拦截率 {rate}（B06 验收口径 = 100%）。"
          f"结果已写入审计事件 CanaryRun。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
