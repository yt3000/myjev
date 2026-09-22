"""MyJev 训练-校准-评测流水线（M2→M3 的进程内平替）。"""
from __future__ import annotations

import json
from pathlib import Path

from .audit import AuditLog
from .contract import SCHEMA_VERSION
from .data import generate, split_events
from .features import feature_digest
from .metrics import evaluate, gate_table
from .models import (HeadBundle, MODEL_ID, MODEL_VERSION, apply_calibration,
                     calibrate, save)
from .thresholds import BandStateMachine, ThresholdSet, decide


def train_and_report(events_per_day: int = 240, seed: int = 20260921,
                     outdir: str | Path = "artifacts") -> dict:
    """返回 {service_parts, eval, calib_report, gates}。"""
    outdir = Path(outdir)
    ts = ThresholdSet()
    events = generate(events_per_day=events_per_day, seed=seed)
    parts = split_events(events)
    bundle = HeadBundle().fit(parts["train"])
    cals = calibrate(bundle, parts["calib"])
    quad = {
        "model_id": MODEL_ID, "model_version": MODEL_VERSION,
        "encoder_version": "none-v0", "threshold_set_id": ts.threshold_set_id,
        "schema_version": SCHEMA_VERSION, "feature_digest": feature_digest(),
    }
    save(bundle, cals, feature_digest(), ts.threshold_set_id, SCHEMA_VERSION, outdir)

    def full_eval(win: str):
        evs = parts[win]
        cal_list, lat = apply_calibration(bundle, cals, evs)
        props = [decide(cp, BandStateMachine(ts).update(cp.risk), ts)
                 for cp in cal_list]  # 评测按独立决策（不串会话迟滞）
        return evaluate(props, lat, evs, ts)

    ev_val, ev_test = full_eval("val"), full_eval("test")
    gates = gate_table(ev_test)
    return {
        "bundle": bundle, "cals": cals, "thresholds": ts, "quad": quad,
        "events": events, "parts": parts,
        "eval_val": ev_val, "eval_test": ev_test,
        "gates": gates, "calib_report": cals.report(), "outdir": str(outdir),
    }
