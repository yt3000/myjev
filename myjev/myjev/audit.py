"""MyJev 审计决策日志（规格 §9）。

append-only JSONL + sha256 哈希链（WORM 语义的进程内平替）：
任何一条历史被篡改/删除，verify() 即失败——满足"决策可精确回放"的最小要求。
记录版本四元组、原始/校准概率、硬规则命中、人工覆盖、缓存与探索标志。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

GENESIS = "sha256:GENESIS"


def _h(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prev = GENESIS
        self._seq = 0
        if self.path.exists():
            last = self._tail()
            if last is not None:
                self._prev = last["entry_hash"]
                self._seq = last["seq"] + 1

    def _tail(self) -> dict | None:
        if not self.path.exists():
            return None
        last = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = json.loads(line)
        return last

    def append(self, *, decision_id: str, ts: float, version_quad: dict,
               fs_id: str, feature_digest: str, features: dict,
               raw: dict, calibrated: dict, final: dict,
               cache: dict | None = None, exploration: bool = False,
               latency_ms: float | None = None) -> dict:
        entry: dict[str, Any] = {
            "seq": self._seq, "ts": ts, "decision_id": decision_id,
            **version_quad,
            "fs_id": fs_id, "feature_digest": feature_digest,
            "feature_snapshot": features,          # §3.3 回放所需全量特征
            "raw_probs": raw, "calibrated_probs": calibrated,
            "final": final, "cache": cache,
            "exploration": exploration, "latency_ms": latency_ms,
        }
        entry["prev_hash"] = self._prev
        body = {k: v for k, v in entry.items() if k != "entry_hash"}
        entry["entry_hash"] = _h(body)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._prev = entry["entry_hash"]
            self._seq += 1
        return entry

    def iter_entries(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)

    def verify(self) -> tuple[bool, str]:
        """哈希链校验：篡改任意字段或删条目都会在此暴露。"""
        prev = GENESIS
        seq = 0
        n = 0
        for e in self.iter_entries():
            body = {k: v for k, v in e.items() if k != "entry_hash"}
            if _h(body) != e.get("entry_hash"):
                return False, f"entry seq={e.get('seq')} 内容哈希不符（疑似字段篡改）"
            if e.get("prev_hash") != prev:
                return False, f"entry seq={e.get('seq')} 链断裂（疑似条目删除/插入）"
            if e.get("seq") != seq:
                return False, f"entry seq 不连续：期望 {seq} 实得 {e.get('seq')}"
            prev, seq, n = e["entry_hash"], seq + 1, n + 1
        return True, f"链完整，共 {n} 条"
