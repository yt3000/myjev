"""MyJev 评测与 §11.1 上线门禁。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.metrics import f1_score, roc_auc_score

from .contract import Band, NOUL_HEADS
from .thresholds import ThresholdSet

ACTION_VOCAB = ["allow", "step_up", "deny"]


def policy_from_true_risk(r: float) -> str:
    return "allow" if r < 0.3 else ("step_up" if r < 0.72 else "deny")


def band_from_risk(r: float, ts: ThresholdSet) -> str:
    return ts.band_of(r).value


@dataclass
class EvalResult:
    action_macro_f1_noisy: float
    action_acc_clean: float
    auroc: dict[str, float]
    ece_noisy: dict[str, float]
    ece_action: float
    band_adjacent_err: float
    band_cross_err: float
    false_block_rate: float
    miss_rate: float
    gray_share: float
    lat_p50_ms: float
    lat_p99_ms: float
    n: int


def evaluate(preds_cal, lat_ms: list[float], events: list[dict],
             ts: ThresholdSet) -> EvalResult:
    """preds_cal: list[ModelProposal]（按事件顺序）；events 含 labels/true_risk。"""
    y_true_noisy = [e["labels"]["action"] for e in events]
    y_pred = [p.action.value for p in preds_cal]
    f1_noisy = f1_score(y_true_noisy, y_pred, labels=ACTION_VOCAB,
                        average="macro", zero_division=0)
    clean = [policy_from_true_risk(e["true_risk"]) for e in events]
    acc_clean = float(np.mean([pr == ct for pr, ct in zip(y_pred, clean)]))
    false_block = float(np.mean([pr != "allow" and ct == "allow"
                                 for pr, ct in zip(y_pred, clean)]))
    miss = float(np.mean([pr == "allow" and ct != "allow"
                          for pr, ct in zip(y_pred, clean)]))
    auroc, ece = {}, {}
    for j, h in enumerate(NOUL_HEADS):
        y = np.asarray([e["labels"]["noul"][h] for e in events])
        s = np.asarray([p.probs.noul[h] for p in preds_cal])
        auroc[h] = float(roc_auc_score(y, s)) if len(np.unique(y)) == 2 else float("nan")
        ece[h] = _ece_binary(s, y)
    y_act = np.asarray([ACTION_VOCAB.index(v) for v in y_true_noisy])
    act_mat = np.asarray([[p.probs.action.get(v, 0.0) for v in ACTION_VOCAB]
                          for p in preds_cal])
    ece_action = _ece_multi(act_mat, y_act)
    b_true = [band_from_risk(e["true_risk"], ts) for e in events]
    b_pred = [p.band.value for p in preds_cal]
    order = ["low", "medium", "high", "critical"]
    dist = [abs(order.index(a) - order.index(b)) for a, b in zip(b_true, b_pred)]
    adj = float(np.mean([d == 1 for d in dist]))
    cross = float(np.mean([d >= 2 for d in dist]))
    gray = float(np.mean([p.fallback_reason is not None for p in preds_cal]))
    lats = np.asarray(lat_ms)
    return EvalResult(
        action_macro_f1_noisy=float(f1_noisy), action_acc_clean=acc_clean,
        auroc=auroc, ece_noisy=ece, ece_action=ece_action,
        band_adjacent_err=adj, band_cross_err=cross,
        false_block_rate=false_block, miss_rate=miss, gray_share=gray,
        lat_p50_ms=float(np.percentile(lats, 50)),
        lat_p99_ms=float(np.percentile(lats, 99)),
        n=len(events),
    )


def _ece_binary(p: np.ndarray, y: np.ndarray, n_bins: int = 15) -> float:
    edges = np.linspace(0, 1, n_bins + 1)
    tot, err = 0.0, 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p > lo) & (p <= hi)
        if not m.any():
            continue
        n = int(m.sum())
        err += n * abs(y[m].mean() - p[m].mean())
        tot += n
    return float(err / tot) if tot else 0.0


def _ece_multi(probs: np.ndarray, y: np.ndarray, n_bins: int = 15) -> float:
    conf = probs.max(axis=1)
    correct = probs.argmax(axis=1) == y
    return _ece_binary(conf, correct.astype(float), n_bins)


GATES_V0 = {  # §11.1 v0 门槛
    "action_macro_f1": 0.80,
    "auroc_min(两关键头)": 0.90,
    "ece_all_heads_max": 0.02,
    "band_adjacent_max": 0.12,
    "band_cross_max": 0.0,
    "false_block_max": 0.02,
    "lat_p50_ms_max": 10.0,
    "lat_p99_ms_max": 200.0,
}


def gate_table(r: EvalResult) -> list[tuple[str, str, str, bool]]:
    key_auroc = min(r.auroc["account_takeover"], r.auroc["credential_stuffing"])
    ece_max = max(v for v in list(r.ece_noisy.values()) + [r.ece_action]
                  if v == v)  # 过滤 nan
    rows = [
        ("action macro-F1(noisy)", f"≥{GATES_V0['action_macro_f1']}", f"{r.action_macro_f1_noisy:.3f}",
         r.action_macro_f1_noisy >= 0.80),
        ("AUROC(ATO/撞库 取小)", f"≥{GATES_V0['auroc_min(两关键头)']}", f"{key_auroc:.3f}",
         key_auroc >= 0.90),
        ("ECE(全部头 取大)", f"≤{GATES_V0['ece_all_heads_max']}", f"{ece_max:.3f}",
         ece_max <= 0.02),
        ("band 相邻档错误", f"≤{GATES_V0['band_adjacent_max']}", f"{r.band_adjacent_err:.3f}",
         r.band_adjacent_err <= 0.12),
        ("band 跨档错误", f"≤{GATES_V0['band_cross_max']}", f"{r.band_cross_err:.3f}",
         r.band_cross_err <= 0.0),
        ("误拦率(clean policy)", f"≤{GATES_V0['false_block_max']}", f"{r.false_block_rate:.3f}",
         r.false_block_rate <= 0.02),
        ("延迟 P50(ms, 进程内)", f"<{GATES_V0['lat_p50_ms_max']}", f"{r.lat_p50_ms:.2f}",
         r.lat_p50_ms < 10.0),
        ("延迟 P99(ms, 进程内)", f"<{GATES_V0['lat_p99_ms_max']}", f"{r.lat_p99_ms:.2f}",
         r.lat_p99_ms < 200.0),
    ]
    return rows
