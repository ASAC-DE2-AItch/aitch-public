"""NelsonEngine 상태 테스트 (합성 데이터, Kafka·DB 불필요)."""
import math

from src.agent_b_spc.nelson_engine import NelsonEngine, Point
from src.agent_b_spc.nelson_rules import RULES

GK = ("SIM_CH_1", "C6_0", 4, "settled", "C11")   # 표준 그룹 키
LIMITS = {GK: {"center": 0.0, "sigma": 1.0, "ucl": 3.0, "lcl": -3.0, "limit_version": "v1"}}
WL = {GK}


def _pt(value, wafer="W1", ts="2026-07-13T10:00:00.000Z", pm=1, ver="v1"):
    return Point(group_key=GK, wafer_id=wafer, timestamp=ts, value=value, pm_count=pm, limit_version=ver)


def test_non_whitelisted_group_skipped():
    eng = NelsonEngine(limits=LIMITS, whitelist=set())   # 빈 화이트리스트
    assert eng.feed(_pt(3.5)) == []


def test_skip_logs_once_per_group(caplog):
    # 회의 결정 "skip+로그": 감시 대상 아닌 그룹은 그룹당 최초 1회만 로그(스팸 없음)
    import logging
    eng = NelsonEngine(limits=LIMITS, whitelist=WL, time_gap_hours=72)
    other = ("SIM_CH_9", "C6_0", 4, "settled", "C99")   # 화이트리스트 밖
    op = Point(group_key=other, wafer_id="W1", timestamp="2026-07-13T10:00:00.000Z",
               value=1.0, pm_count=1, limit_version="v1")
    with caplog.at_level(logging.INFO):
        assert eng.feed(op) == [] and eng.feed(op) == []   # 두 번 skip
    hits = [r for r in caplog.records if "감시 대상 아님" in r.getMessage()]
    assert len(hits) == 1                                   # 최초 1회만


def test_time_gap_loaded_from_config():
    # time_gap 미지정 → config(spc.time_gap_threshold_hours=72) 로드 (헌법 6-1)
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    assert eng.time_gap_sec == 72 * 3600


def test_time_gap_injection_overrides_config():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL, time_gap_hours=10)
    assert eng.time_gap_sec == 10 * 3600


def test_nan_value_skipped_no_crash():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    assert eng.feed(_pt(math.nan)) == []


def test_out_of_order_timestamp_dropped():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    eng.feed(_pt(0.0, ts="2026-07-13T10:00:00.000Z"))
    # 더 과거 timestamp → 드롭 (버퍼 불변, 위반 없음)
    assert eng.feed(_pt(3.5, ts="2026-07-13T09:00:00.000Z")) == []


def test_ts_reversal_drops_counted():
    """역전 드롭을 `stats`로 계측한다 — 로그만으로는 관측이 인코딩·grep에 좌우된다(헌법 7장).

    실측 사례: producer 2개 → 파티션 도착 순서 역전 → 전량 드롭인데, 신호가 한글 WARNING
    하나뿐이라 grep 실패 시 "조용히 멈춘 것"으로 보였다(원인 규명 40분). 숫자로 노출한다.
    """
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    assert eng.stats["ts_reversal_drops"] == 0
    eng.feed(_pt(0.0, ts="2026-07-13T10:00:00.000Z"))
    assert eng.stats["ts_reversal_drops"] == 0              # 정상 진행분은 안 센다
    eng.feed(_pt(3.5, ts="2026-07-13T09:00:00.000Z"))       # 역전 → 드롭
    eng.feed(_pt(3.5, ts="2026-07-13T08:00:00.000Z"))       # 역전 → 드롭
    assert eng.stats["ts_reversal_drops"] == 2


def test_malformed_timestamp_skipped():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    assert eng.feed(_pt(3.5, ts="not-a-timestamp")) == []   # 크래시 없이 스킵


_N1_ONLY = [r for r in RULES if r.id == "N1"]


def _feed_seq(eng, values, ver="v1", pm=1, base_min=0):
    """값 시퀀스를 시간 증가시키며 feed, 마지막 반환값을 돌려준다."""
    out = []
    for i, v in enumerate(values):
        ts = f"2026-07-13T10:{base_min + i:02d}:00.000Z"
        out = eng.feed(_pt(v, wafer=f"W{i}", ts=ts, pm=pm, ver=ver))
    return out


def test_buffer_accumulates_and_evaluates():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    out = eng.feed(_pt(3.5))
    assert len(out) == 1 and out[0]["rule_id"] == "N1"


def test_n1_level_trigger_fires_every_point():
    # 기획서 4-3: 3σ 밖 연속 3장 → 3건 모두 발행 (level). N1만 활성화해 N5 오염 격리.
    eng = NelsonEngine(limits=LIMITS, whitelist=WL, active_rules=_N1_ONLY)
    counts = [len(_feed_seq(eng, [v], base_min=i)) for i, v in enumerate([3.5, 3.6, 3.7])]
    assert counts == [1, 1, 1]


def test_n2_edge_trigger_once_at_ninth():
    # 8점→미발행, 9점→N2 최초 1회 (edge)
    def n2_fired(n):
        out = _feed_seq(NelsonEngine(limits=LIMITS, whitelist=WL), [0.5] * n)
        return any(x["rule_id"] == "N2" for x in out)
    assert n2_fired(8) is False
    assert n2_fired(9) is True


def test_violation_fields_and_offending():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    out = _feed_seq(eng, [2.5, 2.5, 0.0])             # N5 발화 (무가드, 최신 정상점)
    v = next(x for x in out if x["rule_id"] == "N5")
    assert v["sensor_id"] == "C11"
    assert v["severity"] == "WARNING"
    assert v["control_limit_upper"] == 3.0
    assert v["limit_basis"] == "firm"
    assert len(v["offending"]) >= 2                   # 2σ 밖 점들 (귀속)


def test_description_carries_run_range():
    # N2/N3/N4는 offending 대신 description이 run 범위(첫..끝)를 담아야 자기완결
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    out = _feed_seq(eng, [1, 2, 3, 4, 5, 6])          # N3 6점 단조↑
    v = next(x for x in out if x["rule_id"] == "N3")
    assert ".." in v["description"] and "W0" in v["description"]   # 예: "N3: 6점 창 (W0..W5)"


def test_violation_carries_member_wafers():
    """member_wafers = 룰 창의 wafer_id 목록(시간순·창 크기만큼) — B4-1 suspect_window 합집합 소스(Q2 룰창)."""
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    out = _feed_seq(eng, [1, 2, 3, 4, 5, 6])          # N3 6점 창 → W0..W5
    v = next(x for x in out if x["rule_id"] == "N3")
    assert v["member_wafers"] == ["W0", "W1", "W2", "W3", "W4", "W5"]   # 창 크기 6·시간순


def test_member_wafers_scoped_to_rule_window():
    """룰마다 창 크기가 달라 member_wafers도 그 룰 창만큼 — N1(window=1)은 최신 1장."""
    eng = NelsonEngine(limits=LIMITS, whitelist=WL, active_rules=_N1_ONLY)
    out = _feed_seq(eng, [3.5, 3.6])                  # N1 매점 발화, 최신=W1
    v = next(x for x in out if x["rule_id"] == "N1")
    assert v["member_wafers"] == ["W1"]               # N1 window=1 → 최신 1장


def test_sigma_zero_skips_zone_rules():
    # σ=0(붕괴 zone)에서 center를 크게 벗어나도 N1(3σ) 헛발동 안 함 (defense-in-depth)
    limits0 = {GK: {"center": 5.0, "sigma": 0.0, "ucl": 5.0, "lcl": 5.0, "limit_version": "v1"}}
    eng = NelsonEngine(limits=limits0, whitelist=WL)
    out = eng.feed(_pt(100.0))
    assert not any(x["rule_id"] == "N1" for x in out)


def test_reset_on_pm_count_clears_buffer():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [0.5] * 8, pm=1)                    # N2 직전(8점)
    out = eng.feed(_pt(0.5, wafer="W8", ts="2026-07-13T10:20:00.000Z", pm=2))  # PM → 리셋
    # 리셋으로 버퍼가 1점 → N2(9점) 불성립
    assert not any(x["rule_id"] == "N2" for x in out)


def test_reset_on_time_gap_clears_buffer():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL, time_gap_hours=12)
    _feed_seq(eng, [0.5] * 8)                           # 10:00~10:07
    # 24h 뒤 점 → time-gap(>12h) 리셋
    out = eng.feed(_pt(0.5, wafer="W8", ts="2026-07-14T10:20:00.000Z"))
    assert not any(x["rule_id"] == "N2" for x in out)


def test_limit_version_change_no_spurious_fire():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    # old center 0에서 mixed(±0.2) 9점 → N2 False(양쪽), 다른 룰도 미발화(전부 1σ 이내)
    mixed = [0.2, -0.2, 0.2, -0.2, 0.2, -0.2, 0.2, -0.2, 0.2]
    assert not _feed_seq(eng, mixed)                   # 발행 없음
    # 관리선 갱신: center 0.5로 이동 + v2 → ±0.2가 전부 새 center '아래'로 뒤집힘
    eng.limits = {GK: {"center": 0.5, "sigma": 1.0, "ucl": 3.5, "lcl": -2.5, "limit_version": "v2"}}
    out = eng.feed(_pt(0.2, wafer="W9", ts="2026-07-13T10:20:00.000Z", ver="v2"))
    # silent re-baseline이 recenter로 인한 '9 below' 전이를 흡수 → N2 헛발동 없음
    # (re-baseline 없으면 prev=False라 여기서 N2가 spurious 발행됨)
    assert not any(x["rule_id"] == "N2" for x in out)


def test_limit_version_keeps_buffer_not_cleared():
    # 버전 변경 시 버퍼 유지 검증: 5점 단조 적재 → 버전 변경 → 6번째 단조점에서 N3 발화.
    # (버퍼가 비워졌다면 6점이 안 모여 N3 미발화 → keep vs clear를 구분)
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [1, 2, 3, 4, 5], ver="v1")
    eng.limits = {GK: {"center": 0.0, "sigma": 1.0, "ucl": 3.0, "lcl": -3.0, "limit_version": "v2"}}
    out = eng.feed(_pt(6, wafer="W5", ts="2026-07-13T10:20:00.000Z", ver="v2"))
    assert any(x["rule_id"] == "N3" for x in out)       # 버퍼 유지됐어야 6점 → N3 발화
    assert len(eng._state[GK].buffer) == 6              # 버퍼 실제 유지


def test_update_whitelist_shrink_deletes_state():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    eng.feed(_pt(0.5))
    assert GK in eng._state
    eng.update_whitelist(set())                       # 축소 → 제외
    assert GK not in eng._state                        # GroupState 삭제(stale 방지)
    assert eng.feed(_pt(3.5)) == []                    # 이제 비대상


def test_buffer_boundary_n7_at_full_buffer():
    # 16번째 점 유입해도 최근 15점으로 N7 정확 판정 (maxlen≥최장 룰 불변식)
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [0.5] * 16)                          # 16점 유입
    assert len(eng._state[GK].buffer) == 15             # maxlen 유지 — 오래된 1점 소멸(불변식)
    # 15점째에서 N7 발행됐는지 (최근 15점으로 정확 판정)
    eng2 = NelsonEngine(limits=LIMITS, whitelist=WL)
    out15 = _feed_seq(eng2, [0.5] * 15)
    assert any(x["rule_id"] == "N7" for x in out15)


def test_n5_clear_pinpoint_rearm():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [2.5, 2.5])                          # 2점 — N5 아직(3점 필요)
    eng.feed(_pt(0.0, wafer="Wa", ts="2026-07-13T10:05:00.000Z"))  # [2.5,2.5,0.0]=2/3 → N5 최초 발화
    eng.feed(_pt(0.0, wafer="Wb", ts="2026-07-13T10:06:00.000Z"))  # [2.5,0.0,0.0]=1/3 → 해소·재무장(pinpoint)
    eng.feed(_pt(2.5, wafer="Wc", ts="2026-07-13T10:07:00.000Z"))  # [0.0,0.0,2.5]=1/3 → 미발행
    out = eng.feed(_pt(2.5, wafer="Wd", ts="2026-07-13T10:08:00.000Z"))  # [0.0,2.5,2.5]=2/3 → 재무장됐으므로 재발행
    assert any(x["rule_id"] == "N5" for x in out)      # 해소됐어야만 여기서 재발행됨


def test_sigma_zero_keeps_zone_free_rules():
    # σ=0에서 σ-존 룰(N1)은 스킵되지만 zone-무관 룰(N3)은 계속 평가됨
    limits0 = {GK: {"center": 0.0, "sigma": 0.0, "ucl": 0.0, "lcl": 0.0, "limit_version": "v1"}}
    eng = NelsonEngine(limits=limits0, whitelist=WL)
    out = _feed_seq(eng, [1, 2, 3, 4, 5, 6])          # 6점 단조 → N3
    assert any(x["rule_id"] == "N3" for x in out)      # N3(zone 무관)는 σ=0에서도 동작
    assert not any(x["rule_id"] == "N1" for x in out)  # N1(3σ)은 스킵


def test_reset_method_clears_group_state():
    # 수동 reset(gk) → GroupState 삭제. 이후 버퍼가 새로 시작돼 직전 축적이 사라짐.
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [0.5] * 8)                           # N2 직전(8점 버퍼)
    assert len(eng._state[GK].buffer) == 8
    eng.reset(GK)                                       # 수동 리셋
    assert GK not in eng._state                         # 상태 삭제됨
    out = eng.feed(_pt(0.5, wafer="W9", ts="2026-07-13T10:30:00.000Z"))
    assert not any(x["rule_id"] == "N2" for x in out)   # 1점만으론 N2(9점) 불성립
    assert len(eng._state[GK].buffer) == 1              # 버퍼 새로 시작


def test_rebaseline_sigma_zero_no_crash_no_zone_emit():
    # limit_version 변경으로 _rebaseline(silent 재기준) 진입 + 새 관리선 σ=0(붕괴).
    # 재기준·평가 양쪽 σ-존 가드로: ① 크래시 없음(feed 완주), ② σ-존 룰 헛발동 없음,
    # ③ zone-무관 룰(N3)은 σ=0에서도 동작. (가드가 없으면 붕괴 zones에서 σ-존 룰이 헛발동)
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [1, 2, 3, 4, 5], ver="v1")          # 단조↑ 5점 (v1, σ=1)
    eng.limits = {GK: {"center": 3.0, "sigma": 0.0, "ucl": 3.0, "lcl": 3.0, "limit_version": "v2"}}
    out = eng.feed(_pt(6, wafer="W5", ts="2026-07-13T10:20:00.000Z", ver="v2"))  # rebaseline(σ=0)+평가
    assert not any(x["rule_id"] in ("N1", "N5", "N6", "N7", "N8") for x in out)   # σ-존 헛발동 없음
    assert any(x["rule_id"] == "N3" for x in out)      # zone-무관은 σ=0에서도 발화


# ── 결정 A·B 조합②: KEEP* 분위수 그룹 (헌법 1-1 예외 2) ────────────────────
GKQ = ("SIM_CH_1", "C6_0", 4, "settled", "C61")   # KEEP* 비대칭 그룹(분위수 관리선)
# 분위수 상/하한이 ucl/lcl(활성 관리선)에 저장 — 비대칭. sigma는 참고 통계(판정 미사용).
LIMITS_Q = {GKQ: {"center": 0.0, "sigma": 1.0, "ucl": 5.0, "lcl": -2.0,
                  "method": "quantile", "limit_version": "v1"}}
WLQ = {GKQ}


def _ptq(value, wafer="W1", ts="2026-07-13T10:00:00.000Z", pm=1, ver="v1"):
    return Point(group_key=GKQ, wafer_id=wafer, timestamp=ts, value=value, pm_count=pm, limit_version=ver)


def _feed_seq_q(eng, values, ver="v1", pm=1, base_min=0):
    out = []
    for i, v in enumerate(values):
        ts = f"2026-07-13T10:{base_min + i:02d}:00.000Z"
        out = eng.feed(_ptq(v, wafer=f"W{i}", ts=ts, pm=pm, ver=ver))
    return out


def test_quantile_n1_fires_outside_bounds_reports_quantile_limits():
    # 분위수 상한(ucl=5) 밖 → N1 발화, 위반 레코드에 분위수 관리선(ucl/lcl)이 실림
    eng = NelsonEngine(limits=LIMITS_Q, whitelist=WLQ)
    out = eng.feed(_ptq(6.0))
    v = next(x for x in out if x["rule_id"] == "N1")
    assert v["control_limit_upper"] == 5.0 and v["control_limit_lower"] == -2.0


def test_quantile_n1_within_bounds_no_fire():
    # 밴드(-2..5) 안이면 N1 미발화 — σ=1 대칭 ±3σ였다면 4.0은 발화했을 값(분위수 거동 차이)
    eng = NelsonEngine(limits=LIMITS_Q, whitelist=WLQ)
    assert not any(x["rule_id"] == "N1" for x in eng.feed(_ptq(4.0)))


def test_quantile_n1_asymmetric_lower_bound():
    # 비대칭: 하한 lcl=-2 밖(-3)이면 N1 발화 (대칭 ±3σ와 다른 하한)
    eng = NelsonEngine(limits=LIMITS_Q, whitelist=WLQ)
    assert any(x["rule_id"] == "N1" for x in eng.feed(_ptq(-3.0)))


def test_quantile_excludes_sigma_zone_rules_vs_sigma_group():
    # 동일 값이 σ 그룹(σ=1)에선 N5 발화하나, 분위수 그룹은 N5~N8 제외로 미발화 (결정 B-2)
    vals = [2.5, 2.5, 0.0]
    sig = _feed_seq(NelsonEngine(limits=LIMITS, whitelist=WL), vals)     # 대조군: σ 그룹
    assert any(x["rule_id"] == "N5" for x in sig)
    q = _feed_seq_q(NelsonEngine(limits=LIMITS_Q, whitelist=WLQ), vals)  # 분위수 그룹
    assert not any(x["rule_id"] in ("N5", "N6", "N7", "N8") for x in q)


def test_quantile_keeps_zone_free_rules_n2_n3():
    # σ-무관 룰(N2 9점 동일측 / N3 6점 단조)은 분위수 그룹에서도 유지·발화
    out_n2 = _feed_seq_q(NelsonEngine(limits=LIMITS_Q, whitelist=WLQ), [1.0] * 9)
    assert any(x["rule_id"] == "N2" for x in out_n2)
    out_n3 = _feed_seq_q(NelsonEngine(limits=LIMITS_Q, whitelist=WLQ),
                         [0.1, 0.2, 0.3, 0.4, 0.45, 0.48])
    assert any(x["rule_id"] == "N3" for x in out_n3)
