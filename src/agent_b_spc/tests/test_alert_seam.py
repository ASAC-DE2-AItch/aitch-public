"""B4-1 M4 — alert_seam.make_alert_collector 단위 테스트.

consumer 6-튜플 → build_joined → enrichment → context_score → is_crazy → publisher.process.
sqlite in-memory + fake producer/consumer — 브로커·Postgres 불요.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from src.agent_b_spc.alert_seam import make_alert_collector
from src.common.context_score.config import ContextScoreConfig
from src.common.context_score.publisher import CrazyConfig, YThresholdCache

_DDL = """
CREATE TABLE spc_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL, incident_id TEXT,
    chamber_id TEXT NOT NULL, recipe_id TEXT, step INTEGER,
    sensor_window TEXT NOT NULL DEFAULT 'settled',
    rule_id TEXT NOT NULL, sensor_id TEXT NOT NULL, severity TEXT NOT NULL,
    current_value REAL, limit_version TEXT NOT NULL DEFAULT 'v1',
    control_limit_upper REAL, control_limit_lower REAL,
    context_score REAL, description TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(_DDL))
    return eng


class _Cache:
    def __init__(self, hits=None):
        self._h = hits or {}

    def get(self, wafer_id):
        return self._h.get(wafer_id)


class _Consumer:
    """SpcConsumer 흉내 — get_phase·prediction_cache만."""

    def __init__(self, cache, phase_value=None):
        self.prediction_cache = cache
        self._phase = _Phase(phase_value) if phase_value else None

    def get_phase(self, chamber_id):
        return self._phase


class _Phase:
    def __init__(self, value):
        self.value = value


class _Producer:
    def __init__(self):
        self.calls = []

    def produce(self, topic, key=None, value=None):
        self.calls.append({"topic": topic, "key": key, "value": value})


def _next_id(conn, chamber_id, date="20260728"):
    from src.common.context_score.publisher import _chamber_segment
    prefix = f"ALERT-{date}-{_chamber_segment(chamber_id)}-"
    ids = conn.execute(text("SELECT alert_id FROM spc_violations WHERE chamber_id=:c"),
                       {"c": chamber_id}).scalars().all()
    seqs = [int(i.rsplit("-", 1)[-1]) for i in ids if i.startswith(prefix)]
    return f"{prefix}{(max(seqs) + 1 if seqs else 1):04d}"


_CS = ContextScoreConfig.load()
_CZ = CrazyConfig.load()


def _v(rule_id="N1", sensor_id="C11"):
    return {"chamber_id": "SIM_CH_1", "recipe_id": "C6_0", "step": 4,
            "sensor_window": "settled", "sensor_id": sensor_id, "rule_id": rule_id,
            "severity": "CRITICAL", "current_value": -335.0,
            "control_limit_upper": -115.0, "control_limit_lower": -328.0,
            "limit_version": "v1", "description": "N1", "wafer_id": "C64_995",
            "member_wafers": ["C64_995"]}


def _payload(violations=None, tttm=None, recipe="C6_0"):
    return ("C64_995", "SIM_CH_1", "2026-07-28T01:00:00+00:00",
            violations if violations is not None else [_v()], tttm, recipe)


def _seam(engine, cache, producer=None, phase=None, publish=True, **over):
    from src.common.context_score.publisher import PublisherConfig
    y_cache = YThresholdCache(reader=lambda ch, rc: None)   # P99 없음 → crazy fallback 1572
    return make_alert_collector(
        _Consumer(cache, phase), engine, _CS, _CZ, y_cache,
        producer=producer,
        pub_cfg=PublisherConfig(publish_enabled=publish, y_threshold_cache_ttl_sec=600.0),
        next_id=_next_id, **over)


def _rows(engine):
    with engine.begin() as conn:
        return conn.execute(text("SELECT * FROM spc_violations")).mappings().all()


def test_nelson_wafer_records_and_publishes(engine):
    """위반 wafer → 조립·적재·발행 (score는 orchestrator 산출·int)."""
    prod = _Producer()
    collect = _seam(engine, _Cache(), producer=prod)
    collect(_payload(violations=[_v()]))
    rows = _rows(engine)
    assert len(rows) == 1 and rows[0]["rule_id"] == "N1"
    assert isinstance(rows[0]["context_score"], float)   # int→FLOAT 저장
    assert len(prod.calls) == 1


def test_crazy_only_wafer_fires_marker(engine):
    """위반 0 + 예측 극단(pc>1572) → B9 마커 alert."""
    prod = _Producer()
    cache = _Cache({"C64_995": {"pred": 9999.0, "anomaly": 0.01, "shap": ["C11"]}})
    collect = _seam(engine, cache, producer=prod)
    collect(_payload(violations=[]))
    rows = _rows(engine)
    assert len(rows) == 1 and rows[0]["rule_id"] == "B9"
    assert collect.counters.crazy_detected == 1


def test_is_qual_wafer_skips_publish(engine):
    """is_qual wafer → 발행 skip + qual_skipped 카운터(Q3 v0)."""
    cache = _Cache({"C64_995": {"pred": 100.0, "is_qual": True}})
    collect = _seam(engine, cache, producer=_Producer())
    collect(_payload(violations=[_v()]))
    assert _rows(engine) == [] and collect.counters.qual_skipped == 1


def test_prediction_absent_no_crash(engine):
    """예측 결측 → enrichment skip·crazy None·Nelson만 정상(무크래시)."""
    collect = _seam(engine, _Cache(), producer=_Producer())
    collect(_payload(violations=[_v()]))
    assert len(_rows(engine)) == 1
    assert collect.join_counter.misses == 1              # 예측 miss 계측


def test_switch_off_assembles_but_no_send(engine):
    """스위치 off → 적재는 되고 발행만 skip(dry-run, D-11)."""
    prod = _Producer()
    collect = _seam(engine, _Cache(), producer=prod, publish=False)
    collect(_payload(violations=[_v()]))
    assert len(_rows(engine)) == 1 and prod.calls == []


class _SpyYCache:
    """p95/p99 호출 추적 스텁 — enrichment이 p95를 쓰는지 검증용."""

    def __init__(self, p95=None, p99=None):
        self._p95, self._p99 = p95, p99
        self.p95_calls = self.p99_calls = 0

    def p95(self, ch, rc):
        self.p95_calls += 1
        return self._p95

    def p99(self, ch, rc):
        self.p99_calls += 1
        return self._p99

    def invalidate(self, *a, **k):
        pass


def test_pred_enrichment_uses_p95_not_p99(engine):
    """pred enrichment의 p95_threshold는 p95()에서 온다(p99 아님·방향 정합)."""
    from src.common.context_score.publisher import PublisherConfig
    spy = _SpyYCache(p95=1200.0, p99=1572.0)
    cache = _Cache({"C64_995": {"pred": 100.0, "anomaly": 0.01, "shap": ["C11"]}})
    collect = make_alert_collector(
        _Consumer(cache, "PHASE_1"), engine, _CS, _CZ, spy,
        producer=_Producer(),
        pub_cfg=PublisherConfig(publish_enabled=True, y_threshold_cache_ttl_sec=600.0),
        next_id=_next_id)
    collect(_payload(violations=[_v()]))
    assert spy.p95_calls == 1                            # enrichment이 p95 조회(p99 아님)


# ── TSR-0005 이관 ① — Phase 0 억제 가시화 ────────────────────────────────────
# 사고 요지: 억제 skip 은 DEBUG 뿐이고 `counters.suppressed` 는 인메모리라 밖에서 안 보였다.
#   28시간 무음인데 판정·tttm·조인 카운터는 정상 INFO 를 냈다(2026-08-07~09 실측).

def test_phase0_first_suppression_logs_immediately(engine, caplog):
    """첫 억제는 **즉시** INFO — Phase 0 진입이 무로그 전이라 이게 유일한 시각 기록이다."""
    collect = _seam(engine, _Cache(), phase="phase_0")
    with caplog.at_level("INFO", logger="src.agent_b_spc.alert_seam"):
        collect(_payload())
    msgs = [r.message for r in caplog.records]
    assert any("phase0_suppress 시작" in m for m in msgs)
    assert any("SIM_CH_1" in m for m in msgs)
    assert collect.counters.suppressed == {"SIM_CH_1": 1}
    assert _rows(engine) == []                           # 적재도 안 됐다(D-8)


def test_phase0_suppression_does_not_log_every_wafer(engine, caplog):
    """2건째부터는 조용 — wafer 마다 찍으면 스팸(6-1). 누적은 N건마다."""
    collect = _seam(engine, _Cache(), phase="phase_0")
    collect(_payload())                                  # 1건째(시작 로그)
    with caplog.at_level("INFO", logger="src.agent_b_spc.alert_seam"):
        for _ in range(3):
            collect(_payload())
    assert not [r for r in caplog.records if "phase0_suppress" in r.message]
    assert collect.counters.suppressed == {"SIM_CH_1": 4}


def test_phase0_suppression_summary_at_interval(engine, caplog, monkeypatch):
    """N건마다 누적 요약 — 챔버별 내역을 함께 낸다."""
    monkeypatch.setattr("src.agent_b_spc.alert_seam._SUPPRESS_LOG_EVERY", 3)
    collect = _seam(engine, _Cache(), phase="phase_0")
    with caplog.at_level("INFO", logger="src.agent_b_spc.alert_seam"):
        for _ in range(3):
            collect(_payload())
    summary = [r.message for r in caplog.records if "누적" in r.message]
    assert len(summary) == 1 and "3건" in summary[0]


def test_normal_phase_emits_no_suppression_log(engine, caplog):
    """억제가 없으면 로그도 카운터도 없다 — 대조군(오탐 방지)."""
    collect = _seam(engine, _Cache(), phase=None)        # get_phase → None = 억제 없음
    with caplog.at_level("INFO", logger="src.agent_b_spc.alert_seam"):
        collect(_payload())
    assert not [r for r in caplog.records if "phase0_suppress" in r.message]
    assert collect.counters.suppressed == {}
    assert len(_rows(engine)) == 1                       # 정상 적재
