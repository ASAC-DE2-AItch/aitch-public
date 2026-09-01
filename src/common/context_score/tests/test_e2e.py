"""B4-1 M5 — E2E 관통 (collector → context_score → publisher).

계획서 Docs/B4-1_Step4_5_5b_통합구현계획서_v2.md §5 M5 6종. build_joined(실 collector)
→ context_score(실 오케스트레이터·passenger 아님) → is_crazy → process → spc_violations 적재 +
fdc.alert 조립을 **실제 AlertModel로 파싱**(C 관점 최종 관문)까지 관통한다.

DB=sqlite in-memory, producer=fake — 브로커·Postgres 불요. seam(`alert_seam`)은 agent_b_spc라
여기서 import하지 않고 동일 배선(enrichment는 pred 축 off라 생략)을 재현한다 → common self-contained.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from src.common.context_score.collector import build_joined
from src.common.context_score.config import ContextScoreConfig
from src.common.context_score.context_score import context_score
from src.common.context_score.instrumentation import log_contribution as instr_log
from src.common.context_score.publisher import (
    CrazyConfig, PublisherConfig, PublisherCounters, PublisherDeps,
    YThresholdCache, is_crazy, process,
)

_CS = ContextScoreConfig.load()
_CZ = CrazyConfig.load()
_TS = "2026-07-28T01:00:00+00:00"
_ROLLUP = {"reference": "fleet_median", "score": 2.13, "top_gap_sensor": "C11",
           "gap_pct": 34.2, "reference_suspect": False}

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


@pytest.fixture(autouse=True)
def contrib_log(tmp_path, monkeypatch):
    """context_score 계측을 tmp 경로로 리다이렉트 — 리포 logs/ 오염 방지 + 라인 카운트 검증용."""
    import src.common.context_score.context_score as cs_mod
    path = tmp_path / "contrib.jsonl"
    monkeypatch.setattr(cs_mod, "log_contribution",
                        lambda j, s, r, f: instr_log(j, s, r, f, path=path))
    return path


class _FakeProducer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def produce(self, topic, key=None, value=None):
        if self.fail:
            raise RuntimeError("broker down")
        self.calls.append({"topic": topic, "key": key, "value": value})


class _Cache:
    """A PredictionCache 흉내 — 히트 dict(축약키) or miss(None)."""

    def __init__(self, hits=None):
        self._h = hits or {}

    def get(self, wafer_id):
        return self._h.get(wafer_id)


class _Phase:
    def __init__(self, value):
        self.value = value


def _v(rule_id="N1", sensor_id="C11", severity="CRITICAL", members=None, **over):
    v = {"chamber_id": "SIM_CH_1", "recipe_id": "C6_0", "step": 4,
         "sensor_window": "settled", "sensor_id": sensor_id, "rule_id": rule_id,
         "severity": severity, "current_value": -335.0,
         "control_limit_upper": -115.037, "control_limit_lower": -328.753,
         "limit_version": "v1", "limit_basis": "firm",
         "description": f"{rule_id}: 1점 창 (C64_10231)", "wafer_id": "C64_10231",
         "timestamp": _TS, "offending": [],
         "member_wafers": members if members is not None else ["C64_10231"]}
    v.update(over)
    return v


def _sqlite_next_id(conn, chamber_id, date="20260728"):
    """sqlite용 대체 채번 — 운영 SQL(SUBSTRING FROM 정규식)은 Postgres 전용. 포맷·의미 동일."""
    from src.common.context_score.publisher import _chamber_segment
    prefix = f"ALERT-{date}-{_chamber_segment(chamber_id)}-"
    ids = conn.execute(text("SELECT alert_id FROM spc_violations WHERE chamber_id = :ch"),
                       {"ch": chamber_id}).scalars().all()
    seqs = [int(i.rsplit("-", 1)[-1]) for i in ids if i.startswith(prefix)]
    return f"{prefix}{(max(seqs) + 1 if seqs else 1):04d}"


def _deps(engine, producer=None, enabled=True, get_phase=None):
    return PublisherDeps(
        engine=engine, producer=producer,
        pub_cfg=PublisherConfig(publish_enabled=enabled, y_threshold_cache_ttl_sec=600.0),
        sensor_map={}, get_phase=get_phase, next_id=_sqlite_next_id)


def _rows(engine):
    with engine.begin() as conn:
        return conn.execute(text("SELECT * FROM spc_violations")).mappings().all()


def _pipeline(joined, deps, counters=None, *, y_cache=None):
    """seam 배선 재현: context_score(int) → is_crazy → process. (score, issued, counters) 반환."""
    counters = counters if counters is not None else PublisherCounters()
    score = int(round(context_score(joined, _CS)))                 # 실 오케스트레이터 (D-5 int)
    verdict = is_crazy(joined.get("prediction"), joined["chamber_id"],
                       joined.get("recipe_id"), _CZ, y_cache)
    issued = process(joined, score, verdict, deps, counters)
    return score, issued, counters


# ── ① 위반+예측 wafer → AlertModel 파싱 + DB rows + contribution_log 1행 ──────
def test_e2e_violation_with_prediction(engine, contrib_log):
    cache = _Cache({"C64_10231": {"pred": 1500.0, "anomaly": 0.15,
                                  "shap": [{"sensor": "C11", "name": "DC_Bias"}],
                                  "is_qual": False}})
    joined = build_joined("C64_10231", "SIM_CH_1", _TS,
                          [_v("N1", "C11"), _v("N3", "C17")], _ROLLUP, cache, recipe_id="C6_0")
    prod = _FakeProducer()
    score, issued, _ = _pipeline(joined, _deps(engine, prod))

    rows = _rows(engine)                                            # 위반 2건 = 2행·1 alert_id
    assert len(rows) == 2 and len({r["alert_id"] for r in rows}) == 1
    assert len(issued) == 1 and len(prod.calls) == 1

    pytest.importorskip("pydantic")                                # C 관점 최종 관문
    from src.agent_service.app.schemas.alert import AlertModel
    m = AlertModel.model_validate(json.loads(prod.calls[0]["value"]))
    assert m.context_score == score and m.prediction_context.wafer_id == "C64_10231"
    assert m.prediction_context.shap_top3 == ["C11"]               # 객체형→C코드(3-3 순위 무개입)

    assert len(contrib_log.read_text(encoding="utf-8").splitlines()) == 1   # 계측 1행


# ── ② 예측 결측 wafer → SPC 무지연·ae/pred 0·prediction_context 중립 스텁 ─────
def test_e2e_prediction_absent_spc_unblocked(engine):
    joined = build_joined("C64_10231", "SIM_CH_1", _TS, [_v("N1", "C11")],
                          _ROLLUP, _Cache(), recipe_id="C6_0")     # 캐시 miss
    assert joined["prediction"] is None
    prod = _FakeProducer()
    score, issued, _ = _pipeline(joined, _deps(engine, prod))

    assert len(issued) == 1 and len(prod.calls) == 1               # SPC 알람 무지연
    ctx = json.loads(prod.calls[0]["value"])["prediction_context"]
    assert ctx["predicted_c65"] == 0.0 and ctx["shap_top3"] == []  # 중립 스텁
    assert score == int(round(context_score(joined, _CS)))         # ae/pred 0 흡수·무크래시


# ── ③ is_transient → ×0.5 할인 (전 구간) ──────────────────────────────────────
def test_e2e_transient_halves_score(engine):
    jt = build_joined("C64_10231", "SIM_CH_1", _TS, [_v("N1", "C11", sensor_window="transient")],
                      _ROLLUP, _Cache(), recipe_id="C6_0")
    assert jt["is_transient"] is True                              # build_joined 파생(창→플래그)

    # 할인 배율은 동일 settled 위반에서 격리 검증(창별 가중 혼입 회피 — 창 파생은 위에서 확인)
    jn = build_joined("C64_10231", "SIM_CH_1", _TS, [_v("N1", "C11")], _ROLLUP, _Cache(),
                      recipe_id="C6_0")
    jd = build_joined("C64_10231", "SIM_CH_1", _TS, [_v("N1", "C11")], _ROLLUP, _Cache(),
                      recipe_id="C6_0")
    jd["is_transient"] = True
    assert context_score(jd, _CS) == pytest.approx(context_score(jn, _CS) * _CS.transient_discount)


# ── ④ Phase 0 → 억제 (적재·발행 0) ────────────────────────────────────────────
def test_e2e_phase_0_suppresses(engine):
    joined = build_joined("C64_10231", "SIM_CH_1", _TS, [_v("N1", "C11")], _ROLLUP,
                          _Cache(), recipe_id="C6_0")
    prod = _FakeProducer()
    _, issued, counters = _pipeline(
        joined, _deps(engine, prod, get_phase=lambda ch: _Phase("phase_0")))
    assert issued == [] and prod.calls == [] and _rows(engine) == []
    assert counters.suppressed == {"SIM_CH_1": 1}


# ── ⑤ crazy-only wafer → B9 봉투·적재 ─────────────────────────────────────────
def test_e2e_crazy_only_marker(engine):
    cache = _Cache({"C64_10231": {"pred": 9999.0, "anomaly": 0.01, "shap": ["C11"]}})
    joined = build_joined("C64_10231", "SIM_CH_1", _TS, [], None, cache, recipe_id="C6_0")  # 위반 0
    y_cache = YThresholdCache(reader=lambda ch, rc: None)          # P99 부재 → 1572 fallback, 9999>1572
    prod = _FakeProducer()
    _, issued, counters = _pipeline(joined, _deps(engine, prod), y_cache=y_cache)

    rows = _rows(engine)
    assert len(rows) == 1 and rows[0]["rule_id"] == "B9" and rows[0]["sensor_id"] == "C65"
    assert rows[0]["control_limit_lower"] == 0.0                   # 마커 센티넬(D-14)
    assert counters.crazy_detected == 1 and len(issued) == 1

    pytest.importorskip("pydantic")
    from src.agent_service.app.schemas.alert import AlertModel
    m = AlertModel.model_validate(json.loads(prod.calls[0]["value"]))
    assert m.violations[0].rule_id == "B9" and m.violations[0].sensor == "C65"


# ── ⑥ 게이트 31 경계 — B는 게이트하지 않는다(sub-31도 발행, 게이트는 하류 C 몫) ──
def test_e2e_below_gate_still_published_by_b(engine):
    """단일 CRITICAL 위반 = 26점(< C 게이트 31). B는 점수와 무관하게 발행 — 게이트 판정은
    하류 C(`pipeline.gate_open` ≥31)의 몫이라 B에 게이트를 넣으면 안 된다(회귀 가드)."""
    joined = build_joined("C64_10231", "SIM_CH_1", _TS, [_v("N1", "C11")], _ROLLUP,
                          _Cache(), recipe_id="C6_0")              # 예측 결측 → ae/pred 0, 결정적 26
    score = int(round(context_score(joined, _CS)))
    assert score < 31                                              # C 게이트 미달

    prod = _FakeProducer()
    _, issued, _ = _pipeline(joined, _deps(engine, prod))
    assert len(issued) == 1 and len(prod.calls) == 1              # B는 그래도 발행
    assert _rows(engine)[0]["context_score"] == float(score)
