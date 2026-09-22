"""MyJev 决策契约（规格 §2）。

schema v1.0 冻结：枚举与结构不得随意增删；任何变更需升 SCHEMA_VERSION，
并触发 §9 版本四元组变更与 §11 门禁重跑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

SCHEMA_VERSION = "1.0"
GRAY_CONFIDENCE_FLOOR = 0.60  # §2.2：max prob 低于此值强制 step_up 并入灰区


class Action(str, Enum):
    ALLOW = "allow"
    STEP_UP = "step_up"
    DENY = "deny"


class AuthLevel(str, Enum):
    NONE = "none"
    PASSWORD_OK = "password_ok"
    MFA_TOTP = "mfa_totp"
    MFA_FIDO2 = "mfa_fido2"
    DEVICE_CERT = "device_cert"
    HUMAN_REVIEW = "human_review"


class Band(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


BAND_ORDER: list[Band] = [Band.LOW, Band.MEDIUM, Band.HIGH, Band.CRITICAL]


class GrantLevel(str, Enum):
    """§6.3 能力上限（爆炸半径控制）。"""

    FULL = "full"
    READ_ONLY = "read_only"
    READ_ONLY_TTL60 = "read_only_ttl60"


class FallbackReason(str, Enum):
    """§2.2 / §7.2 非正常路径的枚举原因码（进审计与告警）。"""

    GRAY_ZONE = "gray_zone"
    TIMEOUT = "timeout"
    MODEL_ERROR = "model_error"
    RULE_FORCED = "rule_forced"
    BREAK_GLASS = "break_glass"


#: §2.3 Noul 头清单 v1 冻结 8 项（名称即标签键，禁止漂移）
NOUL_HEADS: tuple[str, ...] = (
    "credential_stuffing",
    "account_takeover",
    "device_untrusted",
    "impossible_travel",
    "bot_automation",
    "phishing_target",
    "data_exfil_pattern",
    "sensitive_policy_hit",
)


@dataclass
class RawProbs:
    """模型原始输出（校准前，仅可用于排序——§6.1 红线）。"""

    action: dict[str, float]          # 3 类 softmax
    auth: dict[str, float]            # 6 类 softmax
    risk: float                       # [0,1] 回归原始值
    noul: dict[str, float]            # 8 头 sigmoid 概率
    raw_logits: dict = field(default_factory=dict)


@dataclass
class CalibratedProbs:
    """校准后概率：只有这一层允许进入 §6.2 阈值判定与审计证据。"""

    action: dict[str, float]
    auth: dict[str, float]
    risk: float
    noul: dict[str, float]
    raw: dict = field(default_factory=dict)  # 对应未校准原始值（§9 审计要求两者都留痕）


@dataclass
class ModelProposal:
    """模型 → PDP 的建议（模型无最终裁量权，§7.1）。"""

    probs: CalibratedProbs
    action: Action
    confidence: float
    band: Band
    auth_required: AuthLevel
    fallback_reason: Optional[FallbackReason] = None


@dataclass
class DecisionOutput:
    """§2.1 PDP 对外输出结构（PEP 可见字段；逐头原始概率不外发——§8.2）。"""

    schema_version: str
    model_id: str
    model_version: str
    decision: dict            # {action, confidence}
    risk: dict                # {score, band}
    auth_required: AuthLevel
    grant: GrantLevel         # §6.3 实际签发能力上限
    noul: dict[str, float]    # 校准后（对外可粗粒化，此处保留供审计）
    final_action: Action      # PDP 合成后执行动作
    hard_rules_matched: list[str] = field(default_factory=list)
    override_by: Optional[str] = None
    override_reason: Optional[str] = None
    fallback_reason: Optional[str] = None
    cache_ttl_s: int = 300
    threshold_set_id: str = ""
    feature_snapshot_id: str = ""
    decision_id: str = ""

    def to_json_dict(self) -> dict:
        d = dict(
            schema_version=self.schema_version,
            model=f"{self.model_id}@{self.model_version}",
            decision={"action": self.decision["action"],
                      "confidence": round(float(self.decision["confidence"]), 4)},
            risk={"score": round(float(self.risk["score"]), 4),
                  "band": self.risk["band"]},
            auth_required=self.auth_required.value,
            grant=self.grant.value,
            final_action=self.final_action.value,
            noul={k: round(float(v), 4) for k, v in self.noul.items()},
            policy_meta=dict(
                threshold_set_id=self.threshold_set_id,
                feature_snapshot_id=self.feature_snapshot_id,
                fallback_reason=self.fallback_reason,
                hard_rules_matched=self.hard_rules_matched,
                override_by=self.override_by,
                override_reason=self.override_reason,
            ),
            cache_ttl_s=self.cache_ttl_s,
            decision_id=self.decision_id,
        )
        return d


def enforce_invariants(action: Action, auth: AuthLevel) -> AuthLevel:
    """§2.2 联合约束（决策层强制，不依赖模型自洽）：deny ⇒ auth 恒为 none。"""
    if action == Action.DENY:
        return AuthLevel.NONE
    return auth
