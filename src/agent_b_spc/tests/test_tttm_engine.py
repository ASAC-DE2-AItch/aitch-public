"""TTTMEngine 상태·판정 테스트 (합성 데이터, Kafka·DB 불필요).

설계: src/agent_b_spc/specs/B4-2_TTTM엔진_설계.md (v5)
계획: src/agent_b_spc/plans/B4-2_TTTM엔진_플랜.md
"""
import logging
import math
from pathlib import Path

import pytest
import yaml

from src.agent_b_spc.tttm_engine import (
    TTTMEngine, EPS, MIN_FLEET_CHAMBERS, ALERT_FIELDS, load_tttm_config, _check_q_k)
from src.agent_b_spc.nelson_engine import Point

FK = ("C6_0", 4, "settled", "C17")          # fleet_key = (recipe, step, window, sensor)


def _gk(ch, fk=FK):
    """챔버 + fleet_key → group_key (chamber, recipe, step, window, sensor)."""
    return (ch,) + fk


def _lim(center=0.0, sigma=1.0, ucl=3.0, lcl=-3.0, method="sigma", ver="v1"):
    """control_limits_loader.load()가 내는 limits[gk] 6키 형태."""
    return {"center": center, "sigma": sigma, "ucl": ucl, "lcl": lcl,
            "method": method, "limit_version": ver}


def _pt(gk, value, wafer="W1", ts="2026-07-15T10:00:00.000Z", pm=1, ver="v1"):
    return Point(group_key=gk, wafer_id=wafer, timestamp=ts, value=value,
                 pm_count=pm, limit_version=ver)


def _engine(limits, whitelist, *, window=5, min_fill=5, warning=2.0, critical=3.0,
            reverse_min=3, k_sigma=3.0, excluded=frozenset()):
    """모든 config 값을 주입해 hermetic 구성 (params.yaml 미접근)."""
    return TTTMEngine(limits, whitelist, window=window, min_fill=min_fill,
                      warning=warning, critical=critical, reverse_min=reverse_min,
                      k_sigma=k_sigma, excluded=excluded)


def _feed_seq(eng, gk, values, base_min=0, pm=1, ver="v1"):
    """값 시퀀스를 시간 증가시키며 feed."""
    for i, v in enumerate(values):
        ts = f"2026-07-15T10:{base_min + i:02d}:00.000Z"
        eng.feed(_pt(gk, v, wafer=f"W{i}", ts=ts, pm=pm, ver=ver))


# ============================================================================
# Task 1 — feed 가드 · ts 드롭 · warmup · flush
# ============================================================================

def test_non_whitelisted_group_skipped():
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, set())          # 빈 화이트리스트
    assert eng.feed(_pt(gk, 1.0)) == []


def test_skip_logs_once_per_group(caplog):
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    other = _gk("SIM_CH_9", ("C6_0", 4, "settled", "C99"))   # 화이트리스트 밖
    with caplog.at_level(logging.INFO):
        assert eng.feed(_pt(other, 1.0)) == [] and eng.feed(_pt(other, 1.0)) == []
    hits = [r for r in caplog.records if "감시 대상 아님" in r.getMessage()]
    assert len(hits) == 1                        # 최초 1회만


def test_missing_limits_skipped():
    gk = _gk("SIM_CH_1")
    eng = _engine({}, {gk})                      # 화이트리스트엔 있으나 관리선 없음
    assert eng.feed(_pt(gk, 1.0)) == []


def test_excluded_chamber_skipped():
    gk = _gk("SIM_CH_3")
    eng = _engine({gk: _lim()}, {gk}, excluded={"SIM_CH_3"})
    assert eng.feed(_pt(gk, 1.0)) == []
    assert gk not in eng._state                  # 상태 미생성 (median 오염 방지)


def test_ts_reversal_drops_counted():
    """역전 드롭을 `stats`로 계측한다 (헌법 7장 — 로그 단일 신호는 인코딩·grep에 좌우됨).

    Nelson과 동일 계약(`stats["ts_reversal_drops"]`). 드롭 자체도 함께 확인한다.
    """
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    assert eng.stats["ts_reversal_drops"] == 0
    eng.feed(_pt(gk, 1.0, ts="2026-07-15T10:00:00.000Z"))
    assert eng.stats["ts_reversal_drops"] == 0                       # 정상 진행분은 안 센다
    assert eng.feed(_pt(gk, 2.0, ts="2026-07-15T09:00:00.000Z")) == []   # 역전 → 드롭
    assert eng.stats["ts_reversal_drops"] == 1


def test_nan_value_skipped_no_crash():
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    assert eng.feed(_pt(gk, math.nan)) == []


def test_none_value_skipped_no_crash():
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    assert eng.feed(_pt(gk, None)) == []


def test_degenerate_sigma_ref_skipped():
    # #2: 분위수 밴드 붕괴 ucl==lcl → σ_ref=0 → skip (ZeroDivision·spurious critical 방지)
    gk = _gk("SIM_CH_1")
    lim = _lim(method="quantile", ucl=1.0, lcl=1.0, sigma=None)
    eng = _engine({gk: lim}, {gk})
    assert eng.feed(_pt(gk, 5.0)) == []
    assert gk not in eng._state


def test_first_wafer_no_crash():
    # #2: last_pm_count None에서 flush 조건 평가 크래시 없음
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    assert eng.feed(_pt(gk, 0.0)) == []
    assert len(eng._state[gk].window) == 1


def test_out_of_order_timestamp_dropped():
    # #5: 엄격 과거 드롭 (Nelson 규약)
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    eng.feed(_pt(gk, 0.0, ts="2026-07-15T10:05:00.000Z"))
    eng.feed(_pt(gk, 9.0, ts="2026-07-15T10:04:00.000Z"))    # 더 과거 → 드롭
    assert len(eng._state[gk].window) == 1


def test_malformed_timestamp_skipped():
    # #5: 파싱 실패 skip
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    assert eng.feed(_pt(gk, 1.0, ts="not-a-ts")) == []
    assert gk not in eng._state


def test_warmup_no_center_until_min_fill():
    # 3-B: window < min_fill 동안 center_live 미확정
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk}, window=5, min_fill=5)
    _feed_seq(eng, gk, [0.1, 0.2, 0.3, 0.4])     # 4 < 5
    assert eng._state[gk].center_live is None
    eng.feed(_pt(gk, 0.5, wafer="W4", ts="2026-07-15T10:04:00.000Z"))
    assert eng._state[gk].center_live is not None


def test_window_median_absorbs_spike():
    # 3-A: window median → 단일 크레이지 wafer에 안 흔들림
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk}, window=5, min_fill=5)
    _feed_seq(eng, gk, [0.0, 0.0, 0.0, 0.0, 0.0])
    assert eng._state[gk].center_live == 0.0
    eng.feed(_pt(gk, 100.0, wafer="W5", ts="2026-07-15T10:05:00.000Z"))
    assert eng._state[gk].center_live == 0.0     # median 흡수


def test_regime_flush_on_pm_increment():
    # 3-C: pm_count 증가 → window flush + 재충전 후 판정 재개
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk}, window=5, min_fill=3)
    _feed_seq(eng, gk, [1.0, 1.0, 1.0])
    assert eng._state[gk].center_live == 1.0
    eng.feed(_pt(gk, 5.0, wafer="Wpm", ts="2026-07-15T10:10:00.000Z", pm=2))
    assert len(eng._state[gk].window) == 1       # 비워지고 새 점 1개
    _feed_seq(eng, gk, [5.0, 5.0], base_min=11, pm=2)
    assert eng._state[gk].center_live == 5.0     # 새 레짐 중심으로 재개


def test_regime_flush_on_limit_version_change():
    # 3-C: limit_version 변경도 flush
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk}, window=5, min_fill=3)
    _feed_seq(eng, gk, [1.0, 1.0, 1.0], ver="v1")
    eng.feed(_pt(gk, 5.0, wafer="Wv2", ts="2026-07-15T10:10:00.000Z", ver="v2"))
    assert len(eng._state[gk].window) == 1


def test_module_constants():
    # 6-1: 명명 상수
    assert EPS == 1e-9
    assert MIN_FLEET_CHAMBERS == 2
    assert "reference_id" not in ALERT_FIELDS    # #7: DB 전용
    assert ALERT_FIELDS == frozenset(
        {"reference", "score", "top_gap_sensor", "gap_pct", "reference_suspect",
         "suspect_sensors"})                     # 2026-08-05 decouple: suspect_sensors 추가


# ============================================================================
# Task 2 — fleet median + score (개별 outlier)
# ============================================================================

CHAMBERS = [f"SIM_CH_{i}" for i in range(1, 5)]     # 4 가상 챔버 (6→4 축소, 멘토 2026-07-19)


def _warm_ch(eng, ch, value, *, fk=FK, n=5, base_min=0):
    """챔버를 min_fill만큼 동일 값으로 충전해 center_live=value·warm 상태로 만든다."""
    gk = _gk(ch, fk)
    for i in range(n):
        ts = f"2026-07-15T10:{base_min + i:02d}:00.000Z"
        eng.feed(_pt(gk, value, wafer=f"{ch}_W{i}", ts=ts))


def _fleet_engine(offsets, *, lim_fn=_lim, fk=FK, window=5, min_fill=5, n=5):
    """offsets={chamber: value} → 각 챔버를 그 값으로 warm. (limits·whitelist는 전 챔버.)"""
    limits = {_gk(c, fk): lim_fn() for c in offsets}
    eng = _engine(limits, set(limits), window=window, min_fill=min_fill)
    for c, v in offsets.items():
        _warm_ch(eng, c, v, fk=fk, n=n)
    return eng


def test_score_single_offset_chamber():
    # CH1만 +2σ, 나머지 5대 정상 → fleet median≈0, CH1 score=2.0
    eng = _fleet_engine({c: (2.0 if c == "SIM_CH_1" else 0.0) for c in CHAMBERS})
    out = eng.chamber_rollup("SIM_CH_1")
    assert out["score"] == 2.0
    assert out["top_gap_sensor"] == "C17"
    assert out["reference"] == "fleet_median"


def test_gap_pct_sign_negative_median():
    # #6: 음수 median 센서에서 center(-8) > median(-10) → gap_pct 양수(/|median|)
    eng = _fleet_engine({c: (-8.0 if c == "SIM_CH_1" else -10.0) for c in CHAMBERS})
    out = eng.chamber_rollup("SIM_CH_1")
    assert out["gap_pct"] > 0
    assert abs(out["gap_pct"] - 20.0) < 1e-9        # (2)/10*100


def test_rollup_none_when_insufficient_fleet():
    # fleet 챔버 1대뿐 → fleet_median 미산출 → rollup None
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    _warm_ch(eng, "SIM_CH_1", 1.0)
    assert eng.chamber_rollup("SIM_CH_1") is None


def test_rollup_none_when_not_warm():
    # #4 가드: warm 아님 → cand 없음 → None (크래시 없음)
    gk = _gk("SIM_CH_1")
    eng = _engine({gk: _lim()}, {gk})
    eng.feed(_pt(gk, 1.0))                           # 1점, warmup 미달
    assert eng.chamber_rollup("SIM_CH_1") is None


def test_quantile_sigma_ref_in_score():
    # KEEP* 분위수: σ_ref=(ucl−lcl)/(2k)=(6−(−6))/6=2. CH1 offset 4 → gap_σ=2
    fk = ("C6_0", 1, "settled", "C61")
    lim_q = lambda: _lim(method="quantile", ucl=6.0, lcl=-6.0, sigma=None)
    eng = _fleet_engine({c: (4.0 if c == "SIM_CH_1" else 0.0) for c in CHAMBERS},
                        lim_fn=lim_q, fk=fk, window=3, min_fill=3, n=3)
    out = eng.chamber_rollup("SIM_CH_1")
    assert abs(out["score"] - 2.0) < 1e-9


def test_rollup_field_set():
    # #11: 롤업 필드 = ALERT_FIELDS ∪ {reference_id, chamber_id}, reference_id∉ALERT
    eng = _fleet_engine({c: (1.0 if c == "SIM_CH_1" else 0.0) for c in CHAMBERS})
    out = eng.chamber_rollup("SIM_CH_1")
    assert set(out) == ALERT_FIELDS | {"reference_id", "chamber_id"}
    assert out["reference_id"] == "v1"
    assert out["chamber_id"] == "SIM_CH_1"
    assert out["reference_suspect"] is False         # 공통이동 없음 → suspect 비어있음
    assert out["suspect_sensors"] == []              # decouple: 발동 센서 없음


def test_gap_pct_null_when_median_zero():
    # |fleet_median|≤EPS → gap_pct NULL, score는 정상(σ-등가)
    vals = {"SIM_CH_1": 1.0, "SIM_CH_2": 1.0, "SIM_CH_3": -1.0, "SIM_CH_4": -1.0}
    eng = _fleet_engine(vals)
    out = eng.chamber_rollup("SIM_CH_1")
    assert out["gap_pct"] is None
    assert out["score"] is not None


def test_excluded_chamber_out_of_fleet_median():
    # pin ⑥: 제외 챔버는 fleet median 표본에서 빠짐
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    eng.set_excluded({"SIM_CH_4"})
    for c in CHAMBERS:
        _warm_ch(eng, c, 0.0 if c != "SIM_CH_4" else 99.0)   # CH4 극단이지만 제외
    out = eng.chamber_rollup("SIM_CH_1")
    # CH4(99) 제외라 median 오염 없음 → CH1(0) score≈0
    assert out is not None
    assert out["score"] < 1e-9


# ============================================================================
# Task 3 — 역방향 룰 (집단 이동)
# ============================================================================

def _fire_setup(eng, *, drift_val=3.0, drift=("SIM_CH_1", "SIM_CH_2", "SIM_CH_3"),
                stable=("SIM_CH_4",)):
    """안정 챔버 먼저 warm(0.0), 그다음 drift 챔버 warm(drift_val) — 마지막 feed에 전원 warm."""
    for c in stable:
        _warm_ch(eng, c, 0.0)
    for c in drift:
        _warm_ch(eng, c, drift_val)


def test_reverse_rule_fires_3_of_4():
    # 3/4 챔버 C17 동일방향 >warning → fk suspect=true (과반, reverse_min=3)
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    _fire_setup(eng)
    assert FK in eng._suspect_sensors


def test_reverse_rule_release_no_latch():
    # #1: 발동 후 1챔버 복귀 → up=2(<3) → discard (래칭 없음)
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    _fire_setup(eng)
    assert FK in eng._suspect_sensors
    _warm_ch(eng, "SIM_CH_3", 0.0, base_min=10)      # CH3 복귀 → up=2
    assert FK not in eng._suspect_sensors


def test_reference_suspect_flags_innocent_chamber():
    # #1: 발동 시 끌려간 무고한 안정 챔버가 outlier + reference_suspect=true (fleet_key 튜플 매칭)
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    _fire_setup(eng)
    out = eng.chamber_rollup("SIM_CH_4")             # 안정 챔버 (유일)
    assert out["reference_suspect"] is True
    assert out["score"] > 2.0                        # 끌려간 median 대비 outlier


def test_reverse_rule_direction_split():
    # 2 up + 2 down (각 <3) → suspect false
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    _warm_ch(eng, "SIM_CH_1", 3.0)
    _warm_ch(eng, "SIM_CH_2", 3.0)
    _warm_ch(eng, "SIM_CH_3", -3.0)
    _warm_ch(eng, "SIM_CH_4", -3.0)
    assert FK not in eng._suspect_sensors             # max(2,2)=2 < 3


def test_reverse_rule_skips_degenerate_sigma_chamber():
    # #2: σ_ref=0 챔버는 feed·members에서 제외 → 크래시 없이 나머지 3 up으로 발동
    limits = {_gk(c): _lim() for c in CHAMBERS}
    limits[_gk("SIM_CH_4")] = _lim(method="quantile", ucl=1.0, lcl=1.0, sigma=None)
    eng = _engine(limits, set(limits))
    for c in ["SIM_CH_1", "SIM_CH_2", "SIM_CH_3"]:
        _warm_ch(eng, c, 3.0)
    _warm_ch(eng, "SIM_CH_4", 5.0)                    # σ_ref=0 → feed skip
    assert FK in eng._suspect_sensors                 # CH4 제외해도 3 up


def test_reverse_rule_discard_when_fleet_below_min():
    # #10: 발동 후 fleet 표본이 MIN 미만으로 떨어지면 discard
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    _fire_setup(eng)
    assert FK in eng._suspect_sensors
    eng.set_excluded({"SIM_CH_1", "SIM_CH_2", "SIM_CH_3"})
    _warm_ch(eng, "SIM_CH_4", 0.0, base_min=20)       # members={CH4}<2 → discard
    assert FK not in eng._suspect_sensors


# ============================================================================
# Task 3b — reference_suspect decouple (2026-08-05, 경로① worst-게이팅 분리)
# ============================================================================

def _two_group_engine():
    """C17(3챔버 공통이동·역방향 발동) + C62(단일챔버 큰 offset=worst·미발동) 2그룹 4챔버 엔진.

    decouple 분간용: CH1의 worst 센서는 C62(fleet gap 최대·단일이라 역방향 미발동)이나
    실제 공통이동은 C17(3챔버 동반)이다. 구 게이팅(worst∈suspect)이면 CH1 reference_suspect가
    C62 기준 False로 masking, decouple(any 그룹)이면 C17 기준 True.
    """
    fk_c17 = ("C6_0", 4, "settled", "C17")
    fk_c62 = ("C6_0", 4, "settled", "C62")
    limits = {}
    for c in CHAMBERS:
        limits[_gk(c, fk_c17)] = _lim()
        limits[_gk(c, fk_c62)] = _lim()
    eng = _engine(limits, set(limits))
    for c in ("SIM_CH_1", "SIM_CH_2", "SIM_CH_3"):    # C17 공통이동 +4σ (3/4 → 역방향 발동)
        _warm_ch(eng, c, 4.0, fk=fk_c17)
    _warm_ch(eng, "SIM_CH_4", 0.0, fk=fk_c17)
    _warm_ch(eng, "SIM_CH_1", 5.0, fk=fk_c62)         # C62는 CH1만 +5σ(worst), 나머지 0 → 단일·미발동
    for c in ("SIM_CH_2", "SIM_CH_3", "SIM_CH_4"):
        _warm_ch(eng, c, 0.0, fk=fk_c62)
    return eng, fk_c17, fk_c62


def test_decouple_reference_suspect_off_worst_gating():
    # 핵심: worst=C62(역방향 미발동)여도 실제 공통이동 C17로 reference_suspect=True(masking 해소)
    eng, fk_c17, fk_c62 = _two_group_engine()
    assert fk_c17 in eng._suspect_sensors             # C17 공통이동 발동
    assert fk_c62 not in eng._suspect_sensors          # C62 단일 → 미발동
    out = eng.chamber_rollup("SIM_CH_1")
    assert out["top_gap_sensor"] == "C62"              # worst는 여전히 C62(경로② 진단용 — 유지)
    assert out["reference_suspect"] is True             # 구 게이팅이면 False(masking)였음
    assert out["suspect_sensors"] == ["C17"]            # 실제 공통이동 센서만


def test_decouple_clean_no_common_shift():
    # 순정상(공통이동 없음) → reference_suspect=False·suspect_sensors=[]
    eng = _fleet_engine({c: (1.0 if c == "SIM_CH_1" else 0.0) for c in CHAMBERS})
    out = eng.chamber_rollup("SIM_CH_1")
    assert out["reference_suspect"] is False
    assert out["suspect_sensors"] == []


def _dup_sensor_engine():
    """C17이 step4·step5 두 그룹에서 각각 3챔버 공통이동 → 둘 다 역방향 발동.

    gk[-1]='C17'이 cand에 두 번 들어와, dedup 전이면 suspect_sensors=['C17','C17'](중복),
    set이면 ['C17']. step을 안 싣는 챔버 요약이라 중복은 표현 불가·노이즈(PR #113 C 리뷰).
    """
    fk_s4 = ("C6_0", 4, "settled", "C17")
    fk_s5 = ("C6_0", 5, "settled", "C17")
    limits = {}
    for c in CHAMBERS:
        limits[_gk(c, fk_s4)] = _lim()
        limits[_gk(c, fk_s5)] = _lim()
    eng = _engine(limits, set(limits))
    for c in ("SIM_CH_1", "SIM_CH_2", "SIM_CH_3"):       # 두 그룹 다 +4σ 공통이동 (3/4 발동)
        _warm_ch(eng, c, 4.0, fk=fk_s4)
        _warm_ch(eng, c, 4.0, fk=fk_s5)
    _warm_ch(eng, "SIM_CH_4", 0.0, fk=fk_s4)
    _warm_ch(eng, "SIM_CH_4", 0.0, fk=fk_s5)
    return eng, fk_s4, fk_s5


def test_decouple_suspect_sensors_deduped():
    # 같은 센서가 여러 step 그룹에서 발동해도 suspect_sensors엔 한 번만 (dedup — PR #113 C 리뷰)
    eng, fk_s4, fk_s5 = _dup_sensor_engine()
    assert fk_s4 in eng._suspect_sensors and fk_s5 in eng._suspect_sensors   # 두 그룹 다 발동
    out = eng.chamber_rollup("SIM_CH_1")
    assert out["reference_suspect"] is True
    assert out["suspect_sensors"] == ["C17"]            # ['C17','C17'] 아님 (set dedup)


# ============================================================================
# Task 4 — config 통합 (2키 로드 · 주입 우선 · Φ⁻¹≈k assert)
# ============================================================================

def test_config_loads_new_keys_from_params():
    # 전 인자 None → params.yaml 로드 (tttm_window_wafers=20 / tttm_window_min_fill=10)
    gk = _gk("SIM_CH_1")
    eng = TTTMEngine({gk: _lim()}, {gk})
    assert eng.window == 20
    assert eng.min_fill == 10
    assert eng.k_sigma == 3.0                          # limit_engine.control_limit_k_sigma 재사용


def test_config_injection_overrides():
    gk = _gk("SIM_CH_1")
    eng = TTTMEngine({gk: _lim()}, {gk}, window=7, min_fill=3, warning=1.5,
                     critical=2.5, reverse_min=5, k_sigma=2.0)
    assert (eng.window, eng.min_fill, eng.warning, eng.critical,
            eng.reverse_min, eng.k_sigma) == (7, 3, 1.5, 2.5, 5, 2.0)


def test_load_tttm_config_returns_all_keys():
    cfg = load_tttm_config()
    assert set(cfg) == {"window", "min_fill", "warning", "critical", "reverse_min", "k_sigma"}


def test_quantile_k_consistency_ok():
    # #12: q_high=0.99865의 Φ⁻¹ ≈ 3.0 = k_sigma → 통과 (σ_ref=(ucl−lcl)/2k 역산 유효)
    _check_q_k(0.99865, 3.0)                            # raise 없음


def test_quantile_k_consistency_raises():
    # #12: q_high는 3σ인데 k=2.5로 어긋나면 σ_ref가 조용히 틀어짐 → 즉시 실패
    with pytest.raises(ValueError):
        _check_q_k(0.99865, 2.5)


# ============================================================================
# Task 5 — DoD 시나리오 통합테스트 (multi_chamber_common_c17)
# ============================================================================

SCENARIO = (Path(__file__).resolve().parents[2] / "simulator" / "scenarios"
            / "multi_chamber_common_c17.yaml")
ALL_CHAMBERS = [f"SIM_CH_{i}" for i in range(1, 5)]   # 4 챔버 (6→4)


def _scenario_stream(sc, fk, *, total, sigma=1.0):
    """주입 사양(target_chambers·start·magnitude)만으로 6챔버 Point 스트림을 합성 (fk는 호출자 지정).

    ⚠️ `ground_truth`(정답)는 조회하지 않는다 (절대규칙 5). start 전 전원 baseline(0),
    이후 target 챔버만 +magnitude·σ 계단 이동, 안정 챔버는 baseline 유지.
    """
    targets = set(sc["target_chambers"])
    start = sc["start_after_wafers"]
    mag = sc["magnitude_sigma"]
    stream = []
    for i in range(total):
        ts = f"2026-07-15T{10 + i // 60:02d}:{i % 60:02d}:00.000Z"
        for c in ALL_CHAMBERS:
            gk = (c,) + fk
            v = (mag * sigma) if (i >= start and c in targets) else 0.0
            stream.append((gk, _pt(gk, v, wafer=f"{c}_W{i}", ts=ts)))
    return targets, stream


def test_dod_multi_chamber_common_c17():
    # PM 4챔버 시나리오(target 3개 [CH1,CH2,CH4]·역방향 ≥3/4)로 재검증 — 6→4 축소 반영.
    # reverse_rule_min_chambers=3(params) + 4챔버 → 안정 챔버는 CH3 1개(과반이 이탈).
    sc = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    stable = [c for c in ALL_CHAMBERS if c not in set(sc["target_chambers"])]
    assert len(stable) >= 1                            # #6 전제 (4챔버: 안정 챔버 CH_3 1개)

    fk = ("C6_0", 4, "settled", sc["sensor"])
    limits = {(c,) + fk: _lim(center=0.0, sigma=1.0) for c in ALL_CHAMBERS}
    eng = _engine(limits, set(limits), window=20, min_fill=10)   # config 등가 주입 (hermetic)

    total = sc["start_after_wafers"] + 30              # 150 + window(20) + margin ≥ 180
    targets, stream = _scenario_stream(sc, fk, total=total)

    for idx, (_, pt) in enumerate(stream):
        out = eng.feed(pt)
        if idx < len(ALL_CHAMBERS) * 5:                # 초반 5 wafer(warmup) 판정 없음
            assert out == []

    # 2) post-shift 완충 후 → C17 fk suspect=true
    assert fk in eng._suspect_sensors

    # 3) reference_suspect=true는 *안정* 챔버(CH3)에 (target 3대는 오염 median과 gap≈0)
    for c in stable:
        r = eng.chamber_rollup(c)
        assert r["reference_suspect"] is True
        assert r["score"] > 2.0                        # 끌려간 median 대비 outlier
    for c in targets:
        r = eng.chamber_rollup(c)
        assert r["score"] < 1.0                        # 오염 median에 묻힘 (안 뜸)


def test_dod_single_chamber_drift_not_suspect():
    # #10 대조: 단일 챔버 지속 드리프트 → 역방향 미발동 + 그 챔버 개별 outlier(score>critical)
    fk = ("C6_0", 4, "settled", "C17")
    limits = {(c,) + fk: _lim(center=0.0, sigma=1.0) for c in ALL_CHAMBERS}
    eng = _engine(limits, set(limits), window=20, min_fill=10)
    for i in range(60):
        ts = f"2026-07-15T{10 + i // 60:02d}:{i % 60:02d}:00.000Z"
        for c in ALL_CHAMBERS:
            gk = (c,) + fk
            v = 4.0 if (c == "SIM_CH_1" and i >= 20) else 0.0
            eng.feed(_pt(gk, v, wafer=f"{c}_W{i}", ts=ts))
    assert fk not in eng._suspect_sensors              # 1대뿐 → max(1,0)<3 미발동
    r = eng.chamber_rollup("SIM_CH_1")
    assert r["score"] > 3.0                            # 개별 경로 (critical 초과)
    assert r["reference_suspect"] is False


# ============================================================================
# 리뷰 수정 검증 (2026-07-15) — fleet 캐시 stale(#A) / excluded rollup(#B)
# ============================================================================

def test_rollup_none_after_fleet_collapse():
    # #A: fleet가 MIN 밑으로 떨어지면 stale fleet_median 캐시를 지워 rollup None (죽은 fleet 기준 score 방지)
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    for c in CHAMBERS:
        _warm_ch(eng, c, 0.0)
    assert eng.chamber_rollup("SIM_CH_4") is not None          # 정상 fleet
    eng.set_excluded({"SIM_CH_1", "SIM_CH_2", "SIM_CH_3"})
    _warm_ch(eng, "SIM_CH_4", 0.0, base_min=20)                # members={CH4}<2 → 캐시 pop
    assert eng.chamber_rollup("SIM_CH_4") is None               # stale median 사용 안 함


def test_rollup_none_for_excluded_chamber():
    # #B: 제외 챔버는 score에서 빠짐 → rollup None (§3-7)
    limits = {_gk(c): _lim() for c in CHAMBERS}
    eng = _engine(limits, set(limits))
    for c in CHAMBERS:
        _warm_ch(eng, c, 2.0 if c == "SIM_CH_1" else 0.0)
    assert eng.chamber_rollup("SIM_CH_1") is not None
    eng.set_excluded({"SIM_CH_1"})
    assert eng.chamber_rollup("SIM_CH_1") is None
