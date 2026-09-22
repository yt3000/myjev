"""MyJev 服务后台（交付物 A，docs/01 §1）：纯推理 API 进程 :8091。

职责：/v1/systemone（Jev 兼容超集）、两层审计写入（decisions/access_log）、
发布热加载（epoch 轮询 + 双缓冲换入 + 失败回退留痕）、/health、/api/version。
无管理路由、无界面；仅绑定 127.0.0.1（04 §3 安全边界）。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .db import DB
from .tasks import build_registry

RUNTIME_VERSION = "2.0.0"
EPOCH_POLL_S = 2.0
CACHE_KEYS = ("ip_continent", "device_hash", "role")   # 会话缓存失效上下文键


def _err(status, code, msg):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": str(msg)}})


class HotLoader:
    """01 §3 契约：epoch 变化 → 校验加载 → 双缓冲换入；失败保留旧版并留痕。"""

    def __init__(self, db: DB, engines: dict):
        self.db, self.engines = db, engines
        self.loaded_epoch = db.epoch()
        self.last_error: dict[str, str] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        for tid, eng in engines.items():
            try:
                eng.reload_registry()
                if eng.active:
                    eng.load()
            except Exception as e:            # 首启失败策略见 create_app
                self.last_error[tid] = f"{type(e).__name__}: {e}"

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="epoch-poller")
        t.start()
        return t

    def _loop(self):
        while not self._stop.wait(EPOCH_POLL_S):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self) -> bool:
        ep = self.db.epoch()
        if ep == self.loaded_epoch:
            return False
        with self._lock:
            for tid, eng in self.engines.items():
                old_ver = eng.active
                try:
                    eng.reload_registry()
                    if eng.active:
                        eng._loaded = None           # 同版本重发布（带新配置）也要换入
                        eng.load()
                    self.db.log_access(event="reload_ok", task_id=tid,
                                       detail={"epoch": ep, "version": eng.active})
                except Exception as e:
                    self.last_error[tid] = f"{type(e).__name__}: {e}"
                    try:
                        eng.active = old_ver   # 回退指针，旧工件继续服务
                    except Exception:
                        pass
                    self.db.log_access(event="reload_failed", task_id=tid,
                                       detail={"epoch": ep, "error": self.last_error[tid]})
            self.loaded_epoch = ep
        return True

    def stop(self):
        self._stop.set()


def create_runtime_app(root: Path, db_path: Path | None = None):
    db = DB(db_path or root / "data" / "myjev.db", role="runtime")
    db.init_schema()                      # 幂等建表（init_schema 自带临时写权提升）
    engines = build_registry(root / "data", db, register=False)   # 注册表只读（03 §3）
    loader = HotLoader(db, engines)
    if loader.last_error and any(engines[t].active for t in loader.last_error):
        raise RuntimeError(f"首启加载失败且存在已发布版本: {loader.last_error}")

    app = FastAPI(title="MyJev Runtime", version=RUNTIME_VERSION, docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware,
                       allow_origins=["null"], allow_origin_regex=r"^http://127\.0\.0\.1(:\d+)?$",
                       allow_methods=["*"], allow_headers=["*"])
    app.state.db, app.state.engines, app.state.loader = db, engines, loader
    cache: dict[str, dict] = {}

    @app.on_event("startup")
    def _startup():
        loader.start()

    @app.get("/health")
    def health():
        return {"ok": True, "role": "runtime", "version": RUNTIME_VERSION,
                "ts": time.time(), "version_epoch": db.epoch(),
                "loaded_epoch": loader.loaded_epoch,
                "loaded": {t: e.active for t, e in engines.items()},
                "reload_errors": loader.last_error}

    @app.get("/api/version")
    def version():
        return {"runtime_version": RUNTIME_VERSION, "publish_epoch": db.epoch(),
                "loaded_epoch": loader.loaded_epoch,
                "models": {t: {"active": e.active,
                               "artifact": (e.db.version(t, e.active) or {}).get("artifact_sha256", "")[:20] if e.active else None}
                           for t, e in engines.items()}}

    @app.post("/v1/systemone")
    def systemone(request: Request, payload: dict = Body(...)):
        api_key = request.headers.get("x-api-key")
        tid = payload.get("task", "access-decision")
        eng = engines.get(tid)
        if eng is None:
            return _err(404, "unknown-task", f"unknown task: {tid}")
        questions = payload.get("questions") or []
        if not questions:
            return _err(400, "bad-request", "questions 不能为空")
        if not eng.active:
            return _err(409, "no-active-version", "该任务尚无发布版本")
        ctx = payload.get("context", {})
        ck = f"{tid}|{ctx.get('session') or ctx.get('principal') or 'anon'}"
        fp = _fingerprint(ctx)
        cached = cache.get(ck)
        # 会话缓存命中：不重算，执行痕迹回链（03 §4）
        if (cached and cached["fp"] == fp and time.time() < cached["exp"]
                and not payload.get("no_cache")):
            res = dict(cached["res"])
            res["meta"] = {"task": tid, "model_version": eng.active,
                           "schema_version": "1.0", "ts": time.time(),
                           "cache": "hit", "remaining_ttl_s": round(cached["exp"] - time.time(), 1)}
            db.log_access(decision_id=cached["decision_id"], task_id=tid,
                          session_key=ck, event="hit",
                          ttl_left=cached["exp"] - time.time(), api_key=api_key)
            return res
        t0 = time.time()
        try:
            res = eng.answer(ctx, questions)
        except Exception as e:
            db.log_access(task_id=tid, session_key=ck, event="fallback",
                          detail={"reason": f"{type(e).__name__}: {e}"}, api_key=api_key)
            return _err(500, "model_error", e)
        lat = (time.time() - t0) * 1000
        a = res.pop("_audit", None)
        did = "d-" + uuid.uuid4().hex[:12]
        if a is not None:                      # access 任务：两层审计
            db.log_decision(decision_id=did, ts=time.time(), task_id=tid,
                            model_version=eng.active, epoch=db.epoch(),
                            fs_digest=a["fs_digest"], raw=a["raw"],
                            calibrated=a["calibrated"], final=res["pdp"],
                            latency_ms=round(lat, 3), exploration=False)
            ttl = getattr(eng, "ts", None).cache_ttl_s if getattr(eng, "ts", None) else 0
            if res["pdp"]["final_action"] == "deny":
                ttl = 30
            cache[ck] = {"fp": fp, "exp": time.time() + ttl, "res": res,
                         "decision_id": did}
            db.log_access(decision_id=did, task_id=tid, session_key=ck,
                          event="compute", ttl_left=ttl, api_key=api_key,
                          detail={"fs_id": a["fs_id"]})
            res["pdp"]["decision_id"] = did
        else:
            db.log_access(task_id=tid, session_key=ck, event="compute", api_key=api_key)
        res["meta"] = {"task": tid, "model_version": eng.active,
                       "schema_version": "1.0", "ts": time.time(),
                       "latency_ms": round(lat, 2), "cache": "miss"}
        return res

    @app.exception_handler(404)
    def nf(request, exc):
        return _err(404, "no-route", "此服务后台无该路由（管理操作请使用管理后台 :8090）")

    return app


def _fingerprint(ctx: dict) -> str:
    import json as _json
    return _json.dumps(ctx, sort_keys=True, ensure_ascii=False, default=str)[:1024]
