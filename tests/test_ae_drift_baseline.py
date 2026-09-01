# -*- coding: utf-8 -*-
"""ae_drift_score 분모 보호 테스트 (2026-07-28 코드리뷰 §1-4 대응).

drift = clip((EWMA − B0) / (alarm − B0), 0, 1) 이므로 **B0 < alarm 이 성립해야만** 정의된다.
B0 는 정상 EWMA 의 P99 라 alarm(0.2) 보다 작다는 보장이 없다 — CT② 재학습 후 새 정상셋
분포가 나쁘면:
  · B0 == alarm → ZeroDivisionError → wafer 마다 예외 → 전량 더미 폴백(조용한 기능 정지)
  · B0 >  alarm → 분모 음수 → 부호 반전(정상일수록 drift 1.0)

이 테스트는 산출 시점(fit_drift_baseline)과 사용 시점(DriftTracker·drift_stream) 양쪽에
캡이 걸려 있는지를 고정한다.

실행: python -m pytest tests/test_ae_drift_baseline.py -q
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops" / "Autoencoder"))

pytest.importorskip("numpy")
import numpy as np  # noqa: E402

from ae_pipeline.drift import (  # noqa: E402
    ALARM, B0_MARGIN, DriftTracker, cap_baseline, drift_stream, fit_drift_baseline,
)


# ── ① 산출 시점 캡 ─────────────────────────────────────────────────────────
def test_fit_drift_baseline_caps_when_normal_set_is_bad():
    """★ 정상셋이 전부 alarm 초과여도 B0 는 alarm 미만으로 캡된다."""
    cfg = fit_drift_baseline(np.full(200, 0.5))          # 전 구간 0.5 (alarm 0.2 초과)
    assert cfg["baseline_B0"] < cfg["alarm_level"]
    assert cfg["baseline_B0"] == pytest.approx(ALARM - B0_MARGIN)


def test_fit_drift_baseline_keeps_healthy_value():
    """정상 분포에서는 캡이 개입하지 않는다 (기존 산출값 보존)."""
    rng = np.random.default_rng(42)
    cfg = fit_drift_baseline(np.clip(rng.normal(0.05, 0.01, 500), 0, 1))
    assert 0.0 < cfg["baseline_B0"] < ALARM - B0_MARGIN


@pytest.mark.parametrize("b0", [0.2, 0.25, 1.0])
def test_cap_baseline_rejects_ge_alarm(b0):
    assert cap_baseline(b0, ALARM) == pytest.approx(ALARM - B0_MARGIN)


def test_cap_baseline_passthrough():
    assert cap_baseline(0.13, ALARM) == pytest.approx(0.13)


# ── ② 사용 시점 캡 (외부 drift.json 로드 경로 — 이중 안전망) ───────────────
def _bad_cfg(b0):
    """캡 이전 값이 그대로 적재된 drift.json 모사."""
    return {"method": "ewma_level", "alpha": 0.05, "alarm_level": ALARM, "baseline_B0": b0}


def test_drift_tracker_does_not_divide_by_zero():
    """★ B0 == alarm 인 번들을 로드해도 ZeroDivisionError 없이 [0,1] 값을 낸다."""
    tr = DriftTracker(_bad_cfg(ALARM))
    for s in (0.0, 0.05, 0.5):
        d = tr.update(s)
        assert 0.0 <= d <= 1.0


def test_drift_tracker_no_sign_inversion():
    """B0 > alarm 이면 분모가 음수 → 정상 wafer 가 drift 1.0 이 되던 경로를 막는다."""
    tr = DriftTracker(_bad_cfg(0.5))
    normal = [tr.update(0.02) for _ in range(50)]         # 계속 정상 점수
    assert normal[-1] <= normal[0]                        # 드리프트가 올라가지 않는다
    assert all(0.0 <= d <= 1.0 for d in normal)


def test_drift_stream_caps_loaded_baseline():
    out = drift_stream([0.02] * 30, _bad_cfg(0.3))
    assert np.all((out >= 0.0) & (out <= 1.0))
