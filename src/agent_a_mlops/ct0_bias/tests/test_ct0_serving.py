# -*- coding: utf-8 -*-
"""CT⓪ 서빙 적용부 테스트 — `consumer.build_message` 의 가산·발행 계약 (설계 §4·D7).

검증 대상:
  · `mode=off`  → `bias_applied` 필드 **미발행** (현행 페이로드와 100% 동일)
  · `mode≠off`  → 항상 발행 (shadow 는 0)
  · 가산은 SHAP **뒤**에 — 항등식 `contribs 합 = predicted_c65 − bias_applied` 유지 (헌법 3-3)
  · Qual wafer 는 보정하지 않는다 (D7)
  · bias 가 NaN/Inf 여도 발행이 깨지지 않는다 (표준 JSON 보장)

`consumer.py` 는 lean85 파이프라인(pandas·xgboost)을 끌어오므로 미설치 환경에서는 SKIP.
실행: python -m pytest src/agent_a_mlops/ct0_bias/tests/test_ct0_serving.py -q
"""

import json
import sys
from pathlib import Path

import pytest

AGENT_A = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENT_A))

pytest.importorskip("pandas")
pytest.importorskip("confluent_kafka")
pytest.importorskip("xgboost")
import consumer  # noqa: E402
from ct0_bias import core as ct0_core  # noqa: E402

PRED = {"pred_c65": 700.0, "low_confidence": 0, "model_version": "lean85_test",
        "shap_top3": [{"sensor": "C11", "name": "DC_Bias", "contribution": 0.6},
                      {"sensor": "C62", "name": "RF_Vpp", "contribution": -0.3},
                      {"sensor": "C17", "name": "Chamber_Temp", "contribution": 0.1}]}
META = {"chamber_id": "SIM_CH_3", "is_qual": False}


def _mode(monkeypatch, mode):
    monkeypatch.setattr(consumer, "CT0_MODE", mode)


def test_off_mode_publishes_no_new_field(monkeypatch):
    """컷오버 전 페이로드는 **한 글자도** 달라지지 않는다."""
    _mode(monkeypatch, ct0_core.MODE_OFF)
    msg = consumer.build_message("C64_1", META, PRED, None, bias=0.0)
    assert "bias_applied" not in msg
    assert msg["predicted_c65"] == 700.0


def test_active_mode_adds_bias_and_publishes_field(monkeypatch):
    """가산값과 발행 필드가 **같은 값**이어야 raw 복원이 성립한다 (D1)."""
    _mode(monkeypatch, ct0_core.MODE_ACTIVE)
    msg = consumer.build_message("C64_1", META, PRED, None, bias=85.0)
    assert msg["bias_applied"] == 85.0
    assert msg["predicted_c65"] == 785.0
    assert msg["predicted_c65"] - msg["bias_applied"] == 700.0     # raw 복원


def test_shadow_publishes_zero(monkeypatch):
    """shadow 는 필드를 싣되 값은 0 — 소비자는 필드 존재로 경로 가동을 알 수 있다."""
    _mode(monkeypatch, ct0_core.MODE_SHADOW)
    msg = consumer.build_message("C64_1", META, PRED, None, bias=0.0)
    assert msg["bias_applied"] == 0.0 and msg["predicted_c65"] == 700.0


def test_shap_is_untouched_by_bias(monkeypatch):
    """SHAP 순위·값에 bias 가 개입하지 않는다 (헌법 3-3 독립 채널)."""
    _mode(monkeypatch, ct0_core.MODE_ACTIVE)
    plain = consumer.build_message("C64_1", META, PRED, None, bias=0.0)
    biased = consumer.build_message("C64_1", META, PRED, None, bias=-40.0)
    assert plain["shap_top3"] == biased["shap_top3"]
    assert biased["predicted_c65"] == 660.0 and biased["bias_applied"] == -40.0


def test_qual_wafer_gets_no_bias(monkeypatch):
    """D7 — Qual 판정 입력에 구레짐 bias 를 섞지 않는다."""
    _mode(monkeypatch, ct0_core.MODE_ACTIVE)
    monkeypatch.setattr(consumer, "_ct0_bias_now", {"SIM_CH_3": 85.0})
    assert consumer.ct0_bias_for({"chamber_id": "SIM_CH_3", "is_qual": True}) == 0.0
    assert consumer.ct0_bias_for({"chamber_id": "SIM_CH_3", "is_qual": False}) == 85.0


def test_bias_lookup_is_zero_unless_active(monkeypatch):
    """off·shadow 에서는 조회 자체가 0 — 서빙 경로에 보정이 존재하지 않는다."""
    monkeypatch.setattr(consumer, "_ct0_bias_now", {"SIM_CH_3": 85.0})
    for mode in (ct0_core.MODE_OFF, ct0_core.MODE_SHADOW):
        _mode(monkeypatch, mode)
        assert consumer.ct0_bias_for(META) == 0.0


def test_nonfinite_bias_does_not_break_publish(monkeypatch):
    """비유한 bias 는 0 으로 흡수된다 — NaN 이 페이로드로 나가면 대시보드·DB 가 동시에 깨진다."""
    _mode(monkeypatch, ct0_core.MODE_ACTIVE)
    for bad in (float("nan"), float("inf"), "abc", None):
        msg = consumer.build_message("C64_1", META, PRED, None, bias=bad)
        assert msg["bias_applied"] == 0.0 and msg["predicted_c65"] == 700.0
        json.dumps(msg, allow_nan=False)                            # 표준 JSON 보장
