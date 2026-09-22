"""G11 契约冒烟（01 §4）：以纯外部进程视角验证服务后台 A(:8091) 与管理后台 B(:8090)。

用法：两后台已运行后 `python scripts/smoke_runtime.py`。仅用 stdlib（不依赖 requests）。
断言口径 = 附录 A"兼容超集"：只用 Jev 原语字段即可完成消费。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

A = "http://127.0.0.1:8091"
B = "http://127.0.0.1:8090"
FAILED = []


def call(url, payload=None, method=None, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"}, method=method or ("POST" if payload is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def main():
    print("[smoke] A/health 与版本回执")
    h = call(A + "/health")
    check("health.ok", h.get("ok") is True)
    check("epoch 已追平（无 lag）", h.get("loaded_epoch") == h.get("version_epoch"),
          f"loaded={h.get('loaded_epoch')} pub={h.get('version_epoch')}")
    check("无 reload 错误", not h.get("reload_errors"))

    print("[smoke] 纯 Jev 客户端视角：三任务三原语")
    r = call(A + "/v1/systemone", {"task": "access-decision",
        "context": {"principal": "alice", "features": {
            "s_hist_login_ok_rate_30d": .95, "d_mdm_enrolled": 1, "d_cert_valid": 1,
            "d_posture_ok": 1, "d_posture_sig_valid": 1, "g_ip_reputation": .9,
            "h_risk_ewma": .08, "c_ua_entropy": .55}},
        "questions": [{"id": "c", "type": "choice"}, {"id": "n", "type": "noul",
                       "question": "account_takeover"}, {"id": "s", "type": "score"}]})
    a = {x["id"]: x for x in r["answers"]}
    check("choice 原语字段", all(k in a["c"] for k in ("choice", "probabilities", "confidence")))
    check("choice ∈ 枚举集", a["c"]["choice"] in a["c"]["probabilities"])
    check("noul 概率 [0,1]", 0 <= a["n"]["value"] <= 1)
    check("score 数值 [0,1]", 0 <= a["s"]["score"] <= 1)
    check("扩展件不侵入原语（pdp 独立命名空间）", "pdp" in r and "choice" in a["c"])

    rt = call(A + "/v1/systemone", {"task": "support-routing",
        "context": {"text": "系统登录故障报错无法使用"},
        "questions": [{"id": "c", "type": "choice"}]})
    check("routing choice 可答", rt["answers"][0]["choice"] in rt["answers"][0]["probabilities"])
    ug = call(A + "/v1/systemone", {"task": "urgency-score",
        "context": {"sentiment": .9, "hours_since": 40, "repeat_contacts": 6,
                    "amount": 50000, "vip": 1},
        "questions": [{"id": "s", "type": "score"}]})
    check("urgency score 高分", ug["answers"][0]["score"] > .6)

    print("[smoke] 边界：A 无管理路由 / B 无推理路由")
    try:
        call(A + "/api/tasks")
        check("A 拒绝 /api/tasks", False)
    except urllib.error.HTTPError as e:
        check("A 拒绝 /api/tasks(404)", e.code == 404)
    try:
        call(B + "/v1/systemone", {"task": "urgency-score", "questions": [{"id": "s", "type": "score"}]})
        check("B 拒绝 /v1/systemone", False)
    except urllib.error.HTTPError as e:
        check("B 拒绝 /v1/systemone(404)", e.code == 404)

    print("[smoke] 两层审计与发布链（B 读）")
    st = call(B + "/api/publish/status")
    check("发布状态含三任务", len(st["tasks"]) == 3)
    dec = call(B + "/api/audit/decisions?limit=5")
    check("已有决策审计", len(dec["entries"]) >= 3, f"{len(dec['entries'])} 条")
    acc = call(B + "/api/audit/access-stats")
    check("执行痕迹有 compute/reload", {"compute", "reload_ok"} & set(acc["by_event"]))

    print("\n[smoke] " + ("全部通过 ✓" if not FAILED else f"失败 {len(FAILED)} 项: {FAILED}"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
