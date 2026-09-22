"""MyJev 网关侧服务（规格 §7.2 / §7.3 / §10.3）。

进程内仿真 PEP→模型→PDP 全链路：数据面缓存（含失效条件）、会话级迟滞
状态机、超时/异常 fail-closed、探索样本标记、审计落盘。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .audit import AuditLog
from .contract import (Action, AuthLevel, Band, DecisionOutput, FallbackReason,
                       ModelProposal, NOUL_HEADS)
from .features import take_snapshot
from .models import MODEL_ID, MODEL_VERSION, apply_calibration
from .pdp import HardRuleEngine, PdpDecision, combine, fail_closed
from .thresholds import BandStateMachine, ThresholdSet, decide

CACHE_CONTEXT_KEYS = ("ip_continent", "device_hash", "role")  # §7.3 失效条件


class SessionStore:
    """会话状态：档位迟滞机 + 上下文指纹 + 上次决策缓存。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.band_sm = BandStateMachine(ThresholdSet())
        self.context: dict = {}
        self.cached: DecisionOutput | None = None
        self.expires: float = 0.0


@dataclass
class ServiceStats:
    requests: int = 0
    cache_hits: int = 0
    cache_invalid: int = 0
    fallback_gray_zone: int = 0
    fallback_timeout: int = 0
    fallback_model_error: int = 0
    rule_overrides: int = 0
    exploration: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class MyJevService:
    def __init__(self, bundle, calibrators, ts: ThresholdSet,
                 quad: dict, audit: AuditLog, rng_prob=0.0,
                 hard_rules: HardRuleEngine | None = None):
        self.bundle = bundle
        self.cals = calibrators
        self.ts = ts
        self.quad = quad                      # 版本四元组 + feature_digest
        self.audit = audit
        self.rules = hard_rules or HardRuleEngine()
        self.sessions: dict[str, SessionStore] = {}
        self.global_risk_event: bool = False  # §7.3 TI 命中/漏洞爆发 → 全量失效
        self.inject_timeout = False           # 演示用故障注入开关
        self.exploration_prob = rng_prob      # §10.3 探索性样本比例
        self.stats = ServiceStats()

    # ------------------------------------------------------------ 缓存 §7.3
    def _cache_key_match(self, sess: SessionStore, ctx: dict) -> bool:
        if self.global_risk_event:
            return False
        for k in CACHE_CONTEXT_KEYS:
            if ctx.get(k) != sess.context.get(k):
                return False
        return True

    # ------------------------------------------------------------ 主入口
    def decide(self, ctx: dict, features: dict, ts_wall: float | None = None) -> DecisionOutput:
        """ctx: {session_id, principal, break_glass?}; features: §3 特征字典。"""
        self.stats.requests += 1
        now = ts_wall if ts_wall is not None else time.time()
        sess = self.sessions.setdefault(ctx["session_id"],
                                        SessionStore(ctx["session_id"]))
        hits = self.rules.evaluate(ctx, features)
        if sess.cached is not None and now < sess.expires and self._cache_key_match(sess, ctx):
            self.stats.cache_hits += 1
            out = sess.cached
            out.decision_id = "d-" + uuid.uuid4().hex[:12]
            out.final_action, out.auth_required = _final_from_decision(
                out, hits)
            return out
        if sess.cached is not None:
            self.stats.cache_invalid += 1

        snapshot = take_snapshot(features, ts=now)
        # 模型调用（含超时/故障仿真，§7.2 超时 200ms ⇒ fail-closed）
        if self.inject_timeout:
            prop = None
            decision = fail_closed(FallbackReason.TIMEOUT)
            self.stats.fallback_timeout += 1
        else:
            try:
                cal_list, lat = apply_calibration(self.bundle, self.cals,
                                                  [{"features": features}])
                probs = cal_list[0]
                band = sess.band_sm.update(probs.risk)
                prop = decide(probs, band, self.ts)
                decision = combine(prop, hits)
            except Exception:
                prop, decision = None, fail_closed(FallbackReason.MODEL_ERROR)
                self.stats.fallback_model_error += 1
        if prop is not None and prop.fallback_reason == FallbackReason.GRAY_ZONE:
            self.stats.fallback_gray_zone += 1
        if decision.override_by:
            self.stats.rule_overrides += 1
        exploration = _bern(self.exploration_prob)
        if exploration:
            self.stats.exploration += 1

        out = DecisionOutput(
            schema_version=self.quad["schema_version"],
            model_id=self.quad["model_id"], model_version=self.quad["model_version"],
            decision={"action": prop.action.value if prop else decision.final_action.value,
                      "confidence": prop.confidence if prop else 0.0},
            risk={"score": probs.risk if prop else -1.0,
                  "band": (prop.band if prop else Band.CRITICAL).value},
            auth_required=decision.auth_required,
            grant=decision.grant,
            noul={h: probs.noul.get(h, -1.0) for h in NOUL_HEADS} if prop else {},
            final_action=decision.final_action,
            hard_rules_matched=decision.matched,
            override_by=decision.override_by, override_reason=decision.override_reason,
            fallback_reason=decision.fallback_reason,
            cache_ttl_s=0.0 if decision.fallback_reason else self.ts.cache_ttl_s,
            threshold_set_id=self.ts.threshold_set_id,
            feature_snapshot_id=snapshot.fs_id,
            decision_id="d-" + uuid.uuid4().hex[:12],
        )
        if decision.final_action == Action.DENY:
            out.cache_ttl_s = 30  # 拒绝结果短缓存，便于持续验证降权重试
        sess.context = {k: ctx.get(k) for k in CACHE_CONTEXT_KEYS}
        sess.cached = out
        sess.expires = now + float(out.cache_ttl_s)

        self.audit.append(
            decision_id=out.decision_id, ts=now, version_quad=self.quad,
            fs_id=snapshot.fs_id, feature_digest=snapshot.digest,
            features=snapshot.features,
            raw=(prop.probs.raw if prop else {}),
            calibrated=dict(action=prop.probs.action if prop else {},
                            risk=prop.probs.risk if prop else -1.0,
                            noul=out.noul) if prop else {},
            final=dict(action=decision.final_action.value,
                       auth=decision.auth_required.value,
                       grant=decision.grant.value,
                       model_suggestion=decision.model_suggestion,
                       override_by=decision.override_by,
                       override_reason=decision.override_reason,
                       hard_rules=decision.matched,
                       fallback=decision.fallback_reason),
            cache=dict(ttl_s=out.cache_ttl_s),
            exploration=exploration,
        )
        return out

    def invalidate_all(self):
        """§7.3 全局风险事件：TI 命中/漏洞爆发 ⇒ 强制全量重决策。"""
        self.global_risk_event = True
        for s in self.sessions.values():
            s.cached, s.expires = None, 0.0
        self.global_risk_event = False


def _final_from_decision(out: DecisionOutput, hits) -> tuple[Action, AuthLevel]:
    """缓存命中仍受硬规则约束（denylist 即时生效，不等 TTL）。"""
    forced = combine(None, hits)
    if forced.override_by and "HR-001" in forced.matched:
        return Action.DENY, AuthLevel.NONE
    return out.final_action, out.auth_required


def _bern(p: float) -> bool:
    import random

    return random.random() < p
