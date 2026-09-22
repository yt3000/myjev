"""G11 集成回归（01 §4）：双进程拓扑 + 写权分离 + 发布热加载 + 两层审计 + 边界端点。

运行：PYTHONPATH=. python -m unittest discover -s scripts/tests -p "test_*.py"
使用临时目录做数据根，TestClient 模拟 A/B 两进程（同一 db 文件、不同 role 连接）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from myjev.admin_app import create_admin_app
from myjev.db import DB
from myjev.runtime_app import create_runtime_app


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="myjev_it_"))
        cls.admin = TestClient(create_admin_app(cls.root))
        cls.aapp = create_runtime_app(cls.root)
        cls.rt = TestClient(cls.aapp)

    def _speed(self):
        """缩样以保测试时长可控（不改变链路语义）。"""
        for t, e in self.admin.app.state.engines.items():
            e.train_base = e.train_base[:2200]
            if hasattr(e, "calib"):
                e.calib = e.calib[:180]
            e.holdout = e.holdout[:200]

    # ------------------------------------------------ G3 写权/不可变
    def test_a0_write_isolation(self):
        with self.assertRaises(sqlite3.Error):
            self.admin.app.state.db.conn.execute(
                "INSERT INTO decisions VALUES('x',0,'t','v',0,'','','{}','',0,0)")
        self.admin.app.state.db.conn.rollback()
        with self.assertRaises(sqlite3.Error):
            self.rt.app.state.db.conn.execute(
                "INSERT INTO tasks(id,kind,title) VALUES('h','x','y')")
        self.rt.app.state.db.conn.rollback()

    def test_a1_decisions_immutable(self):
        db = self.rt.app.state.db
        db.log_decision(decision_id="d-test", ts=1, task_id="t", model_version="v",
                        epoch=0, fs_digest="", raw={}, calibrated={}, final={}, latency_ms=1)
        with self.assertRaises(sqlite3.IntegrityError):
            db.conn.execute("UPDATE decisions SET latency_ms=999 WHERE decision_id='d-test'")
        db.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            db.conn.execute("DELETE FROM decisions WHERE decision_id='d-test'")
        db.conn.rollback()

    # ------------------------------------------------ 全链路
    def test_b_train_publish_hotload(self):
        self._speed()
        for t in ("support-routing", "urgency-score", "access-decision"):
            r = self.admin.post(f"/api/tasks/{t}/train")
            self.assertEqual(r.status_code, 200, r.text)
            ver = r.json()["version"]
            p = self.admin.post("/api/publish", json={"task": t, "version": ver})
            self.assertEqual(p.status_code, 200, p.text)
        loader = self.rt.app.state.loader
        loader.tick()                      # 模拟轮询周期
        self.assertEqual(loader.loaded_epoch,
                         self.rt.app.state.db.epoch())
        h = self.rt.get("/health").json()
        self.assertTrue(all(h["loaded"].values()))
        self.assertEqual(h["loaded_epoch"], h["version_epoch"])

    def test_c_systemone_and_two_layer_audit(self):
        before_d = len(self.rt.app.state.db.decisions_page(10 ** 6))
        ctx = {"principal": "alice", "features": {
            "s_hist_login_ok_rate_30d": .95, "d_mdm_enrolled": 1, "d_cert_valid": 1,
            "d_posture_ok": 1, "d_posture_sig_valid": 1, "g_ip_reputation": .9,
            "h_risk_ewma": .08, "c_ua_entropy": .55}}
        q = [{"id": "c", "type": "choice"}, {"id": "n", "type": "noul",
             "question": "account_takeover"}, {"id": "s", "type": "score"}]
        body = {"task": "access-decision", "context": ctx, "questions": q}
        r1 = self.rt.post("/v1/systemone", json=body).json()
        r2 = self.rt.post("/v1/systemone", json=body).json()
        a = {x["id"]: x for x in r1["answers"]}
        self.assertIn(a["c"]["choice"], a["c"]["probabilities"])
        self.assertEqual(r2["meta"]["cache"], "hit")
        self.assertEqual(r1["pdp"]["decision_id"], r2["pdp"]["decision_id"] if "decision_id" in r2["pdp"] else r1["pdp"]["decision_id"])
        after_d = len(self.rt.app.state.db.decisions_page(10 ** 6))
        self.assertEqual(after_d - before_d, 1)               # 两次调用一次计算
        st = self.admin.get("/api/audit/access-stats").json()
        self.assertGreaterEqual(st["by_event"].get("hit", 0), 1)

    def test_d_boundary_routes(self):
        self.assertEqual(self.rt.get("/api/tasks").status_code, 404)
        self.assertEqual(self.admin.post("/v1/systemone", json={}).status_code, 404)
        e = self.rt.post("/v1/systemone", json={"task": "nope", "questions": [{"id": "c", "type": "choice"}]}).json()
        self.assertEqual(e["error"]["code"], "unknown-task")   # 封套

    def test_e_feedback_discipline_and_kind_guard(self):
        self.admin.post("/api/tasks/access-decision/feedback",
                        json={"features": {}, "label": "deny", "src": "synthetic"})
        r = self.admin.post("/api/tasks/access-decision/finetune").json()
        self.assertIn("skipped", r)
        k = self.admin.put("/api/tasks/urgency-score/model-kind", json={"kind": "logit-probe"})
        self.assertEqual(k.status_code, 200)
        g = self.admin.post("/api/tasks/urgency-score/train")
        self.assertEqual(g.status_code, 501)
        self.admin.put("/api/tasks/urgency-score/model-kind", json={"kind": "gbm-multitask"})

    def test_f_dataset_import_and_bench(self):
        rows = [{"features": e["features"], "true_risk": e["true_risk"], "gold": None}
                for e in self.admin.app.state.engines["access-decision"].holdout[:12]]
        r = self.admin.post("/api/datasets", json={"name": "it-mini", "kind": "upload-access",
                                                   "rows": rows})
        self.assertEqual(r.status_code, 200, r.text)
        ds = r.json()["id"]
        b = self.admin.post("/api/bench", json={"task": "access-decision", "dataset_id": ds,
                                                "providers": ["myjev"], "sample_n": 12})
        self.assertEqual(b.status_code, 200, b.text)
        self.assertEqual(b.json()["rows"]["myjev"]["n"], 12)
        bad = self.admin.post("/api/datasets", json={"kind": "upload-access",
                                                     "rows": [{"nope": 1}]})
        self.assertEqual(bad.status_code, 400)

    def test_g_config_clone_and_republish(self):
        cs = self.admin.get("/api/config/thresholds").json()["sets"]
        base = next(c for c in cs if c["id"] == "ts-default")
        import json as J
        payload = J.loads(base["payload_json"])
        payload["theta_allow"] = 0.995
        r = self.admin.post("/api/config/thresholds", json={"values": payload, "note": "it"})
        self.assertEqual(r.status_code, 200, r.text)
        new_id = r.json()["id"]
        ver = self.admin.app.state.engines["access-decision"].active
        p = self.admin.post("/api/publish", json={"task": "access-decision", "version": ver,
                                                  "config": {"thresholds": new_id}})
        self.assertEqual(p.status_code, 200, p.text)
        eng = self.rt.app.state.loader.engines["access-decision"]
        self.rt.app.state.loader.tick()
        self.assertAlmostEqual(eng.ts.theta_allow, 0.995)


if __name__ == "__main__":
    unittest.main(verbosity=2)
