"""Nelson 룰 순수 함수 유닛테스트 (B3-1 DoD 8/8)."""
from src.agent_b_spc.nelson_rules import (
    Zones,
    rule_n1, rule_n2, rule_n3, rule_n4, rule_n5, rule_n6, rule_n7, rule_n8,
    RULES, MAX_WINDOW,
)

# 표준 zones: center=0, σ=1 → s1=±1, s2=±2, s3=±3
Z = Zones.from_limit(center=0.0, sigma=1.0)


def test_n1_beyond_3sigma_fires():
    assert rule_n1([3.5], Z) is True      # +3σ 밖
    assert rule_n1([-3.5], Z) is True     # -3σ 밖


def test_n1_within_returns_false():
    assert rule_n1([2.9], Z) is False


def test_n1_exact_boundary_is_strict_false():
    assert rule_n1([3.0], Z) is False     # 정확히 s3p → 위반 아님(strict)


def test_n1_empty_returns_false():
    assert rule_n1([], Z) is False


def test_n2_nine_same_side_fires():
    assert rule_n2([0.5] * 9, Z) is True       # 9점 전부 center 위
    assert rule_n2([-0.5] * 9, Z) is True


def test_n2_eight_only_false():
    assert rule_n2([0.5] * 8, Z) is False


def test_n2_middle_opposite_breaks():
    assert rule_n2([0.5, 0.5, 0.5, 0.5, -0.5, 0.5, 0.5, 0.5, 0.5], Z) is False


def test_n2_middle_equals_center_breaks():
    # ==center는 위도 아래도 아님 → run 단절 (일관성 잠금)
    assert rule_n2([0.5, 0.5, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5, 0.5], Z) is False


def test_n3_six_monotonic_up_fires():
    assert rule_n3([1, 2, 3, 4, 5, 6], Z) is True
    assert rule_n3([6, 5, 4, 3, 2, 1], Z) is True


def test_n3_tie_breaks_strict():
    assert rule_n3([1, 2, 3, 3, 4, 5], Z) is False   # 동점 → strict 단절


def test_n4_alternating_fires():
    zig = [10, 0, 10, 0, 10, 0, 10, 0, 10, 0, 10, 0, 10, 0]  # 14점 교대
    assert rule_n4(zig, Z) is True


def test_n4_delta_zero_breaks():
    w = [10, 0, 10, 0, 0, 0, 10, 0, 10, 0, 10, 0, 10, 0]     # delta=0 낌
    assert rule_n4(w, Z) is False


def test_n5_two_of_three_beyond_2sigma_fires():
    assert rule_n5([2.5, 2.5, 0.0], Z) is True    # 무가드: 최신 정상이어도 True


def test_n5_one_only_false():
    assert rule_n5([2.5, 0.0, 0.0], Z) is False


def test_n6_four_of_five_beyond_1sigma_fires():
    assert rule_n6([1.5, 1.5, 1.5, 1.5, 0.0], Z) is True


def test_n6_three_only_false():
    assert rule_n6([1.5, 1.5, 1.5, 0.0, 0.0], Z) is False


def test_n7_fifteen_within_fires():
    assert rule_n7([0.5] * 15, Z) is True


def test_n7_one_outside_breaks():
    w = [0.5] * 14 + [1.5]
    assert rule_n7(w, Z) is False


def test_n7_exact_1sigma_is_inclusive():
    # 정확히 1σ 값 → 이내(포함)
    assert rule_n7([1.0] * 15, Z) is True


def test_n8_eight_outside_both_sides_fires():
    w = [1.5, -1.5, 1.5, -1.5, 1.5, -1.5, 1.5, -1.5]   # 8점 전부 밖 + 양쪽
    assert rule_n8(w, Z) is True


def test_n8_one_side_only_false():
    assert rule_n8([1.5] * 8, Z) is False              # 전부 밖이나 한쪽만


def test_n8_exact_1sigma_is_strict_not_outside():
    # 정확히 1σ 값은 '밖' 아님 → N8 단절 (N7과 상보)
    w = [1.0, -1.5, 1.5, -1.5, 1.5, -1.5, 1.5, -1.5]
    assert rule_n8(w, Z) is False


def test_registry_has_eight_rules():
    assert [r.id for r in RULES] == ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8"]


def test_registry_triggers():
    trig = {r.id: r.trigger for r in RULES}
    assert trig["N1"] == "level"                       # 기획서 4-3
    assert all(trig[f"N{i}"] == "edge" for i in range(2, 9))


def test_max_window_is_15():
    assert MAX_WINDOW == 15                             # N7 윈도우


def test_n5_below_side_fires():
    assert rule_n5([-2.5, -2.5, 0.0], Z) is True      # 2점 −2σ 밖 (below 분기 검증)


def test_n6_below_side_fires():
    assert rule_n6([-1.5, -1.5, -1.5, -1.5, 0.0], Z) is True   # 4점 −1σ 밖 (below 분기)


# ── 결정 A 조합②: 분위수 zones (헌법 1-1 예외 2) ──────────────────────────
def test_from_quantile_n1_uses_bounds_and_sigma_zones_nan():
    # 분위수 관리선 → s3=ucl/lcl(비대칭), σ-존(s1/s2)은 NaN(미사용 — N5~N8 제외 대상).
    zq = Zones.from_quantile(center=0.0, lcl=-2.0, ucl=5.0)
    assert zq.s3p == 5.0 and zq.s3m == -2.0
    assert rule_n1([6.0], zq) is True and rule_n1([-3.0], zq) is True   # 상/하한 밖
    assert rule_n1([4.0], zq) is False                                  # 밴드 안(±3σ면 밖일 값)
    # σ-존 NaN → σ-존 룰은 항상 False (방어: 혹시 평가돼도 헛발동 없음)
    assert rule_n5([2.5, 2.5, 2.5], zq) is False
    assert rule_n7([0.5] * 15, zq) is False
