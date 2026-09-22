"""MyJev PDP 决策合成（规格 §7）。

权限边界（§7.1）：
  1. 硬规则（denylist / 红线 / floor）优先，模型不可翻转；
  2. 模型只允许【提升】验证强度；【降低】仅在白名单业务 + 低风险档内允许；
  3. break-glass 通道绕过模型，全程强审计。
超时/异常 ⇒ fail-closed（step_up + human_review），缓存失效条件见 §7.3。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .contract import (Action, AuthLevel, Band, CalibratedProbs, FallbackReason,
                       GrantLevel, ModelProposal, enforce_invariants)
from .thresholds import capability_cap, pick_auth


@dataclass
class HardRuleHit:
    rule_id: str
    kind: str          # force_deny | floor_step_up | whitelist | break_glass
    reason: str


@dataclass
class HardRuleEngine:
    """§7.1 硬规则（v0 示例集，生产替换为策略库加载）。"""

    denylist_principals: frozenset[str] = frozenset({"svc-deprecated", "u-blackhole"})
    whitelist_services: frozenset[str] = frozenset({"svc-backup-internal"})
    break_glass_tokens: frozenset[str] = frozenset({"bg-emergency-001"})

    def to_dict(self) -> dict:
        return {"denylist_principals": sorted(self.denylist_principals),
                "whitelist_services": sorted(self.whitelist_services),
                "break_glass_tokens": sorted(self.break_glass_tokens)}

    @classmethod
    def from_dict(cls, d: dict) -> "HardRuleEngine":
        g = lambda k, default=frozenset(): frozenset((d or {}).get(k) or default)
        return cls(denylist_principals=g("denylist_principals"),
                   whitelist_services=g("whitelist_services"),
                   break_glass_tokens=g("break_glass_tokens"))

    def evaluate(self, ctx: dict, feats: dict) -> list[HardRuleHit]:
        hits: list[HardRuleHit] = []
        if ctx.get("principal") in self.denylist_principals:
            hits.append(HardRuleHit("HR-001", "force_deny", "principal 在 denylist"))
        if feats.get("t_is_admin_api") == 1 and feats.get("g_off_hours") == 1 \
                and feats.get("t_resource_sensitivity", 0) >= 4:
            hits.append(HardRuleHit("HR-002", "floor_step_up",
                                    "非工作时间高敏管理面：验证强度下限 step_up"))
        if feats.get("d_posture_sig_valid") == 0:
            hits.append(HardRuleHit("HR-003", "floor_step_up", "设备姿态签名无效：下限 step_up"))
        if ctx.get("principal") in self.whitelist_services \
                and feats.get("h_risk_ewma", 1.0) < 0.15:
            hits.append(HardRuleHit("HR-004", "whitelist", "内部服务白名单（低风险先验内）"))
        if ctx.get("break_glass") in self.break_glass_tokens:
            hits.append(HardRuleHit("HR-005", "break_glass", "break-glass 应急通道"))
        return hits


@dataclass
class PdpDecision:
    final_action: Action
    auth_required: AuthLevel
    grant: GrantLevel
    matched: list[str]
    override_by: Optional[str] = None
    override_reason: Optional[str] = None
    model_suggestion: Optional[dict] = None
    fallback_reason: Optional[str] = None


_ORDER = [Action.ALLOW, Action.STEP_UP, Action.DENY]


def _stronger(a: Action, b: Action) -> Action:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b


def fail_closed(reason: FallbackReason) -> PdpDecision:
    """§7.2 超时 200ms 或模型异常：不允许放行（fail-closed on uncertainty）。"""
    return PdpDecision(
        final_action=Action.STEP_UP, auth_required=AuthLevel.HUMAN_REVIEW,
        grant=GrantLevel.READ_ONLY_TTL60, matched=[f"HR-FAIL({reason.value})"],
        fallback_reason=reason.value,
    )


def combine(proposal: Optional[ModelProposal], hits: list[HardRuleHit]) -> PdpDecision:
    """§7.1 合成：硬规则优先；模型仅有受限裁量权。"""
    kinds = {h.kind for h in hits}
    sug = None if proposal is None else {
        "action": proposal.action.value, "confidence": round(proposal.confidence, 4),
        "band": proposal.band.value,
    }
    if "force_deny" in kinds:
        hr = next(h for h in hits if h.kind == "force_deny")
        return PdpDecision(
            final_action=Action.DENY, auth_required=AuthLevel.NONE,
            grant=GrantLevel.READ_ONLY_TTL60,
            matched=[h.rule_id for h in hits], override_by=hr.rule_id,
            override_reason=hr.reason, model_suggestion=sug)
    if "break_glass" in kinds:
        hr = next(h for h in hits if h.kind == "break_glass")
        return PdpDecision(
            final_action=Action.ALLOW,
            auth_required=enforce_invariants(Action.ALLOW, proposal.auth_required
                                             if proposal else AuthLevel.DEVICE_CERT),
            grant=GrantLevel.FULL, matched=[h.rule_id for h in hits],
            override_by=hr.rule_id, override_reason=hr.reason, model_suggestion=sug)
    if proposal is None:
        return fail_closed(FallbackReason.MODEL_ERROR)
    final = proposal.action
    override_by = override_reason = None
    if "floor_step_up" in kinds and final == Action.ALLOW:
        # 模型降权被禁止：硬规则下限生效
        hr = next(h for h in hits if h.kind == "floor_step_up")
        final, override_by, override_reason = Action.STEP_UP, hr.rule_id, hr.reason
    if "whitelist" in kinds and final == Action.STEP_UP and proposal.band == Band.LOW:
        # §7.1-2：降低强度仅允许发生在白名单业务 + 低风险档内
        hr = next(h for h in hits if h.kind == "whitelist")
        final, override_by, override_reason = Action.ALLOW, hr.rule_id, hr.reason
    auth = enforce_invariants(final, proposal.auth_required)
    if override_by and final == Action.STEP_UP and proposal.action == Action.DENY:
        auth = proposal.auth_required
    return PdpDecision(
        final_action=final, auth_required=auth,
        grant=capability_cap(final, proposal.band),
        matched=[h.rule_id for h in hits], override_by=override_by,
        override_reason=override_reason,
        model_suggestion=sug,
        fallback_reason=proposal.fallback_reason.value if proposal.fallback_reason else None)
