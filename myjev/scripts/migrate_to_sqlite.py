"""G3 迁移：旧 JSON 状态 → SQLite（docs/03 §6）。幂等：已迁移的跳过。

用法（停服状态下）：python scripts/migrate_to_sqlite.py
读取 data/models/<task>/index.json + <ver>/model.pkl、data/buffer/*.jsonl、
data/decision_log.jsonl（校验哈希链），写入 data/myjev.db 并把原件挪到 data/legacy/。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myjev.db import DB, sha256_file
from myjev.pdp import HardRuleEngine
from myjev.thresholds import ThresholdSet

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TASK_KINDS = {"access-decision": "systemone", "support-routing": "choice",
              "urgency-score": "score"}
TITLES = {"access-decision": "零信任访问决策", "support-routing": "客服工单路由",
          "urgency-score": "工单紧急度打分"}


def main():
    DATA.mkdir(exist_ok=True)
    db = DB(DATA / "myjev.db", role="init")
    db.init_schema()
    legacy = DATA / "legacy"
    legacy.mkdir(exist_ok=True)
    moved = 0

    # 1) 版本与 active
    for task, kind in TASK_KINDS.items():
        idx = DATA / "models" / task / "index.json"
        if not idx.exists():
            continue
        d = json.loads(idx.read_text(encoding="utf-8"))
        db.upsert_task(task, kind, TITLES[task])
        for vid, v in d.get("versions", {}).items():
            art = idx.parent / vid / "model.pkl"
            if not art.exists() or db.version(task, vid):
                continue
            dst_dir = DATA / "artifacts" / task / vid
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(art, dst_dir / "model.pkl")
            if kind == "systemone":
                (dst_dir / "config.json").write_text(json.dumps(
                    {"thresholds": ThresholdSet().to_dict(),
                     "rules": HardRuleEngine().to_dict()},
                    ensure_ascii=False, sort_keys=True), encoding="utf-8")
            db.add_version(task, vid, kind, "gbm-multitask", v.get("metrics", {}),
                           dst_dir / "model.pkl", sha256_file(dst_dir / "model.pkl"),
                           v.get("note", "迁移自旧版"))
            moved += 1
        if d.get("active") and db.version(task, d["active"]):
            if not next((t for t in db.tasks() if t["id"] == task
                         and t["active_version"]), None):
                db.publish(task, d["active"], note="迁移初始发布")

    # 2) buffer
    for f in (DATA / "buffer").glob("*.jsonl"):
        task = f.stem
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            s = json.loads(line)
            s.setdefault("_migrated", int(time.time()))
            db.add_buffer(task, s, s.get("src", "ui-feedback"))
        shutil.move(str(f), legacy / f"buffer-{f.name}")
        moved += 1

    # 3) 决策（旧哈希链校验通过才迁移）
    log = DATA / "decision_log.jsonl"
    if log.exists():
        import hashlib
        prev, n = "sha256:GENESIS", 0
        ok = True
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k != "entry_hash"}
            h = "sha256:" + hashlib.sha256(json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                default=str).encode()).hexdigest()
            if h != e.get("entry_hash") or e.get("prev_hash") != prev:
                ok = False
                break
            prev = e["entry_hash"]
            n += 1
        if ok:
            for line in log.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                if db.decisions_page(10 ** 9, None) and e["decision_id"] in {
                        r["decision_id"] for r in db.decisions_page(10 ** 9)}:
                    continue
                db.log_decision(decision_id=e["decision_id"], ts=e["ts"],
                                task_id=e.get("model_id", "access-decision"),
                                model_version=e.get("model_version", ""),
                                epoch=0, fs_digest=e.get("feature_digest", ""),
                                raw=e.get("raw_probs", {}),
                                calibrated=e.get("calibrated_probs", {}),
                                final=e.get("final", {}),
                                latency_ms=e.get("latency_ms"),
                                exploration=e.get("exploration", False))
                db.log_access(ts=e["ts"], decision_id=e["decision_id"],
                              task_id=e.get("model_id"), event="compute",
                              detail={"migrated": True})
                moved += 1
            shutil.move(str(log), legacy / "decision_log.jsonl.migrated")
        else:
            print("[migrate] ✗ 旧哈希链校验失败，decision_log 不迁移（人工核对后重试）")
            return 1

    (DATA / "models").rename(legacy / "models") if (DATA / "models").exists() else None
    print(f"[migrate] 完成：迁移对象 {moved} 个；publish_epoch={db.epoch()}；"
          f"原件在 data/legacy/")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
