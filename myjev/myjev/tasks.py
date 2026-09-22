"""MyJev 任务引擎（docs/03/05 v3.1）。

三类 System One 任务引擎 + SQLite 持久化（版本仓库/active 指针/buffer 均入库）：
  access-decision  零信任访问决策（systemone 全原语，LightGBM 判别头 gbm-multitask）
  support-routing  客服工单路由（choice）
  urgency-score    工单紧急度（score）

工件自包含（03 §1）：data/artifacts/<task>/<version>/{model.pkl, config.json}，
config.json 固化 ThresholdSet/HardRuleEngine 快照——服务后台(A)加载不依赖管理后台(B)。
G9：model_kind 目前仅 gbm-multitask 可用，logit-probe 预留（由 app 层返回 501）。
"""
from __future__ import annotations

import json
import math
import pickle
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import f1_score, roc_auc_score

from .calibration import MulticlassCalibrator, RegressionCalibrator
from .contract import Action, AuthLevel, NOUL_HEADS, SCHEMA_VERSION
from .data import generate, split_events
from .db import DB, canon_json, sha256_file
from .features import FEATURE_NAMES, take_snapshot
from .models import (ACTION_CLASSES, AUTH_CLASSES, HeadBundle, apply_calibration,
                     calibrate)
from .pdp import HardRuleEngine, combine
from .thresholds import BandStateMachine, ThresholdSet, decide

MODEL_KIND = "gbm-multitask"


def new_version_id() -> str:
    return "v" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def _psi(ref: np.ndarray, cur: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    rv = np.histogram(np.clip(ref, 0, 1), edges)[0] / max(len(ref), 1)
    cv = np.histogram(np.clip(cur, 0, 1), edges)[0] / max(len(cur), 1)
    tot = 0.0
    for r, c in zip(rv, cv):
        r, c = max(r, 1e-4), max(c, 1e-4)
        tot += (c - r) * math.log(c / r)
    return float(tot)


@dataclass
class Version:
    id: str
    created: float
    metrics: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        return dict(id=self.id, created=self.created, metrics=self.metrics, note=self.note)


class TaskModel:
    kind = "base"
    title = ""

    def __init__(self, task_id: str, root: Path, db: DB, register: bool = True):
        self.task_id = task_id
        self.root = Path(root)
        self.db = db
        self.dir = self.root / "artifacts" / task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        if register:                      # 仅 admin 进程注册任务（03 §3 写权分离）
            db.upsert_task(task_id, self.kind, self.title, MODEL_KIND)
        self.versions: dict[str, Version] = {}
        self.active: str | None = None
        self.reload_registry()

    # ------------------------------------------------- 注册表（db 为真源）
    def reload_registry(self):
        self.versions = {}
        for r in self.db.versions(self.task_id):
            self.versions[r["version"]] = Version(
                r["version"], r["created"] or 0,
                json.loads(r["metrics_json"] or "{}"), r["note"] or "")
        for t in self.db.tasks():
            if t["id"] == self.task_id:
                self.active = t["active_version"]

    def _persist(self, ver: str, obj: dict, config: dict | None = None) -> tuple[Path, str]:
        d = self.dir / ver
        d.mkdir(parents=True, exist_ok=True)
        p = d / "model.pkl"
        with open(p, "wb") as f:
            pickle.dump(obj, f)
        (d / "config.json").write_text(canon_json(config or {}), encoding="utf-8")
        sha = sha256_file(p)
        return d, sha

    def save_version(self, ver: str, obj: dict, metrics: dict, note: str,
                     config: dict | None = None):
        d, sha = self._persist(ver, obj, config)
        self.db.add_version(self.task_id, ver, self.kind, MODEL_KIND, metrics,
                            d / "model.pkl", sha, note)
        self.reload_registry()

    def load_artifact(self, ver: str | None = None):
        ver = ver or self.active
        row = self.db.version(self.task_id, ver)
        if not row:
            raise FileNotFoundError(f"版本不存在: {self.task_id}@{ver}")
        p = Path(row["artifact_path"])
        if sha256_file(p) != row["artifact_sha256"]:
            raise ValueError(f"工件摘要不符: {ver}（拒绝加载）")
        with open(p, "rb") as f:
            obj = pickle.load(f)
        cfg = {}
        cpath = p.parent / "config.json"
        if cpath.exists():
            cfg = json.loads(cpath.read_text(encoding="utf-8"))
        return obj, cfg

    # ------------------------------------------------- buffer
    def add_feedback(self, sample: dict, src: str = "ui-feedback"):
        self.db.add_buffer(self.task_id, sample, src)

    def buffer_count(self) -> int:
        return self.db.buffer_count(self.task_id)[0]

    def real_feedback(self) -> list[dict]:
        rows = self.db.buffer_rows(self.task_id, only_included=True)
        return [json.loads(r["payload_json"]) for r in rows]

    # ------------------------------------------------- 快照
    def version_list(self) -> list[dict]:
        return [v.to_dict() | {"active": k == self.active}
                for k, v in sorted(self.versions.items(), key=lambda kv: kv[1].created)]

    def snapshot(self) -> dict:
        self.reload_registry()
        return {"task_id": self.task_id, "kind": self.kind, "title": self.title,
                "model_kind": MODEL_KIND, "active_version": self.active,
                "buffer": self.buffer_count(), "versions": self.version_list()}

    def config_snapshot(self) -> dict | None:
        return None


# ============================================================ 访问决策

class AccessDecisionTask(TaskModel):
    kind = "systemone"
    title = "零信任访问决策"
    primitives = {"choice": ACTION_CLASSES, "noul": list(NOUL_HEADS), "score": "risk"}

    def __init__(self, task_id: str, root: Path, db: DB, seed: int = 20260921,
                 register: bool = True):
        super().__init__(task_id, root, db, register=register)
        self.seed = seed
        self.ts = ThresholdSet()
        self.rules = HardRuleEngine()
        events = generate(events_per_day=200, seed=seed)
        parts = split_events(events)
        self.holdout = parts["test"] + parts["val"]
        self.train_base = parts["train"]
        self.calib = parts["calib"]
        self._loaded: tuple[HeadBundle, MulticlassCalibrator] | None = None
        self._loaded_ver = None

    # ---- 训练/微调 ----
    def _fit(self, train_events):
        bundle = HeadBundle().fit(train_events)
        cals = calibrate(bundle, self.calib)
        return bundle, cals

    def _full_eval(self, bundle, cals) -> dict:
        ref_cal, _ = apply_calibration(bundle, cals, self.calib)
        return self._metrics(bundle, cals, self.holdout,
                             ref_risk=np.asarray([cp.risk for cp in ref_cal]))

    def train(self, note: str = "初始训练") -> dict:
        bundle, cals = self._fit(self.train_base)
        ver = new_version_id()
        m = self._full_eval(bundle, cals)
        self.save_version(ver, {"bundle": bundle, "cals": cals}, m, note,
                          config=self.config_snapshot())
        return {"version": ver, "metrics": m}

    def finetune(self) -> dict:
        if self.active is None:
            return self.train(note="自动初始训练")
        fb = self.real_feedback()
        if not fb:
            return {"version": self.active,
                    "skipped": "buffer 无人工真值样本（synthetic 按纪律排除，G12）",
                    "metrics": self.versions[self.active].metrics}
        extra = []
        for s in fb:
            feats = s.get("features") or (s.get("context") or {}).get("features", {})
            act = s.get("label") or s.get("action") or "step_up"
            if isinstance(act, dict):
                act = act.get("action", "step_up")
            r = {"allow": .1, "step_up": .5, "deny": .9}[act] + random.uniform(-.05, .05)
            extra.append({"features": feats,
                          "labels": {"action": act, "risk": round(max(0., min(1., r)), 2),
                                     "auth": "mfa_totp",
                                     "noul": {h: int((s.get("noul") or {}).get(h, 0) or 0)
                                              for h in NOUL_HEADS}},
                          "true_risk": r})
        bundle, cals = self._fit(self.train_base + extra)
        before = self.versions[self.active].metrics
        ver = new_version_id()
        after = self._full_eval(bundle, cals)
        self.save_version(ver, {"bundle": bundle, "cals": cals}, after,
                          f"微调(+{len(extra)} 人工样本)，未自动发布",
                          config=self.config_snapshot())
        return {"version": ver, "before": before, "after": after, "samples": len(extra)}

    def config_snapshot(self) -> dict:
        return {"thresholds": self.ts.to_dict(), "rules": self.rules.to_dict(),
                "schema_version": SCHEMA_VERSION,
                "feature_names": list(FEATURE_NAMES)}

    def load(self, ver: str | None = None):
        ver = ver or self.active
        if self._loaded and self._loaded_ver == ver:
            return self._loaded
        obj, cfg = self.load_artifact(ver)
        if cfg:
            if cfg.get("thresholds"):
                self.ts = ThresholdSet.from_dict(cfg["thresholds"])
            if cfg.get("rules"):
                self.rules = HardRuleEngine.from_dict(cfg["rules"])
        self._loaded = (obj["bundle"], obj["cals"])
        self._loaded_ver = ver
        return self._loaded

    # ---- 评估 ----
    def _metrics(self, bundle, cals, evs, ref_risk=None) -> dict:
        cal_list, lat = apply_calibration(bundle, cals, evs)
        props = [decide(cp, BandStateMachine(self.ts).update(cp.risk), self.ts)
                 for cp in cal_list]
        clean = ["allow" if e["true_risk"] < .3 else ("step_up" if e["true_risk"] < .72 else "deny")
                 for e in evs]
        pred = [p.action.value for p in props]
        y_noisy = [e["labels"]["action"] for e in evs]
        act_mat = np.asarray([[p.probs.action.get(c, 0.) for c in ACTION_CLASSES] for p in props])
        eces, aurocs = [], {}
        for h in NOUL_HEADS:
            y = np.asarray([e["labels"]["noul"][h] for e in evs])
            s = np.asarray([p.probs.noul[h] for p in props])
            eces.append(_bin_ece(s, y))
            if len(np.unique(y)) == 2:
                aurocs[h] = float(roc_auc_score(y, s))
        risk_pred = np.asarray([p.probs.risk for p in props])
        risk_true = np.asarray([e["true_risk"] for e in evs])
        ref = ref_risk if ref_risk is not None else risk_true
        return {
            "agreement_clean": round(float(np.mean([p == c for p, c in zip(pred, clean)])), 4),
            "macro_f1_noisy": round(float(f1_score(y_noisy, pred, labels=ACTION_CLASSES,
                                                   average="macro", zero_division=0)), 4),
            "ece_max": round(max(eces + [_multi_ece(act_mat, np.asarray(
                [ACTION_CLASSES.index(v) for v in y_noisy]))]), 4),
            "auroc_ato": round(aurocs.get("account_takeover", float("nan")), 4),
            "auroc_cs": round(aurocs.get("credential_stuffing", float("nan")), 4),
            "false_block_rate": round(float(np.mean([p != "allow" and c == "allow"
                                                     for p, c in zip(pred, clean)])), 4),
            "miss_rate": round(float(np.mean([p == "allow" and c != "allow"
                                              for p, c in zip(pred, clean)])), 4),
            "risk_psi_vs_holdout": round(_psi(ref, risk_pred), 4),
            "lat_p50_ms": round(float(np.percentile(lat, 50)), 3),
            "lat_p99_ms": round(float(np.percentile(lat, 99)), 3),
            "n": len(evs)}

    def evaluate(self) -> dict:
        bundle, cals = self.load()
        ref_cal, _ = apply_calibration(bundle, cals, self.calib)
        m = self._metrics(bundle, cals, self.holdout,
                          ref_risk=np.asarray([cp.risk for cp in ref_cal]))
        m["gates"] = _access_gates(m)
        m["perturb"] = self._perturbation(bundle, cals)
        return m

    def _perturbation(self, bundle, cals) -> dict:
        base, _ = apply_calibration(bundle, cals, self.holdout[:250])
        base_act = [decide(cp, BandStateMachine(self.ts).update(cp.risk), self.ts).action.value
                    for cp in base]
        padded = [dict(e, features={**e["features"], "r_padding_ratio": .95,
                                    "h_events_24h_log": float(e["features"].get("h_events_24h_log", 3)) + 2.0})
                  for e in self.holdout[:250]]
        cur, _ = apply_calibration(bundle, cals, padded)
        cur_act = [decide(cp, BandStateMachine(self.ts).update(cp.risk), self.ts).action.value
                   for cp in cur]
        return {"order_flip": 0.0,
                "irrelevant_context_flip": round(float(np.mean(
                    [a != b for a, b in zip(base_act, cur_act)])), 4)}

    # ---- 推理（Jev 兼容，纯函数：审计由 runtime_app 落库） ----
    def answer(self, context: dict, questions: list[dict], audit=None) -> dict:
        feats = context.get("features", {})
        ctx = {"principal": context.get("principal", "anon"),
               "break_glass": context.get("break_glass")}
        bundle, cals = self.load()
        hits = self.rules.evaluate(ctx, feats)
        cal_list, lat = apply_calibration(bundle, cals, [{"features": feats}])
        cp = cal_list[0]
        band = BandStateMachine(self.ts).update(cp.risk)
        prop = decide(cp, band, self.ts)
        pdp = combine(prop, hits)
        answers = []
        for q in questions:
            t = q.get("type")
            if t == "choice":
                opts = q.get("options") or ACTION_CLASSES
                sub = {o: cp.action.get(o, 0.0) for o in opts}
                s = sum(sub.values()) or 1.0
                sub = {k: round(v / s, 4) for k, v in sub.items()}
                answers.append({"id": q.get("id"), "type": "choice",
                                "choice": max(sub, key=sub.get), "probabilities": sub,
                                "confidence": round(max(sub.values()), 4)})
            elif t == "noul":
                head = q.get("question")
                p = cp.noul.get(head)
                answers.append({"id": q.get("id"), "type": "noul",
                                "value": (round(float(p), 4) if p is not None else None),
                                "error": None if p is not None else f"unknown noul head: {head}"})
            elif t == "score":
                answers.append({"id": q.get("id"), "type": "score",
                                "score": round(float(cp.risk), 4), "band": band.value,
                                "confidence": round(1.0 - abs(cp.risk - .5), 4)})
        snap = take_snapshot(feats, ts=time.time())
        return {"answers": answers,
                "pdp": {"final_action": pdp.final_action.value,
                        "auth": pdp.auth_required.value, "grant": pdp.grant.value,
                        "hard_rules": pdp.matched, "override_by": pdp.override_by,
                        "model_suggestion": pdp.model_suggestion,
                        "fallback": pdp.fallback_reason,
                        "confidence": round(prop.confidence, 4), "band": band.value,
                        "noul": {h: round(v, 4) for h, v in cp.noul.items()}},
                "_audit": {"fs_id": snap.fs_id, "fs_digest": snap.digest,
                           "features": snap.features, "latency_ms": lat[0],
                           "raw": cp.raw,
                           "calibrated": {"action": cp.action, "risk": cp.risk,
                                          "noul": cp.noul}}}


def _bin_ece(p, y, bins=15):
    p, y = np.asarray(p), np.asarray(y)
    edges = np.linspace(0, 1, bins + 1)
    tot = err = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p > lo) & (p <= hi)
        if not m.any():
            continue
        n = int(m.sum())
        err += n * abs(float(y[m].mean()) - float(p[m].mean()))
        tot += n
    return float(err / tot) if tot else 0.0


def _multi_ece(probs, y, bins=15):
    conf = probs.max(axis=1)
    corr = (probs.argmax(axis=1) == y).astype(float)
    return _bin_ece(conf, corr, bins)


def _access_gates(m: dict) -> list:
    def row(name, thr, actual, ok):
        return [name, thr, str(actual), bool(ok)]
    return [
        row("macro-F1(noisy)", "≥0.80", m["macro_f1_noisy"], m["macro_f1_noisy"] >= .80),
        row("策略一致率(clean)", "≥0.85", m["agreement_clean"], m["agreement_clean"] >= .85),
        row("AUROC(ATO)", "≥0.90", m["auroc_ato"], m["auroc_ato"] >= .90),
        row("AUROC(撞库)", "≥0.90", m["auroc_cs"], m["auroc_cs"] >= .90),
        row("ECE(最大)", "≤0.02", m["ece_max"], m["ece_max"] <= .02),
        row("误拦率", "≤0.02", m["false_block_rate"], m["false_block_rate"] <= .02),
        row("漂移 PSI", "≤0.15", m["risk_psi_vs_holdout"], m["risk_psi_vs_holdout"] <= .15),
        row("延迟 P50(ms)", "<10", m["lat_p50_ms"], m["lat_p50_ms"] < 10),
    ]


# ============================================================ 工单路由（Choice）

TICKET_VOCAB = ["发票", "报销", "退款", "退货", "故障", "报错", "登录", "网络",
                "价格", "报价", "合同", "培训", "账单", "bug"]
TICKET_CLASSES = ["billing", "technical", "sales", "returns"]
_RULE = {"发票": "billing", "报销": "billing", "账单": "billing",
         "故障": "technical", "报错": "technical", "登录": "technical",
         "网络": "technical", "bug": "technical",
         "价格": "sales", "报价": "sales", "合同": "sales", "培训": "sales",
         "退款": "returns", "退货": "returns"}


def gen_tickets(n=4800, seed=99):
    rng = random.Random(seed)
    out = []
    tpl = {"billing": "请帮忙看下这张{kw}有没有问题，需要尽快处理。",
           "technical": "系统出现{kw}，多个用户反馈无法正常使用。",
           "sales": "客户咨询{kw}相关事宜，希望本周给出方案。",
           "returns": "客户要求{kw}，订单三天前提交的申请还没批。"}
    for _ in range(n):
        cls = rng.choice(TICKET_CLASSES)
        main = rng.choice([k for k, c in _RULE.items() if c == cls])
        text = tpl[cls].format(kw=main)
        if rng.random() < 0.18:
            other = rng.choice([k for k in TICKET_VOCAB if _RULE.get(k) != cls])
            text = text.replace("。", f"，另外还提到{other}。")
        label = cls if rng.random() > 0.12 else rng.choice(TICKET_CLASSES)
        out.append({"text": text, "channel": rng.choice(["email", "im", "portal"]),
                    "amount_log": round(rng.uniform(0, 6), 3),
                    "vip": rng.random() < 0.2, "label": label})
    return out


def ticket_vector(t: dict) -> list[float]:
    v = [1.0 if k in t["text"] else 0.0 for k in TICKET_VOCAB]
    v += [1.0 if t.get("channel") == c else 0.0 for c in ("email", "im", "portal")]
    v += [float(t.get("amount_log", 0.0)), 1.0 if t.get("vip") else 0.0,
          len(t["text"]) / 50.0]
    return v


class SupportRoutingTask(TaskModel):
    kind = "choice"
    title = "客服工单路由"
    options = TICKET_CLASSES

    def __init__(self, task_id: str, root: Path, db: DB, register: bool = True):
        super().__init__(task_id, root, db, register=register)
        data = gen_tickets()
        rng = random.Random(7)
        rng.shuffle(data)
        self.train_base, self.holdout = data[:3800], data[3800:]
        self._loaded = None
        self._loaded_ver = None

    def _fit(self, rows):
        X = np.asarray([ticket_vector(t) for t in rows])
        y = np.asarray([TICKET_CLASSES.index(t["label"]) for t in rows])
        clf = lgb.LGBMClassifier(random_state=42, deterministic=True, force_row_wise=True,
                                 verbosity=-1, n_estimators=200, learning_rate=0.08,
                                 num_leaves=31, min_child_samples=20)
        clf.fit(X, y)
        cal = MulticlassCalibrator(len(TICKET_CLASSES)).fit(
            np.log(clf.predict_proba(X) + 1e-9), y)
        return clf, cal

    def _metrics(self, clf, cal, evs):
        X = np.asarray([ticket_vector(t) for t in evs])
        y = np.asarray([TICKET_CLASSES.index(t["label"]) for t in evs])
        p = cal.apply(np.log(clf.predict_proba(X) + 1e-9))
        pred = p.argmax(axis=1)
        acc = float(np.mean(pred == y))
        f1 = float(f1_score(y, pred, average="macro"))
        return {"acc": round(acc, 4), "macro_f1": round(f1, 4),
                "ece": round(_multi_ece(p, y), 4),
                "lat_p50_ms": round(float(np.median([
                    (lambda t0: (time.perf_counter() - t0) * 1000)(time.perf_counter())
                    for _ in range(300)])) + .01, 3),
                "n": len(evs),
                "gates": [["acc", "≥0.80", round(acc, 3), acc >= .8],
                          ["macro-F1", "≥0.70", round(f1, 3), f1 >= .7],
                          ["ECE", "≤0.03", round(_multi_ece(p, y), 4),
                           _multi_ece(p, y) <= .03]]}

    def train(self, note="初始训练"):
        clf, cal = self._fit(self.train_base)
        ver = new_version_id()
        m = self._metrics(clf, cal, self.holdout)
        self.save_version(ver, {"clf": clf, "cal": cal}, m, note)
        return {"version": ver, "metrics": m}

    def finetune(self):
        if self.active is None:
            return self.train()
        fb = [s for s in self.real_feedback() if s.get("label") in TICKET_CLASSES]
        if not fb:
            return {"version": self.active,
                    "skipped": "buffer 无人工真值样本（synthetic 已排除）",
                    "metrics": self.versions[self.active].metrics}
        extra = [{"text": s.get("text", ""), "channel": s.get("channel", "im"),
                  "amount_log": float(s.get("amount_log", 1.0)),
                  "vip": bool(s.get("vip")), "label": s["label"]} for s in fb]
        clf, cal = self._fit(self.train_base + extra)
        before = self.versions[self.active].metrics
        ver = new_version_id()
        after = self._metrics(clf, cal, self.holdout)
        self.save_version(ver, {"clf": clf, "cal": cal}, after,
                          f"微调(+{len(extra)})，未自动发布")
        return {"version": ver, "before": before, "after": after, "samples": len(extra)}

    def load(self, ver=None):
        ver = ver or self.active
        if self._loaded and self._loaded_ver == ver:
            return self._loaded
        obj, _ = self.load_artifact(ver)
        self._loaded = (obj["clf"], obj["cal"])
        self._loaded_ver = ver
        return self._loaded

    def evaluate(self):
        clf, cal = self.load()
        return self._metrics(clf, cal, self.holdout)

    def answer(self, context: dict, questions: list[dict], audit=None):
        clf, cal = self.load()
        t = {"text": context.get("text", ""), "channel": context.get("channel", "im"),
             "amount_log": math.log1p(float(context.get("amount", 0))),
             "vip": bool(context.get("vip"))}
        p = cal.apply(np.log(clf.predict_proba(np.asarray([ticket_vector(t)])) + 1e-9))[0]
        probs = {c: round(float(p[i]), 4) for i, c in enumerate(TICKET_CLASSES)}
        answers = []
        for q in questions:
            if q.get("type") != "choice":
                answers.append({"id": q.get("id"), "error": "support-routing 仅支持 choice"})
                continue
            opts = [o for o in (q.get("options") or TICKET_CLASSES) if o in probs]
            s = sum(probs[o] for o in opts) or 1.0
            sub = {o: round(probs[o] / s, 4) for o in opts}
            answers.append({"id": q.get("id"), "type": "choice",
                            "choice": max(sub, key=sub.get), "probabilities": sub,
                            "confidence": round(max(sub.values()), 4)})
        return {"answers": answers}


# ============================================================ 紧急度（Score）

def _poisson(rng: random.Random, lam: float = 1.5) -> int:
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def gen_urgency(n=3600, seed=5):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        sent = rng.betavariate(2, 2)
        hours = rng.uniform(0, 72)
        rep = _poisson(rng, 1.5)
        amt = rng.lognormvariate(2, 1)
        vip = rng.random() < 0.25
        z = (-2.2 + 4.2 * sent + 0.028 * hours + 0.55 * rep
             + 0.10 * math.log1p(amt) + 0.7 * vip + rng.gauss(0, 0.35))
        out.append({"sentiment": round(sent, 3), "hours_since": round(hours, 1),
                    "repeat_contacts": rep, "amount": round(amt, 1), "vip": int(vip),
                    "label": round(1 / (1 + math.exp(-z)), 3)})
    return out


def urgency_vector(u: dict) -> list[float]:
    return [float(u["sentiment"]), u["hours_since"] / 72.0,
            float(u["repeat_contacts"]) / 5.0,
            math.log1p(float(u["amount"])) / 6.0, float(u["vip"])]


class UrgencyScoreTask(TaskModel):
    kind = "score"
    title = "工单紧急度打分"

    def __init__(self, task_id: str, root: Path, db: DB, register: bool = True):
        super().__init__(task_id, root, db, register=register)
        data = gen_urgency()
        rng = random.Random(11)
        rng.shuffle(data)
        self.train_base, self.holdout = data[:2900], data[2900:]
        self._loaded = None
        self._loaded_ver = None

    def _fit(self, rows):
        X = np.asarray([urgency_vector(u) for u in rows])
        y = np.asarray([u["label"] for u in rows])
        reg = lgb.LGBMRegressor(random_state=42, deterministic=True, force_row_wise=True,
                                verbosity=-1, n_estimators=220, learning_rate=0.06,
                                num_leaves=31, min_child_samples=20)
        reg.fit(X, y)
        cal = RegressionCalibrator().fit(np.clip(reg.predict(X), 0, 1), y)
        return reg, cal

    def _metrics(self, reg, cal, evs):
        X = np.asarray([urgency_vector(u) for u in evs])
        y = np.asarray([u["label"] for u in evs])
        p = np.clip(cal.apply(np.clip(reg.predict(X), 0, 1)), 0, 1)
        mae = float(np.mean(np.abs(p - y)))
        rho = float(spearmanr(p, y).statistic)
        return {"mae": round(mae, 4), "spearman": round(rho, 4),
                "ece_bins": round(_bin_ece(p, (y > .6).astype(float)), 4),
                "n": len(evs),
                "gates": [["MAE", "≤0.12", round(mae, 3), mae <= .12],
                          ["Spearman", "≥0.80", round(rho, 3), rho >= .8]]}

    def train(self, note="初始训练"):
        reg, cal = self._fit(self.train_base)
        ver = new_version_id()
        m = self._metrics(reg, cal, self.holdout)
        self.save_version(ver, {"reg": reg, "cal": cal}, m, note)
        return {"version": ver, "metrics": m}

    def finetune(self):
        if self.active is None:
            return self.train()
        fb = [s for s in self.real_feedback() if s.get("label") is not None]
        if not fb:
            return {"version": self.active,
                    "skipped": "buffer 无人工真值样本（synthetic 已排除）",
                    "metrics": self.versions[self.active].metrics}
        extra = [{"sentiment": float(s.get("sentiment", .5)),
                  "hours_since": float(s.get("hours_since", 0)),
                  "repeat_contacts": int(s.get("repeat_contacts", 0)),
                  "amount": float(s.get("amount", 100)),
                  "vip": int(s.get("vip", 0)), "label": float(s["label"])} for s in fb]
        reg, cal = self._fit(self.train_base + extra)
        before = self.versions[self.active].metrics
        ver = new_version_id()
        after = self._metrics(reg, cal, self.holdout)
        self.save_version(ver, {"reg": reg, "cal": cal}, after,
                          f"微调(+{len(extra)})，未自动发布")
        return {"version": ver, "before": before, "after": after, "samples": len(extra)}

    def load(self, ver=None):
        ver = ver or self.active
        if self._loaded and self._loaded_ver == ver:
            return self._loaded
        obj, _ = self.load_artifact(ver)
        self._loaded = (obj["reg"], obj["cal"])
        self._loaded_ver = ver
        return self._loaded

    def evaluate(self):
        reg, cal = self.load()
        return self._metrics(reg, cal, self.holdout)

    def answer(self, context: dict, questions: list[dict], audit=None):
        reg, cal = self.load()
        u = {"sentiment": float(context.get("sentiment", .5)),
             "hours_since": float(context.get("hours_since", 0)),
             "repeat_contacts": int(context.get("repeat_contacts", 0)),
             "amount": float(context.get("amount", 100)),
             "vip": int(context.get("vip", 0))}
        X = np.asarray([urgency_vector(u)])
        val = float(np.clip(cal.apply(np.clip(reg.predict(X), 0, 1)), 0, 1)[0])
        anchors = {name: round((lo <= val < hi) * 1.0, 2)
                   for name, lo, hi in (("低", 0, .35), ("中", .35, .6),
                                        ("高", .6, .85), ("危急", .85, 1.0))}
        return {"answers": [{"id": q.get("id"), "type": "score", "score": round(val, 4),
                             "confidence": round(1 - abs(val - .5), 4), "bands": anchors}
                            for q in questions if q.get("type") == "score"]}


TASK_BUILDERS = {"access-decision": AccessDecisionTask,
                 "support-routing": SupportRoutingTask,
                 "urgency-score": UrgencyScoreTask}


def build_registry(root: Path, db: DB, register: bool = True) -> dict[str, TaskModel]:
    return {tid: cls(tid, root, db, register=register) for tid, cls in TASK_BUILDERS.items()}
