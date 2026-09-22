"""MyJev 合成数据与标签体系（规格 §4）。

真实数据飞轮（M1）就绪前的替身生成器，用途：
  1. 打通并验证 M2–M4 全链路（训练→校准→门禁→影子仿真）；
  2. 内置攻击剧本（撞库/ATO/伪造姿态/bot/外传/钓鱼/模板注入/阈值探针），
     使 §11 门禁与 §8 对抗设计可被真实度量。
标签规则严格按 §4.1/§4.2：action 弱监督 + 灰区翻转噪声 + risk 代理构造。
按时间滚动切分（§4.3）：train 70d / calib 7d / val 7d / test 7d，主体不跨 split。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .contract import Action, AuthLevel, NOUL_HEADS

TRAIN_DAYS, CALIB_DAYS, VAL_DAYS, TEST_DAYS = 70, 7, 7, 7
TOTAL_DAYS = TRAIN_DAYS + CALIB_DAYS + VAL_DAYS + TEST_DAYS

# 场景配比（正常为主，攻击/异常约 9%）
SCENARIO_MIX = [
    ("normal", 0.86), ("traveler", 0.030), ("credential_stuffing", 0.018),
    ("ato", 0.014), ("spoofed_ato", 0.010), ("bot", 0.012),
    ("exfil", 0.010), ("phishing", 0.008), ("insider_policy", 0.006),
    ("probing", 0.012),
]


@dataclass
class User:
    uid: str
    home_day: int          # 用户生命期起点（切分按用户块，不跨 split）
    role_level: int
    hist_ok_rate: float


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _jitter(rng: random.Random, x: float, s: float) -> float:
    return _clamp(x + rng.gauss(0, s))


def make_users(rng: random.Random, n: int) -> list[User]:
    return [
        User(uid=f"u{i:05d}", home_day=rng.randrange(TOTAL_DAYS),
             role_level=rng.choice([1, 1, 1, 2, 2, 3]),
             hist_ok_rate=_jitter(rng, 0.93, 0.05))
        for i in range(n)
    ]


def _base_features(rng: random.Random, u: User, day: int) -> dict:
    """正常基线画像。"""
    off_hours = 1 if rng.random() < 0.12 else 0
    return {
        "s_role_level": u.role_level,
        "s_hist_login_ok_rate_30d": round(u.hist_ok_rate, 3),
        "s_recent_cred_change": 1 if rng.random() < 0.02 else 0,
        "s_acct_age_days_log": round(math.log1p(30 + day - u.home_day + rng.randrange(60)), 3),
        "s_fail_logins_1h": 0,
        "d_mdm_enrolled": 1 if rng.random() < 0.9 else 0,
        "d_cert_valid": 1 if rng.random() < 0.93 else 0,
        "d_jailbroken": 0,
        "d_os_known": 1 if rng.random() < 0.95 else 0,
        "d_posture_ok": 1,
        "d_posture_sig_valid": 1,
        "c_since_prev_login_min_log": round(math.log1p(rng.randrange(1, 2880)), 3),
        "c_device_switch": 0, "c_new_device": 1 if rng.random() < 0.05 else 0,
        "c_req_rate_z": round(rng.gauss(0, 0.6), 3),
        "c_ua_entropy": round(_jitter(rng, 0.55, 0.15), 3),
        "c_session_conc_same_asn": rng.choice([1, 1, 1, 2]),
        "g_ip_reputation": round(_jitter(rng, 0.88, 0.10), 3),
        "g_proxy_idc": 0, "g_geo_anomaly": 0, "g_off_hours": off_hours,
        "t_resource_sensitivity": rng.choice([1, 2, 2, 3, 4, 5]),
        "t_zone_public": 1 if rng.random() < 0.35 else 0,
        "t_is_export_api": 0, "t_is_admin_api": 0,
        "h_events_24h_log": round(math.log1p(rng.randrange(3, 80)), 3),
        "h_risk_ewma": round(_jitter(rng, 0.08, 0.06), 3),
        "h_logins_5m": rng.choice([0, 0, 1]), "h_anom_events_24h": 0,
        "r_injection_flag": 0, "r_padding_ratio": round(_jitter(rng, 0.05, 0.05), 3),
    }


def _apply_scenario(rng: random.Random, f: dict, scen: str) -> tuple[dict, dict]:
    """返回（扰动后特征, 场景真值 noul/latent 权重增量）。"""
    noul = {h: 0 for h in NOUL_HEADS}
    w = 0.0  # 场景对潜在风险的附加对数几率
    if scen == "normal":
        pass
    elif scen == "traveler":
        f["g_geo_anomaly"], f["g_off_hours"] = 1, 1
        f["c_since_prev_login_min_log"] = 0.7
        noul["impossible_travel"] = 1
        w += 0.8
    elif scen == "credential_stuffing":
        f["s_fail_logins_1h"] = rng.randrange(8, 40)
        f["c_req_rate_z"] = round(rng.uniform(2.5, 6), 2)
        f["c_ua_entropy"] = round(rng.uniform(0.02, 0.15), 3)
        f["c_new_device"], f["g_proxy_idc"] = 1, 1
        f["h_logins_5m"] = rng.randrange(5, 20)
        noul["credential_stuffing"] = 1
        w += 2.6
    elif scen == "ato":
        f["c_device_switch"], f["c_new_device"] = 1, 1
        f["c_req_rate_z"] = round(rng.uniform(1.8, 4.5), 2)
        f["g_geo_anomaly"] = 1
        f["s_fail_logins_1h"] = rng.randrange(2, 9)
        f["h_anom_events_24h"] = rng.randrange(1, 4)
        f["h_risk_ewma"] = round(rng.uniform(0.4, 0.85), 3)
        noul["account_takeover"] = 1
        w += 2.4
    elif scen == "spoofed_ato":
        # 姿态字段全部伪装为“干净”（模型不可观测 hidden 因子），只能靠行为面抓
        f["c_device_switch"] = 1
        f["c_req_rate_z"] = round(rng.uniform(1.2, 3.0), 2)
        f["s_fail_logins_1h"] = rng.randrange(0, 4)
        f["h_risk_ewma"] = round(rng.uniform(0.3, 0.6), 3)
        noul["account_takeover"] = 1
        noul["device_untrusted"] = 1  # 真值不信任（签名其实有效——最难样本）
        w += 1.7
    elif scen == "bot":
        f["c_ua_entropy"] = round(rng.uniform(0.0, 0.1), 3)
        f["c_req_rate_z"] = round(rng.uniform(3.0, 7.0), 2)
        f["g_proxy_idc"] = 1
        f["r_padding_ratio"] = round(rng.uniform(0.5, 0.95), 3)
        noul["bot_automation"] = 1
        w += 1.9
    elif scen == "exfil":
        f["t_is_export_api"] = 1
        f["t_resource_sensitivity"] = rng.choice([4, 5])
        f["c_req_rate_z"] = round(rng.uniform(2.0, 5.0), 2)
        f["h_events_24h_log"] = round(rng.uniform(5.0, 6.5), 2)
        noul["data_exfil_pattern"] = 1
        w += 1.6
    elif scen == "phishing":
        f["g_ip_reputation"] = round(rng.uniform(0.02, 0.25), 3)
        f["g_proxy_idc"] = 1
        f["t_resource_sensitivity"] = rng.choice([1, 2])
        noul["phishing_target"] = 1
        w += 1.3
    elif scen == "insider_policy":
        f["r_injection_flag"] = 1
        f["t_is_admin_api"] = 1
        f["t_resource_sensitivity"] = rng.choice([4, 5])
        f["g_off_hours"] = 1
        f["r_padding_ratio"] = round(rng.uniform(0.3, 0.7), 3)
        noul["sensitive_policy_hit"] = 1
        w += 1.5
    elif scen == "probing":
        # 阈值探针：特征被精心摆到“看起来恰好过线”的位置（§8.2 监测对象）
        f["c_req_rate_z"] = round(rng.uniform(1.9, 2.4), 2)
        f["g_ip_reputation"] = round(rng.uniform(0.45, 0.6), 3)
        f["h_risk_ewma"] = round(rng.uniform(0.3, 0.34), 3)
        f["t_resource_sensitivity"] = 3
        noul["bot_automation"] = 1
        w += 1.1
    return f, {"noul": noul, "w": w}


def _true_risk(f: dict, w: float) -> float:
    """潜在风险的生成模型（特征的非线性组合 + 噪声由调用方注入）。"""
    z = w
    z += 2.2 * f["s_fail_logins_1h"] / 20
    z += 1.4 * (f["c_req_rate_z"] - 0.6) / 2
    z -= 2.0 * (f["g_ip_reputation"] - 0.6)
    z += 1.0 * f["g_proxy_idc"] + 0.7 * f["g_geo_anomaly"]
    z += 0.9 * (f["h_risk_ewma"] - 0.2)
    z += 0.5 * f["t_resource_sensitivity"] / 5 + 0.6 * f["t_is_export_api"]
    z -= 1.2 * f["d_mdm_enrolled"] - 0.8 * f["d_cert_valid"]
    z += 1.5 * f["d_jailbroken"] + 0.8 * (1 - f["d_posture_ok"])
    z += 0.6 * f["c_device_switch"] + 0.5 * (0.5 - f["c_ua_entropy"])
    z += 0.5 * f["h_anom_events_24h"] + 0.4 * f["r_injection_flag"]
    return _clamp(_sigmoid(z - 1.2), 0.0, 0.99)


def _labels(rng: random.Random, f: dict, truth: dict, true_risk: float):
    """§4.1/§4.2：policy 真值 + 灰区 8% 翻转（模拟人工复核噪声）；noul 5% 软化。"""
    action = Action.ALLOW if true_risk < 0.3 else (
        Action.STEP_UP if true_risk < 0.72 else Action.DENY)
    flipped = False
    if 0.25 <= true_risk < 0.45 or 0.62 <= true_risk < 0.85:
        if rng.random() < 0.08:
            action = rng.choice([Action.ALLOW, Action.STEP_UP, Action.DENY])
            flipped = True
    risk_target = {Action.ALLOW: 0.1, Action.STEP_UP: 0.5, Action.DENY: 0.9}[action] \
        + rng.uniform(-0.05, 0.05)
    noul = {}
    for h in NOUL_HEADS:
        t = truth["noul"].get(h, 0)
        if t == 1 and rng.random() < 0.05:
            t = 0                       # 复核漏标噪声
        if t == 0 and rng.random() < 0.004:
            t = 1                       # 误标噪声
        if h == "device_untrusted" and truth["noul"].get("device_untrusted") == 1:
            t = 1                       # MDM 事实源近真值，不加噪
        noul[h] = t
    if action == Action.DENY:
        auth = AuthLevel.NONE.value
    elif action == Action.ALLOW:
        auth = AuthLevel.PASSWORD_OK.value if true_risk < 0.2 else AuthLevel.MFA_TOTP.value
    else:
        auth = rng.choice([AuthLevel.MFA_TOTP.value, AuthLevel.MFA_FIDO2.value,
                           AuthLevel.HUMAN_REVIEW.value])
    return {"action": action.value, "risk": round(_clamp(risk_target), 2),
            "auth": auth, "noul": noul, "flipped": flipped}


def _sample_scenario(rng: random.Random) -> str:
    x = rng.random()
    acc = 0.0
    for name, p in SCENARIO_MIX:
        acc += p
        if x <= acc:
            return name
    return "normal"


def generate(events_per_day: int = 240, seed: int = 20260921,
             n_users: int = 900) -> list[dict]:
    """产出按天有序的事件流；每条含特征、标签、元信息（day/uid/场景/真实risk）。"""
    rng = random.Random(seed)
    users = make_users(rng, n_users)
    events: list[dict] = []
    for day in range(TOTAL_DAYS):
        for _ in range(events_per_day):
            u = rng.choice(users)
            if day < u.home_day:
                u = rng.choice([x for x in users if x.home_day <= day] or users)
            scen = _sample_scenario(rng)
            f = _base_features(rng, u, day)
            f, truth = _apply_scenario(rng, f, scen)
            tr = _true_risk(f, truth["w"]) + rng.gauss(0, 0.03)
            tr = _clamp(tr)
            y = _labels(rng, f, truth, tr)
            events.append({"day": day, "uid": u.uid, "scenario": scen,
                           "true_risk": round(tr, 4), "features": f, "labels": y})
    return events


def window_of_day(day: int) -> str:
    if day < TRAIN_DAYS:
        return "train"
    if day < TRAIN_DAYS + CALIB_DAYS:
        return "calib"
    if day < TRAIN_DAYS + CALIB_DAYS + VAL_DAYS:
        return "val"
    return "test"


def split_users(events: list[dict]) -> dict[str, set[str]]:
    """§4.3 主体不跨 split：用户整体归入其活跃峰值窗口。"""
    from collections import Counter

    cnt: dict[str, Counter] = {}
    for e in events:
        cnt.setdefault(e["uid"], Counter())[window_of_day(e["day"])] += 1
    out: dict[str, set[str]] = {"train": set(), "calib": set(), "val": set(), "test": set()}
    tie = {"train": 0, "calib": 1, "val": 2, "test": 3}
    for uid, c in cnt.items():
        w = max(c.items(), key=lambda kv: (kv[1], -tie[kv[0]]))[0]
        out[w].add(uid)
    return out


def split_events(events: list[dict]) -> dict[str, list[dict]]:
    by_user = split_users(events)
    owner = {u: w for w, us in by_user.items() for u in us}   # uid → 窗口
    return {w: [e for e in events if owner.get(e["uid"]) == w] for w in by_user}
