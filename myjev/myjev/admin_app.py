"""MyJev 管理后台（交付物 B，docs/01 §2）：控制台 + 控制面 API :8090。

配置/任务/训练/微调/评估/数据集/发布（bump epoch 驱动 A 热加载）/审计只读/bench。
写权分离（03 §3）：本进程禁写 decisions/access_log（DB authorizer 强制）；
无 /v1/systemone（推理属 A）。仅绑定 127.0.0.1。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .db import DB
from .providers import run_bench
from .pdp import HardRuleEngine
from .tasks import MODEL_KIND, TASK_BUILDERS, build_registry
from .thresholds import ThresholdSet

ADMIN_VERSION = "2.0.0"
AVAILABLE_KINDS = {"gbm-multitask"}
RESERVED_KINDS = {"logit-probe"}
DATASET_ROOT_NAME = "datasets"


def _err(status, code, msg):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": str(msg)}})


def _seed_configs(db: DB):
    if not db.config_sets("thresholds"):
        db.add_config_set("ts-default", "thresholds", ThresholdSet().to_dict(), "内置默认")
    if not db.config_sets("rules"):
        db.add_config_set("rules-default", "rules", HardRuleEngine().to_dict(), "内置默认")


def _dec_row(r: dict) -> dict:
    out = dict(r)
    for k in ("raw_json", "calibrated_json", "final_json", "detail_json"):
        if k in out and isinstance(out.get(k), str):
            try:
                out[k.replace("_json", "")] = json.loads(out[k])
            except Exception:
                pass
    return out


def _validate_rows(kind: str, rows: list[dict]) -> list[str]:
    errs = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            errs.append(f"行 {i}: 非 object"); continue
        if kind in ("builtin-synthetic", "upload-access"):
            if "features" not in r:
                errs.append(f"行 {i}: 缺 features")
            g = r.get("gold")
            if g is not None and g not in ("allow", "step_up", "deny"):
                errs.append(f"行 {i}: gold 非法")
        elif kind == "upload-routing":
            if "text" not in r or r.get("label") not in ("billing", "technical", "sales", "returns"):
                errs.append(f"行 {i}: 需 text + label∈4类")
        elif kind == "upload-urgency":
            if r.get("label") is None:
                errs.append(f"行 {i}: 需 label 0..1")
        elif kind in ("jev-102", "openjev-matrix"):
            q = r.get("question") or {}
            if not (r.get("case_id") and r.get("context") and q.get("type") in ("choice", "noul", "score")):
                errs.append(f"行 {i}: 需 case_id/context/question.type")
    return errs[:20]


def create_admin_app(root: Path, db_path: Path | None = None):
    db = DB(db_path or root / "data" / "myjev.db", role="admin")
    db.init_schema()
    _seed_configs(db)
    engines = build_registry(root / "data", db)
    app = FastAPI(title="MyJev Admin", version=ADMIN_VERSION)
    app.state.db, app.state.engines = db, engines
    WEB = Path(__file__).resolve().parent / "web"

    # ------------------------------------------------ 元信息
    @app.get("/api/health")
    def health():
        return {"ok": True, "role": "admin", "version": ADMIN_VERSION,
                "tasks": list(engines), "ts": time.time()}

    @app.get("/api/bootstrap")
    def bootstrap():
        snaps = [engines[t].snapshot() for t in engines]
        ep = db.epoch()
        status = []
        for t in engines:
            lr = db.last_reload(t)
            loaded = None
            if lr:
                try:
                    loaded = json.loads(lr["detail_json"] or "{}").get("epoch")
                except Exception:
                    loaded = None
            status.append({"task": t, "active": engines[t].active,
                           "publish_epoch": ep, "loaded_epoch": loaded,
                           "lag": loaded != ep})
        ok_ledger = True
        return {"health": {"ok": True, "version": ADMIN_VERSION, "ts": time.time(),
                           "tasks": list(engines)},
                "tasks": snaps,
                "publish_status": status,
                "audit": {"ok": ok_ledger, "msg": f"epoch={ep}",
                          "count": len(db.decisions_page(10 ** 6)),
                          "recent": [_dec_row(r) for r in db.decisions_page(10)]},
                "access": db.access_stats(),
                "datasets": db.datasets()}

    # ------------------------------------------------ 任务/模型
    @app.get("/api/tasks")
    def tasks():
        out = []
        for t, e in engines.items():
            d = e.snapshot()
            d["primitives"] = getattr(e, "primitives", None)
            d["options"] = getattr(e, "options", None)
            d["model_kind_db"] = next((x["model_kind"] for x in db.tasks() if x["id"] == t), MODEL_KIND)
            out.append(d)
        return {"tasks": out}

    @app.get("/api/model-kinds")
    def kinds():
        return {"available": sorted(AVAILABLE_KINDS), "reserved": sorted(RESERVED_KINDS)}

    @app.put("/api/tasks/{task_id}/model-kind")
    def set_kind(task_id: str, payload: dict = Body(...)):
        _need(task_id)
        k = payload.get("kind")
        if k not in AVAILABLE_KINDS | RESERVED_KINDS:
            raise HTTPException(400, f"未知模型族: {k}")
        db.set_model_kind(task_id, k)
        return {"task_id": task_id, "model_kind": k,
                "note": "预留族执行 train/finetune/publish 将返回 501" if k in RESERVED_KINDS else "ok"}

    def _kind_guard(task_id: str):
        row = next((x for x in db.tasks() if x["id"] == task_id), None)
        if row and row["model_kind"] not in AVAILABLE_KINDS:
            raise HTTPException(501, f"model_kind={row['model_kind']} 为预留实现（G9/M5），当前仅支持 {sorted(AVAILABLE_KINDS)}")

    # ------------------------------------------------ 生命周期
    @app.post("/api/tasks/{task_id}/train")
    def train(task_id: str):
        e = _need(task_id); _kind_guard(task_id)
        e.reload_registry()
        return e.train()

    @app.post("/api/tasks/{task_id}/finetune")
    def finetune(task_id: str):
        e = _need(task_id); _kind_guard(task_id)
        e.reload_registry()
        return e.finetune()

    @app.post("/api/tasks/{task_id}/evaluate")
    def evaluate(task_id: str):
        e = _need(task_id)
        e.reload_registry()
        if not e.active:
            raise HTTPException(409, "该任务尚无版本")
        return {"version": e.active, **e.evaluate()}

    @app.get("/api/tasks/{task_id}/versions")
    def versions(task_id: str):
        e = _need(task_id); e.reload_registry()
        return {"versions": e.version_list()}

    # ------------------------------------------------ 反馈/buffer
    @app.post("/api/tasks/{task_id}/feedback")
    def feedback(task_id: str, payload: dict = Body(...)):
        _need(task_id)
        src = payload.pop("src", None) or "ui-feedback"
        if payload.get("label") is None and payload.get("action"):
            payload["label"] = payload["action"]
        db.add_buffer(task_id, payload, src)
        total, included = db.buffer_count(task_id)
        return {"buffer": total, "included": included}

    @app.get("/api/tasks/{task_id}/buffer")
    def buffer(task_id: str):
        _need(task_id)
        total, included = db.buffer_count(task_id)
        return {"count": total, "included": included}

    # ------------------------------------------------ 配置（G10）
    @app.get("/api/config/{kind}")
    def config_list(kind: str):
        if kind not in ("thresholds", "rules"):
            raise HTTPException(400, "kind ∈ {thresholds, rules}")
        return {"sets": db.config_sets(kind)}

    @app.post("/api/config/{kind}")
    def config_add(kind: str, payload: dict = Body(...)):
        if kind not in ("thresholds", "rules"):
            raise HTTPException(400, "kind ∈ {thresholds, rules}")
        if kind == "thresholds":
            try:
                ts = ThresholdSet.from_dict(payload.get("values") or {})
            except Exception as e:
                raise HTTPException(400, f"阈值组非法: {e}")
            payload = dict(payload); payload["values"] = ts.to_dict()
        else:
            payload = dict(payload); payload["values"] = HardRuleEngine.from_dict(
                payload.get("values") or {}).to_dict()
        cid = payload.get("id") or f"cfg-{kind[:3]}-{uuid.uuid4().hex[:6]}"
        db.add_config_set(cid, kind, payload["values"], payload.get("note", ""))
        return {"id": cid, "kind": kind, "values": payload["values"],
                "note": "新配置组需在发布时以工件快照生效（03 §1）"}

    # ------------------------------------------------ 发布（驱动 D）
    @app.post("/api/publish")
    def publish(payload: dict = Body(...)):
        task_id, ver = payload.get("task"), payload.get("version")
        _need(task_id); _kind_guard(task_id)
        # 可选：绑定新配置组（克隆后发布）
        e = engines[task_id]
        e.reload_registry()
        cfg_refs = payload.get("config") or {}
        forced = False
        if cfg_refs:
            e.reload_registry()
            if task_id == "access-decision":
                if cfg_refs.get("thresholds"):
                    cs = db.config_set(cfg_refs["thresholds"])
                    if not cs:
                        raise HTTPException(404, "阈值组不存在")
                    e.ts = ThresholdSet.from_dict(json.loads(cs["payload_json"]))
                if cfg_refs.get("rules"):
                    cs = db.config_set(cfg_refs["rules"])
                    if not cs:
                        raise HTTPException(404, "规则集不存在")
                    e.rules = HardRuleEngine.from_dict(json.loads(cs["payload_json"]))
                obj, _ = e.load_artifact(ver)
                e.save_version(ver, obj, e.versions[ver].metrics,
                               e.versions[ver].note + "（重发布带配置）",
                               config=e.config_snapshot())
            forced = True
        ep = db.publish(task_id, ver, note=payload.get("note", ""), force=forced)
        return {"task": task_id, "version": ver, "publish_epoch": ep,
                "note": "A 将在 ≤3 个轮询周期内热加载并回写 reload 事件"}

    @app.get("/api/publish/status")
    def publish_status():
        ep = db.epoch()
        out = []
        for t in engines:
            lr = db.last_reload(t)
            detail = json.loads((lr or {}).get("detail_json") or "{}")
            out.append({"task": t, "active": engines[t].active, "publish_epoch": ep,
                        "loaded_epoch": detail.get("epoch"),
                        "state": "failed" if lr and lr["event"] == "reload_failed"
                                 else ("ok" if lr and detail.get("epoch") == ep else "lagging")})
        return {"publish_epoch": ep, "tasks": out,
                "events": db.publish_events(15)}

    # ------------------------------------------------ 数据集（G8）
    @app.post("/api/datasets")
    def dataset_add(payload: dict = Body(...)):
        kind = payload.get("kind", "upload-access")
        name = payload.get("name") or f"dataset-{int(time.time())}"
        rows = payload.get("rows")
        path = payload.get("path")
        ds_dir = Path(root) / "data" / DATASET_ROOT_NAME
        ds_dir.mkdir(parents=True, exist_ok=True)
        if rows is None and path:
            p = Path(path).resolve()
            if ds_dir.resolve() not in p.parents and p.parent != ds_dir.resolve():
                raise HTTPException(400, "path 必须位于 data/datasets/ 内")
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            stored = p
        else:
            if not rows or not isinstance(rows, list):
                raise HTTPException(400, "rows 或 path 必选其一")
            errs = _validate_rows(kind, rows)
            if errs:
                raise HTTPException(400, "校验失败: " + "; ".join(errs))
            stored = ds_dir / f"ds-{uuid.uuid4().hex[:8]}.jsonl"
            stored.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                              encoding="utf-8")
        errs = _validate_rows(kind, rows)
        if errs:
            raise HTTPException(400, "校验失败: " + "; ".join(errs))
        task_map = {"upload-access": "access-decision", "upload-routing": "support-routing",
                    "upload-urgency": "urgency-score"}
        did = db.register_dataset(name=name, kind=kind, path=stored,
                                  row_count=len(rows), task_id=task_map.get(kind),
                                  provenance=payload.get("provenance", "user-import"))
        return {"id": did, "rows": len(rows), "path": str(stored)}

    @app.get("/api/datasets")
    def dataset_list():
        return {"datasets": db.datasets()}

    @app.delete("/api/datasets/{ds_id}")
    def dataset_del(ds_id: str):
        if not db.delete_dataset(ds_id):
            raise HTTPException(400, "仅 upload-* 数据集可删除")
        return {"deleted": ds_id}

    @app.get("/api/datasets/{ds_id}/preview")
    def dataset_preview(ds_id: str, n: int = 20):
        d = db.dataset(ds_id)
        if not d:
            raise HTTPException(404, "数据集不存在")
        rows = [json.loads(l) for l in Path(d["path"]).read_text(encoding="utf-8").splitlines() if l.strip()]
        return {"id": ds_id, "kind": d["kind"], "row_count": len(rows), "rows": rows[:n]}

    # ------------------------------------------------ bench（G8 数据源）
    @app.post("/api/bench")
    def bench(payload: dict = Body(...)):
        tid = payload.get("task", "access-decision")
        e = _need(tid)
        providers = payload.get("providers") or ["myjev", "jev", "openjev"]
        n = int(payload.get("sample_n", 300))
        rows = None
        ds_label = "builtin"
        if payload.get("dataset_id"):
            d = db.dataset(payload["dataset_id"])
            if not d:
                raise HTTPException(404, "数据集不存在")
            rows = [json.loads(l) for l in Path(d["path"]).read_text(encoding="utf-8").splitlines() if l.strip()]
            rows = [{"features": r.get("features", {}),
                     "true_risk": r.get("true_risk", 0.0), "gold": r.get("gold"),
                     "labels": r.get("labels") or {"noul": {h: 0 for h in []}, "action": r.get("gold", "allow")}}
                    for r in rows][:n]
            ds_label = d["id"]
        out = run_bench(e, providers, n, rows=rows)
        out["dataset"] = ds_label
        db.add_bench_run(tid, ds_label, providers, n, out)
        return out

    @app.get("/api/bench/baselines")
    def baselines():
        from .providers import JEV_BASELINE, OPENJEV_BASELINE
        return {"jev": JEV_BASELINE, "openjev": OPENJEV_BASELINE}

    # ------------------------------------------------ 审计只读（A 写入，B 读）
    @app.get("/api/audit/decisions")
    def audit_decisions(limit: int = 40, task: str | None = None):
        return {"entries": [_dec_row(r) for r in db.decisions_page(limit, task)]}

    @app.get("/api/audit/access-stats")
    def audit_stats():
        return db.access_stats()

    @app.post("/api/audit/integrity-snapshot")
    def integrity():
        db.integrity_append()
        return {"ok": True, "ts": time.time()}

    # ------------------------------------------------ 控制台
    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/systemone")
    def no_runtime():
        return _err(404, "no-route", "推理请调用服务后台 :8091 /v1/systemone")

    def _need(task_id: str):
        e = engines.get(task_id)
        if not e:
            raise HTTPException(404, f"unknown task: {task_id}")
        return e

    return app
