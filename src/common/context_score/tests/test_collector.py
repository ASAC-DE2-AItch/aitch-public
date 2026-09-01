"""collector.build_joined 단위 테스트 (Step 3a — 순수 조인, 브로커/캐시 불요).

스펙: Docs/spec/B4-1_Step3-4_collector_score_스펙플랜.md §4·§7·§9.
prediction_cache는 `get(wafer_id)` 인터페이스만 의존 → fake(dict/NullCache) 주입.
"""
from __future__ import annotations

import pytest

from src.common.context_score.collector import (
    build_joined, NullCache, _normalize_prediction, prediction_hit, JoinCounter,
)


def _v(sensor_window="settled", sensor_id="C11"):
    return {"rule_id": "N1", "sensor_id": sensor_id, "sensor_window": sensor_window}


def _joined(violations=None, tttm=None, cache=None, wafer="W1", chamber="SIM_CH_1",
            ts="2026-07-20T01:00:00+00:00", recipe_id=None):
    return build_joined(wafer, chamber, ts, violations or [], tttm,
                        cache or NullCache(), recipe_id=recipe_id)


def test_schema_keys_and_passthrough():
    """§4 스키마 9키(+recipe_id, B4-1 Step5b) + wafer_id·chamber_id·ts seam 전달값 그대로."""
    j = _joined(violations=[_v()], tttm={"score": 2.5})
    assert set(j) == {"wafer_id", "chamber_id", "ts", "violations",
                      "tttm", "prediction", "is_transient", "is_qual", "recipe_id"}
    assert j["wafer_id"] == "W1" and j["chamber_id"] == "SIM_CH_1"
    assert j["ts"] == "2026-07-20T01:00:00+00:00"
    assert j["violations"] == [_v()] and j["tttm"] == {"score": 2.5}


def test_recipe_id_passthrough_and_default_none():
    """recipe_id는 seam(consumer)이 raw row C6에서 주입한 값 그대로 joined 최상위 승계(리뷰②).

    crazy-only(위반 0) wafer도 recipe를 확보해 챔버×recipe P99를 정타(B4-1 Step5b D1). 미지정=None.
    """
    assert _joined(violations=[_v()], recipe_id="C6_0")["recipe_id"] == "C6_0"
    assert _joined(violations=[_v()])["recipe_id"] is None


def test_prediction_absent_none_and_qual_false():
    """예측 결측(NullCache) → prediction None·is_qual False, 진행(대기 없음, 결정③)."""
    j = _joined(violations=[_v()])
    assert j["prediction"] is None and j["is_qual"] is False


def test_prediction_normalized_to_contract_names_and_qual_inherited():
    """캐시 hit → A 축약키를 §2 계약명으로 정규화((f))·is_qual True 승계.

    A get()은 축약키({pred, drift, anomaly, shap, is_qual, ts})를 준다 →
    joined["prediction"]엔 계약명({predicted_c65, drift_score, ...})으로 담긴다.
    """
    cache = {"W1": {"pred": 1200.0, "drift": 0.5, "anomaly": 2.1,
                    "shap": [["C11", 0.9]], "is_qual": True, "ts": 123.0}}
    j = _joined(cache=cache)
    assert j["prediction"] == {"predicted_c65": 1200.0, "drift_score": 0.5,
                               "anomaly_score": 2.1, "shap_top3": [["C11", 0.9]],
                               "is_qual": True, "ts": 123.0}
    assert j["is_qual"] is True


def test_prediction_missing_short_keys_map_to_none():
    """A entry에 일부 축약키 없으면 계약명 값 None (is_qual 없음→승계 False)."""
    j = _joined(cache={"W1": {"pred": 1200.0}})
    assert j["prediction"]["predicted_c65"] == 1200.0
    assert j["prediction"]["drift_score"] is None
    assert j["prediction"]["is_qual"] is None and j["is_qual"] is False


def test_normalize_prediction_none_passthrough():
    """raw None(결측) → None (매핑 대상 없음)."""
    assert _normalize_prediction(None) is None


def test_is_transient_true_on_transient_window():
    """위반 중 sensor_window=='transient' 존재 → is_transient True (#7 리터럴)."""
    j = _joined(violations=[_v(sensor_window="settled"), _v(sensor_window="transient")])
    assert j["is_transient"] is True


def test_is_transient_false_without_transient():
    j = _joined(violations=[_v(sensor_window="settled")])
    assert j["is_transient"] is False


def test_ts_preserved_when_violations_empty_tttm_only():
    """violations=[]·TTTM 단독이어도 ts 확보(seam 전달, chamber_rollup엔 ts 없음, #8)."""
    j = _joined(violations=[], tttm={"score": 3.0})
    assert j["ts"] == "2026-07-20T01:00:00+00:00" and j["violations"] == []


def test_nullcache_get_returns_none():
    assert NullCache().get("anything") is None


# ── B2: 예측 조인 hit/miss 카운터 (build_joined 순수 유지 — 반환값 기반) ──────
def test_prediction_hit_classifies_by_return_value():
    """hit/miss는 build_joined 반환(joined["prediction"])으로 판정 — 함수 순수 유지(B2)."""
    assert prediction_hit({"prediction": {"predicted_c65": 1.0}}) is True
    assert prediction_hit({"prediction": None}) is False
    assert prediction_hit({}) is False                       # 키 결측도 miss


def test_join_counter_tallies_and_miss_rate():
    """seam이 build_joined 반환을 record → hit/miss·miss율 누적(§2-3 조인 유예 판단 입력)."""
    jc = JoinCounter()
    jc.record({"prediction": {"predicted_c65": 1.0}})
    jc.record({"prediction": None})
    jc.record({"prediction": None})
    assert (jc.hits, jc.misses, jc.total) == (1, 2, 3)
    assert jc.miss_rate == pytest.approx(2 / 3)


def test_join_counter_miss_rate_zero_when_empty():
    """표본 0 → miss율 0(무크래시)."""
    assert JoinCounter().miss_rate == 0.0
