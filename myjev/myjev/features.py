"""MyJev 特征规范与快照（规格 §3）。

v0（M2 基线）覆盖 S/D/C/G/T/H 六组数值化特征；R 组文本 v0 仅保留
注入标记位与 padding 比例两个代理特征，句向量在融合模型阶段接入（§5.1）。
缺失策略按 §3.1：类别缺失→专用 -1 哨兵值并置缺失位；数值缺失→群体先验 + 缺失位。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

MISSING_SENTINEL = -1.0

#: 特征组 → 列名（顺序即模型输入列顺序，冻结；变更升 feature_digest）
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "S": (  # 主体
        "s_role_level", "s_hist_login_ok_rate_30d", "s_recent_cred_change",
        "s_acct_age_days_log", "s_fail_logins_1h",
    ),
    "D": (  # 设备
        "d_mdm_enrolled", "d_cert_valid", "d_jailbroken", "d_os_known",
        "d_posture_ok", "d_posture_sig_valid",
    ),
    "C": (  # 会话/行为
        "c_since_prev_login_min_log", "c_device_switch", "c_new_device",
        "c_req_rate_z", "c_ua_entropy", "c_session_conc_same_asn",
    ),
    "G": (  # 地理/时间
        "g_ip_reputation", "g_proxy_idc", "g_geo_anomaly", "g_off_hours",
    ),
    "T": (  # 目标资源
        "t_resource_sensitivity", "t_zone_public", "t_is_export_api",
        "t_is_admin_api",
    ),
    "H": (  # 历史窗口
        "h_events_24h_log", "h_risk_ewma", "h_logins_5m", "h_anom_events_24h",
    ),
    "R": (  # 请求文本代理（v0）
        "r_injection_flag", "r_padding_ratio",
    ),
}

#: 全部列的展平顺序（模型契约的一部分，写进模型卡）
FEATURE_NAMES: tuple[str, ...] = tuple(
    c for group in FEATURE_GROUPS.values() for c in group
)

#: §3.1 各组的缺失哨兵约定：这些列缺失时置 MISSING_SENTINEL 并生成 __na 指示位
NA_TRACKED = (
    "s_hist_login_ok_rate_30d", "c_since_prev_login_min_log",
    "g_ip_reputation", "h_risk_ewma",
)


@dataclass
class FeatureSnapshot:
    """§3.3 特征快照：fs-id + 列式记录 + sha256 摘要，供审计回放。"""

    fs_id: str
    ts: float
    features: dict[str, Any]
    digest: str


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def take_snapshot(features: dict[str, Any], ts: float = 0.0) -> FeatureSnapshot:
    fs_id = "fs-" + uuid.uuid4().hex[:12]
    digest = "sha256:" + hashlib.sha256(canonical_json(features).encode("utf-8")).hexdigest()
    return FeatureSnapshot(fs_id=fs_id, ts=ts, features=dict(features), digest=digest)


def to_row(features: dict[str, Any]) -> list[float]:
    """特征字典 → 固定顺序数值行（含缺失指示位）。"""
    row: list[float] = []
    for name in FEATURE_NAMES:
        v = features.get(name, None)
        if v is None:
            row.append(MISSING_SENTINEL if name in NA_TRACKED else 0.0)
        else:
            row.append(float(v))
    return row


def to_matrix(rows: Iterable[dict[str, Any]]) -> "Any":
    import numpy as np

    return np.asarray([to_row(r) for r in rows], dtype=np.float64)


def feature_digest() -> str:
    """特征契约摘要（进版本四元组的 model 组件标识）。"""
    payload = canonical_json({"names": FEATURE_NAMES, "na": list(NA_TRACKED),
                              "sentinel": MISSING_SENTINEL})
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
