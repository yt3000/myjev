"""端到端演示与 M2 训练入口：python -m myjev.run_demo

流程：合成数据(§4) → GBM 多任务头训练(§5) → 校准(§6.1) → §11.1 门禁
→ PEP 决策仿真(§7，含 fail-closed) → 哈希链审计写入与校验(§9)。
"""
from __future__ import annotations

import json
from pathlib import Path

from .audit import AuditLog
from .pipeline import train_and_report
from .service import MyJevService


def _print_table(title, rows):
    print(f"\n== {title} ==")
    for r in rows:
        print("  " + " | ".join(str(x) for x in r))


def main(seed: int = 20260921, events_per_day: int = 240):
    outdir = Path(__file__).resolve().parent.parent / "artifacts"
    outdir.mkdir(exist_ok=True)
    print(f"[1/5] 生成合成数据并训练（seed={seed}, {events_per_day}/day）…")
    rep = train_and_report(events_per_day=events_per_day, seed=seed, outdir=outdir)
    print(f"      train/calib/val/test 样本量: " +
          ", ".join(f"{k}={len(v)}" for k, v in rep['parts'].items()))

    print("[2/5] §6.1 校准报告（before→after ECE，iso=是否升级保序）:")
    for h, r in rep["calib_report"]["noul"].items():
        print(f"      {h:22s} ECE {r['before']:.3f}→{r['after']:.3f} T={r['T']:.2f} iso={r['iso']}")
    a = rep["calib_report"]["action"]
    print(f"      {'action(multiclass)':22s} ECE {a['before']:.3f}→{a['after']:.3f} T={a['T']:.2f}")

    print("[3/5] §11.1 上线门禁（test split）:")
    _print_table("Gate", rep["gates"])
    n_pass = sum(1 for r in rep["gates"] if r[-1])
    print(f"      通过 {n_pass}/{len(rep['gates'])}")

    # -------- PEP 决策仿真（§7）--------
    print("[4/5] PEP 决策仿真（含硬规则/fail-closed/灰区/缓存）:")
    audit = AuditLog(outdir / "decision_log.jsonl")
    svc = MyJevService(rep["bundle"], rep["cals"], rep["thresholds"],
                       rep["quad"], audit, rng_prob=0.03)
    feats_allow = {"s_role_level": 1, "s_hist_login_ok_rate_30d": 0.97,
                   "d_mdm_enrolled": 1, "d_cert_valid": 1, "d_posture_ok": 1,
                   "d_posture_sig_valid": 1, "g_ip_reputation": 0.95,
                   "t_resource_sensitivity": 2, "h_risk_ewma": 0.05,
                   "c_ua_entropy": 0.6}
    feats_ato = dict(feats_allow, **{
        "c_device_switch": 1, "c_new_device": 1, "s_fail_logins_1h": 15,
        "c_req_rate_z": 4.2, "g_geo_anomaly": 1, "h_risk_ewma": 0.7,
        "h_anom_events_24h": 2, "g_ip_reputation": 0.2})
    cases = [
        ("正常办公访问", {"session_id": "s1", "principal": "alice",
                         "ip_continent": "AS", "device_hash": "dA", "role": 1}, feats_allow),
        ("疑似 ATO", {"session_id": "s2", "principal": "bob",
                      "ip_continent": "EU", "device_hash": "dB", "role": 1}, feats_ato),
        ("denylist 主体（ATO 特征）", {"session_id": "s3", "principal": "u-blackhole",
                                       "ip_continent": "EU", "device_hash": "dC", "role": 1}, feats_ato),
        ("break-glass 应急", {"session_id": "s4", "principal": "ops",
                              "ip_continent": "AS", "device_hash": "dD", "role": 3,
                              "break_glass": "bg-emergency-001"}, feats_ato),
    ]
    for name, ctx, feats in cases:
        out = svc.decide(ctx, feats)
        print(f"      {name:26s} → final={out.final_action.value:8s} "
              f"auth={out.auth_required.value:13s} grant={out.grant.value:16s} "
              f"rules={out.hard_rules_matched} fb={out.fallback_reason}")
    # 同会话缓存命中
    _ = svc.decide(cases[0][1], cases[0][2])
    print(f"      缓存命中检查: hits={svc.stats.cache_hits}")
    # fail-closed（超时注入）
    svc.inject_timeout = True
    out = svc.decide({"session_id": "s9", "principal": "carol"}, feats_ato)
    svc.inject_timeout = False
    print(f"      超时注入 → final={out.final_action.value} "
          f"fallback={out.fallback_reason}（fail-closed）")

    ok, msg = audit.verify()
    print(f"[5/5] §9 审计链校验: {'PASS' if ok else 'FAIL'} — {msg} "
          f"({outdir / 'decision_log.jsonl'})")
    (outdir / "gates.json").write_text(
        json.dumps([list(map(str, r)) for r in rep["gates"]], ensure_ascii=False, indent=2),
        encoding="utf-8")
    et = rep["eval_test"]
    (outdir / "eval_test.json").write_text(json.dumps(vars(et), ensure_ascii=False,
                                                      indent=2, default=str), encoding="utf-8")
    print(f"\n全部产物见 {outdir}")
    return rep


if __name__ == "__main__":
    main()
