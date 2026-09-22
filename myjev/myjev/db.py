"""MyJev 数据层（docs/03 v3.1 实现）：SQLite 单文件库 + 双进程写权分离 + 不可变审计。

写权矩阵（03 §3）：
  runtime :  decisions, access_log                （其余表只读）
  admin   :  除 decisions/access_log 外的管理表   （日志表只读）
用 connection.set_authorizer 在 SQLite 层强制，越权写抛 OperationalError('not authorized')。
decisions 表另有触发器禁 UPDATE/DELETE（任何角色，含 admin）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2"

RUNTIME_WRITE = {"decisions", "access_log", "sqlite_sequence"}
ADMIN_WRITE = {"meta", "tasks", "model_versions", "buffer_samples", "datasets",
               "publish_events", "bench_runs", "config_sets", "sqlite_sequence"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
  model_kind TEXT NOT NULL DEFAULT 'gbm-multitask', active_version TEXT);
CREATE TABLE IF NOT EXISTS model_versions(
  task_id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT, model_kind TEXT,
  status TEXT NOT NULL DEFAULT 'idle', created REAL, note TEXT,
  metrics_json TEXT, artifact_path TEXT, artifact_sha256 TEXT,
  PRIMARY KEY(task_id, version));
CREATE TABLE IF NOT EXISTS buffer_samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, src TEXT,
  payload_json TEXT, included INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS datasets(
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, task_id TEXT, path TEXT,
  sha256 TEXT, row_count INTEGER, fields_ver INTEGER DEFAULT 1,
  provenance TEXT, created REAL);
CREATE TABLE IF NOT EXISTS decisions(
  decision_id TEXT PRIMARY KEY, ts REAL, task_id TEXT, model_version TEXT,
  publish_epoch INTEGER, fs_digest TEXT, raw_json TEXT, calibrated_json TEXT,
  final_json TEXT, latency_ms REAL, exploration INTEGER DEFAULT 0);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT,'decisions immutable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT,'decisions immutable'); END;
CREATE TABLE IF NOT EXISTS access_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, decision_id TEXT,
  task_id TEXT, session_key TEXT, event TEXT, ttl_left REAL,
  detail_json TEXT, api_key_seen TEXT);
CREATE TABLE IF NOT EXISTS publish_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT,
  from_ver TEXT, to_ver TEXT, actor TEXT, epoch_after INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS bench_runs(
  id TEXT PRIMARY KEY, ts REAL, task_id TEXT, dataset_id TEXT,
  providers_json TEXT, sample_n INTEGER, results_json TEXT);
CREATE TABLE IF NOT EXISTS config_sets(
  id TEXT PRIMARY KEY, kind TEXT, payload_json TEXT, created REAL, note TEXT);
"""


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def canon_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


class DB:
    """薄封装。role ∈ {init, admin, runtime}；init 仅用于建库/迁移。"""

    def __init__(self, path: str | Path, role: str = "init"):
        self.path = str(path)
        self.role = role
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._allowed_write = (
            RUNTIME_WRITE | ADMIN_WRITE | {"sqlite_sequence"} if role == "init" else
            RUNTIME_WRITE if role == "runtime" else
            ADMIN_WRITE if role == "admin" else set())
        self.conn.set_authorizer(self._authorize)

    # ----------------------------------------------------- authorizer
    _WRITE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
                      sqlite3.SQLITE_DELETE, sqlite3.SQLITE_ALTER_TABLE,
                      sqlite3.SQLITE_DROP_TABLE}
    if hasattr(sqlite3, "SQLITE_CREATE_TABLE"):
        _WRITE_ACTIONS |= {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
                           sqlite3.SQLITE_CREATE_TRIGGER}

    def _authorize(self, action, arg1, arg2, dbname, trigger):
        # 注意：本 Python 发行版 authorizer 语义 = 返回 sqlite 结果码：
        # 0(SQLITE_OK)=允许、1(SQLITE_DENY)=拒绝（实测，勿凭文档直觉）。
        if self._allowed_write is None:     # init_schema 期间临时放开
            return sqlite3.SQLITE_OK
        if action not in self._WRITE_ACTIONS:
            return sqlite3.SQLITE_OK        # 读/杂项放行
        table = (arg1 or "").lower()
        allowed = table in {t.lower() for t in self._allowed_write}
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

    # ----------------------------------------------------- schema/init
    def init_schema(self):
        old = self._allowed_write
        self._allowed_write = None
        try:
            self.conn.executescript(_SCHEMA)
            if not self.meta_get("schema_version"):
                self.meta_set("schema_version", SCHEMA_VERSION)
                self.meta_set("publish_epoch", "0")
            self.conn.commit()
        finally:
            self._allowed_write = old

    def _w(self, sql, params=()):
        with self.lock:
            try:
                cur = self.conn.execute(sql, params)
                self.conn.commit()
                return cur
            except Exception:
                try:
                    self.conn.rollback()        # 防异常后隐式事务悬挂→全库锁死
                except Exception:
                    pass
                raise

    # ----------------------------------------------------- meta/epoch
    def meta_get(self, key):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key, value):
        self._w("INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def epoch(self) -> int:
        try:
            return int(self.meta_get("publish_epoch") or 0)
        except ValueError:
            return 0

    # ----------------------------------------------------- tasks/versions
    def upsert_task(self, task_id, kind, title, model_kind="gbm-multitask"):
        self._w("INSERT INTO tasks(id,kind,title,model_kind) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,title=excluded.title",
                (task_id, kind, title, model_kind))

    def tasks(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM tasks ORDER BY id")]

    def set_model_kind(self, task_id, kind):
        self._w("UPDATE tasks SET model_kind=? WHERE id=?", (kind, task_id))

    def add_version(self, task_id, version, kind, model_kind, metrics,
                    artifact_path, artifact_sha256, note=""):
        self._w("INSERT OR REPLACE INTO model_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, version, kind, model_kind, "idle", time.time(), note,
                 canon_json(metrics), str(artifact_path), artifact_sha256))

    def versions(self, task_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM model_versions WHERE task_id=? ORDER BY created", (task_id,))]

    def version(self, task_id, version):
        r = self.conn.execute("SELECT * FROM model_versions WHERE task_id=? AND version=?",
                              (task_id, version)).fetchone()
        return dict(r) if r else None

    def publish(self, task_id, version, actor="admin", note="", force=False) -> int:
        """原子发布：校验工件摘要 → 换 active → bump epoch → 留痕。返回新 epoch。
        幂等：已是 active 且非 force 时返回当前 epoch（不重复 bump）；
        带新配置的重发布用 force=True 强制 bump（驱动 A 换入新 config）。"""
        with self.lock:
            try:
                return self._publish_inner(task_id, version, actor, note, force)
            finally:
                try:
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()

    def _publish_inner(self, task_id, version, actor, note, force=False):
        row = self.conn.execute("SELECT * FROM model_versions WHERE task_id=? AND version=?",
                                (task_id, version)).fetchone()
        if not row:
            raise ValueError(f"版本不存在: {task_id}@{version}")
        p = Path(row["artifact_path"])
        if not p.exists():
            raise ValueError(f"工件缺失: {p}")
        if sha256_file(p) != row["artifact_sha256"]:
            raise ValueError("工件摘要不符，拒绝发布（防替换）")
        t = self.conn.execute("SELECT active_version FROM tasks WHERE id=?",
                              (task_id,)).fetchone()
        if t and t["active_version"] == version and not force:
            return self.epoch()
        prev = t["active_version"] if t else None
        self.conn.execute("UPDATE tasks SET active_version=? WHERE id=?", (version, task_id))
        ep = self.epoch() + 1
        self.conn.execute("INSERT INTO meta(key,value) VALUES('publish_epoch',?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(ep),))
        self.conn.execute("INSERT INTO publish_events(ts,task_id,from_ver,to_ver,actor,epoch_after,note) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (time.time(), task_id, prev, version, actor, ep, note))
        self.conn.commit()
        return ep

    def publish_events(self, limit=20):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM publish_events ORDER BY id DESC LIMIT ?", (limit,))]

    # ----------------------------------------------------- buffer (G12)
    def add_buffer(self, task_id, payload: dict, src: str) -> int:
        included = 0 if src == "synthetic" else 1     # 写入侧纪律（03 §2）
        cur = self._w("INSERT INTO buffer_samples(task_id,src,payload_json,included,created) "
                      "VALUES(?,?,?,?,?)",
                      (task_id, src, canon_json(payload), included, time.time()))
        return cur.lastrowid

    def buffer_rows(self, task_id, only_included=False):
        q = "SELECT * FROM buffer_samples WHERE task_id=?" + (" AND included=1" if only_included else "")
        return [dict(r) for r in self.conn.execute(q, (task_id,))]

    def buffer_count(self, task_id) -> tuple[int, int]:
        r = self.conn.execute("SELECT COUNT(*) c, COALESCE(SUM(included),0) i "
                              "FROM buffer_samples WHERE task_id=?", (task_id,)).fetchone()
        return int(r["c"]), int(r["i"])

    # ----------------------------------------------------- decisions/access_log (G4)
    def log_decision(self, *, decision_id, ts, task_id, model_version, epoch,
                     fs_digest, raw, calibrated, final, latency_ms, exploration=False):
        self._w("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, ts, task_id, model_version, epoch, fs_digest,
                 canon_json(raw), canon_json(calibrated), canon_json(final),
                 latency_ms, 1 if exploration else 0))

    def log_access(self, *, ts=None, decision_id=None, task_id=None, session_key=None,
                   event, ttl_left=None, detail=None, api_key=None):
        self._w("INSERT INTO access_log(ts,decision_id,task_id,session_key,event,ttl_left,detail_json,api_key_seen) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ts or time.time(), decision_id, task_id, session_key, event, ttl_left,
                 canon_json(detail) if detail is not None else None, api_key))

    def decisions_page(self, limit=40, task_id=None):
        q, p = "SELECT * FROM decisions", []
        if task_id:
            q += " WHERE task_id=?"; p.append(task_id)
        q += " ORDER BY ts DESC LIMIT ?"; p.append(limit)
        return [dict(r) for r in self.conn.execute(q, p)]

    def access_stats(self):
        tot = self.conn.execute("SELECT COUNT(*) c FROM access_log").fetchone()["c"]
        by = {r["event"]: r["c"] for r in self.conn.execute(
            "SELECT event, COUNT(*) c FROM access_log GROUP BY event")}
        return {"total": tot, "by_event": by}

    def last_reload(self, task_id=None):
        q = ("SELECT * FROM access_log WHERE event LIKE 'reload%' " +
             ("AND task_id=?" if task_id else "") + " ORDER BY seq DESC LIMIT 1")
        r = self.conn.execute(q, (task_id,) if task_id else ()).fetchone()
        return dict(r) if r else None

    def integrity_append(self):
        """周快照登记：把当前 decisions 计数/哈希摘要记入 meta.integrity_ledger。"""
        r = self.conn.execute("SELECT COUNT(*) c, COALESCE(MAX(decision_id),'-') m FROM decisions").fetchone()
        led = json.loads(self.meta_get("integrity_ledger") or "[]")
        led.append({"ts": time.time(), "decisions": r["c"], "max_id": r["m"], "epoch": self.epoch()})
        self.meta_set("integrity_ledger", canon_json(led[-200:]))

    # ----------------------------------------------------- datasets / bench / configs
    def register_dataset(self, *, name, kind, path, row_count, provenance, task_id=None,
                         fields_ver=1) -> str:
        did = "ds-" + uuid.uuid4().hex[:8]
        self._w("INSERT INTO datasets(id,name,kind,task_id,path,sha256,row_count,fields_ver,provenance,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (did, name, kind, task_id, str(path), sha256_file(path),
                 row_count, fields_ver, provenance, time.time()))
        return did

    def datasets(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM datasets ORDER BY created")]

    def dataset(self, ds_id):
        r = self.conn.execute("SELECT * FROM datasets WHERE id=?", (ds_id,)).fetchone()
        return dict(r) if r else None

    def delete_dataset(self, ds_id) -> bool:
        d = self.dataset(ds_id)
        if not d or not str(d["kind"]).startswith("upload"):
            return False                      # 内置与注册基线不可删（01 §2.3）
        self._w("DELETE FROM datasets WHERE id=?", (ds_id,))
        Path(d["path"]).unlink(missing_ok=True)
        return True

    def add_bench_run(self, task_id, dataset_id, providers, sample_n, results):
        self._w("INSERT INTO bench_runs VALUES(?,?,?,?,?,?,?)",
                ("br-" + uuid.uuid4().hex[:8], time.time(), task_id, dataset_id,
                 canon_json(providers), sample_n, canon_json(results)))

    def add_config_set(self, cid, kind, payload, note="") -> str:
        self._w("INSERT OR REPLACE INTO config_sets VALUES(?,?,?,?,?)",
                (cid, kind, canon_json(payload), time.time(), note))
        return cid

    def config_sets(self, kind=None):
        q = "SELECT * FROM config_sets" + (" WHERE kind=?" if kind else "") + " ORDER BY created"
        return [dict(r) for r in self.conn.execute(q, (kind,) if kind else ())]

    def config_set(self, cid):
        r = self.conn.execute("SELECT * FROM config_sets WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
