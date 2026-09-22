"""MyJev 方案 A 主线模型（规格 §5，M2 阶段 = LightGBM 每头基线）。

确定性训练（deterministic=True、seed 固定），推理输出 RawProbs（校准前，
仅可用于排序——§6.1 红线）。融合 MLP+encoder 主线在 GBM 基线通过 §11.1
门禁后按 §5.1 规则替换。
"""
from __future__ import annotations

import json
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np

from .calibration import (BinaryCalibrator, MulticlassCalibrator,
                          RegressionCalibrator)
from .contract import Action, AuthLevel, CalibratedProbs, NOUL_HEADS, RawProbs
from .features import FEATURE_NAMES, to_row

MODEL_ID = "myjev-gbm"
MODEL_VERSION = "0.4.0"

LGB_PARAMS = dict(
    random_state=42, deterministic=True, force_row_wise=True, verbosity=-1,
    n_estimators=240, learning_rate=0.06, num_leaves=31, min_child_samples=25,
    subsample=0.9, subsample_freq=1, colsample_bytree=0.9,
)

ACTION_CLASSES = [Action.ALLOW.value, Action.STEP_UP.value, Action.DENY.value]
#: auth 头训练词表：deny⇒none 由契约强制（§2.2），故 NONE/DEVICE_CERT 不入训练集
AUTH_CLASSES_TRAIN = [AuthLevel.PASSWORD_OK.value, AuthLevel.MFA_TOTP.value,
                      AuthLevel.MFA_FIDO2.value, AuthLevel.HUMAN_REVIEW.value]
AUTH_CLASSES = AUTH_CLASSES_TRAIN


def _matrix(events: list[dict]) -> np.ndarray:
    return np.asarray([to_row(e["features"]) for e in events], dtype=np.float64)


def _pos_proba(clf, X: np.ndarray) -> np.ndarray:
    """二分类正类概率；训练窗内单类时退化为常数。"""
    p = clf.predict_proba(X)
    if p.shape[1] > 1:
        return p[:, 1]
    return np.full(len(X), float(clf.predict(X)[0]) if len(X) else 0.0)


def _logprobs(proba: np.ndarray) -> np.ndarray:
    """概率 → log 概率当作 logits 使用（softmax 会吸收每样本常数，温度缩放等价）。"""
    return np.log(np.clip(proba, 1e-9, 1.0))


@dataclass
class HeadBundle:
    """多任务头：8×Noul（二分类）+ risk（回归）+ action（3 类）+ auth（6 类）。"""

    noul: dict[str, lgb.LGBMClassifier] = field(default_factory=dict)
    risk: lgb.LGBMRegressor | None = None
    action: lgb.LGBMClassifier | None = None
    auth: lgb.LGBMClassifier | None = None

    def fit(self, events: list[dict]) -> "HeadBundle":
        X = _matrix(events)
        y_noul = {h: np.asarray([e["labels"]["noul"][h] for e in events])
                  for h in NOUL_HEADS}
        for h in NOUL_HEADS:
            pos = int(y_noul[h].sum())
            params = dict(LGB_PARAMS)
            if 0 < pos < len(events):
                params["is_unbalance"] = True
            clf = lgb.LGBMClassifier(**params)
            clf.fit(X, y_noul[h])
            self.noul[h] = clf
        self.risk = lgb.LGBMRegressor(**LGB_PARAMS).fit(
            X, np.asarray([e["labels"]["risk"] for e in events]))
        self.action = lgb.LGBMClassifier(**LGB_PARAMS).fit(
            X, np.asarray([ACTION_CLASSES.index(e["labels"]["action"])
                           for e in events]))
        rows = [e for e in events if e["labels"]["action"] != Action.DENY.value]
        Xa = _matrix(rows)  # auth 头只在非 deny 样本上训练（deny⇒none 由契约强制）
        self.auth = lgb.LGBMClassifier(**LGB_PARAMS).fit(
            Xa, np.asarray([AUTH_CLASSES.index(e["labels"]["auth"]) for e in rows]))
        return self

    def predict_raw(self, events: list[dict]) -> tuple[list[RawProbs], float]:
        """逐样本输出 RawProbs；返回 (结果, 单样本 P50/P99 计时列表毫秒)。"""
        X = _matrix(events)
        n, k = len(events), len(NOUL_HEADS)
        noul_p = np.column_stack([_pos_proba(self.noul[h], X) for h in NOUL_HEADS])
        risk_p = np.clip(self.risk.predict(X), 0.0, 1.0)
        act_p = self.action.predict_proba(X)
        auth_p = self._auth_full_width(X)
        outs: list[RawProbs] = []
        lat: list[float] = []
        for i in range(n):
            t0 = time.perf_counter()
            outs.append(RawProbs(
                action={ACTION_CLASSES[int(c)]: float(act_p[i, j])
                        for j, c in enumerate(self.action.classes_)},
                auth={AUTH_CLASSES[int(c)]: float(auth_p[i, int(c)])
                      for c in self.auth.classes_},
                risk=float(risk_p[i]),
                noul={h: float(noul_p[i, jj]) for jj, h in enumerate(NOUL_HEADS)},
                raw_logits={
                    "action_logp": _logprobs(act_p[i]).tolist(),
                    "auth_logp": {AUTH_CLASSES[int(c)]: float(v) for c, v in
                                   zip(self.auth.classes_, _logprobs(auth_p[i]))},
                    "noul_logit": {h: float(np.log(noul_p[i, jj] / (1 - noul_p[i, jj])))
                                   for jj, h in enumerate(NOUL_HEADS)},
                },
            ))
            lat.append((time.perf_counter() - t0) * 1000)
        return outs, lat

    def _auth_full_width(self, X: np.ndarray) -> np.ndarray:
        """把 auth 头输出对齐到训练词表全宽（缺席类补 0）。

        LightGBM sklearn API 单类时仍可能返回两列，不能假设列数=len(classes_)，
        逐类映射并取正列。
        """
        p = self.auth.predict_proba(X)
        full = np.zeros((X.shape[0], len(AUTH_CLASSES)))
        for col, cls in enumerate(self.auth.classes_):
            src = -1 if p.shape[1] > len(self.auth.classes_) else col
            full[:, int(cls)] = p[:, src]
        return full

    def predict_matrix(self, X: np.ndarray) -> dict[str, np.ndarray]:
        return {
            "noul": np.column_stack([_pos_proba(self.noul[h], X) for h in NOUL_HEADS]),
            "risk": np.clip(self.risk.predict(X), 0.0, 1.0),
            "action_p": self.action.predict_proba(X),
            "auth_p": self._auth_full_width(X),
        }


@dataclass
class Calibrators:
    noul: dict[str, BinaryCalibrator] = field(default_factory=dict)
    risk: RegressionCalibrator = field(default_factory=RegressionCalibrator)
    action: MulticlassCalibrator | None = None
    auth: MulticlassCalibrator | None = None

    def report(self) -> dict:
        return {"noul": {h: {"before": round(self.noul[h].ece_before, 4),
                             "after": round(self.noul[h].ece_after, 4),
                             "T": round(self.noul[h].temp.T, 3),
                             "iso": self.noul[h].iso is not None}
                         for h in NOUL_HEADS},
                "action": {"before": round(self.action.ece_before, 4),
                           "after": round(self.action.ece_after, 4),
                           "T": round(self.action.temp.T, 3)} if self.action else {},
                "auth": {"before": round(self.auth.ece_before, 4),
                         "after": round(self.auth.ece_after, 4),
                         "T": round(self.auth.temp.T, 3)} if self.auth else {}}


def calibrate(bundle: HeadBundle, calib_events: list[dict]) -> Calibrators:
    """§6.1 仅在校准集上拟合，每头独立。"""
    X = _matrix(calib_events)
    M = bundle.predict_matrix(X)
    c = Calibrators()
    for j, h in enumerate(NOUL_HEADS):
        y = np.asarray([e["labels"]["noul"][h] for e in calib_events])
        if len(np.unique(y)) == 2:
            c.noul[h] = BinaryCalibrator().fit(M["noul"][:, j], y)
        else:  # 校准集上退化的头：恒等映射并标记
            c.noul[h] = BinaryCalibrator()
            c.noul[h].ece_before = c.noul[h].ece_after = 0.0
    y_risk = np.asarray([e["labels"]["risk"] for e in calib_events])
    c.risk.fit(M["risk"], y_risk)
    y_act = np.asarray([ACTION_CLASSES.index(e["labels"]["action"])
                        for e in calib_events])
    c.action = MulticlassCalibrator(len(ACTION_CLASSES)).fit(_logprobs(M["action_p"]), y_act)
    keep = [i for i, e in enumerate(calib_events)
            if e["labels"]["action"] != Action.DENY.value]  # deny⇒none 由契约处理，头不含
    y_auth = np.asarray([AUTH_CLASSES.index(calib_events[i]["labels"]["auth"])
                         for i in keep])
    c.auth = MulticlassCalibrator(len(AUTH_CLASSES)).fit(
        _logprobs(M["auth_p"][keep]), y_auth)
    return c


def apply_calibration(bundle: HeadBundle, cals: Calibrators,
                      events: list[dict]) -> tuple[list[CalibratedProbs], list[float]]:
    raw, lat = bundle.predict_raw(events)
    outs: list[CalibratedProbs] = []
    for r in raw:
        noul = {h: float(cals.noul[h].apply([r.noul[h]])[0]) for h in NOUL_HEADS}
        act_lp = np.asarray(r.raw_logits["action_logp"])
        act = dict(zip(bundle.action.classes_, cals.action.apply(act_lp)[0]))
        act = {ACTION_CLASSES[int(k)]: float(v) for k, v in act.items()}
        act = {c: float(act.get(c, 0.0)) for c in ACTION_CLASSES}  # 缺席类补 0
        auth_lp = np.asarray([r.raw_logits["auth_logp"].get(name, -9.0)
                              for name in AUTH_CLASSES])
        authp = cals.auth.apply(auth_lp)[0]
        auth = {AUTH_CLASSES[i]: float(v) for i, v in enumerate(authp)}
        outs.append(CalibratedProbs(
            action={k: float(v) for k, v in act.items()},
            auth=auth,
            risk=float(cals.risk.apply([r.risk])[0]),
            noul=noul,
            raw={"action": dict(r.action), "auth": dict(r.auth),
                 "risk": r.risk, "noul": dict(r.noul), "logits": r.raw_logits},
        ))
    return outs, lat


# ---------------------------------------------------------------- 持久化

def save(bundle: HeadBundle, cals: Calibrators, feature_digest: str,
         threshold_set_id: str, schema_version: str, outdir: str | Path) -> None:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "model_bundle.pkl", "wb") as f:
        pickle.dump({"bundle": bundle, "cals": cals}, f)
    quad = {  # §9 版本四元组 + 特征契约（可复现性硬要求）
        "model_id": MODEL_ID, "model_version": MODEL_VERSION,
        "encoder_version": "none-v0（文本头未启用）",
        "threshold_set_id": threshold_set_id, "schema_version": schema_version,
        "feature_digest": feature_digest,
        "feature_names": list(FEATURE_NAMES),
    }
    (outdir / "model_card.json").write_text(
        json.dumps(quad, ensure_ascii=False, indent=2), encoding="utf-8")


def load(outdir: str | Path) -> tuple[HeadBundle, Calibrators, dict]:
    outdir = Path(outdir)
    with open(outdir / "model_bundle.pkl", "rb") as f:
        d = pickle.load(f)
    card = json.loads((outdir / "model_card.json").read_text(encoding="utf-8"))
    return d["bundle"], d["cals"], card
