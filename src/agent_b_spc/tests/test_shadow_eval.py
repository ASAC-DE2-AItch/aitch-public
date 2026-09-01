"""shadow_eval 테스트 (B5-1b — 실력치 재설정 섀도 평가).

전부 순수(DB·Kafka 없이): 구/신 limits + replay Point 주입 → 오탐 감소율·verdict.
미탐은 미채점(2026-07-22 확정 — 판정 기준 애매) → verdict는 오탐 감소율만.
"""
from __future__ import annotations

import pytest

from src.agent_b_spc.nelson_engine import Point
from src.agent_b_spc import shadow_eval as se

GK = ("SIM_CH_1", "C6_0", 4, "settled", "C11")


def _lim(center, sigma, ucl, lcl, ver="v1", method="sigma"):
    """단일 그룹 limits dict (Nelson 계약 6키)."""
    return {GK: {"center": center, "sigma": sigma, "ucl": ucl, "lcl": lcl,
                 "limit_version": ver, "method": method}}


def _pt(value, wafer, i=0, gk=GK):
    """Point 1개 — 시간순 위해 i로 분(minute) 증가."""
    ts = f"2026-07-13T10:{i:02d}:00.000Z"
    return Point(gk, wafer, ts, value, 1, "v1")


def _stream(*values, gk=GK):
    """신호점을 zero 2개로 격리 → 다중점 룰(N5/N6) 오염 없이 N1 단일점 카운트 명확.

    Nelson N1은 center±3σ 판정이라, 인접 고점 2개는 N5(2/3점 beyond 2σ)도 발동한다.
    카운트 검증을 명확히 하려고 신호 사이에 zero wafer 2개(zone C, 미위반)를 끼운다.
    """
    pts, i = [], 0
    for k, v in enumerate(values):
        pts.append(_pt(v, f"W{k}", i, gk=gk)); i += 1
        pts.append(_pt(0.0, f"Z{k}a", i, gk=gk)); i += 1
        pts.append(_pt(0.0, f"Z{k}b", i, gk=gk)); i += 1
    return pts


def _cfg(reduction_min_pct=35.0, n_wafers=30, time_gap_hours=72.0):
    return se.ShadowConfig(reduction_min_pct=reduction_min_pct,
                           n_wafers=n_wafers, time_gap_hours=time_gap_hours)


# 구 tight(3σ=3) / 신 wide(3σ=30) — value 4는 구에서만 위반(N1). 신은 zone C라 무위반.
# (N1은 center±3σ 판정이지 ucl 필드가 아님 — ucl은 밴드 보고용. 신 σ=10으로 2σ 룰도 회피)
_OLD_TIGHT = _lim(0.0, 1.0, 3.0, -3.0)
_NEW_WIDE = _lim(0.0, 10.0, 30.0, -30.0)


# ── 오탐 감소 산식 (결정 3) ──────────────────────────────────────────

def test_false_alarm_reduction_counted():
    """구에서 위반 2 wafer, 신에서 0 → 감소율 100%."""
    pts = [_pt(4.0, "W1", 0), _pt(4.0, "W2", 1)]     # 둘 다 구 3σ 밖(N1), 신 zone C
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    assert r["old_violating_wafers"] == 2 and r["new_violating_wafers"] == 0
    assert r["false_alarm_reduction_pct"] == 100.0


def test_old_zero_gives_unknown():
    """구=0(원래 알람 없음) → 감소율 정의 불가 → verdict UNKNOWN(판정 유보)."""
    pts = [_pt(0.0, "W1", 0), _pt(0.5, "W2", 1)]     # 구 ucl=3 내라 위반 0
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    assert r["old_violating_wafers"] == 0
    assert r["false_alarm_reduction_pct"] == 0.0
    assert r["verdict"] == "UNKNOWN"                  # measurable=False


def test_negative_reduction_when_old_positive():
    """구>0인데 신이 더 많이 위반(재설정이 알람 늘림) → 음수 감소율 → FAIL."""
    old = _lim(0.0, 2.0, 6.0, -6.0)                   # 3σ=6 → value 7만 위반
    new = _lim(0.0, 1.0, 3.0, -3.0)                   # 3σ=3 → value 7·3.5 둘 다 위반
    pts = _stream(7.0, 3.5, 0.0)
    r = se.evaluate_shadow(old, new, pts, _cfg(), group_key=GK)
    assert r["old_violating_wafers"] == 1 and r["new_violating_wafers"] == 2
    assert r["false_alarm_reduction_pct"] == -100.0
    assert r["verdict"] == "FAIL"


def test_violation_counted_per_wafer_not_per_rule():
    """한 wafer가 여러 룰 위반해도 1로 카운트 (이벤트 아님 — 결정 3-④)."""
    # 3σ 밖 연속은 N1(매 점)+N5/N6 등 다중 룰 발동 가능 → distinct wafer 수로 집계
    pts = [_pt(5.0, f"W{i}", i) for i in range(4)]    # 4 wafer 전부 구 위반
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    assert r["old_violating_wafers"] == 4             # wafer 수(다중룰이어도 wafer당 1)


# ── verdict 파생 (결정 5 — 오탐만) ──────────────────────────────────

def test_verdict_pass_when_reduction_ok():
    """오탐 감소 ≥35% → PASS (측정가능)."""
    pts = [_pt(4.0, "W1", 0), _pt(4.0, "W2", 1)]
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    assert r["false_alarm_reduction_pct"] == 100.0 and r["verdict"] == "PASS"


def test_verdict_fail_when_reduction_below_threshold():
    """감소율 < 35% → FAIL."""
    # 구 3 위반, 신 2 위반 → 감소 33.3% < 35
    old = _OLD_TIGHT                                  # 3σ=3 → 7·7·4 셋 다 위반
    new = _lim(0.0, 2.0, 6.0, -6.0)                   # 3σ=6 → 7·7만 위반(4는 통과)
    pts = _stream(7.0, 7.0, 4.0)
    r = se.evaluate_shadow(old, new, pts, _cfg(), group_key=GK)
    assert r["old_violating_wafers"] == 3 and r["new_violating_wafers"] == 2
    assert r["false_alarm_reduction_pct"] == 33.3 and r["verdict"] == "FAIL"


# ── stateful 격리 · 단일 그룹 · 방어 정렬 ────────────────────────────

def test_old_new_use_separate_engines():
    """구/신 각각 새 NelsonEngine — 한쪽 replay가 다른쪽 상태 오염 안 함."""
    pts = [_pt(4.0, "W1", 0), _pt(4.0, "W2", 1)]
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    # 신이 구 상태를 물려받았다면 위반 카운트가 뒤틀림 — 격리면 신=0 유지
    assert r["new_violating_wafers"] == 0


def test_other_group_points_skipped():
    """단일 그룹 — whitelist={group_key}, 타 그룹 point는 위반 집계 제외."""
    other = ("SIM_CH_1", "C6_0", 5, "settled", "C11")
    pts = [_pt(4.0, "W1", 0), _pt(9.0, "W2", 1, gk=other)]   # W2는 타 그룹
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    assert r["old_violating_wafers"] == 1               # W1만 (W2 타그룹 skip)


def test_points_sorted_defensively():
    """입력이 시간 역순이어도 방어 정렬 후 replay (Nelson stateful 안전)."""
    pts = [_pt(4.0, "W2", 5), _pt(4.0, "W1", 0)]        # 역순 주입
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    assert r["old_violating_wafers"] == 2               # 정렬 무관 카운트 동일


def test_output_shape():
    """출력 계약 — 필드·타입."""
    pts = [_pt(4.0, "W1", 0)]
    r = se.evaluate_shadow(_OLD_TIGHT, _NEW_WIDE, pts, _cfg(), group_key=GK)
    assert set(r) == {"group_key", "n_wafers", "false_alarm_reduction_pct",
                      "old_violating_wafers", "new_violating_wafers", "verdict"}
    assert r["group_key"] == GK and r["n_wafers"] == 1


# ── ShadowConfig 로드 (2-섹션 · 네임스페이스) ────────────────────────

def test_shadow_config_loads_from_params():
    """A5/A6 — limit_engine(shadow_*) + spc(time_gap) 2-섹션 로드, 키 명시 매핑."""
    cfg = se.ShadowConfig.load()
    assert cfg.reduction_min_pct == 35.0             # limit_engine.shadow_pass_false_alarm_reduction_min_pct
    assert cfg.n_wafers == 30                         # limit_engine.shadow_eval_wafers
    assert cfg.time_gap_hours == 72.0                # spc.time_gap_threshold_hours
    assert not hasattr(cfg, "missed_max")            # 미채점 — missed_max 필드 제거 확인


# ── verify_forward (B6-3-b V — 새 firm 관리선 forward 오탐 품질 채점) ─────────
#
# shadow_eval의 과거 replay와 반대 방향: 승인·적용된 firm 관리선을 **forward N장**으로
# 채점. 합격선 = 절대 오탐율 상한(감소율 아님 — 새 관리선은 비교 대상 없음). b는 채점만.


def _vcfg(false_alarm_max_pct=10.0, time_gap_hours=72.0):
    return se.VerifyConfig(false_alarm_max_pct=false_alarm_max_pct, time_gap_hours=time_gap_hours)


def test_verify_forward_pass_when_false_alarm_low():
    """새 firm 광폭 관리선에서 forward 5장 전부 밴드 내 → 오탐 0% ≤ 상한 → PASS."""
    pts = [_pt(4.0, f"W{i}", i) for i in range(5)]   # 신 wide(±30)라 무위반
    r = se.verify_forward(_NEW_WIDE, pts, _vcfg(), group_key=GK)
    assert r["flagged_wafers"] == 0
    assert r["n_wafers"] == 5
    assert r["false_alarm_pct"] == 0.0
    assert r["verdict"] == "PASS"


def test_verify_forward_fail_when_false_alarm_high():
    """firm 관리선이 과협착이면 forward 대부분 위반 → 오탐율 > 상한 → FAIL."""
    pts = [_pt(4.0, f"W{i}", i) for i in range(5)]   # 구 tight(3σ=3)면 4.0 전부 N1 위반
    r = se.verify_forward(_OLD_TIGHT, pts, _vcfg(false_alarm_max_pct=10.0), group_key=GK)
    assert r["flagged_wafers"] == 5
    assert r["false_alarm_pct"] == 100.0
    assert r["verdict"] == "FAIL"


def test_verify_forward_boundary_equal_is_pass():
    """오탐율 == 상한 → PASS (`<=` 시맨틱). 5장 중 1 위반 = 20% == 상한 20."""
    pts = [_pt(4.0, "W0", 0)] + [_pt(0.0, f"Z{i}", i + 1) for i in range(4)]
    r = se.verify_forward(_OLD_TIGHT, pts, _vcfg(false_alarm_max_pct=20.0), group_key=GK)
    assert r["flagged_wafers"] == 1 and r["n_wafers"] == 5
    assert r["false_alarm_pct"] == 20.0
    assert r["verdict"] == "PASS"


def test_verify_forward_empty_unknown():
    """forward 0장 → 채점 불가 → UNKNOWN(판정 유보)."""
    r = se.verify_forward(_NEW_WIDE, [], _vcfg(), group_key=GK)
    assert r["n_wafers"] == 0
    assert r["verdict"] == "UNKNOWN"


def test_verify_forward_sorts_and_single_group():
    """방어 정렬 + 단일 그룹(타 그룹 point 제외)."""
    other = ("SIM_CH_1", "C6_0", 5, "settled", "C11")
    pts = [_pt(4.0, "W1", 5), _pt(9.0, "W2", 0, gk=other)]   # 역순 + 타그룹
    r = se.verify_forward(_OLD_TIGHT, pts, _vcfg(), group_key=GK)
    assert r["flagged_wafers"] == 1                  # W1만(W2 타그룹 skip)


def test_verify_forward_output_shape():
    """출력 계약 — 필드."""
    r = se.verify_forward(_OLD_TIGHT, [_pt(4.0, "W1", 0)], _vcfg(), group_key=GK)
    assert set(r) == {"group_key", "n_wafers", "flagged_wafers",
                      "false_alarm_pct", "verdict"}


def test_verify_config_loads_from_params():
    """VerifyConfig — limit_engine(신규 절대 상한 키) + spc(time_gap) 2-섹션."""
    cfg = se.VerifyConfig.load()
    assert cfg.false_alarm_max_pct == 5.0            # limit_engine.verify_forward_false_alarm_max_pct
    assert cfg.time_gap_hours == 72.0               # spc.time_gap_threshold_hours


def test_verify_forward_excludes_nan_from_denominator():
    """분모/분자 universe 정합 — NaN/None wafer는 채점 불가라 분모(n_wafers)에서도 제외.

    _replay_flagged는 NaN을 feed 단계서 스킵(분자 미포함)하는데 분모가 전체를 세면 오탐율이
    희석돼 경계 판정이 잘못 PASS로 뒤집힐 수 있다(리뷰 🟡).
    """
    pts = [_pt(4.0, "W0", 0), _pt(0.0, "W1", 1), _pt(0.0, "W2", 2),
           _pt(float("nan"), "W3", 3), _pt(float("nan"), "W4", 4)]
    r = se.verify_forward(_OLD_TIGHT, pts, _vcfg(false_alarm_max_pct=25.0), group_key=GK)
    assert r["n_wafers"] == 3                         # NaN 2장 제외(유효 3)
    assert r["flagged_wafers"] == 1                   # W0만
    assert r["false_alarm_pct"] == pytest.approx(33.3, abs=0.1)   # 1/3, not 1/5
    assert r["verdict"] == "FAIL"                     # 33.3 > 25


def test_verify_forward_unknown_when_limits_missing_group():
    """new_firm_limits에 group_key 행이 없으면 채점 불가 → UNKNOWN(거짓 PASS 방지, 리뷰 🟡)."""
    other = ("SIM_CH_1", "C6_0", 9, "settled", "C99")
    wrong = {other: {"center": 0.0, "sigma": 1.0, "ucl": 3.0, "lcl": -3.0,
                     "limit_version": "v1", "method": "sigma"}}
    pts = [_pt(4.0, "W0", 0)]                         # GK point — 관리선 있었으면 위반
    r = se.verify_forward(wrong, pts, _vcfg(), group_key=GK)
    assert r["verdict"] == "UNKNOWN"                  # GK 관리선 부재 → 채점 안 됨
