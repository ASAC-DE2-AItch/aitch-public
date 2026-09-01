"""B4-1 Step5b — B9 crazy 판정(detect 코어) + y_thresholds 캐시 단위 테스트.

스펙: Docs/B4-1_Step4_5_5b_통합구현계획서_v2.md (원 스펙은 Docs/spec/_archive/B4-1_Step5b_B9_crazy_wafer_스펙플랜.md §3·§5·§6).
순수 함수·주입식(fake reader/clock/y_cache) — 브로커·DB 불요. 발행(M3)의 봉투 조립(build_crazy_violation)은
순수라 여기서 검증하고, DB·process 배선(insert_spc_violation·process crazy 분기)은 test_publisher에서 검증한다.
"""
from __future__ import annotations

import pytest

from src.agent_service.app.schemas.alert import AlertModel   # C 계약 — 마커 봉투 파싱 검증(리뷰2)
from src.common.context_score.publisher import (
    CrazyConfig, CrazyVerdict, YThresholdCache, _anomaly_is_crazy, is_crazy,
    build_crazy_violation,
)

# CrazyConfig: predicted fallback 1572 · anomaly cut = 3.0 × 0.2 = 0.6
_CFG = CrazyConfig(predicted_c65_threshold=1572.0, anomaly_multiplier=3.0, ae_anomaly_threshold=0.2)


class _FakeYCache:
    """y_cache 스텁 — p99(ch, rc) 고정값 반환 + 호출 기록."""

    def __init__(self, value):
        self._v = value
        self.calls: list = []

    def p99(self, chamber_id, recipe_id):
        self.calls.append((chamber_id, recipe_id))
        return self._v


def _pred(pc=None, an=None):
    return {"predicted_c65": pc, "anomaly_score": an, "shap_top3": [["C11", 0.9]]}


# ── CrazyConfig ──────────────────────────────────────────────────────────────
def test_crazy_config_anomaly_cut_is_product():
    """anomaly 컷 = multiplier × C6 (매직넘버 아님·D2). 3.0×0.2=0.6."""
    assert _CFG.anomaly_cut == pytest.approx(0.6)


def test_crazy_config_load_from_params():
    """params.yaml 실로드 — predicted fallback 1572 · cut 0.6 (합의안 값)."""
    cfg = CrazyConfig.load()
    assert cfg.predicted_c65_threshold == pytest.approx(1572.0)
    assert cfg.anomaly_cut == pytest.approx(0.6)


# ── predicted arm ────────────────────────────────────────────────────────────
def test_predicted_arm_hit_uses_y_p99():
    """pc > 챔버×recipe P99 → crazy(pred_hit). threshold = P99."""
    yc = _FakeYCache(1572.0)
    v = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, yc)
    assert isinstance(v, CrazyVerdict) and v.pred_hit and not v.an_hit
    assert v.threshold == 1572.0 and v.predicted_c65 == 1600.0
    assert yc.calls == [("SIM_CH_1", "C6_0")]


def test_predicted_arm_below_p99_none():
    """pc ≤ P99 → 미발동(anomaly도 없으면 None)."""
    assert is_crazy(_pred(pc=1500.0), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0)) is None


def test_predicted_arm_falls_back_to_config_when_no_threshold():
    """y_thresholds 부재(P99 None) → config fallback 1572 사용."""
    v = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(None))
    assert isinstance(v, CrazyVerdict) and v.pred_hit and v.threshold == 1572.0


def test_predicted_arm_fallback_when_y_cache_none():
    """y_cache 미주입(None)이어도 fallback 경로로 무크래시."""
    v = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, None)
    assert isinstance(v, CrazyVerdict) and v.threshold == 1572.0


# ── anomaly arm ──────────────────────────────────────────────────────────────
def test_anomaly_arm_hit_above_cut():
    """an > 0.6 → crazy(an_hit). predicted 없어도 OR로 발동."""
    v = is_crazy(_pred(an=0.7), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(None))
    assert isinstance(v, CrazyVerdict) and v.an_hit and not v.pred_hit and v.anomaly_score == 0.7


def test_anomaly_arm_dummy_below_cut_none():
    """현 라이브 dummy(0.15) < 0.6 → 무발동(구조적 오발동 불가·A3-3 후 실동작)."""
    assert is_crazy(_pred(an=0.15), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(None)) is None


def test_anomaly_is_crazy_helper_strict_gt():
    """_anomaly_is_crazy 격리 — 컷 경계(0.6)는 strict >(FAIL 아님)."""
    assert _anomaly_is_crazy(0.7, _CFG) is True
    assert _anomaly_is_crazy(0.6, _CFG) is False       # 경계 미포함
    assert _anomaly_is_crazy(0.5, _CFG) is False
    assert _anomaly_is_crazy(None, _CFG) is False
    assert _anomaly_is_crazy("x", _CFG) is False        # 비수치 → False(raise 금지)


# ── OR 결합 ──────────────────────────────────────────────────────────────────
def test_or_both_arms_hit():
    """둘 다 충족 → pred_hit·an_hit 모두 True."""
    v = is_crazy(_pred(pc=1600.0, an=0.7), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0))
    assert v.pred_hit and v.an_hit


# ── total (무크래시) ──────────────────────────────────────────────────────────
def test_total_prediction_none():
    """예측 결측(None) → None(무크래시)."""
    assert is_crazy(None, "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0)) is None


def test_total_non_numeric_prediction_values():
    """pc·an 비수치 → 각 arm False, raise 금지(total)."""
    assert is_crazy(_pred(pc="high", an=None), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0)) is None
    assert is_crazy({"predicted_c65": None, "anomaly_score": None}, "SIM_CH_1", "C6_0",
                    _CFG, _FakeYCache(1572.0)) is None


# ── YThresholdCache ──────────────────────────────────────────────────────────
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _FakeReader:
    def __init__(self, value):
        self._v = value                                # {"p95":.., "p99":..} | None (쌍 계약)
        self.calls = 0

    def __call__(self, chamber_id, recipe_id):
        self.calls += 1
        return self._v


_PAIR = {"p95": 1200.0, "p99": 1572.0}                 # 활성 행 {p95, p99} 쌍


def test_y_cache_delegates_and_caches():
    """첫 조회는 reader 호출, TTL 내 재조회는 캐시(호출 1회)."""
    r = _FakeReader(_PAIR)
    cache = YThresholdCache(r, ttl_seconds=600.0, clock=_Clock())
    assert cache.p99("SIM_CH_1", "C6_0") == 1572.0
    assert cache.p99("SIM_CH_1", "C6_0") == 1572.0
    assert r.calls == 1                                # 캐시 hit — reader 재호출 없음


def test_y_cache_p95_and_p99_from_one_fetch():
    """p95·p99는 같은 행 → reader 1회 호출로 둘 다 공급(DB 왕복 절약)."""
    r = _FakeReader(_PAIR)
    cache = YThresholdCache(r, ttl_seconds=600.0, clock=_Clock())
    assert cache.p95("SIM_CH_1", "C6_0") == 1200.0     # Qual·pred enrichment 임계
    assert cache.p99("SIM_CH_1", "C6_0") == 1572.0     # crazy predicted arm 컷
    assert r.calls == 1                                # 쌍 캐시 — 한 번만 fetch


def test_y_cache_p95_none_when_absent():
    """행 부재(reader None) → p95·p99 모두 None(호출부 fallback)."""
    r = _FakeReader(None)
    cache = YThresholdCache(r, ttl_seconds=600.0, clock=_Clock())
    assert cache.p95("SIM_CH_1", "C6_0") is None
    assert cache.p99("SIM_CH_1", "C6_0") is None


def test_y_cache_ttl_expiry_refetches():
    """TTL 초과 → reader 재호출(out-of-consumer 재수립·재시작 backstop)."""
    r = _FakeReader(_PAIR)
    clk = _Clock()
    cache = YThresholdCache(r, ttl_seconds=600.0, clock=clk)
    cache.p99("SIM_CH_1", "C6_0")
    clk.t = 601.0
    cache.p99("SIM_CH_1", "C6_0")
    assert r.calls == 2


def test_y_cache_invalidate_key_and_all():
    """재수립 시 무효화 — (ch,rc) 단건 or 전체."""
    r = _FakeReader(_PAIR)
    cache = YThresholdCache(r, ttl_seconds=600.0, clock=_Clock())
    cache.p99("SIM_CH_1", "C6_0")
    cache.invalidate("SIM_CH_1", "C6_0")
    cache.p99("SIM_CH_1", "C6_0")
    assert r.calls == 2
    cache.invalidate()                                 # 전체 clear
    cache.p99("SIM_CH_1", "C6_0")
    assert r.calls == 3


def test_y_cache_recipe_none_returns_none_no_read():
    """recipe 결측(None) → None(호출부 fallback), reader 미호출."""
    r = _FakeReader(_PAIR)
    cache = YThresholdCache(r, ttl_seconds=600.0, clock=_Clock())
    assert cache.p99("SIM_CH_1", None) is None
    assert cache.p95("SIM_CH_1", None) is None
    assert r.calls == 0


# ── M3: build_crazy_violation 봉투 조립 (순수) ────────────────────────────────
def _joined_crazy(pc=1600.0, an=0.15, shap=("C11", "C62", "C17")):
    """crazy-only wafer joined — 위반·tttm 없음, 예측만."""
    return {"wafer_id": "C64_995", "chamber_id": "SIM_CH_1", "recipe_id": "C6_0",
            "ts": "2026-07-20T01:00:00+00:00", "violations": [], "tttm": None,
            "prediction": {"predicted_c65": pc, "anomaly_score": an, "shap_top3": list(shap)},
            "is_transient": False, "is_qual": False}


def test_build_crazy_violation_predicted_arm_parses_as_alertmodel():
    """predicted arm 마커 봉투가 C AlertModel로 파싱 통과(리뷰2) + 마커 필드 D-14."""
    verdict = is_crazy(_pred(pc=1600.0, an=0.15), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0))
    alert = build_crazy_violation(verdict, _joined_crazy(pc=1600.0, an=0.15), 55,
                                  "ALERT-20260720-SIMCH1-0001")
    AlertModel.model_validate(alert)                      # C가 폐기하지 않음 = 핵심
    m = alert["violations"][0]
    assert m["rule_id"] == "B9" and m["sensor"] == "C65"
    assert m["window"] == "settled" and m["control_limit_lower"] == 0.0
    assert m["severity"] == "CRITICAL"
    assert m["current_value"] == 1600.0 and m["control_limit_upper"] == 1572.0  # predicted arm 값·컷
    assert alert["prediction_context"]["anomaly_score"] == 0.15                 # additive 첨부
    assert "suspect_window" not in alert                                        # 마커엔 member_wafers 없음


def test_build_crazy_violation_anomaly_only_sentinel():
    """anomaly-only(pc≤P99·an>컷) → current=anomaly_score·upper=컷·limit_version='v1' 센티넬."""
    verdict = is_crazy(_pred(pc=1500.0, an=0.7), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0))
    alert = build_crazy_violation(verdict, _joined_crazy(pc=1500.0, an=0.7), 55,
                                  "ALERT-20260720-SIMCH1-0002")
    AlertModel.model_validate(alert)
    m = alert["violations"][0]
    assert m["current_value"] == 0.7 and m["control_limit_upper"] == pytest.approx(0.6)
    assert m["limit_version"] == "v1"


# ── Q2: threshold_version 승계 (2026-07-28 확정 — 고정 센티넬 → 실값) ──────────
# 재수립(write_y_threshold)마다 v1→v2… 로 bump 되므로, 센티넬 고정이면 재수립 이후 crazy 가
# **실제 판정 컷과 다른 버전**으로 기록된다(데모 2막 재수립 → 데모4 crazy 경로에서 실제 발생).

class _VersionedYCache:
    """threshold_version 까지 주는 y_cache fake (운영 YThresholdCache 인터페이스)."""

    def __init__(self, p99, version):
        self._p99, self._ver = p99, version

    def p99(self, chamber_id, recipe_id):
        return self._p99

    def threshold_version(self, chamber_id, recipe_id):
        return self._ver


def test_verdict_carries_threshold_version():
    """is_crazy 가 판정에 쓴 컷의 출처 버전을 verdict 에 승계한다."""
    v = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, _VersionedYCache(1572.0, "v2"))
    assert v.threshold_version == "v2"


def test_marker_limit_version_uses_real_version_after_reestablish():
    """재수립 후(v2) crazy 마커의 limit_version 이 'v1' 이 아니라 실제 'v2' 여야 한다."""
    verdict = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, _VersionedYCache(1572.0, "v2"))
    alert = build_crazy_violation(verdict, _joined_crazy(pc=1600.0), 55,
                                  "ALERT-20260720-SIMCH1-0003")
    AlertModel.model_validate(alert)
    assert alert["violations"][0]["limit_version"] == "v2"


def test_marker_falls_back_to_sentinel_when_no_y_row():
    """y_thresholds 행 부재(config 1572 fallback) → 버전 미상이라 센티넬 유지."""
    verdict = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(None))
    assert verdict.threshold_version is None
    alert = build_crazy_violation(verdict, _joined_crazy(pc=1600.0), 55, "A")
    assert alert["violations"][0]["limit_version"] == "v1"


def test_version_lookup_failure_does_not_block_verdict():
    """버전 조회가 터져도 판정은 살아야 한다(버전은 기록용 부가 정보)."""
    class _Boom:
        def p99(self, c, r):
            return 1572.0

        def threshold_version(self, c, r):
            raise RuntimeError("db down")

    v = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, _Boom())
    assert v is not None and v.pred_hit and v.threshold_version is None


def test_legacy_cache_without_accessor_still_works():
    """threshold_version 접근자가 없는 캐시(구버전·fake)도 무크래시 — 센티넬로 진행."""
    v = is_crazy(_pred(pc=1600.0), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0))
    assert v.threshold_version is None


# ── AE 포화 격리 — anomaly arm 스위치 (2026-08-11, PM 승인) ──────────────────
# 왜: 라이브 anomaly_score 가 98.2% 가 정확히 1.0 이라 컷(0.6)을 상시 초과해
#   wafer 의 40~57% 가 crazy 로 판정된다. 컷 조정은 대안이 아니다(1.0 미만은 전부 발동 /
#   1.0 초과는 영구 미발동 = 잠금과 동일) → **스위치**로 끈다. predicted arm 은 불변.

_CFG_LOCKED = CrazyConfig(predicted_c65_threshold=1572.0, anomaly_multiplier=3.0,
                          ae_anomaly_threshold=0.2, use_anomaly_arm=False)


def test_anomaly_arm_locked_does_not_fire():
    """잠금 시 포화값(1.0)이 와도 crazy 가 아니다 — 이 PR 의 목적."""
    assert is_crazy(_pred(an=1.0), "SIM_CH_1", "C6_0", _CFG_LOCKED, _FakeYCache(1572.0)) is None


def test_anomaly_arm_default_is_open():
    """기본값은 True — 키 부재(구 config)에서 현행 동작이 유지돼야 한다."""
    assert _CFG.use_anomaly_arm is True
    v = is_crazy(_pred(an=1.0), "SIM_CH_1", "C6_0", _CFG, _FakeYCache(1572.0))
    assert v is not None and v.an_hit and not v.pred_hit


def test_predicted_arm_survives_lock():
    """잠금은 anomaly arm 전용 — predicted arm(> P99)은 그대로 발동한다."""
    v = is_crazy(_pred(pc=1600.0, an=1.0), "SIM_CH_1", "C6_0", _CFG_LOCKED, _FakeYCache(1572.0))
    assert v is not None and v.pred_hit
    assert not v.an_hit, "잠금 상태인데 anomaly arm 이 hit 로 기록됨"


def test_locked_config_loads_from_params():
    """실 params 의 `spc.crazy_wafer.use_anomaly_arm` 을 읽는다 — 값이 바뀌면 red(정책 고정).

    ⚠️ 실패하면 "테스트가 낡았다"가 아니라 **"격리 스위치가 바뀌었다"** 는 신호다.
    AE 포화 해소로 되돌릴 때 이 리터럴을 같은 PR 에서 갱신한다.
    """
    cfg = CrazyConfig.load()
    assert cfg.use_anomaly_arm is False, "AE 포화 격리 해제됨 — 포화율 재측정 후인가?"
