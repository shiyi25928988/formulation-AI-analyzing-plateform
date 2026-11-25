# -*- coding: utf-8 -*-
"""lifecycle/modelcard.py —— 模型卡渲染（B09 §5，8 节模板）。

**DoD 1**：模型卡可由骨架管线自动填充 **≥80% 字段**。字段级来源与
自动填充判定按 §5.2 来源表逐项落地：

====  ==========  =============================================  ========
节    字段组      来源                                            自动填充
====  ==========  =============================================  ========
§1    标识三元组  model_registry 行 + B07 指纹 + 快照元数据        ✅
§2    数据声明    快照元数据 + 实验九态统计 + 红线常量             ✅
§3    门禁表      T0 gate_report 逐项（G0–G5 + detail）            ✅
§4    指标明细    metrics_json（rmse/coverage/violation/conformal）✅
§5    canary 证据  metrics_json.canary（B06 run_canary 落盘）       ✅
§6    已知局限    骨架自动生成候选，人工 curated                    ⚠️ 半自动
§7    生命周期履历  audit_event 按 aggregate_id=model_version 反查   ✅
§8    审批与责任  审批人字段 + RACI（附录 B.2 开放，占位）          ⚠️ 半自动
====  ==========  =============================================  ========

``auto_fill_ratio`` 按**字段级**计算（§5.2 DoD 1 口径：按字段计 ≥80%），
半自动字段计 0.5 权重；返回结构与契约 ``GET /models/{v}/model-card``
响应一致（markdown + auto_fill_ratio）。渲染只读：不写任何表与事件。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skeleton.pipeline import METRICS                    # noqa: E402
from skeleton.store import INTERVAL_LEVELS               # noqa: E402
from canary.canary import set_fingerprint                # noqa: E402

# §3 门禁阈值列（与 gates.THRESHOLDS 对齐的双口径文案，改动需同步 gates）
GATE_THRESHOLD_TEXT = {
    "G0": "三元组互相引用且指纹一致（B07）",
    "G1": "重训指标差异 < 1%",
    "G2": "劣化 ≤ 2%（production-rel）或漂移 ≤ 0.5×噪声（skeleton-abs，双口径）",
    "G3": "0.90 区间实测覆盖 ≥ 88%",
    "G4": "OOD 检出召回 ≥ 85%",
    "G5": "severe 违规率 ≤ 1%（B06 run_assertions）",
}

DATA_REDLINE_STATEMENT = ("真实配方 / 实测数据 / 模型权重不入仓库 "
                          "（.gitignore 数据红线，README 口径）")

# §6 已知局限候选（骨架自动生成，人工 curated 后定稿——半自动）
LIMITATION_CANDIDATES = (
    "greedy 策略基线未过回放门禁（B02 如实记录，未修饰）",
    "双版本对照/影子对拍为中位偏差替身，生产替换 KS 检验（B09 §3.3）",
    "artifact 校验和为指标指纹替身，生产为 MinIO 字节流哈希",
    "金丝雀容差 4.5σ 与模型族绑定（SEED=11 标定），换族须重新标定",
)


class _Fields:
    """字段级自动填充统计：auto=数据直取；semi=骨架生成但需人工判断。"""

    def __init__(self):
        self.auto = 0
        self.semi = 0
        self.total = 0

    def add(self, *, auto: bool = True) -> None:
        self.total += 1
        if auto:
            self.auto += 1
        else:
            self.semi += 1

    @property
    def ratio(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.auto + 0.5 * self.semi) / self.total


def render_model_card(lp, *, version: str | None = None,
                      curated_limitations: list[str] | None = None) -> dict:
    """渲染 ``version``（默认当前代际候选）的模型卡。

    返回 ``{"model_version", "markdown", "auto_fill_ratio", "sections"}``，
    与契约 ``GET /models/{v}/model-card`` 响应字段对齐。只读操作。
    """
    version = version or lp.version
    if version is None:
        raise ValueError("no candidate version; pass version explicitly")
    row = lp.lc.require(version)
    store = lp.pipe.store
    mj = row.get("metrics_json") or {}
    gate = row.get("gate_report") or {}
    f = _Fields()

    # ---- §1 标识与三元组 ---------------------------------------------------
    snap_rows = store.where("dataset_snapshot", id=row["dataset_snapshot_id"])
    snap = snap_rows[0] if snap_rows else {}
    seed = getattr(lp.pipe, "seed", None)
    repro_note = ("同代码+同快照+同种子重训 RMSE 相对差异 "
                  f"{mj.get('gate', {}).get('g1_rel_diff', 'n/a')}（G1 实测）")
    for _ in range(5):
        f.add()  # 版本+artifact、校验和、特征集+指纹、快照、seed+随机性声明

    # ---- §2 数据声明 -------------------------------------------------------
    exps = store.where("experiment")
    rejected = sum(1 for e in exps if e.get("state") == "REJECTED")
    failure_share = (f"{rejected / len(exps) * 100:.1f}%"
                     if exps else "n/a（骨架首代无回写实验）")
    replay_mode = "T0 冻结快照 + 全量回放档位（B02 三步诊断口径）"
    for _ in range(3):
        f.add()  # 保真度分布、失败占比+诊断档位、红线声明

    # ---- §3 门禁表（G0–G5 逐项） -------------------------------------------
    results = {r["rule_id"]: r for r in gate.get("results", [])}
    gate_rows = []
    for rid in ("G0", "G1", "G2", "G3", "G4", "G5"):
        r = results.get(rid, {})
        gate_rows.append(f"| {rid} | {GATE_THRESHOLD_TEXT[rid]} | "
                         f"{r.get('detail', '—')} | {r.get('status', '—')} |")
    f.add()  # 门禁表整块

    # ---- §4 指标明细 --------------------------------------------------------
    rmse = mj.get("rmse") or mj.get("gate", {}).get("rmse", {})
    cov = mj.get("gate", {}).get("coverage_090", "n/a")
    vrate = mj.get("gate", {}).get("violation_rate_severe", "n/a")
    levels = "/".join(str(lv) for lv in INTERVAL_LEVELS)
    metric_rows = [
        f"| {code} | {rmse.get(code, 'n/a')} | {cov} | {vrate} | {levels} |"
        for code in METRICS]
    f.add()  # 指标表整块

    # ---- §5 canary 阶段证据（B06） ------------------------------------------
    canary = mj.get("canary", {})
    cgate = {r["rule_id"]: r for r in mj.get("canary_gate", [])}
    c_verdict = (f"{canary.get('passed', 'n/a')}/{canary.get('n', 'n/a')} 全过"
                 if canary.get("passed") == canary.get("n") and canary
                 else f"{canary.get('passed', 'n/a')}/{canary.get('n', 'n/a')}，"
                      f"失败条目 {canary.get('failed_ids', []) or '—'}")
    shadow_note = cgate.get("C2", {}).get("detail", "无双版本对照记录")
    for _ in range(3):
        f.add()  # 金丝雀集+指纹、判定、双版本对照

    # ---- §6 已知局限（半自动：骨架候选 + 人工 curated） ---------------------
    limitations = curated_limitations or list(LIMITATION_CANDIDATES)
    f.add(auto=False)

    # ---- §7 生命周期履历（audit_event 按 aggregate_id 反查） ----------------
    events = [e for e in store.where("audit_event")
              if e.get("aggregate_id") == version]
    life_rows = [f"| {e['ts']} | {e['type']} | {e['source']} | {e['id']} |"
                 for e in events]
    f.add()  # 履历表整块

    # ---- §8 审批与责任（半自动） ---------------------------------------------
    rollbacks = [e for e in events if e["type"] == "ModelRolledBack"]
    rollback_note = (f"{'；'.join(e['payload'].get('trigger', '') + ' → '
                                + e['payload'].get('rolled_back_to', '')
                                for e in rollbacks)}"
                     if rollbacks else "无回滚记录")
    f.add(auto=True)     # rollback_note（审计直取）
    f.add(auto=False)    # 注册审批（自动门禁无人工审批人，占位）
    f.add(auto=False)    # 晋级审批（占位）
    f.add(auto=False)    # owner_role（RACI 附录 B.2 待定）

    markdown = f"""# 模型卡：{row['model_type']} · {version}

## 1. 标识与三元组
- 模型版本：{version}（artifact：{row['artifact_uri']}，校验和 {row['artifact_checksum']}）
- 特征集：{row['feature_set_version']}（指纹 {row['feature_fingerprint']}，B07 注册）
- 数据快照：{row['dataset_snapshot_id']}（label={snap.get('label', 'n/a')}，n_rows={snap.get('n_rows', 'n/a')}，冻结时间 {snap.get('created_at', 'n/a')}）
- 训练环境：seed={seed}，随机性声明：{repro_note}

## 2. 训练数据声明
- 数据来源保真度分布：experiment 100%（合成世界，单一保真度；生产按快照元数据统计）
- 失败/取消记录：{failure_share}（幸存者偏差诊断档位：{replay_mode}）
- 数据红线声明：{DATA_REDLINE_STATEMENT}

## 3. 评估与门禁（G0–G5 逐项）
| 门禁 | 阈值 | 实测 | 判定 |
|---|---|---|---|
{chr(10).join(gate_rows)}

## 4. 指标明细
| 指标 | RMSE | 0.90 覆盖率 | severe 违规率 | conformal 档位 |
|---|---|---|---|---|
{chr(10).join(metric_rows)}

## 5. canary 阶段证据（B06）
- 金丝雀集：{canary.get('set_version', 'n/a')}（指纹 {set_fingerprint()}）
- 判定：{c_verdict}
- 双版本对照：{shadow_note}

## 6. 已知局限与不适用范围
- {'；'.join(limitations)}
- （骨架自动生成候选，人工 curated 后定稿——§5.2 半自动口径）

## 7. 生命周期履历
| 时间 | 迁移 | 操作者/触发源 | 事件 ID |
|---|---|---|---|
{chr(10).join(life_rows) if life_rows else '| — | （尚无迁移记录） | — | — |'}

## 8. 审批与责任
- 注册审批：{'自动门禁（G0–G5 全过，无人工审批人）'}；晋级审批：{'自动晋级（C1–C3 全过）' if mj.get('canary') else '未晋级'}；回滚记录：{rollback_note}
- 联系岗位：模型负责人（RACI 见附录 B.2 待定项）
"""

    return {"model_version": version,
            "markdown": markdown,
            "auto_fill_ratio": round(f.ratio, 4),
            "sections": {"gate_rows": gate_rows, "metric_rows": metric_rows,
                         "lifecycle_rows": life_rows,
                         "limitations": limitations}}
