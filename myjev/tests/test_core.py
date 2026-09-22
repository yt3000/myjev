"""MyJev 核心行为单元测试（python -m unittest discover -s tests）。

覆盖：契约不变量（§2.2）、双阈值/灰区/迟滞（§6.2）、PDP 权限边界与
fail-closed（§7）、审计哈希链篡改检测（§9）、校准方向性、切分不跨主体（§4.3）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myjev.audit import AuditLog
from myjev.calibration import BinaryCalibrator
from myjev.contract import (Action, AuthLevel, Band, CalibratedProbs,
                            FallbackReason, ModelProposal, NOUL_HEADS,
                            enforce_invariants)
from myjev.data import generate, split_events, split_users
from myjev.pdp import HardRuleEngine, combine, fail_closed
from myjev.thresholds import BandStateMachine, ThresholdSet, decide
from myjev.contract import GrantLevel


def probs(a=0.95, s=0.03, d=0.02, risk=0.1, noul=None):
    return CalibratedProbs(
        action={"allow": a, "step_up": s, "deny": d},
        auth={x.value: 0.25 for x in AuthLevel if x.value != "none"},
        risk=risk,
        noul={h: (noul or 0.05) for h in NOUL_HEADS},
    )


class TestContract(unittest.TestCase):
    def test_deny_forces_auth_none(self):
        self.assertEqual(
            enforce_invariants(Action.DENY, AuthLevel.MFA_FIDO2), AuthLevel.NONE)
        self.assertEqual(
            enforce_invariants(Action.ALLOW, AuthLevel.MFA_FIDO2), AuthLevel.MFA_FIDO2)


class TestThresholds(unittest.TestCase):
    def setUp(self):
        self.ts = ThresholdSet()

    def test_allow_requires_double_gate(self):
        # 高 P(allow) 但 risk≥0.35 → 不允许 allow（双门）
        p = probs(a=0.97, s=0.02, d=0.01, risk=0.40)
        prop = decide(p, Band.MEDIUM, self.ts)
        self.assertEqual(prop.action, Action.STEP_UP)

    def test_gray_zone_forced_step_up(self):
        p = probs(a=0.40, s=0.35, d=0.25)
        prop = decide(p, Band.MEDIUM, self.ts)
        self.assertEqual(prop.action, Action.STEP_UP)
        self.assertEqual(prop.fallback_reason, FallbackReason.GRAY_ZONE)

    def test_deny_threshold(self):
        prop = decide(probs(a=0.02, s=0.08, d=0.90, risk=0.9),
                      Band.CRITICAL, self.ts)
        self.assertEqual(prop.action, Action.DENY)
        self.assertEqual(prop.auth_required, AuthLevel.NONE)  # §2.2

    def test_hysteresis_blocks_flapping(self):
        sm = BandStateMachine(self.ts)
        sm.update(0.10)                       # low
        self.assertEqual(sm.update(0.90), Band.LOW)   # 第 1 次越界：保持 low
        self.assertEqual(sm.update(0.90), Band.CRITICAL)  # 连续第 2 次：切换
        self.assertEqual(sm.update(0.10), Band.CRITICAL)  # 回落也要迟滞
        self.assertEqual(sm.update(0.10), Band.LOW)


class TestPDP(unittest.TestCase):
    def setUp(self):
        self.rules = HardRuleEngine()
        self.ts = ThresholdSet()

    def _hits(self, ctx, feats=None):
        return self.rules.evaluate(ctx, feats or {})

    def test_denylist_not_overridable(self):
        prop = ModelProposal(probs=probs(), action=Action.ALLOW, confidence=0.99,
                             band=Band.LOW, auth_required=AuthLevel.PASSWORD_OK)
        dec = combine(prop, self._hits({"principal": "u-blackhole"}))
        self.assertEqual(dec.final_action, Action.DENY)
        self.assertEqual(dec.auth_required, AuthLevel.NONE)
        self.assertEqual(dec.model_suggestion["action"], "allow")  # 建议留痕

    def test_model_cannot_lower_hard_floor(self):
        prop = ModelProposal(probs=probs(), action=Action.ALLOW, confidence=0.99,
                             band=Band.LOW, auth_required=AuthLevel.PASSWORD_OK)
        feats = {"t_is_admin_api": 1, "g_off_hours": 1, "t_resource_sensitivity": 5}
        dec = combine(prop, self._hits({"principal": "ops"}, feats))
        self.assertEqual(dec.final_action, Action.STEP_UP)
        self.assertEqual(dec.override_by, "HR-002")

    def test_downgrade_only_in_whitelist_low_band(self):
        gray = ModelProposal(probs=probs(a=0.4, s=0.35, d=0.25),
                             action=Action.STEP_UP, confidence=0.4,
                             band=Band.LOW, auth_required=AuthLevel.MFA_TOTP,
                             fallback_reason=FallbackReason.GRAY_ZONE)
        dec = combine(gray, self._hits({"principal": "svc-backup-internal"},
                                        {"h_risk_ewma": 0.05}))
        self.assertEqual(dec.final_action, Action.ALLOW)  # 白名单+low 档内允许降

        gray_hi = ModelProposal(probs=probs(a=0.4, s=0.35, d=0.25),
                                action=Action.STEP_UP, confidence=0.4,
                                band=Band.HIGH, auth_required=AuthLevel.MFA_FIDO2)
        dec2 = combine(gray_hi, self._hits({"principal": "svc-backup-internal"},
                                           {"h_risk_ewma": 0.05}))
        self.assertEqual(dec2.final_action, Action.STEP_UP)  # 非 low 档不得降

    def test_fail_closed(self):
        for reason in (FallbackReason.TIMEOUT, FallbackReason.MODEL_ERROR):
            dec = fail_closed(reason)
            self.assertEqual(dec.final_action, Action.STEP_UP)
            self.assertEqual(dec.auth_required, AuthLevel.HUMAN_REVIEW)
            self.assertEqual(dec.grant, GrantLevel.READ_ONLY_TTL60)


class TestAudit(unittest.TestCase):
    def test_tamper_detection(self):
        with tempfile.TemporaryDirectory() as td:
            log = AuditLog(Path(td) / "a.jsonl")
            quad = {"model_id": "t", "model_version": "0", "encoder_version": "e",
                    "threshold_set_id": "ts", "schema_version": "1"}
            for i in range(3):
                log.append(decision_id=f"d{i}", ts=i, version_quad=quad,
                           fs_id=f"fs{i}", feature_digest="x", features={"a": i},
                           raw={}, calibrated={}, final={"action": "allow"},
                           cache=None, exploration=False, latency_ms=1.0)
            ok, _ = log.verify()
            self.assertTrue(ok)
            # 篡改第 2 条的 final.action
            lines = (Path(td) / "a.jsonl").read_text(encoding="utf-8").splitlines()
            e = json.loads(lines[1])
            e["final"]["action"] = "deny"
            lines[1] = json.dumps(e, ensure_ascii=False)
            (Path(td) / "a.jsonl").write_text("\n".join(lines), encoding="utf-8")
            ok2, msg = AuditLog(Path(td) / "a.jsonl").verify()
            self.assertFalse(ok2)
            self.assertIn("篡改", msg)


class TestCalibration(unittest.TestCase):
    def test_temperature_reduces_ece(self):
        import numpy as np

        rng = np.random.default_rng(3)
        y = rng.binomial(1, 0.3, 4000)
        over = np.clip(0.25 + y * 0.6 + rng.normal(0, 0.1, 4000), 0.02, 0.98)  # 系统性过自信
        cal = BinaryCalibrator().fit(over, y)
        self.assertLess(cal.ece_after, cal.ece_before)
        self.assertLessEqual(ece_multi_test(cal.apply(over), y), 0.03)


def ece_multi_test(p, y, bins=15):
    import numpy as np

    edges = np.linspace(0, 1, bins + 1)
    tot = err = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p > lo) & (p <= hi)
        if not m.any():
            continue
        n = int(m.sum())
        err += n * abs(y[m].mean() - p[m].mean())
        tot += n
    return err / tot


class TestData(unittest.TestCase):
    def test_user_not_across_splits(self):
        ev = generate(events_per_day=20, seed=11)
        parts = split_events(ev)
        owners = {}
        for w, evs in parts.items():
            for e in evs:
                assert owners.setdefault(e["uid"], w) == w, "同一主体跨 split（§4.3）"
        by_user = split_users(ev)
        assert sum(len(s) for s in by_user.values()) == len(
            {e["uid"] for e in ev})


if __name__ == "__main__":
    unittest.main(verbosity=2)
