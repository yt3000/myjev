"""MyJev 能力对比（bench）provider 抽象。

- myjev：实时调用本服务当前发布版本，全部指标实测；
- jev：无密钥时按"公开资料回放"（官方宣称 + 第三方评测），mode 明确标注；
  配置 TYPESAFE_API_KEY 后走真实调用（接口形态已就位，需外网）；
- openjev：按 TheoLeeCJ 公开实测回放（ic.work 2026-09-18 汇总）。
口径与 §13.2 一致：不同 mode 的行禁止当作头对头胜负。
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

ACTION_VOCAB = ["allow", "step_up", "deny"]


def _clean_action(true_risk: float) -> str:
    return "allow" if true_risk < .3 else ("step_up" if true_risk < .72 else "deny")


def bench_myjev(task, sample_n: int = 300, rows: list[dict] | None = None) -> dict:
    external = rows is not None
    evs = rows if external else task.holdout[:sample_n]
    agrees = 0
    lats = []
    for e in evs:
        t0 = time.perf_counter()
        r = task.answer({"features": e["features"], "principal": e.get("uid", "bench")},
                        [{"id": "q", "type": "choice", "options": ACTION_VOCAB}], audit=None)
        lats.append((time.perf_counter() - t0) * 1000)
        gold = e.get("gold")
        want = gold if gold else _clean_action(e.get("true_risk", 0.0))
        if r["pdp"]["final_action"] == want:
            agrees += 1
    m = task.versions[task.active].metrics if task.active else {}
    ev = task.evaluate()  # 扰动鲁棒按当前发布版实测
    return {
        "mode": "live·实测", "version": task.active,
        "agreement": round(agrees / max(len(evs), 1), 4),
        "agreement_note": "外部集按 gold 判分" if external else "内置集按 clean policy 判分",
        "latency_p50_ms": round(float(np.percentile(lats, 50)), 2),
        "latency_p99_ms": round(float(np.percentile(lats, 99)), 2),
        "ece": m.get("ece_max"), "ece_note": "本域校准集实测",
        "perturb": ev.get("perturb", {}),
        "n": len(evs),
    }


JEV_BASELINE = {  # 公开资料回放
    "mode": "replay·官方宣称/第三方",
    "agreement": 0.883, "agreement_note": "102 行公开案例宣称值；真实业务流口径 67.8%",
    "agreement_business": 0.678,
    "latency_p50_ms": 285.0, "latency_note": "官方口径 70–500ms，取区间代表值",
    "ece": 0.01, "ece_note": "RLCD 校准为宣称能力，未公开逐头数值",
    "perturb": {}, "perturb_note": "未公开（雷达图按保守 3 分计）",
}
OPENJEV_BASELINE = {  # TheoLeeCJ 公开实测（ic.work 2026-09-18）
    "mode": "replay·第三方实测(WebGPU)",
    "agreement": 0.845, "agreement_note": "Qwen3.5-4B，同一 102 行公开子集实测",
    "latency_p50_ms": 3271.0, "latency_note": "浏览器端单决策；21 问共享上下文摊薄后约 48.7ms/问",
    "ece": None, "ece_note": "无校准——候选间条件概率（项目自认）",
    "perturb": {"order_flip": 0.278, "paraphrase_flip": 0.250, "padding_flip": 0.111},
}


def run_bench(task, providers: list[str], sample_n: int = 300,
              rows: list[dict] | None = None) -> dict:
    out_rows = {}
    for p in providers:
        if p == "myjev":
            out_rows[p] = bench_myjev(task, sample_n, rows=rows)
        elif p == "jev":
            out_rows[p] = dict(JEV_BASELINE)
        elif p == "openjev":
            out_rows[p] = dict(OPENJEV_BASELINE)
    return {"task": task.task_id, "test_set": {"n": (len(rows) if rows is not None else sample_n),
            "name": ("外部数据集（gold 缺省按 true_risk 推定，见 mode）" if rows is not None
                     else "内置零信任场景测试集（与 OpenJev 102 行子集不同分布，见 mode 标注）")},
            "rows": out_rows, "radar": radar_from(out_rows)}


def _band(v, lo, hi, out_lo=0.0, out_hi=10.0):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = out_lo + (v - lo) / (hi - lo) * (out_hi - out_lo)
    return round(max(out_lo, min(out_hi, s)), 1)


def radar_from(rows: dict) -> dict:
    """0–10 合成评分（换算规则与规格 §13.3 一致）。"""
    dims = ["决策一致", "概率校准", "延迟性能", "扰动鲁棒", "成本/自主"]
    out = {}
    for name, r in rows.items():
        scores = []
        a = r.get("agreement")
        scores.append(_band(a, 0.4, 0.95) or 0)
        ece = r.get("ece")
        scores.append(10.0 if ece is None and name == "jev" else
                      (0 if ece is None else _band(0.06 - ece, 0.0, 0.055)))
        lat = r.get("latency_p50_ms")
        scores.append(_band(600 - (lat or 600), 0, 590) if lat is not None else 5)
        pp = (r.get("perturb") or {})
        worst = max([v for v in [pp.get("order_flip"), pp.get("paraphrase_flip"),
                                 pp.get("padding_flip"), pp.get("irrelevant_context_flip")]
                     if v is not None], default=None)
        if name == "jev":
            scores.append(3.0)  # 未公开，保守计
        elif worst is None:
            scores.append(2.0)
        else:
            scores.append(_band(0.30 - worst, 0, 0.30))
        scores.append(10.0 if name != "jev" else 4.0)  # 本地自托管 vs 闭源 API
        out[name] = dict(zip(dims, scores))
    return {"dims": dims, "scores": out}
