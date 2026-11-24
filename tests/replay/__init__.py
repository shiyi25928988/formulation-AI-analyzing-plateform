# -*- coding: utf-8 -*-
"""
AdheFormAI 离线回放框架（B02 / R2）
架构依据：docs/ARCHITECTURE.md §17.2（离线回放）、§17.1（闭环层门禁）、附录 B.1（幸存者偏差定论）。

设计立场（与 §17.2 逐条对应）：
  1. point-in-time 冻结：T0 时刻「当时可见」的数据才可进入历史模型（freeze.py）；
  2. 候选池由约束库在 T0 重建，不从结果反推（strategies.py 的 pool 过滤）；
  3. 只揭示历史上真实做过的实验标签（replay.py historical 模式）；
  4. 三条基线：随机 / 工程师历史顺序 / 单目标贪心（strategies.py）；
  5. 幸存者偏差三步诊断先行，全量 / 保守两档回放（diagnose.py + 附录 B.1）；
  6. 报告输出轮次下降的分布与置信区间，而非单点数字（report.py）；
  7. 合成数据模式为默认，满足数据红线（§17.3：绝不拿真实配方做测试 fixture）。

零依赖：纯 Python 标准库实现（含 bagged CART 代理模型），可在气隙环境直接运行。
"""
