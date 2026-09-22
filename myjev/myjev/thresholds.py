"""MyJev 双阈值 + 灰区决策与能力上限（规格 §6.2 / §6.3）。

输入必须是校准后概率（§6.1 红线）。阈值组 threshold_set_id 版本化，
绑定进 §9 版本四元组。
"""
from __future__ import annotations

from dataclasses import dataclass

from .contract import (
    BAND_ORDER,
    Action,
    AuthLevel,
    Band,
    FallbackReason,
    GrantLevel,
    ModelProposal,
    CalibratedProbs,
    enforce_invariants,
)


@dataclass(frozen=True)
class ThresholdSet:
    """§6.2 默认参数；非对称代价 误拦:漏放=1:8 在验证集代价曲线上标定。"""

    threshold_set_id: str = "ts-2026w39"
    theta_allow: float = 0.90      # P(allow) 门
    theta_deny: float = 0.85       # P(deny) 门
    gray_floor: float = 0.60       # max prob 低于 → 灰区
    risk_allow_max: float = 0.35   # allow 需 risk 同时低于此值
    band_edges: tuple[float, float, float] = (0.35, 0.65, 0.85)
    hysteresis_k: int = 2          # 档位切换需连续 k 次越界
    cache_ttl_s: int = 300

    def to_dict(self) -> dict:
        from dataclasses import asdict
        d = asdict(self)
        d["band_edges"] = list(self.band_edges)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ThresholdSet":
        import dataclasses
        names = {f.name for f in dataclasses.fields(cls)}
        kw = {k: (tuple(v) if k == "band_edges" else v)
              for k, v in (d or {}).items() if k in names}
        return cls(**kw)

    def band_of(self, risk: float) -> Band:
        e = self.band_edges
        if risk < e[0]:
            return Band.LOW
        if risk < e[1]:
            return Band.MEDIUM
        if risk < e[2]:
            return Band.HIGH
        return Band.CRITICAL


class BandStateMachine:
    """§6.2 迟滞：会话内档位迁移需连续 k 次越界，防抖动。"""

    def __init__(self, ts: ThresholdSet):
        self.ts = ts
        self.current: Band | None = None
        self._pending: Band | None = None
        self._count = 0

    def update(self, risk: float) -> Band:
        target = self.ts.band_of(risk)
        if self.current is None or target == self.current:
            self.current, self._pending, self._count = target, None, 0
            return self.current
        if target == self._pending:
            self._count += 1
        else:
            self._pending, self._count = target, 1
        if self._count >= self.ts.hysteresis_k:
            self.current, self._pending, self._count = target, None, 0
        return self.current


def decide(
    probs: CalibratedProbs,
    band: Band,
    ts: ThresholdSet,
) -> ModelProposal:
    """校准概率 → 模型建议（§2.2 逻辑：灰区强制 step_up；allow 双门）。"""
    a = probs.action
    fallback = None
    if max(a.values() or [0.0]) < ts.gray_floor:
        action, fallback = Action.STEP_UP, FallbackReason.GRAY_ZONE
    elif a.get(Action.DENY.value, 0.0) > ts.theta_deny:
        action = Action.DENY
    elif (a.get(Action.ALLOW.value, 0.0) > ts.theta_allow
          and probs.risk < ts.risk_allow_max):
        action = Action.ALLOW
    else:
        action = Action.STEP_UP
    confidence = a.get(action.value, 0.0)
    auth = enforce_invariants(action, pick_auth(action, band, probs.risk))
    return ModelProposal(
        probs=probs, action=action, confidence=float(confidence),
        band=band, auth_required=auth, fallback_reason=fallback,
    )


def pick_auth(action: Action, band: Band, risk: float) -> AuthLevel:
    """§6.3 / §2.2 认证强度表：动作 × 风险档 → 要求因子。"""
    if action == Action.DENY:
        return AuthLevel.NONE
    if action == Action.ALLOW:
        return {Band.LOW: AuthLevel.PASSWORD_OK, Band.MEDIUM: AuthLevel.MFA_TOTP,
                Band.HIGH: AuthLevel.MFA_FIDO2, Band.CRITICAL: AuthLevel.DEVICE_CERT}[band]
    # step_up
    if band == Band.CRITICAL:
        return AuthLevel.HUMAN_REVIEW
    return AuthLevel.MFA_FIDO2 if risk >= 0.7 else AuthLevel.MFA_TOTP


def capability_cap(action: Action, band: Band) -> GrantLevel:
    """§6.3 爆炸半径：granted = min(资源策略, 档位上限)；critical 即使 allow 也只 60s 只读。"""
    if action == Action.DENY:
        return GrantLevel.READ_ONLY_TTL60  # 语义上无授权，占位最小值（PEP 侧执行拒绝）
    if band == Band.LOW:
        return GrantLevel.FULL
    if band == Band.MEDIUM:
        return GrantLevel.READ_ONLY if action == Action.STEP_UP else GrantLevel.FULL
    return GrantLevel.READ_ONLY_TTL60


def pick_auth_from_probs(probs: CalibratedProbs, action: Action, ts: ThresholdSet) -> AuthLevel:
    """auth 头可选启用：以头输出选因子，但受 decide() 表的下限约束（取更强因子）。"""
    tab = pick_auth(action, ts.band_of(probs.risk), probs.risk)
    order = [AuthLevel.NONE, AuthLevel.PASSWORD_OK, AuthLevel.MFA_TOTP,
             AuthLevel.MFA_FIDO2, AuthLevel.DEVICE_CERT, AuthLevel.HUMAN_REVIEW]
    head = max(probs.auth, key=probs.auth.get)
    head_lv = AuthLevel(head)
    return head_lv if order.index(head_lv) >= order.index(tab) else tab
