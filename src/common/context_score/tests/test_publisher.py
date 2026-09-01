"""Step 5 publisher 공통(억제·채번·조립·적재·발행) 단위 테스트.

스펙: Docs/B4-1_Step4_5_5b_통합구현계획서_v2.md §8-② · D-8~D-12.
DB는 sqlite in-memory, producer는 fake — 브로커·Postgres 불요.

핵심 계약: Phase 0 = 적재·발행 둘 다 skip(D-8) · severity·context_score는 운반만(passenger, D-9) ·
alert_id = (날짜,챔버) max+1·`:04d`·챔버 언더스코어 제거(D-10) · 스위치 off = send만 skip(D-11) ·
INSERT는 발행보다 먼저·독립 try(D-12) · AlertModel 함정 4종(최상위 wafer_id 금지·tttm 6필드·
timestamp=wafer t0·shap 객체형→C코드).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from src.common.context_score.publisher import (
    CrazyVerdict, PublisherConfig, PublisherCounters, PublisherDeps, TOPIC_ALERT,
    _build_suspect_window, _build_tttm, _shap_to_sensor_codes, _wafer_seq,
    build_alert, insert_spc_violation, insert_violations, next_alert_id, process,
)

# M3 — predicted arm crazy 판정(테스트 고정값). is_crazy 없이 직접 구성.
_CRAZY = CrazyVerdict(pred_hit=True, an_hit=False, predicted_c65=1600.0,
                      anomaly_score=0.15, threshold=1572.0, anomaly_cut=0.6)
_CRAZY_PRED = {"predicted_c65": 1600.0, "anomaly_score": 0.15, "shap_top3": ["C11", "C62"]}

# 하류 파서 규약 (mock_alert_publisher.py:52) — 언더스코어 불허
ALERT_ID_RE = r"^ALERT-\d{8}-[A-Z0-9]+-\d{4,}$"  # A33: SEQ 는 4자리 최소폭 — 8/06 실측 11,311 (5자리)

_DDL = """
CREATE TABLE spc_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL, incident_id TEXT,
    chamber_id TEXT NOT NULL, recipe_id TEXT, step INTEGER,
    sensor_window TEXT NOT NULL DEFAULT 'settled',
    rule_id TEXT NOT NULL, sensor_id TEXT NOT NULL, severity TEXT NOT NULL,
    current_value REAL, limit_version TEXT NOT NULL DEFAULT 'v1',
    control_limit_upper REAL, control_limit_lower REAL,
    context_score REAL, description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""
# sqlite엔 SUBSTRING(... FROM 정규식)이 없어 채번 SQL이 안 돈다 → 채번은 fake conn으로 검증하고
#   적재·process는 sqlite로 검증한다(각자 관심사 분리).


@pytest.fixture()
def engine():
    """sqlite in-memory + spc_violations DDL(init.sql:59~77 축약)."""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(_DDL))
    return eng


class _FakeProducer:
    """produce 호출을 기록하는 fake. fail=True면 예외를 던져 best-effort 경로를 본다."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def produce(self, topic, key=None, value=None):
        if self.fail:
            raise RuntimeError("broker down")
        self.calls.append({"topic": topic, "key": key, "value": value})


class _FakeConn:
    """채번 SQL 검증용 — scalar()가 고정 SEQ를 반환하고 바인드 파라미터를 기록."""

    def __init__(self, seq):
        self._seq = seq
        self.params = None

    def execute(self, stmt, params=None):
        self.params = params
        return self

    def scalar(self):
        return self._seq


class _Phase:
    """provisional Phase enum 흉내 (.value)."""

    def __init__(self, value):
        self.value = value


def _v(rule_id="N1", sensor_id="C11", severity="CRITICAL", members=None, **over):
    """엔진 위반 dict (nelson_engine._make_violation 정합 — 내부 키 포함)."""
    v = {"chamber_id": "SIM_CH_1", "recipe_id": "C6_0", "step": 4,
         "sensor_window": "settled", "sensor_id": sensor_id,
         "rule_id": rule_id, "severity": severity,
         "current_value": -335.0,
         "control_limit_upper": -115.037, "control_limit_lower": -328.753,
         "limit_version": "v1", "limit_basis": "firm",
         "description": f"{rule_id}: 1점 창 (C64_10231)",
         "wafer_id": "C64_10231", "timestamp": "2026-07-28T01:00:00+00:00",
         "offending": [], "member_wafers": members if members is not None else ["C64_10231"]}
    v.update(over)
    return v


def _joined(violations=None, tttm=None, prediction=None, **over):
    """joined 이벤트 (collector.build_joined 스키마 정합)."""
    j = {"wafer_id": "C64_10231", "chamber_id": "SIM_CH_1", "recipe_id": "C6_0",
         "ts": "2026-07-28T01:00:00+00:00",
         "violations": violations if violations is not None else [_v()],
         "tttm": tttm, "prediction": prediction, "is_transient": False, "is_qual": False}
    j.update(over)
    return j


def _sqlite_next_id(conn, chamber_id, date="20260728"):
    """sqlite용 대체 채번 — 운영 SQL(`SUBSTRING FROM 정규식`)이 Postgres 전용이라 백엔드만 대체.

    포맷·max+1 의미는 동일하게 유지한다(하류 정규식 검증도 그대로 받는다).
    """
    from src.common.context_score.publisher import _chamber_segment

    prefix = f"ALERT-{date}-{_chamber_segment(chamber_id)}-"
    ids = conn.execute(text("SELECT alert_id FROM spc_violations WHERE chamber_id = :ch"),
                       {"ch": chamber_id}).scalars().all()
    seqs = [int(i.rsplit("-", 1)[-1]) for i in ids if i.startswith(prefix)]
    return f"{prefix}{(max(seqs) + 1 if seqs else 1):04d}"


def _deps(engine, producer=None, enabled=True, get_phase=None, sensor_map=None):
    return PublisherDeps(
        engine=engine, producer=producer,
        pub_cfg=PublisherConfig(publish_enabled=enabled, y_threshold_cache_ttl_sec=600.0),
        sensor_map=sensor_map if sensor_map is not None else {},
        get_phase=get_phase, next_id=_sqlite_next_id)


def _rows(engine):
    with engine.begin() as conn:
        return conn.execute(text("SELECT * FROM spc_violations")).mappings().all()


# ── PublisherConfig (M2-1) ───────────────────────────────────────────────────

def test_config_load_from_real_params():
    """실 params.yaml 로드 — 발행 스위치 on(2026-07-30 PM 컷오버), TTL 승격값 600.

    D-11 초기값은 off였으나 2026-07-30 PM이 `publish_enabled: true`로 컷오버(params.yaml:120
    주석). config→테스트 방향 표류라 코드는 안 깨지고 CI만 red였음 → 컷오버 현실에 맞춰 갱신.
    """
    cfg = PublisherConfig.load()
    assert cfg.publish_enabled is True                # 7/30 컷오버(구 False는 D-11 초기값)
    assert cfg.y_threshold_cache_ttl_sec == 600.0


def test_load_sensor_map_missing_file_returns_empty(tmp_path):
    """sensor_map 부재·오류 → 빈 dict. 표시층 전용이라 없어도 발행은 정상 동작해야 한다(6-4)."""
    from src.common.context_score.publisher import load_sensor_map

    assert load_sensor_map(tmp_path / "nope.yaml") == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed\n", encoding="utf-8")
    assert load_sensor_map(bad) == {}


def test_load_sensor_map_reads_real_file():
    """실 sensor_map.yaml — C코드 → 표시명 룩업."""
    from src.common.context_score.publisher import load_sensor_map

    assert load_sensor_map().get("C11") == "DC_Bias"


def test_next_alert_id_defaults_to_utc_today():
    """date 미지정 → UTC 오늘 8자리."""
    from datetime import datetime, timezone

    aid = next_alert_id(_FakeConn(1), "SIM_CH_1")
    assert aid.split("-")[1] == datetime.now(timezone.utc).strftime("%Y%m%d")


def test_config_missing_key_raises(tmp_path):
    """키 부재 → 매직넘버 대체 없이 명시 실패(6-1)."""
    p = tmp_path / "params.yaml"
    p.write_text("spc:\n  context_score_agent_min: 31\n", encoding="utf-8")
    with pytest.raises(KeyError, match="publish_enabled"):
        PublisherConfig.load(p)


# ── alert_id 채번 (M2-2 · D-10) ──────────────────────────────────────────────

def test_alert_id_format_and_chamber_underscore_stripped():
    """`SIM_CH_1` → `SIMCH1`. 하류 정규식이 언더스코어를 불허(V6)."""
    aid = next_alert_id(_FakeConn(1), "SIM_CH_1", date="20260728")
    assert aid == "ALERT-20260728-SIMCH1-0001"
    import re
    assert re.match(ALERT_ID_RE, aid)


def test_alert_id_seq_is_zero_padded_4():
    """SEQ 4자리 **최소폭** 제로패딩 — 4자리까지 pad, 초과분은 자리 그대로 (A33).

    `:04d` 는 상한이 아니다: 8/06 실측에서 챔버당 seq 가 11,311 까지 갔고
    5자리 id 4,339건이 정상 유통됐다(그룹화율 98.7%). 계약 정규식도 `\\d{4,}`.
    """
    assert next_alert_id(_FakeConn(7), "SIM_CH_2", date="20260728").endswith("-0007")
    assert next_alert_id(_FakeConn(1234), "SIM_CH_2", date="20260728").endswith("-1234")


def test_alert_id_seq_over_9999_not_truncated_or_wrapped():
    """A33 — 9999 초과 시 감기·절단 없이 5자리로 계속 (8/06 SIMCH1 11,311 실측 재현)."""
    import re
    aid = next_alert_id(_FakeConn(11311), "SIM_CH_1", date="20260806")
    assert aid == "ALERT-20260806-SIMCH1-11311"
    assert re.match(ALERT_ID_RE, aid)


def test_alert_id_query_filters_by_chamber_column_and_date_prefix():
    """챔버는 컬럼 필터(언더스코어 LIKE 와일드카드 회피), 날짜는 prefix LIKE."""
    conn = _FakeConn(1)
    next_alert_id(conn, "SIM_CH_1", date="20260728")
    assert conn.params == {"ch": "SIM_CH_1", "date_like": "ALERT-20260728-%"}


def test_alert_id_uses_max_plus_one_restart_safe():
    """SEQ는 DB max+1 — 재시작해도 이어채번(인메모리 카운터 아님)."""
    assert next_alert_id(_FakeConn(42), "SIM_CH_1", date="20260728").endswith("-0042")


# ── build_alert 조립 (M2-3) — AlertModel 함정 4종 ────────────────────────────

def test_no_wafer_id_at_top_level():
    """함정① 최상위 wafer_id 금지 — 계약 위치는 prediction_context 안."""
    alert = build_alert(_joined(), 45, "ALERT-20260728-SIMCH1-0001")
    assert "wafer_id" not in alert
    assert alert["prediction_context"]["wafer_id"] == "C64_10231"


def test_tttm_neutral_stub_has_all_fields():
    """함정② tttm None → 중립 6필드 전부(일부 누락 시 C ValidationError → alert 유실)."""
    t = build_alert(_joined(tttm=None), 45, "A")["tttm"]
    assert set(t) == {"reference", "score", "top_gap_sensor", "gap_pct",
                      "reference_suspect", "suspect_sensors"}
    assert t == {"reference": "fleet_median", "score": 0.0, "top_gap_sensor": "",
                 "gap_pct": 0.0, "reference_suspect": False, "suspect_sensors": []}


def test_tttm_db_only_fields_excluded():
    """함정② rollup의 reference_id·chamber_id는 DB 전용 — alert 유입 금지(V8)."""
    rollup = {"reference": "fleet_median", "score": 2.13, "top_gap_sensor": "C11",
              "gap_pct": 34.2, "reference_suspect": False,
              "reference_id": "v1", "chamber_id": "SIM_CH_1"}
    t = build_alert(_joined(tttm=rollup), 45, "A")["tttm"]
    assert "reference_id" not in t and "chamber_id" not in t
    assert t["score"] == 2.13 and t["top_gap_sensor"] == "C11"


def test_tttm_suspect_sensors_flows_to_alert():
    """decouple(2026-08-05): rollup suspect_sensors 가 alert tttm 으로 유입(worst≠suspect)."""
    rollup = {"reference": "fleet_median", "score": 5.0, "top_gap_sensor": "C62",
              "gap_pct": 30.0, "reference_suspect": True, "suspect_sensors": ["C17"],
              "reference_id": "v1", "chamber_id": "SIM_CH_1"}
    t = build_alert(_joined(tttm=rollup), 45, "A")["tttm"]
    assert t["suspect_sensors"] == ["C17"]
    assert t["top_gap_sensor"] == "C62"          # worst 유지(진단용)
    assert t["reference_suspect"] is True


def test_tttm_partial_rollup_filled_with_neutral():
    """rollup에 일부 키가 없어도 6필드를 채운다(계약 필수 충족). suspect_sensors는 [] 중립."""
    t = _build_tttm({"score": 3.1})
    assert len(t) == 6 and t["score"] == 3.1 and t["reference"] == "fleet_median"
    assert t["suspect_sensors"] == []


def test_tttm_none_valued_field_replaced_with_neutral():
    """키가 **있는데 값이 None** 이어도 중립값으로 대체.

    `tttm_engine._gap_pct`는 |fleet_median| <= EPS 일 때 None을 반환하고 rollup이 그대로
    싣는다 → `.get(k, default)`만 쓰면 None이 통과해 C ValidationError로 alert이 폐기된다.
    """
    t = _build_tttm({"reference": "fleet_median", "score": 2.0, "top_gap_sensor": "C11",
                     "gap_pct": None, "reference_suspect": False})
    assert t["gap_pct"] == 0.0
    assert _build_tttm({"score": None})["score"] == 0.0


def test_alert_with_none_gap_pct_still_parses():
    """위 방어가 실제로 C 파싱을 살리는지 — 회귀 가드."""
    pytest.importorskip("pydantic")
    from src.agent_service.app.schemas.alert import AlertModel

    rollup = {"reference": "fleet_median", "score": 2.13, "top_gap_sensor": "C11",
              "gap_pct": None, "reference_suspect": False}
    m = AlertModel.model_validate(build_alert(_joined(tttm=rollup), 45, "A"))
    assert m.tttm.gap_pct == 0.0


def test_timestamp_is_wafer_t0_not_now():
    """함정③ timestamp = joined ts(wafer t0). now() 쓰면 Incident 병합 정렬이 깨진다."""
    alert = build_alert(_joined(ts="2026-07-28T01:00:00+00:00"), 45, "A")
    assert alert["timestamp"] == "2026-07-28T01:00:00+00:00"


def test_shap_object_form_converted_to_c_codes():
    """함정④ §2 객체형 → §3 C코드 리스트(V7). 순서 보존(SHAP 순위 무개입·3-3)."""
    pred = {"predicted_c65": 1500.0,
            "shap_top3": [{"sensor": "C11", "name": "DC_Bias", "contribution": 0.9},
                          {"sensor": "C62", "name": "RF_Vpp", "contribution": 0.5},
                          {"sensor": "C17", "name": "Chamber_Temp", "contribution": 0.3}]}
    ctx = build_alert(_joined(prediction=pred), 45, "A")["prediction_context"]
    assert ctx["shap_top3"] == ["C11", "C62", "C17"]


def test_shap_string_form_passthrough():
    """이미 C코드 문자열 리스트면 그대로."""
    assert _shap_to_sensor_codes(["C11", "C62"]) == ["C11", "C62"]


def test_shap_malformed_elements_dropped_no_crash():
    """비-dict·키 결측 원소는 버린다(계약 위반 입력이 alert 전체를 죽이지 않게)."""
    assert _shap_to_sensor_codes([{"sensor": "C11"}, None, 42, {"x": 1}]) == ["C11"]
    assert _shap_to_sensor_codes(None) == []


# ── violations 매핑 ──────────────────────────────────────────────────────────

def test_violation_key_renames():
    """sensor_id→sensor · sensor_window→window (계약 키명)."""
    v = build_alert(_joined(), 45, "A")["violations"][0]
    assert v["sensor"] == "C11" and v["window"] == "settled"
    assert "sensor_id" not in v and "sensor_window" not in v


def test_violation_engine_internal_keys_excluded():
    """엔진 내부 키(limit_basis·offending·member_wafers·wafer_id·timestamp)는 안 싣는다."""
    v = build_alert(_joined(), 45, "A")["violations"][0]
    for k in ("limit_basis", "offending", "member_wafers", "wafer_id", "timestamp",
              "chamber_id", "recipe_id", "step"):
        assert k not in v


def test_sensor_name_attached_only_when_mapped():
    """sensor_name은 sensor_map에 있을 때만 병기(표시층 전용·6-4). 없으면 생략."""
    with_map = build_alert(_joined(), 45, "A", sensor_map={"C11": "DC_Bias"})
    assert with_map["violations"][0]["sensor_name"] == "DC_Bias"
    assert "sensor_name" not in build_alert(_joined(), 45, "A")["violations"][0]


def test_severity_carried_not_recalculated():
    """severity는 엔진 값 운반만(D-9) — publisher가 계산하지 않는다."""
    alert = build_alert(_joined(violations=[_v("N3", severity="WARNING")]), 45, "A")
    assert alert["violations"][0]["severity"] == "WARNING"


def test_context_score_is_passenger_int():
    """context_score는 주입값 그대로·int(계약 0~100). 재계산 없음(D-9)."""
    assert build_alert(_joined(), 72, "A")["context_score"] == 72


# ── suspect_window (§5-1) ────────────────────────────────────────────────────

def test_suspect_window_union_and_dedup():
    """여러 위반의 member_wafers 합집합·dedup(Q1) — 룰마다 창 크기가 달라 최대 커버."""
    vs = [_v("N1", members=["C64_10"]), _v("N3", sensor_id="C17", members=["C64_8", "C64_9", "C64_10"])]
    w = _build_suspect_window(vs)
    assert w["member_wafers"] == ["C64_8", "C64_9", "C64_10"]


def test_suspect_window_numeric_sort_not_string():
    """numeric 정렬 — 문자열 정렬이면 `C64_1013 < C64_995`로 시간 역전(§5-1 ⚠️)."""
    vs = [_v(members=["C64_1013_CH_3", "C64_995_CH_3"])]
    w = _build_suspect_window(vs)
    assert w["member_wafers"] == ["C64_995_CH_3", "C64_1013_CH_3"]
    assert w["start_wafer"] == "C64_995_CH_3" and w["end_wafer"] == "C64_1013_CH_3"


def test_wafer_seq_uses_first_numeric_segment():
    """접미 `_CH_x` 가 있어도 첫 숫자 세그먼트를 쓴다(끝자리 파싱은 CH 번호를 집음)."""
    assert _wafer_seq("C64_1013_CH_3")[:2] == (0, 1013)
    assert _wafer_seq("C64_995")[:2] == (0, 995)


def test_wafer_seq_unparseable_falls_back_without_crash():
    """숫자 세그먼트가 없으면 원문 fallback — 크래시 대신 결정적 순서."""
    assert _wafer_seq("WEIRD")[0] == 1
    assert sorted(["WEIRD", "C64_5"], key=_wafer_seq) == ["C64_5", "WEIRD"]


def test_suspect_window_omitted_when_no_members():
    """member_wafers 없으면 필드 자체를 생략(Optional)."""
    alert = build_alert(_joined(violations=[_v(members=[])]), 45, "A")
    assert "suspect_window" not in alert


def test_suspect_window_range_matches_member_list():
    """start/end = 정렬 합집합의 min/max — 범위와 목록이 항상 정합."""
    w = _build_suspect_window([_v(members=["C64_3", "C64_1", "C64_2"])])
    assert (w["start_wafer"], w["end_wafer"]) == ("C64_1", "C64_3")


# ── prediction_context ───────────────────────────────────────────────────────

def test_prediction_missing_neutral_stub():
    """예측 결측이어도 계약 필수라 생략 불가 → 중립 스텁(predicted_c65=0.0·shap 빈 리스트)."""
    ctx = build_alert(_joined(prediction=None), 45, "A")["prediction_context"]
    assert ctx["predicted_c65"] == 0.0 and ctx["shap_top3"] == []
    assert ctx["wafer_id"] == "C64_10231"


def test_spc_flags_parallel_channel():
    """spc_flags는 SHAP와 독립된 병렬 채널(헌법 3-3) — 위반에서 그대로 생성."""
    vs = [_v("N1", sensor_id="C11"), _v("N3", sensor_id="C17")]
    ctx = build_alert(_joined(violations=vs), 45, "A")["prediction_context"]
    assert ctx["spc_flags"] == [{"sensor": "C11", "rule": "N1"}, {"sensor": "C17", "rule": "N3"}]


def test_anomaly_score_additive_when_present():
    """anomaly_score는 additive(D-15) — 예측에 있으면 첨부, 없으면 키 자체 없음."""
    with_an = build_alert(_joined(prediction={"predicted_c65": 1.0, "anomaly_score": 0.5}),
                          45, "A")["prediction_context"]
    assert with_an["anomaly_score"] == 0.5
    without = build_alert(_joined(prediction={"predicted_c65": 1.0}), 45, "A")["prediction_context"]
    assert "anomaly_score" not in without


def test_non_numeric_predicted_c65_falls_back_to_zero():
    """비수치 predicted_c65 → 0.0 (계약 float 필수 — ValidationError 방지)."""
    ctx = build_alert(_joined(prediction={"predicted_c65": "N/A"}), 45, "A")["prediction_context"]
    assert ctx["predicted_c65"] == 0.0


# ── spc_violations 적재 (M2-4) ───────────────────────────────────────────────

def test_insert_one_row_per_violation_sharing_alert_id(engine):
    """위반 1건 = 1행, 같은 alert_id 공유(1 알람 = N 행)."""
    j = _joined(violations=[_v("N1", sensor_id="C11"), _v("N3", sensor_id="C17")])
    with engine.begin() as conn:
        assert insert_violations(conn, j, "ALERT-20260728-SIMCH1-0001", 45) == 2
    rows = _rows(engine)
    assert len(rows) == 2
    assert {r["alert_id"] for r in rows} == {"ALERT-20260728-SIMCH1-0001"}


def test_insert_columns_match_schema(engine):
    """init.sql 컬럼 정합 — 값이 제자리에 들어갔는지."""
    with engine.begin() as conn:
        insert_violations(conn, _joined(), "A1", 45)
    r = _rows(engine)[0]
    assert r["chamber_id"] == "SIM_CH_1" and r["recipe_id"] == "C6_0" and r["step"] == 4
    assert r["rule_id"] == "N1" and r["sensor_id"] == "C11" and r["severity"] == "CRITICAL"
    assert r["limit_version"] == "v1" and r["sensor_window"] == "settled"
    assert r["context_score"] == 45.0


def test_insert_empty_violations_is_noop(engine):
    """위반 0건 → 0행(크래시 없음)."""
    with engine.begin() as conn:
        assert insert_violations(conn, _joined(violations=[]), "A1", 45) == 0
    assert _rows(engine) == []


def test_insert_accepts_injected_violations(engine):
    """violations 주입 경로 — 합성 마커 적재(M3)를 위한 seam."""
    with engine.begin() as conn:
        insert_violations(conn, _joined(), "A1", 99, violations=[_v("B9", sensor_id="C65")])
    r = _rows(engine)[0]
    assert r["rule_id"] == "B9" and r["sensor_id"] == "C65" and r["context_score"] == 99.0


# ── process 진입점 (M2-5) ────────────────────────────────────────────────────

def test_phase_0_suppresses_both_insert_and_publish(engine):
    """D-8 Phase 0 → 적재·발행 둘 다 skip + 챔버별 카운터."""
    prod = _FakeProducer()
    counters = PublisherCounters()
    out = process(_joined(), 45, None,
                  _deps(engine, prod, get_phase=lambda ch: _Phase("phase_0")), counters)
    assert out == [] and prod.calls == [] and _rows(engine) == []
    assert counters.suppressed == {"SIM_CH_1": 1}


def test_phase_1_publishes_normally(engine):
    """Phase 1 이후는 정상 경로."""
    prod = _FakeProducer()
    out = process(_joined(), 45, None,
                  _deps(engine, prod, get_phase=lambda ch: _Phase("phase_1")))
    assert len(out) == 1 and len(prod.calls) == 1


def test_get_phase_absent_means_no_suppression(engine):
    """get_phase 미주입 → 억제 없음(발행 진행)."""
    prod = _FakeProducer()
    assert len(process(_joined(), 45, None, _deps(engine, prod))) == 1


def test_get_phase_failure_does_not_block_alert(engine):
    """억제 판정이 실패해도 알람은 나간다(억제는 최적화지 필수 경로가 아님)."""
    def _boom(ch):
        raise RuntimeError("provisional down")

    prod = _FakeProducer()
    assert len(process(_joined(), 45, None, _deps(engine, prod, get_phase=_boom))) == 1


def test_tttm_only_wafer_skips_publish(engine):
    """D-9 위반 0 ∧ crazy 없음(TTTM 단독) → 발행·적재 skip."""
    prod = _FakeProducer()
    j = _joined(violations=[], tttm={"score": 3.1, "reference_suspect": False})
    assert process(j, 30, None, _deps(engine, prod)) == []
    assert prod.calls == [] and _rows(engine) == []


def test_publish_switch_off_still_assembles_and_inserts(engine):
    """D-11 스위치 off = send만 skip — 조립·적재는 정상 수행(dry-run)."""
    prod = _FakeProducer()
    out = process(_joined(), 45, None, _deps(engine, prod, enabled=False))
    assert prod.calls == []                      # send 0회
    assert len(out) == 1 and len(_rows(engine)) == 1   # 적재는 됨


def test_insert_precedes_publish_record_survives_send_failure(engine):
    """D-12 발행이 터져도 기록은 남는다(적재가 먼저·독립 try)."""
    prod = _FakeProducer(fail=True)
    counters = PublisherCounters()
    out = process(_joined(), 45, None, _deps(engine, prod), counters)
    assert len(_rows(engine)) == 1               # 기록 = must
    assert counters.publish_failed == 1 and counters.published == 0
    assert out == ["ALERT-" + out[0].split("-", 1)[1]]   # alert_id는 채번됨


def test_publish_payload_shape(engine):
    """토픽·키·직렬화 형태 — key=chamber_id, value=JSON."""
    prod = _FakeProducer()
    process(_joined(), 45, None, _deps(engine, prod))
    call = prod.calls[0]
    assert call["topic"] == TOPIC_ALERT and call["key"] == "SIM_CH_1"
    payload = json.loads(call["value"])
    assert payload["context_score"] == 45 and payload["chamber_id"] == "SIM_CH_1"


def test_db_and_alert_carry_same_context_score(engine):
    """passenger — DB와 alert이 같은 값(재계산 없음, D-9)."""
    prod = _FakeProducer()
    process(_joined(), 72, None, _deps(engine, prod))
    assert json.loads(prod.calls[0]["value"])["context_score"] == 72
    assert _rows(engine)[0]["context_score"] == 72.0


def test_counters_accumulate_across_calls(engine):
    """호출자가 카운터를 주입하면 누적된다(seam이 리허설 계측에 사용)."""
    prod = _FakeProducer()
    counters = PublisherCounters()
    deps = _deps(engine, prod)
    for _ in range(3):
        process(_joined(), 45, None, deps, counters)
    assert counters.published == 3


def test_seq_increments_across_alerts(engine):
    """SEQ가 실제로 증가하는지 — 같은 (날짜,챔버) 안에서 0001→0002→0003."""
    prod = _FakeProducer()
    deps = _deps(engine, prod)
    ids = [process(_joined(), 45, None, deps)[0] for _ in range(3)]
    assert [i[-4:] for i in ids] == ["0001", "0002", "0003"]


def test_missing_pub_cfg_is_fail_safe_no_publish(engine):
    """cfg 미주입 = 발행 안 함(fail-safe). params 기본값(false)과 코드 기본값을 일치시킨다.

    반대(fail-open)면 M4 seam이 pub_cfg 주입을 빠뜨렸을 때 mock 가동 중 이중 발행이
    조용히 난다(§11·E2).
    """
    prod = _FakeProducer()
    deps = PublisherDeps(engine=engine, producer=prod, next_id=_sqlite_next_id)
    out = process(_joined(), 45, None, deps)
    assert prod.calls == []                      # 발행 0회
    assert len(out) == 1 and len(_rows(engine)) == 1   # 조립·적재는 수행


def test_insert_failure_counted_and_publish_continues(engine, caplog):
    """적재 실패 → insert_failed 카운터 + ERROR(SEQ 재사용 경고), 발행은 계속(best-effort 분리)."""
    prod = _FakeProducer()
    counters = PublisherCounters()
    bad = _joined(violations=[_v(severity=None)])          # NOT NULL 위반 유도
    with caplog.at_level("ERROR"):
        process(bad, 45, None, _deps(engine, prod), counters)
    assert counters.insert_failed == 1
    assert any("SEQ 재사용" in r.getMessage() for r in caplog.records)
    assert len(prod.calls) == 1                             # 발행은 진행


# ── C 관점 최종 검증 — 실제 AlertModel 파싱 ──────────────────────────────────

def test_alert_parses_with_real_alert_model():
    """조립 산출이 C의 AlertModel로 **실제 파싱**되는지(계약 정합 최종 관문)."""
    pydantic = pytest.importorskip("pydantic")          # noqa: F841
    from src.agent_service.app.schemas.alert import AlertModel

    alert = build_alert(
        _joined(tttm={"reference": "fleet_median", "score": 2.13, "top_gap_sensor": "C11",
                      "gap_pct": 34.2, "reference_suspect": False,
                      "reference_id": "v1", "chamber_id": "SIM_CH_1"},
                prediction={"predicted_c65": 1500.0,
                            "shap_top3": [{"sensor": "C11", "name": "DC_Bias"}],
                            "anomaly_score": 0.5}),
        45, "ALERT-20260728-SIMCH1-0001", sensor_map={"C11": "DC_Bias"})
    m = AlertModel.model_validate(alert)
    assert m.alert_id == "ALERT-20260728-SIMCH1-0001"
    assert m.context_score == 45 and m.max_severity == "CRITICAL"
    assert m.prediction_context.wafer_id == "C64_10231"
    assert m.suspect_window is not None


def test_alert_parses_when_prediction_and_tttm_missing():
    """최악 입력(예측·TTTM 둘 다 결측)에서도 파싱 통과 — 중립 스텁이 계약을 채운다."""
    pytest.importorskip("pydantic")
    from src.agent_service.app.schemas.alert import AlertModel

    m = AlertModel.model_validate(
        build_alert(_joined(tttm=None, prediction=None), 0, "ALERT-20260728-SIMCH1-0002"))
    assert m.tttm.reference == "fleet_median" and m.prediction_context.predicted_c65 == 0.0


# ── 실 Postgres 통합 — 운영 채번 SQL 검증 ────────────────────────────────────
# `_NEXT_ALERT_SEQ` 는 Postgres 전용 문법(`SUBSTRING(x FROM 정규식)`)이라 sqlite 로는
# 실행 자체가 안 된다. 위 단위 테스트들은 fake conn·대체 구현으로 **포맷과 의미**만 보므로,
# SQL 문자열 자체(컬럼명·정규식·CAST)는 여기서만 검증된다. DB 없으면 skip(리포 관례).
from src.common.context_score.tests.db_schema_isolation import db_available, isolated_schema

pg_only = pytest.mark.skipif(not db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")


@pytest.fixture()
def pg_engine():
    """실 Postgres 엔진 — **전용 스키마의 `spc_violations` 사본**만 본다 (TSR-0003).

    ⚠️ 이 fixture 는 `DELETE FROM spc_violations`(WHERE 없음)로 앞뒤를 정리한다. 채번
    SQL(`max+1`)이 빈 테이블 전제라 정리가 필요한데, **public 에서 돌면 라이브 전량 삭제**다
    (2026-08-09 실측: 61,503행 → 0. `pg_only` 는 `DATABASE_URL` 이 붙으면 통과하므로 스택을
    켠 로컬에서 그대로 발동한다). 예외가 안 나서 테스트는 초록으로 통과하고 라이브만 빈다.

    그래서 정리를 얌전히 하는 대신 **보는 테이블을 바꾼다** — `isolated_schema` 가 사본을
    만들고 `search_path` 로 그쪽을 먼저 보게 하며, 격리 여부를 조회로 확인한 뒤 넘겨준다.
    """
    with isolated_schema(["spc_violations"]) as eng:
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM spc_violations"))    # 사본 — 채번 max+1 전제
        yield eng


@pg_only
def test_pg_next_alert_id_sql_runs_and_starts_at_one(pg_engine):
    """운영 SQL 실행 — 빈 테이블에서 SEQ 0001(COALESCE 기본값 경로)."""
    import re

    with pg_engine.begin() as conn:
        aid = next_alert_id(conn, "SIM_CH_1", date="20260728")
    assert aid == "ALERT-20260728-SIMCH1-0001"
    assert re.match(ALERT_ID_RE, aid)


@pg_only
def test_pg_next_alert_id_max_plus_one(pg_engine):
    """적재된 행이 있으면 max+1 — SUBSTRING 정규식이 SEQ 끝자리를 실제로 뽑는지."""
    with pg_engine.begin() as conn:
        a1 = next_alert_id(conn, "SIM_CH_1", date="20260728")
        insert_violations(conn, _joined(), a1, 45)
    with pg_engine.begin() as conn:
        assert next_alert_id(conn, "SIM_CH_1", date="20260728").endswith("-0002")


@pg_only
def test_pg_seq_scoped_per_chamber_and_date(pg_engine):
    """SEQ 범위 = (날짜, 챔버). 다른 챔버·다른 날짜는 독립 채번.

    챔버는 컬럼 필터라 `SIM_CH_1` 의 언더스코어가 LIKE 와일드카드로 오매치되지 않는다.
    """
    with pg_engine.begin() as conn:
        insert_violations(conn, _joined(), next_alert_id(conn, "SIM_CH_1", date="20260728"), 45)
    with pg_engine.begin() as conn:
        assert next_alert_id(conn, "SIM_CH_2", date="20260728").endswith("-0001")   # 타 챔버
        assert next_alert_id(conn, "SIM_CH_1", date="20260729").endswith("-0001")   # 타 날짜


@pg_only
def test_pg_seq_is_restart_safe(pg_engine):
    """새 엔진(=재시작)으로도 이어채번 — 인메모리 카운터가 아님(D-10)."""
    with pg_engine.begin() as conn:
        for _ in range(2):
            insert_violations(conn, _joined(), next_alert_id(conn, "SIM_CH_1", date="20260728"), 45)
    # 새 엔진은 **같은 스키마**를 봐야 한다 — `DATABASE_URL` 그대로 쓰면 격리 사본이 아니라
    #   public 을 보게 되어 "이어채번" 대신 0001 이 나온다(TSR-0003 격리 도입 후 실측).
    #   검증 대상은 "커넥션 풀이 달라도 DB 가 SEQ 를 들고 있는가"이므로 스키마는 동일해야 한다.
    fresh = create_engine(pg_engine.url.render_as_string(hide_password=False))
    try:
        with fresh.begin() as conn:
            assert next_alert_id(conn, "SIM_CH_1", date="20260728").endswith("-0003")
    finally:
        fresh.dispose()


@pg_only
def test_pg_insert_matches_real_schema(pg_engine):
    """실 spc_violations 스키마에 INSERT — 컬럼명·타입·NOT NULL 정합(sqlite 축약본이 못 잡는 것)."""
    j = _joined(violations=[_v("N1", sensor_id="C11"), _v("N3", sensor_id="C17")])
    with pg_engine.begin() as conn:
        assert insert_violations(conn, j, "ALERT-20260728-SIMCH1-0001", 45) == 2
    with pg_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT alert_id, chamber_id, recipe_id, step, sensor_window, rule_id, sensor_id, "
            "severity, current_value, limit_version, control_limit_upper, control_limit_lower, "
            "context_score, description, incident_id FROM spc_violations ORDER BY sensor_id"
        )).mappings().all()
    assert len(rows) == 2
    assert {r["alert_id"] for r in rows} == {"ALERT-20260728-SIMCH1-0001"}
    assert rows[0]["sensor_id"] == "C11" and rows[1]["sensor_id"] == "C17"
    assert rows[0]["context_score"] == 45.0 and rows[0]["step"] == 4
    assert rows[0]["incident_id"] is None          # grouper 몫 — B는 안 채운다


@pg_only
def test_pg_end_to_end_process_inserts_and_assembles(pg_engine):
    """process 전 구간을 실 DB로 — 채번 → 적재 → 조립 → (스위치 off라) 발행 skip."""
    prod = _FakeProducer()
    deps = PublisherDeps(
        engine=pg_engine, producer=prod,
        pub_cfg=PublisherConfig(publish_enabled=False, y_threshold_cache_ttl_sec=600.0))
    out = process(_joined(), 45, None, deps)
    assert len(out) == 1 and prod.calls == []      # dry-run
    with pg_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM spc_violations")).scalar() == 1


# ── M3: crazy 발행 경로 (build_crazy_violation·insert_spc_violation·process 합류) ──
def test_insert_spc_violation_marker_row(engine):
    """insert_spc_violation → 마커 1행(rule_id=B9·sensor=C65·lower=0.0·predicted arm 값)."""
    with engine.begin() as conn:
        n = insert_spc_violation(conn, _joined(violations=[]), "ALERT-20260728-SIMCH1-0001", 55, _CRAZY)
    assert n == 1
    r = _rows(engine)[0]
    assert r["rule_id"] == "B9" and r["sensor_id"] == "C65"
    assert r["control_limit_lower"] == 0.0 and r["current_value"] == 1600.0
    assert r["severity"] == "CRITICAL"


def test_process_crazy_only_issues_one_alert(engine):
    """crazy-only(위반 0) → 마커 alert 1건 조립·적재·발행 + crazy_detected 카운터."""
    prod = _FakeProducer()
    counters = PublisherCounters()
    issued = process(_joined(violations=[], prediction=_CRAZY_PRED), 55, _CRAZY,
                     _deps(engine, producer=prod), counters)
    assert len(issued) == 1 and counters.crazy_detected == 1
    rows = _rows(engine)
    assert len(rows) == 1 and rows[0]["rule_id"] == "B9"
    assert len(prod.calls) == 1
    sent = json.loads(prod.calls[0]["value"])
    assert sent["violations"][0]["rule_id"] == "B9" and sent["violations"][0]["sensor"] == "C65"


def test_process_nelson_and_crazy_two_separate_alerts(engine):
    """같은 wafer에 Nelson 위반 + crazy → alert 2건·SEQ 2개 (합치지 않음, D-13)."""
    counters = PublisherCounters()
    issued = process(_joined(violations=[_v()], prediction=_CRAZY_PRED), 55, _CRAZY,
                     _deps(engine, producer=_FakeProducer()), counters)
    assert len(issued) == 2 and len(set(issued)) == 2      # 별도 alert·SEQ 2개
    assert {r["rule_id"] for r in _rows(engine)} == {"N1", "B9"}


def test_process_crazy_suppressed_in_phase_0(engine):
    """Phase 0 억제는 crazy에도 적용 — 적재·발행 0 (Q4 v0 = 억제 포함)."""
    counters = PublisherCounters()
    issued = process(_joined(violations=[], prediction=_CRAZY_PRED), 55, _CRAZY,
                     _deps(engine, producer=_FakeProducer(), get_phase=lambda ch: _Phase("phase_0")),
                     counters)
    assert issued == [] and _rows(engine) == []
