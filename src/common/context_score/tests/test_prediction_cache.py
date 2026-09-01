"""PredictionCache 반환 격리 계약 테스트 (A 담당 · 코드리뷰 §3-3).

범위: `get()` 이 돌려주는 dict 가 캐시 내부 상태와 **완전히 분리**되는지만 검증한다.
TTL·용량 축출·순서 역전 등 캐시 코어 동작은 별도 스위트 대상이다(현재 미작성 — B의
`test_spc_consumer.py` 는 `FakePredictionCache` 로 배선만 검증하므로 실제 캐시는 미커버).

계약: 반환값은 깊은 사본이다. 하류(오케스트레이터·instrumentation·리포트)가 반환 dict 를
어떻게 변조하든 캐시 원본은 불변이며, 재조회 시 항상 upsert 원본이 나온다.
"""
from __future__ import annotations

from src.common.context_score.prediction_cache import PredictionCache


def _cache(now: float = 1000.0) -> PredictionCache:
    """고정 시계 캐시 — TTL 만료가 테스트 결과에 개입하지 않도록 clock 주입."""
    return PredictionCache(ttl_seconds=600.0, max_entries=10, clock=lambda: now)


def _msg(wafer_id: str = "W1", **over) -> dict:
    """fdc.prediction 최소본 (계약 §2 변경금지 필드 + 부가 필드)."""
    base = {
        "wafer_id": wafer_id,
        "predicted_c65": 1500.0,
        "drift_score": 0.4,
        "anomaly_score": 0.3,
        "shap_top3": [{"sensor": "C11", "value": 0.5},
                      {"sensor": "C42", "value": 0.3}],
        "is_qual": False,
        "ts": 1000.0,
    }
    base.update(over)
    return base


def test_get_returns_values_and_excludes_internal_flag():
    """정상 조회 — 축약키 값 그대로, 내부 플래그 `_read` 는 노출 금지."""
    c = _cache()
    c.upsert(_msg())
    got = c.get("W1")
    assert got["pred"] == 1500.0 and got["drift"] == 0.4 and got["anomaly"] == 0.3
    assert "_read" not in got


def test_get_nested_shap_mutation_does_not_pollute_cache():
    """★ 핵심 — 반환 dict 의 중첩 `shap` 원소를 변조해도 캐시 원본은 불변 (§3-3 실증 케이스).

    1단계 사본이던 시절 이 변조가 캐시 원본까지 "HACKED" 로 오염시켰다.
    """
    c = _cache()
    c.upsert(_msg())
    first = c.get("W1")
    first["shap"][0]["sensor"] = "HACKED"
    first["shap"].append({"sensor": "INJECTED", "value": 9.9})

    second = c.get("W1")
    assert second["shap"] == [{"sensor": "C11", "value": 0.5},
                              {"sensor": "C42", "value": 0.3}]


def test_get_top_level_mutation_does_not_pollute_cache():
    """최상위 키 변조·삭제도 캐시에 새지 않는다(1단계 사본도 막던 경로 — 회귀 가드)."""
    c = _cache()
    c.upsert(_msg())
    first = c.get("W1")
    first["pred"] = 9999.0
    del first["drift"]

    second = c.get("W1")
    assert second["pred"] == 1500.0 and second["drift"] == 0.4


def test_repeated_get_returns_independent_objects():
    """두 번의 get 은 서로 독립 객체 — 한쪽 변조가 다른 쪽에 보이지 않는다.

    같은 wafer 가 재전달(at-least-once)로 두 번 조인될 때 첫 조인의 하류 변조가
    두 번째 조인 결과에 새지 않아야 한다.
    """
    c = _cache()
    c.upsert(_msg())
    a, b = c.get("W1"), c.get("W1")
    assert a["shap"] is not b["shap"]
    a["shap"][0]["value"] = -1.0
    assert b["shap"][0]["value"] == 0.5


def test_upsert_source_mutation_after_upsert_does_not_leak_into_get():
    """upsert 이후 **원본 메시지**를 변조해도 캐시 반환값은 영향받지 않는다.

    consumer 가 역직렬화 dict 를 재사용/변조하는 경우의 역방향 오염 경로.
    현재 upsert 는 값 참조를 그대로 담으므로 이 케이스는 **의도적으로 현 동작을 고정**한다 —
    깨지면 upsert 측에도 사본이 필요하다는 신호다.
    """
    c = _cache()
    msg = _msg()
    c.upsert(msg)
    msg["shap_top3"][0]["sensor"] = "MUTATED_SOURCE"

    got = c.get("W1")
    assert got["shap"][0]["sensor"] == "MUTATED_SOURCE"   # 현 동작 고정(문서화된 한계)
