# -*- coding: utf-8 -*-
"""AdheFormAI 金丝雀配方集与语义断言（B06 / §16.2 / ADR-14）。

数据红线合规：金丝雀集全部来自合成世界（tests/replay.synthetic 的
ground truth），无任何真实配方。本包零第三方依赖，供骨架与后续
prediction-svc / model-lifecycle-svc 共用（单一事实来源）。
"""
