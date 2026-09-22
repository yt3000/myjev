"""MyJev 概率校准（规格 §6.1）。

每个概率头独立校准，仅在校准集上做：先温度缩放，ECE 仍 > target 再升 isotonic。
红线：未经校准的原始概率只可用于排序，禁止进入阈值判定与审计证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize_scalar
from sklearn.isotonic import IsotonicRegression


def softmax(logits: np.ndarray) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64)
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def ece_multi(probs: np.ndarray, y: np.ndarray, n_bins: int = 15) -> float:
    """多分类期望校准误差（取被选类的标称概率分桶）。probs: (N,K) 或 (N,)。"""
    p = np.asarray(probs, dtype=np.float64)
    y = np.asarray(y)
    if p.ndim == 1:
        conf, correct = p, (y == 1)
    else:
        conf = p.max(axis=1)
        correct = p.argmax(axis=1) == y
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    total, err = 0.0, 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi)
        if not m.any():
            continue
        n = int(m.sum())
        err += n * abs(correct[m].mean() - conf[m].mean())
        total += n
    return float(err / total) if total else 0.0


class TemperatureScaler:
    """logits^(1/T) 重归一化；T 用校准集 NLL 一维搜索。"""

    def __init__(self) -> None:
        self.T = 1.0

    def fit_logits(self, logits: np.ndarray, y: np.ndarray) -> "TemperatureScaler":
        logits = np.asarray(logits, dtype=np.float64)
        y = np.asarray(y)

        def nll(t: float) -> float:
            t = max(t, 1e-3)
            if logits.ndim == 1:  # 二分类：标准 sigmoid 交叉熵
                p1 = np.clip(1.0 / (1.0 + np.exp(-logits / t)), 1e-9, 1 - 1e-9)
                return float(-np.mean(y * np.log(p1) + (1 - y) * np.log(1 - p1)))
            p = softmax(logits / t)
            sel = np.clip(p[np.arange(len(y)), y], 1e-9, 1.0)
            return float(-np.mean(np.log(sel)))

        r = minimize_scalar(nll, bounds=(0.05, 20.0), method="bounded")
        self.T = float(r.x)
        return self

    def apply_logits(self, logits: np.ndarray) -> np.ndarray:
        z = np.asarray(logits, dtype=np.float64)
        if z.ndim == 1:  # 二分类：logit 视为正类对数几率
            p1 = 1.0 / (1.0 + np.exp(-z / self.T))
            return np.stack([1 - p1, p1], axis=1)
        return softmax(z / self.T)


class BinaryCalibrator:
    """sigmoid 概率 → 温度(logit 域) → ECE 超标时 isotonic。"""

    def __init__(self, target_ece: float = 0.02):
        self.target = target_ece
        self.temp = TemperatureScaler()
        self.iso: IsotonicRegression | None = None
        self.ece_before = self.ece_after = float("nan")

    def fit(self, probs: np.ndarray, y: np.ndarray) -> "BinaryCalibrator":
        p = np.clip(np.asarray(probs, dtype=np.float64), 1e-6, 1 - 1e-6)
        logits = np.log(p / (1 - p))
        self.temp.fit_logits(logits, y)
        cal = self.temp.apply_logits(logits)[:, 1]
        self.ece_before = ece_multi(p, y)
        self.ece_after = ece_multi(cal, y)
        if self.ece_after > self.target:
            self.iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            self.iso.fit(cal, y)
            self.ece_after = ece_multi(self.apply(probs), y)
        return self

    def apply(self, probs: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(probs, dtype=np.float64), 1e-6, 1 - 1e-6)
        cal = self.temp.apply_logits(np.log(p / (1 - p)))[:, 1]
        if self.iso is not None:
            cal = self.iso.predict(cal)
        return np.clip(cal, 0.0, 1.0)


class MulticlassCalibrator:
    """softmax 概率（由 logits 提供）→ 温度；每类 OvR isotonic 兜底。"""

    def __init__(self, n_classes: int, target_ece: float = 0.02):
        self.n = n_classes
        self.target = target_ece
        self.temp = TemperatureScaler()
        self.iso: list[IsotonicRegression | None] = [None] * n_classes
        self.ece_before = self.ece_after = float("nan")

    def fit(self, logits: np.ndarray, y: np.ndarray) -> "MulticlassCalibrator":
        logits = np.asarray(logits, dtype=np.float64)
        y = np.asarray(y)
        self.temp.fit_logits(logits, y)
        cal = self.temp.apply_logits(logits)
        self.ece_before = ece_multi(softmax(logits), y)
        self.ece_after = ece_multi(cal, y)
        if self.ece_after > self.target:
            for k in range(self.n):
                iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
                iso.fit(cal[:, k], (y == k).astype(float))
                self.iso[k] = iso
            self.ece_after = ece_multi(self.apply(logits), y)
        return self

    def apply(self, logits: np.ndarray) -> np.ndarray:
        lg = np.atleast_2d(np.asarray(logits, dtype=np.float64))
        cal = self.temp.apply_logits(lg)
        if self.iso[0] is not None:
            out = np.column_stack([iso.predict(cal[:, k])
                                   for k, iso in enumerate(self.iso)])
            out = np.clip(out, 1e-9, None)
            cal = out / out.sum(axis=1, keepdims=True)
        return cal


class RegressionCalibrator:
    """risk 回归头的保序修正（等渗映射，保持排序语义 + 修正系统性偏移）。"""

    def __init__(self) -> None:
        self.iso: IsotonicRegression | None = None

    def fit(self, pred: np.ndarray, y: np.ndarray) -> "RegressionCalibrator":
        self.iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        self.iso.fit(np.asarray(pred, dtype=np.float64), np.asarray(y, dtype=np.float64))
        return self

    def apply(self, pred):
        if self.iso is None:
            return np.clip(pred, 0.0, 1.0) if np.isscalar(pred) else np.clip(pred, 0.0, 1.0)
        return self.iso.predict(np.asarray(pred, dtype=np.float64))
