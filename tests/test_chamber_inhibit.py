# -*- coding: utf-8 -*-
"""chamber_inhibit 유닛테스트 — 발동 판정·신호 파일·스위치 (DB 무의존 부분).

DB 경로(record_trigger/release)는 부분 유니크 인덱스와 한 몸이라 level2(pg) 검증 대상 —
여기서는 판정 로직과 파일 채널의 순수 부분만 고정한다 (헌법 1-1 예외 4 조건의 회귀 방지).
실행: python -m pytest tests/test_chamber_inhibit.py -q
"""
import importlib
import json

import src.orchestrator.chamber_inhibit as ci


def _reload_with_dir(tmp_path, monkeypatch):
    """INHIBIT_DIR 을 tmp 로 격리 — env 경유 리로드."""
    monkeypatch.setenv("RTD_INHIBIT_DIR", str(tmp_path / "inhibit"))
    importlib.reload(ci)
    return ci


# ── should_inhibit — 사전 필터 (예외 4 ⓐ 1단 — 본 판정은 persistent_breach) ──
#   챔버 재료 = alert.violations[].sensor distinct 수 (그 챔버 고유 이상의 폭)
#   장비 재료 = tttm.reference_suspect / suspect_sensors (공통 이동 — 두 필드는 동치)
#   ⚠️ 계약 키는 `sensor` (publisher._build_violation 이 엔진 sensor_id → sensor 변환).
#      2026-08-06 실측에서 `sensor_id` 로 세면 전건 0 이 나와 확정 — 테스트도 계약 형태로 쓴다.
#
#   🔴 **`severity` 도 `violations[]` 안이다** (2026-08-10 정정). 계약 §3 이 그렇고 실제
#      payload 도 그렇다 — 알람 최상위에는 그 필드가 **없다**. 구 fixture 가 최상위에 두는
#      바람에 **테스트는 초록불인데 운영에서는 사전 필터가 항상 False** 였다(RTD 사문화).
#      fixture 형태가 계약과 어긋나면 그 테스트는 **아무것도 지키지 않는다.**
_V2 = [{"rule_id": "N1", "sensor": "C32", "severity": "CRITICAL"},
       {"rule_id": "N2", "sensor": "C17", "severity": "WARNING"}]
_V1 = [{"rule_id": "N1", "sensor": "C32", "severity": "CRITICAL"}]
_V2_WARN = [{"rule_id": "N2", "sensor": "C32", "severity": "WARNING"},
            {"rule_id": "N2", "sensor": "C17", "severity": "WARNING"}]
_LOOSE = {"rtd": {"min_violation_sensors": 1}}


def test_should_inhibit_requires_critical_and_multi_sensor():
    """CRITICAL + 동시 위반 센서 2종 이상이면 발동 (대소문자 무관)."""
    assert ci.should_inhibit({"violations": _V2}) is True
    lower = [{**v, "severity": v["severity"].capitalize()} for v in _V2]
    assert ci.should_inhibit({"violations": lower}) is True


def test_should_inhibit_rejects_noncritical_or_single_sensor():
    """WARNING·단일 센서·violations 부재는 전부 미발동 — 안전 기본값."""
    assert ci.should_inhibit({"violations": _V2_WARN}) is False   # 최고치가 WARNING
    assert ci.should_inhibit({"violations": _V1}) is False        # 1종 → 차단
    assert ci.should_inhibit({"violations": []}) is False
    assert ci.should_inhibit({}) is False
    assert ci.should_inhibit(None) is False                       # 형상 불량


def test_severity_is_read_from_violations_not_alert_root():
    """🔴 회귀 방지 — severity 는 `violations[]` 에서 읽는다 (계약 §3).

    2026-08-10 실측 결함: 구 코드가 `alert.get("severity")` 로 **최상위**를 읽어
    항상 `None` → 사전 필터가 무조건 False → **RTD 자동 정지가 한 번도 발동 못 함**.
    실패가 로그를 안 남겨(record_trigger 가 조용히 None 반환) 켜고 봐도 안 보였다.

    ⓐ 최상위에 값이 **없어도** violations 로 판정된다 (실제 payload 형태)
    ⓑ 최상위에만 있으면 **미발동** — 계약에 없는 자리라 폴백으로도 보지 않는다.
       폴백을 두면 같은 어긋남이 다시 조용해진다.
    """
    assert "severity" not in {"violations": _V2}                       # 실 payload 엔 최상위 없음
    assert ci.alert_severity({"violations": _V2}) == "CRITICAL"        # ⓐ 최댓값을 집는다
    assert ci.should_inhibit({"violations": _V2}) is True              # ⓐ

    root_only = {"severity": "CRITICAL", "violations": _V2_WARN}       # ⓑ 최상위에만 CRITICAL
    assert ci.alert_severity(root_only) == "WARNING"                   # violations 가 정본
    assert ci.should_inhibit(root_only) is False


def test_min_violation_sensors_threshold():
    """빈도 정합(§5): 단일 센서 위반은 기본 임계 2 에서 미발동 — params 로 완화 가능."""
    one = {"violations": _V1}
    assert ci.should_inhibit(one) is False                                   # 기본 2 → 차단
    assert ci.should_inhibit(one, _LOOSE) is True                            # 1 로 완화 시 발동


def test_violation_sensor_count_dedups():
    """같은 센서의 다중 룰 위반은 1종으로 센다 (N1·N2 둘 다 C32 → 1)."""
    dup = [{"rule_id": "N1", "sensor": "C32"}, {"rule_id": "N2", "sensor": "C32"}]
    assert ci.violation_sensor_count({"violations": dup}) == 1
    assert ci.violation_sensor_count({"violations": _V2}) == 2
    assert ci.violation_sensor_count({}) == 0


def test_violation_sensor_count_contract_key_is_sensor():
    """★계약 키는 `sensor` — `sensor_id` 로 세면 실 alert 에서 전건 0 이 된다(2026-08-06 실측).

    publisher._build_violation 이 엔진 내부 `sensor_id` 를 계약 `sensor` 로 변환해 싣는다.
    엔진 dict 직접 소비 대비로 `sensor_id` 폴백도 유지하되, 정본은 `sensor` 다.
    """
    contract = [{"rule_id": "N1", "sensor": "C32"}, {"rule_id": "N5", "sensor": "C61"}]
    engine = [{"rule_id": "N1", "sensor_id": "C32"}, {"rule_id": "N5", "sensor_id": "C61"}]
    assert ci.violation_sensor_count({"violations": contract}) == 2      # 계약 형태 (정본)
    assert ci.violation_sensor_count({"violations": engine}) == 2        # 엔진 형태 (폴백)
    crit = [{**v, "severity": "CRITICAL"} for v in contract]
    assert ci.should_inhibit({"violations": crit}) is True


def test_tttm_suspect_sensors_is_not_chamber_material():
    """★B 리뷰 정정 — tttm.suspect_sensors 는 챔버 조건 재료가 아니다.

    그 필드는 reference_suspect 를 발생시킨 **공통 이동 센서** 목록이라 두 값이 동치이며,
    챔버 고유 이상의 폭이 아니다. 이것만 있고 violations 가 없으면 발동하지 않아야 한다.
    """
    a = {"tttm": {"reference": "fleet_median", "score": 3.2, "top_gap_sensor": "C62",
                  "gap_pct": 2.1, "reference_suspect": True, "suspect_sensors": ["C62", "C17"]}}
    assert ci.should_inhibit(a) is False           # violations 부재 → 챔버 조건 미충족


# ── 스코프 2단 — 챔버 / 장비 (멘토: "장비 단위로도 정지한다") ────────────────
def _tttm(suspect_ref: bool, sensors=None) -> dict:
    """계약 6필드 tttm 스텁 (#113). suspect_sensors≠∅ ⟺ reference_suspect=True 가 계약."""
    return {"reference": "fleet_median", "score": 3.0, "top_gap_sensor": "C62",
            "gap_pct": 2.0, "reference_suspect": suspect_ref,
            "suspect_sensors": (sensors if sensors is not None
                                else (["C62", "C17"] if suspect_ref else []))}


def test_equipment_scope_on_reference_suspect():
    """공통 이동(reference_suspect=True + suspect_sensors≠∅) → 장비 스코프 승격."""
    a = {"violations": _V2, "tttm": _tttm(True)}
    assert ci.should_inhibit(a) is True             # 챔버 조건은 violations 로 성립
    assert ci.should_inhibit_equipment(a) is True   # 공통 이동이므로 장비로 승격


def test_chamber_scope_when_not_common():
    """reference_suspect=False(그 챔버만의 이상) → 챔버 단위 유지."""
    a = {"violations": _V2, "tttm": _tttm(False)}
    assert ci.should_inhibit(a) is True
    assert ci.should_inhibit_equipment(a) is False


def test_equipment_scope_min_common_sensors():
    """min_common_sensors 로 장비 승격 문턱 조절 (기본 1)."""
    a = {"violations": _V2, "tttm": _tttm(True, ["C62"])}
    assert ci.should_inhibit_equipment(a) is True                                   # 1개 ≥ 기본 1
    assert ci.should_inhibit_equipment(a, {"rtd": {"min_common_sensors": 2}}) is False


def test_equipment_scope_switch_off():
    """allow_equipment_scope=false → 공통 이동이어도 챔버 단위로만 (보수 운영)."""
    a = {"violations": _V2, "tttm": _tttm(True)}
    assert ci.should_inhibit_equipment(a, {"rtd": {"allow_equipment_scope": False}}) is False


# ── AE arm 제외 (PM 경계 전 챔버 동시 정지 방지 — 2026-08-06 실증) ───────────
def test_ae_only_alert_excluded_even_when_loosened():
    """AE anomaly 단독 근거는 정지시키지 않는다 (exclude_ae_arm — 2차 방어선).

    AE-only 알람(B9 anomaly 마커)은 구조상 violations 1건이라 챔버 조건(2종)에서
    이미 걸러진다. 이 테스트는 **min_violation_sensors 를 1로 완화한 상황**에서도
    AE 단독이 못 서는지 — 즉 방어선이 이중인지를 고정한다 (PM 후 폭주 방지의 최후 보루).
    """
    ae_only = {"violations": [{"rule_id": "B9", "sensor_id": "C65", "severity": "CRITICAL",
                               "description": "B9 crazy: anomaly_score 1.00 > cut 0.20"}]}
    loose = {"rtd": {"min_violation_sensors": 1}}
    assert ci.should_inhibit(ae_only, loose) is False                 # AE 제외가 막는다
    assert ci.should_inhibit(
        ae_only, {"rtd": {"min_violation_sensors": 1, "exclude_ae_arm": False}}) is True


def test_spc_or_predicted_arm_still_inhibits():
    """Nelson 위반·예측 arm 근거는 AE 제외 정책과 무관하게 발동한다."""
    nelson = {"violations": _V2}
    pred = {"violations": [{"rule_id": "B9", "sensor_id": "C65", "severity": "CRITICAL",
                            "description": "B9 crazy: predicted_c65 1600.0 > P99 1572.0"},
                           {"rule_id": "N1", "sensor_id": "C32", "severity": "CRITICAL"}]}
    assert ci.should_inhibit(nelson) is True
    assert ci.should_inhibit(pred) is True


# ── 본 판정 — 지속 이탈률 (2026-08-06 판별축 교체, 상황판 D-5) ───────────────
#   실측 고정: 스톰(SC4) C17 44.1%·C62 39.3% → 2개 / 평시 최고 C17 23.0% → 0개.
#   테스트 수치는 그 실측을 그대로 쓴다 — 임계(0.30)가 두 군집 사이임을 회귀로 보증.
_STORM_HITS = {"C17": 442, "C62": 394, "C11": 211}      # 총 1003 알람 (CH_2 실측)
_BASELINE_HITS = {"C17": 171, "C9": 70, "C54": 61}      # 총  742 알람 (CH_1 실측)


def test_persistent_sensors_storm_vs_baseline():
    """★판별축 실측 회귀 — 스톰은 2개(C17·C62) 초과, 평시는 0개."""
    assert ci._persistent_sensors_from_counts(_STORM_HITS, 1003) == ["C17", "C62"]
    assert ci._persistent_sensors_from_counts(_BASELINE_HITS, 742) == []


def test_persistent_sensors_ratio_configurable():
    """breach_ratio 를 params 로 조절 — 0.20 이면 평시 C17(23.0%)도 걸린다(임계 민감도)."""
    loose = {"rtd": {"breach_ratio": 0.20}}
    assert ci._persistent_sensors_from_counts(_BASELINE_HITS, 742, loose) == ["C17"]
    tight = {"rtd": {"breach_ratio": 0.45}}
    assert ci._persistent_sensors_from_counts(_STORM_HITS, 1003, tight) == []


def test_persistent_sensors_zero_denominator():
    """0 분모 방어 (7장) — 빈 창이면 빈 목록 (예외 없이)."""
    assert ci._persistent_sensors_from_counts({"C17": 5}, 0) == []
    assert ci._persistent_sensors_from_counts({}, 100) == []


class _FakeCursor:
    """persistent_breach 2쿼리 + record_trigger INSERT 를 흉내내는 최소 커서.

    queue 의 결과를 execute 순서대로 내주고, 실행 SQL 을 기록한다 — 판정 순서
    (사전 필터 → 본 판정 → INSERT) 가 지켜지는지 SQL 시퀀스로 검증하기 위함.
    """
    def __init__(self, results):
        self.results = list(results)
        self.executed = []
        self._current = None

    def execute(self, sql, args=None):
        self.executed.append(" ".join(sql.split())[:60])
        self._current = self.results.pop(0) if self.results else []

    def fetchall(self):
        return self._current or []

    def fetchone(self):
        return self._current[0] if self._current else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, results):
        self.cur = _FakeCursor(results)

    def cursor(self):
        return self.cur


_STORM_ALERT = {"chamber_id": "SIM_CH_2",
                "alert_id": "ALERT-T-001", "violations": _V2}


# fail-closed 반전(2026-08-07) 후 record_trigger 테스트는 **차단기를 명시적으로 켜고**
# 본 판정을 검증한다 — params 없이 부르면 스위치에서 잘려 무엇도 검증하지 못한다.
_RTD_ON = {"rtd": {"auto_inhibit": True}}


def test_record_trigger_blocks_on_baseline_like_window():
    """사전 필터 통과 알람이라도 **창이 평시 분포면 미발동** — 구 축과의 결정적 차이.

    구 축은 알람 단건(3센서 스톰)이 임계 5 미달로 정탐을 놓치거나, 3으로 내리면
    평시 10% 오발동. 신 축은 같은 알람이라도 창의 지속률로 갈라낸다.
    """
    conn = _FakeConn([
        [("C17", 171), ("C9", 70), ("C54", 61)],   # N1 hits — 평시(CH_1 실측)
        [(742,)],                                   # total
    ])
    assert ci.record_trigger(conn, "INC-T-1", _STORM_ALERT, _RTD_ON) is None
    assert any(s for s in conn.cur.executed)                   # 본 판정 조회는 도달 (스위치에서 안 잘림)
    assert not any("INSERT" in s for s in conn.cur.executed)   # INSERT 미도달


def test_record_trigger_fires_on_storm_window():
    """창이 스톰 분포(지속 이탈 2센서)면 INSERT 도달 — 챔버 스코프."""
    conn = _FakeConn([
        [("C17", 442), ("C62", 394), ("C11", 211)],  # N1 hits — 스톰(CH_2 실측)
        [(1003,)],                                    # total
        [(1,)],                                       # INSERT RETURNING id
    ])
    hit = ci.record_trigger(conn, "INC-T-2", _STORM_ALERT, _RTD_ON)
    assert hit == (["SIM_CH_2"], "ALERT-T-001", "chamber")
    assert any("INSERT INTO chamber_inhibits" in s for s in conn.cur.executed)


def test_record_trigger_denominator_guard(caplog):
    """분모 가드 — 창 알람 < min_window_alerts 면 보수적 미발동 + WARNING.

    consumer 적재 중단(8/6 producer 중복 사고: 40분 적재 0건)이면 RTD 가 조용히
    침묵하는데, 그 상태를 로그로 드러내는 게 이 가드의 두 번째 역할이다.
    """
    conn = _FakeConn([
        [("C17", 4), ("C62", 4)],                    # 비율은 초과지만
        [(10,)],                                      # 분모 10 < 기본 30
    ])
    import logging
    with caplog.at_level(logging.WARNING, logger="orchestrator.inhibit"):
        assert ci.record_trigger(conn, "INC-T-3", _STORM_ALERT, _RTD_ON) is None
    assert any("판정 보류" in r.message for r in caplog.records)
    assert not any("INSERT" in s for s in conn.cur.executed)


def test_record_trigger_prefilter_skips_db():
    """사전 필터 불통과(WARNING 알람)면 **DB 조회 자체가 없다** — 비용 순서 보증."""
    conn = _FakeConn([])
    warn = {"chamber_id": "SIM_CH_2", "alert_id": "A", "violations": _V2_WARN}
    assert ci.record_trigger(conn, "INC-T-4", warn, _RTD_ON) is None
    assert conn.cur.executed == []


# ── auto_inhibit_enabled — 회귀 스위치 (예외 4 ⓓ) ──────────────────────────
def test_switch_default_and_off():
    """★fail-closed 고정 (2026-08-07 반전) — params 부재·형상 불량 = **잠금**.

    구 기본 True 는 rtd_params 를 안 넘기는 호출부(agent_service 그루퍼)에서 미검증
    임계로 자동 정지를 조용히 켰다(리뷰 3건 공통 지적). 발동은 명시적 true 만."""
    assert ci.auto_inhibit_enabled(None) is False
    assert ci.auto_inhibit_enabled({}) is False
    assert ci.auto_inhibit_enabled({"rtd": "형상불량"}) is False
    assert ci.auto_inhibit_enabled({"rtd": {"auto_inhibit": False}}) is False
    assert ci.auto_inhibit_enabled({"rtd": {"auto_inhibit": True}}) is True


# ── 시나리오 추적 — 설계 의도 대 코드 (검증 세션 2026-08-06) ─────────────────
def test_scenario_trace_matrix():
    """대표 시나리오 5종의 **사전 필터** 판정 고정 (2026-08-06 축 교체 후 역할 갱신).

    should_inhibit 은 이제 1단(사전 필터)이다 — 통과해도 정지가 아니라 본 판정
    (persistent_breach — 창 내 지속 이탈률)으로 넘어간다. 여기서 False 는 곧 최종
    미발동이므로 배제 케이스(①④⑤)의 의미는 그대로 유효하다.

    ① 단일 센서 spike(crazy 아님)      → 필터 차단 (단발 노이즈로 챔버 안 세움 — 현업 정합)
    ② RF 사건(C32·C61·C62 동시)        → 필터 통과 → 본 판정으로
    ③ ②+공통 이동(reference_suspect)   → 필터 통과 + 장비 승격 재료
    ④ 공통 이동인데 그 챔버 위반 1종    → 필터 차단 (챔버 조건이 선행 게이트 — 보수적 설계)
    ⑤ B9 predicted crazy 단독(1센서)   → 필터 차단 (wafer 는 B9 HOLD 가 따로 격리 — 챔버 정지는 과잉)
    """
    rf = [{"rule_id": "N1", "sensor_id": "C32", "severity": "CRITICAL"},
          {"rule_id": "N5", "sensor_id": "C61", "severity": "CRITICAL"},
          {"rule_id": "N5", "sensor_id": "C62", "severity": "WARNING"}]
    common = _tttm(True, ["C62", "C17"])

    s1 = {"violations": _V1}
    s2 = {"violations": rf, "tttm": _tttm(False)}
    s3 = {"violations": rf, "tttm": common}
    s4 = {"violations": _V1, "tttm": common}
    s5 = {"violations": [{"rule_id": "B9", "sensor_id": "C65", "severity": "CRITICAL",
                          "description": "B9 crazy: predicted_c65 1600.0 > P99 1572.0"}]}

    assert ci.should_inhibit(s1) is False
    assert ci.should_inhibit(s2) is True and ci.should_inhibit_equipment(s2) is False
    assert ci.should_inhibit(s3) is True and ci.should_inhibit_equipment(s3) is True
    assert ci.should_inhibit(s4) is False          # 챔버 게이트 선행 — 문서 §스코프 명시
    assert ci.should_inhibit(s5) is False


# ── 신호 파일 채널 — 원자 드롭·멱등 제거 ───────────────────────────────────
def test_signal_drop_and_remove(tmp_path, monkeypatch):
    """드롭 → 존재+내용 정합 → 제거 멱등 (7장: tmp+os.replace 원자 교체)."""
    m = _reload_with_dir(tmp_path, monkeypatch)
    p = m._drop_signal("SIM_CH_2", "INC-TEST-001", "ALERT-TEST-001")
    assert p.exists() and p == m.signal_path("SIM_CH_2")
    body = json.loads(p.read_text(encoding="utf-8"))
    assert body["chamber_id"] == "SIM_CH_2" and body["incident_id"] == "INC-TEST-001"
    assert not list(p.parent.glob("*.tmp"))          # 원자 교체 — tmp 잔류물 없음
    assert m._remove_signal("SIM_CH_2") is True
    assert m._remove_signal("SIM_CH_2") is False     # 멱등 — 두 번째는 no-op
    monkeypatch.delenv("RTD_INHIBIT_DIR")
    importlib.reload(m)                              # 전역 원복 (다른 테스트 오염 방지)


# ── 형제 챔버 목록 소스 (2026-08-07 — B 리뷰 #118) ──────────────────────────
#   구 소스 wafer_predictions(A 소유)는 A 다운 시 비어 equipment 스코프가 **조용히**
#   chamber 로 강등됐다. 소스를 config→control_limits 로 바꾸고, 못 얻으면 WARNING.
def _write_offsets(tmp_path, chambers):
    p = tmp_path / "chamber_offsets.json"
    p.write_text(json.dumps({"offsets": {c: {} for c in chambers}}), encoding="utf-8")
    return p


def test_siblings_prefers_config_and_never_touches_db(tmp_path, monkeypatch):
    """★핵심 회귀 가드 — config 가 있으면 DB 를 아예 안 본다 (A·B 가동 무관)."""
    monkeypatch.setattr(ci, "CHAMBER_OFFSETS_PATH",
                        _write_offsets(tmp_path, ["SIM_CH_2", "SIM_CH_1"]))
    conn = _FakeConn([])
    assert ci._sibling_chambers(conn, "SIM_CH_1") == ["SIM_CH_1", "SIM_CH_2"]   # 정렬 보장
    assert conn.cur.executed == []                    # DB 조회 자체가 없다


def test_siblings_falls_back_to_control_limits(tmp_path, monkeypatch):
    """config 부재 → control_limits 폴백. wafer_predictions 는 더 이상 안 읽는다."""
    monkeypatch.setattr(ci, "CHAMBER_OFFSETS_PATH", tmp_path / "없음.json")
    conn = _FakeConn([[("SIM_CH_1",), ("SIM_CH_2",)]])
    assert ci._sibling_chambers(conn, "SIM_CH_1") == ["SIM_CH_1", "SIM_CH_2"]
    assert any("control_limits" in s for s in conn.cur.executed)
    assert not any("wafer_predictions" in s for s in conn.cur.executed)   # 구 소스 폐기 고정


def test_siblings_malformed_config_falls_through(tmp_path, monkeypatch):
    """형상 불량 config 는 예외 없이 폴백 — RTD 가 죽으면 안 된다."""
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(ci, "CHAMBER_OFFSETS_PATH", bad)
    conn = _FakeConn([[("SIM_CH_3",)]])
    assert ci._sibling_chambers(conn, "SIM_CH_3") == ["SIM_CH_3"]
    assert any("control_limits" in s for s in conn.cur.executed)


def test_siblings_empty_sources_warn_not_silent(tmp_path, monkeypatch, caplog):
    """★ 조용한 강등 금지 — 소스가 다 비면 chamber 로 강등하되 WARNING 을 남긴다."""
    monkeypatch.setattr(ci, "CHAMBER_OFFSETS_PATH", tmp_path / "없음.json")
    with caplog.at_level("WARNING"):
        out = ci._sibling_chambers(_FakeConn([[]]), "SIM_CH_4")
    assert out == ["SIM_CH_4"]                        # 보수적 강등은 유지
    assert any("강등" in r.getMessage() for r in caplog.records)


def test_siblings_includes_self_when_missing_from_source(tmp_path, monkeypatch, caplog):
    """구성 소스에 대상 챔버가 없어도 자기 자신은 항상 포함 + 불일치 경고."""
    monkeypatch.setattr(ci, "CHAMBER_OFFSETS_PATH", _write_offsets(tmp_path, ["SIM_CH_1"]))
    with caplog.at_level("WARNING"):
        out = ci._sibling_chambers(_FakeConn([]), "SIM_CH_9")
    assert out == ["SIM_CH_1", "SIM_CH_9"]
    assert any("불일치" in r.getMessage() for r in caplog.records)
