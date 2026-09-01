"""B5-4 recipe_engine 단위 테스트 (스펙 v2.2 §5).

축소 스코프(D11): C4 축 "방향만" + 고정 정책 스텝. 나머지 축 escalate_only.
compute_tuning은 주입 cfg만 소비하는 순수 함수(D1·D7) — 아티팩트 파일 무의존.
"""
from __future__ import annotations

import copy

import pytest

from src.agent_b_spc.recipe_engine import (
    RecipeTuningConfig,
    TuningProposal,
    _validate_knob_map,
    compute_tuning,
    to_tuning_dict,
)

# ── 픽스처 (knob_map = v2.2 §3-1 축약) ──────────────────────────────────
KNOB_MAP = {
    "nominal_setpoints": {
        "C6_0": {"C4": 40.0, "C1": 118.0, "C5": 71.0, "C41": 10.2316},
        "C6_1": {"C4": 60.0, "C1": 119.0, "C5": 50.0, "C41": 14.1919},
    },
    "axes": {
        "gas": {"status": "active", "knobs": ["C4"], "monitors": ["C16", "C15"],
                "direction_sign": 1},
        "temp": {"status": "active", "knobs": ["temp_target"], "monitors": ["C17"],
                 "mode": "target"},
        "power": {"status": "escalate_only", "knobs": ["C1"], "monitors": [],
                  "reason": "측정 짝 부재(앵커 1개·rank 1) — 자동 귀속 불가, 사람 판단 필요"},
        "gas_b": {"status": "escalate_only", "knobs": ["C5"], "monitors": [],
                  "reason": "측정 짝 부재"},
        "time": {"status": "escalate_only", "knobs": ["C41"], "monitors": [],
                 "reason": "setpoint 아님"},
        "gas_main": {"status": "escalate_only", "knobs": ["C48"], "monitors": [],
                     "reason": "사실상 불변(정보 0)"},
    },
    "forbidden": ["C12", "C17", "C31", "C65"],
}


def _cfg(*, delta_max_pct=3.0, step_pct=1.0, knob_map=None):
    return RecipeTuningConfig(delta_max_pct=delta_max_pct, step_pct=step_pct,
                             knob_map=knob_map if knob_map is not None else KNOB_MAP)


def _viol(sensor, *, current, ucl, lcl, rule_id="N3", severity="WARNING"):
    """fdc.alert violations[] 1건 (계약 §3 키)."""
    return {"sensor": sensor, "rule_id": rule_id, "severity": severity,
            "current_value": current, "control_limit_upper": ucl, "control_limit_lower": lcl}


def _run(violations, shap, *, recipe_id="C6_0", applied=None, temp_tuned=False, cfg=None):
    return compute_tuning(violations=violations, shap_top3=shap, chamber_id="SIM_CH_1",
                          recipe_id=recipe_id, applied_setpoints=applied,
                          temp_tuned_this_incident=temp_tuned,
                          cfg=cfg if cfg is not None else _cfg())


# ── 선정 ────────────────────────────────────────────────────────────────
def test_no_violation_returns_none():
    assert _run([], ["C15"]) is None


def test_b9_marker_only_returns_none():
    v = _viol("C65", current=1600, ucl=1572, lcl=0, rule_id="B9")
    assert _run([v], ["C65"]) is None


def test_shap_string_order_preserved_no_reorder():
    """shap 문자열 리스트 — 주어진 순서 그대로(재정렬 금지, §1-5). 첫 매칭 우선."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C5", "C15"])   # C5=gas_b(escalate_only)가 먼저 → escalate (순서 존중)
    assert p.knob_axis == "gas_b" and p.escalate_reason is not None


def test_forbidden_c12_regime_escalates():
    v = _viol("C12", current=5.0, ucl=4.0, lcl=2.0)
    p = _run([v], ["C12"])
    assert p.parameter_id is None and p.escalate_reason is not None


def test_unmapped_pressure_escalates():
    v = _viol("C50", current=5.0, ucl=4.0, lcl=2.0)
    p = _run([v], ["C50"])
    assert p.parameter_id is None and p.escalate_reason is not None


def test_c11_unpaired_escalates_generic():
    """C11 파워축 monitor 해제(A, 2026-08-01) — 어느 축에도 매칭 안 됨 → 일반 escalate.

    파워축은 원래 escalate_only라 판정(에스컬)은 불변. 바뀐 건 사유 경로뿐: power 축의
    인과 주장 사유 대신, 축 미매칭의 인과 없는 일반 사유로 빠진다(knob_axis=None).
    """
    v = _viol("C11", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C11"])
    assert p.parameter_id is None and p.knob_axis is None
    assert p.escalate_reason is not None


# ── C15·C16 동축 — 대표 치환 금지 ────────────────────────────────────────
@pytest.mark.parametrize("sensor", ["C15", "C16"])
def test_gas_monitors_map_to_c4_basis_verbatim(sensor):
    """C15/C16 어느 쪽이 떠도 gas·knob=C4·1건, basis_sensor는 SHAP 지목 그대로(3-3)."""
    v = _viol(sensor, current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], [sensor])
    assert p.knob_axis == "gas" and p.parameter_id == "C4"
    assert p.basis_sensor == sensor          # 대표(C4/C16) 치환 금지


def test_both_gas_monitors_single_proposal():
    """C15·C16 동시 위반 — 첫 매칭에서 끊겨 제안 1건, basis=첫 shap."""
    vs = [_viol("C16", current=1700, ucl=1600, lcl=1550),
          _viol("C15", current=30.0, ucl=29.0, lcl=27.0)]
    p = _run(vs, ["C15", "C16"])
    assert p.parameter_id == "C4" and p.basis_sensor == "C15"


# ── escalate_only ───────────────────────────────────────────────────────
@pytest.mark.parametrize("sensor,axis", [("C1", "power"), ("C5", "gas_b"),
                                          ("C41", "time"), ("C48", "gas_main")])
def test_escalate_only_axes(sensor, axis):
    """C1(power)·C5·C41·C48 축 → 수치 없이 escalate + 맵 reason 인용.

    power는 C11 monitor 해제(A) 후 knob(C1)으로만 매칭 — C11은 별도 test_c11_unpaired_escalates_generic.
    """
    v = _viol(sensor, current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], [sensor])
    assert p.knob_axis == axis and p.parameter_id is None
    assert p.escalate_reason and KNOB_MAP["axes"][axis]["reason"] in p.escalate_reason


# ── 방향 (D14) ──────────────────────────────────────────────────────────
def test_direction_upper_exceed_down():
    """UCL 초과(dev+) × direction_sign+1 → down."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"])
    assert p.direction == "down"


def test_direction_lower_exceed_up():
    """LCL 미만(dev−) → up."""
    v = _viol("C15", current=26.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"])
    assert p.direction == "up"


def test_direction_within_band_pattern_uses_midpoint():
    """밴드 내 패턴 룰(N3) — 중점 대비 부호. current>중점(28)이면 down."""
    v = _viol("C15", current=28.5, ucl=29.0, lcl=27.0, rule_id="N3")  # 중점 28.0
    p = _run([v], ["C15"])
    assert p.direction == "down"


def test_direction_exact_midpoint_escalates():
    """current == 중점 → 방향 불명 escalate."""
    v = _viol("C15", current=28.0, ucl=29.0, lcl=27.0, rule_id="N3")
    p = _run([v], ["C15"])
    assert p.escalate_reason is not None and p.delta_pct is None


def test_degenerate_band_escalates():
    v = _viol("C15", current=30.0, ucl=27.0, lcl=27.0)
    p = _run([v], ["C15"])
    assert p.escalate_reason is not None and p.delta_pct is None


# ── 크기 (정책 고정) ────────────────────────────────────────────────────
def test_size_from_step_pct_not_gain():
    """delta_pct = ±recipe_step_pct 유래(감도 무관). down → -1%, 4자리."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"])
    assert p.value_current == 40.0                       # nominal C6_0 C4
    assert p.value_proposed == pytest.approx(40.0 * 0.99, abs=1e-9)   # 39.6
    assert p.delta_pct == pytest.approx(-1.0, abs=1e-9)


def test_missing_nominal_key_escalates():
    """knob 명목값 부재 → 명시 실패 escalate (6-1)."""
    km = {k: v for k, v in KNOB_MAP.items()}
    km["nominal_setpoints"] = {"C6_0": {"C1": 118.0}}     # C4 없음
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"], cfg=_cfg(knob_map=km))
    assert p.escalate_reason is not None and p.value_proposed is None


# ── 반복 조정 (D10) ─────────────────────────────────────────────────────
def test_applied_setpoints_advances_not_reverses():
    """applied 주입 시 base=applied로 전진(역주행 방지). value_current은 항상 nominal."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)   # down
    p = _run([v], ["C15"], applied={"C4": 39.6})
    assert p.value_current == 40.0                        # nominal 유지(시뮬 앵커)
    assert p.value_proposed == pytest.approx(39.6 * 0.99, abs=1e-9)   # 39.204 전진


def test_applied_value_field_carries_current_applied():
    """일반 knob: 제안에 applied_value(직전 적용값) 포함 — 승인 화면 '현재값' 정확화(C ⓐ).

    value_current은 명목(누적 기준)이라 실제 장비값(applied)과 다를 수 있어, applied 를 함께 실어
    리포트가 '현재 39.6(명목 40.0 대비 누적 −1.0%) → …'로 정확히 서술하게 한다.
    """
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"], applied={"C4": 39.6})
    assert p.applied_value == 39.6
    assert p.value_current == 40.0                        # 명목(누적 기준)은 유지


def test_applied_value_none_on_first_step():
    """applied 이력 없으면 applied_value None (첫 스텝 = 명목이 곧 현재)."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"], applied=None)
    assert p.applied_value is None


def test_applied_value_in_tuning_dict():
    """to_tuning_dict 에 applied_value 포함 (C 리포트가 소비)."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    d = to_tuning_dict(_run([v], ["C15"], applied={"C4": 39.6}))
    assert d["applied_value"] == 39.6


def test_applied_zero_does_not_fallback_to_nominal():
    """applied=0.0이 nominal로 폴백하면 역주행 재발 — `is None` 검사(or 함정 회귀)."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"], applied={"C4": 0.0})
    # base=0.0 사용 시 delta=(0-40)/40=-100% → 예산 초과 escalate. nominal 폴백이면 39.6 정상 제안.
    assert p.escalate_reason is not None and p.value_proposed is None


def test_applied_none_seeds_nominal():
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"], applied=None)
    assert p.value_proposed == pytest.approx(39.6, abs=1e-9)


def test_cumulative_budget_exhaustion_escalates():
    """명목 대비 누적 |delta| > D6(3%) → 수치 없이 escalate (≈3스텝 후)."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)   # down
    p = _run([v], ["C15"], applied={"C4": 38.0})          # 38→37.62, (37.62-40)/40=-5.95% > 3
    assert p.escalate_reason is not None and p.value_proposed is None


# ── P0 가드 ─────────────────────────────────────────────────────────────
def test_recipe_id_none_escalates():
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"], recipe_id=None)
    assert p.escalate_reason is not None and p.value_proposed is None


def test_nominal_zero_escalates():
    km = {k: v for k, v in KNOB_MAP.items()}
    km["nominal_setpoints"] = {"C6_0": {"C4": 0.0}}
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"], cfg=_cfg(knob_map=km))
    assert p.escalate_reason is not None and p.value_proposed is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nonfinite_current_escalates_no_numbers(bad):
    """NaN/Inf current → 수치 없이 escalate (무raise만으론 불충분, 7장)."""
    v = _viol("C15", current=bad, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"])
    assert p.escalate_reason is not None and p.value_proposed is None and p.delta_pct is None


def test_non_dict_violation_ignored_no_raise():
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run(["not a dict", v], ["C15"])
    assert p.knob_axis == "gas"


class _FakeViolation:
    """Pydantic Violation 미러 — model_dump()만 (C가 객체를 그대로 넘겨도 강건, seam 방어)."""

    def __init__(self, d):
        self._d = d

    def model_dump(self, *a, **k):
        return dict(self._d)


def test_pydantic_violation_object_coerced_no_silent_none():
    """C가 Violation 객체를 model_dump 없이 넘겨도 dict로 강제 → 조용한 None 아님."""
    v = _FakeViolation(_viol("C15", current=30.0, ucl=29.0, lcl=27.0))
    p = _run([v], ["C15"])
    assert p is not None and p.knob_axis == "gas" and p.parameter_id == "C4"


# ── 급변 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kw", [{"rule_id": "N1"}, {"severity": "CRITICAL"}])
def test_abrupt_change_escalates(kw):
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0, **kw)
    p = _run([v], ["C15"])
    assert p.escalate_reason is not None and p.value_proposed is None


# ── temp_target (D8) ────────────────────────────────────────────────────
def test_temp_target_value_pair_present():
    """C17(temp monitor·forbidden이지만 basis 허용) → temp_target 절대값 쌍."""
    v = _viol("C17", current=180.0, ucl=179.0, lcl=176.0)
    p = _run([v], ["C17"])
    assert p.parameter_id == "temp_target" and p.knob_axis == "temp"
    assert p.value_current is not None and p.value_proposed is not None
    assert abs(p.delta_pct) <= 3.0


def test_temp_target_first_step_proposes():
    """온도 첫 스텝(이 Incident에서 온도 튜닝 이력 없음) → temp_target 제안 (A — 1스텝 허용)."""
    v = _viol("C17", current=180.0, ucl=179.0, lcl=176.0)
    p = _run([v], ["C17"], temp_tuned=False)
    assert p.parameter_id == "temp_target" and p.value_proposed is not None


def test_temp_target_repeat_in_incident_escalates():
    """같은 Incident에서 온도 반복(temp_tuned_this_incident=True) → 에스컬 (A·C ⓑ).

    온도는 명목 앵커가 없어(setpoint 부재·C17 측정값 std~42) 누적 상한을 못 잰다 → 한 Incident에
    1스텝만 자동 제안하고, 같은 사안 반복이면 사람에게 넘긴다(에스컬).
    """
    v = _viol("C17", current=180.0, ucl=179.0, lcl=176.0)
    p = _run([v], ["C17"], temp_tuned=True)
    assert p.escalate_reason is not None
    assert p.value_proposed is None and p.delta_pct is None


def test_temp_target_new_incident_re_proposes():
    """새 Incident(temp_tuned_this_incident=False)면 과거 적용 이력이 있어도 다시 1스텝 제안 (A — Incident 단위 리셋).

    챔버 평생 1회용(B)이 아니라, 사안이 바뀌면(새 Incident) 다시 보수 스텝을 낸다.
    """
    v = _viol("C17", current=180.0, ucl=179.0, lcl=176.0)
    p = _run([v], ["C17"], applied={"temp_target": 178.2}, temp_tuned=False)   # 과거 적용 O, 이번 Incident는 처음
    assert p.parameter_id == "temp_target" and p.value_proposed is not None


# ── sensitivity 재정의 ──────────────────────────────────────────────────
def test_sensitivity_reports_gain_unavailable_no_provisional():
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"])
    assert p.sensitivity["gain_available"] is False
    assert p.sensitivity.get("gain_reason")
    assert "_provisional" not in p.sensitivity


# ── D15 기동 불변식 (load 시 1회 · 맵 오편집 방어) ───────────────────────
def test_d15_valid_map_passes():
    """정상 맵(픽스처)은 통과."""
    _validate_knob_map(KNOB_MAP)     # no raise


def test_d15_knob_in_forbidden_raises():
    """① knobs ∩ forbidden ≠ ∅ (예: power.knobs:[C31]) → RuntimeError(기동 차단)."""
    km = copy.deepcopy(KNOB_MAP)
    km["axes"]["power"]["knobs"] = ["C31"]   # C31 ∈ forbidden
    with pytest.raises(RuntimeError):
        _validate_knob_map(km)


def test_d15_active_axis_empty_monitors_raises():
    """② active 축인데 monitors 비면 → RuntimeError(도달 불가 축)."""
    km = copy.deepcopy(KNOB_MAP)
    km["axes"]["gas"]["monitors"] = []
    with pytest.raises(RuntimeError):
        _validate_knob_map(km)


def test_d15_active_knob_missing_nominal_raises():
    """③ active knob이 nominal_setpoints에 없으면 → RuntimeError (target 모드 제외)."""
    km = copy.deepcopy(KNOB_MAP)
    km["nominal_setpoints"]["C6_0"].pop("C4")
    with pytest.raises(RuntimeError):
        _validate_knob_map(km)


def test_d15_target_mode_knob_exempt_from_nominal():
    """temp_target(target 모드)은 nominal_setpoints 부재라도 통과(예외)."""
    km = copy.deepcopy(KNOB_MAP)          # temp_target은 nominal에 없음 — 통과해야 함
    _validate_knob_map(km)                # no raise


def test_load_real_config_passes_invariants():
    """실 params.yaml + recipe_knob_map.yaml 로드 — 불변식 통과·정책값 확인."""
    cfg = RecipeTuningConfig.load()
    assert cfg.step_pct == 1.0 and cfg.delta_max_pct == 3.0
    assert cfg.knob_map["axes"]["gas"]["status"] == "active"


# ── to_tuning_dict (C 어댑터 — proposal → dict) ─────────────────────────
def test_to_tuning_dict_none_returns_none():
    assert to_tuning_dict(None) is None


def test_to_tuning_dict_has_stub_fields_no_provisional():
    """dataclass → dict. 스텁 5필드 포함·`_provisional` 미탑재(실 API 도장 없음)."""
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    d = to_tuning_dict(_run([v], ["C15"]))
    for f in ("parameter_id", "value_current", "value_proposed", "delta_pct", "sensitivity"):
        assert f in d
    assert "_provisional" not in d
    assert d["value_proposed"] == pytest.approx(39.6, abs=1e-9)


# ── C 계약 왕복 ─────────────────────────────────────────────────────────
def test_proposal_superset_of_stub_fields():
    v = _viol("C15", current=30.0, ucl=29.0, lcl=27.0)
    p = _run([v], ["C15"])
    for f in ("parameter_id", "value_current", "value_proposed", "delta_pct", "sensitivity"):
        assert hasattr(p, f)
    # 4자리 반올림 (enforce_number_guard float 완전일치 대비)
    assert p.value_proposed == round(p.value_proposed, 4)
