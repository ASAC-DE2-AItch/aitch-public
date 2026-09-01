"""Step 7 S3 — TTTM baseline 주입(D6-a)·feed flush is_reset_point 제외(D6-b) 단위 테스트.

스펙: Docs/B4-1_Step7_정기리캘리_라이브트리거_스펙플랜.md §3 D6.
baseline 맵({gk: {center, sigma_ref, limit_version}}) 주입 시 sigma_ref·center_ref·reference_id가
레짐 수립 행을 따라간다(periodic auto 미추종·B4-2 결정 1). 맵 비면 현행(활성 행) 동작.
"""
from __future__ import annotations

from src.agent_b_spc.tests.test_tttm_engine import _engine, _gk, _lim, _pt

_GK = _gk("SIM_CH_1")


def _base(center, sigma_ref, ver):
    return {"center": center, "sigma_ref": sigma_ref, "limit_version": ver}


# ── sigma_ref / center_ref / reference_id : baseline 우선 (D6-a) ─────────────
def test_sigma_ref_baseline_wins_else_fallback():
    """baseline 주입 시 그 σ_ref, 미주입이면 활성 행 sigma(fallback)."""
    eng = _engine({_GK: _lim(sigma=10.0)}, {_GK})
    assert eng.sigma_ref(_GK) == 10.0                          # fallback(활성 행)
    eng.set_baseline({_GK: _base(5.0, 7.5, "v1")})
    assert eng.sigma_ref(_GK) == 7.5                           # baseline(레짐 행)


def test_center_ref_baseline_wins_else_fallback():
    """역방향 룰 앵커 center — baseline 우선, 미주입이면 활성 행 center."""
    eng = _engine({_GK: _lim(center=3.0)}, {_GK})
    assert eng.center_ref(_GK) == 3.0                          # fallback
    eng.set_baseline({_GK: _base(5.0, 7.5, "v2")})
    assert eng.center_ref(_GK) == 5.0                          # baseline


def test_reference_id_follows_baseline_not_active():
    """reference_id = σ_ref 출처 limit_version — baseline이면 레짐 행 버전(활성 행 아님)."""
    eng = _engine({_GK: _lim(ver="v9")}, {_GK})                # 활성 행 = periodic으로 밀린 v9
    assert eng._reference_id(_GK) == "v9"                      # fallback
    eng.set_baseline({_GK: _base(5.0, 7.5, "v1")})
    assert eng._reference_id(_GK) == "v1"                      # 레짐 수립 행


def test_empty_baseline_is_current_behavior():
    """set_baseline({}) → 전량 fallback(현행). 무회귀 가드."""
    eng = _engine({_GK: _lim(sigma=2.0, center=1.0, ver="v3")}, {_GK})
    eng.set_baseline({})
    assert eng.sigma_ref(_GK) == 2.0
    assert eng.center_ref(_GK) == 1.0
    assert eng._reference_id(_GK) == "v3"


# ── feed window flush : is_reset_point 제외 (D6-b) ────────────────────────────
def _feed_warm(eng, gk, n=3, ver="v1", pm=1):
    """warm까지 n장 적재 (min_fill=3)."""
    for i in range(n):
        eng.feed(_pt(gk, float(i), wafer=f"W{i}", ts=f"2026-07-15T10:0{i}:00.000Z", pm=pm, ver=ver))


def test_feed_no_flush_on_periodic_version_change():
    """is_reset_point=False(정기 auto로 밀린 버전) → limit_version 바뀌어도 window 유지(D6-b)."""
    lim = _lim(sigma=1.0)
    lim["is_reset_point"] = False                             # 정기 auto = 같은 운전점·자만 갱신
    eng = _engine({_GK: lim}, {_GK}, window=10, min_fill=3)
    _feed_warm(eng, _GK, n=3, ver="v1")
    eng.feed(_pt(_GK, 3.0, wafer="W4", ts="2026-07-15T10:05:00.000Z", pm=1, ver="v2"))
    assert len(eng._state[_GK].window) == 4                    # 유지 + append (flush 없음)


def test_feed_flush_on_reset_version_change():
    """is_reset_point=True(리셋: qual/incident/승인/initial) → flush(새 운전점 오염 차단)."""
    lim = _lim(sigma=1.0)
    lim["is_reset_point"] = True
    eng = _engine({_GK: lim}, {_GK}, window=10, min_fill=3)
    _feed_warm(eng, _GK, n=3, ver="v1")
    eng.feed(_pt(_GK, 3.0, wafer="W4", ts="2026-07-15T10:05:00.000Z", pm=1, ver="v2"))
    assert len(eng._state[_GK].window) == 1                    # flush 후 새 점만


def test_feed_flush_on_pm_increase_regardless_of_reset():
    """pm_count 증가는 is_reset_point 무관 항상 flush (새 PM = 새 레짐)."""
    lim = _lim(sigma=1.0)
    lim["is_reset_point"] = False                             # 버전은 안 바뀌어도
    eng = _engine({_GK: lim}, {_GK}, window=10, min_fill=3)
    _feed_warm(eng, _GK, n=3, ver="v1", pm=1)
    eng.feed(_pt(_GK, 3.0, wafer="W4", ts="2026-07-15T10:05:00.000Z", pm=2, ver="v1"))
    assert len(eng._state[_GK].window) == 1                    # PM 증가 → flush
