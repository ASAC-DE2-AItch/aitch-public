# =============================================================================
# tests/test_markers.py  —  합성 마커 rule_id 단일 소스 가드
# =============================================================================
# 목적: 발행측(publisher)과 채점측(axes/score_spc)이 같은 마커 정의를 읽는지 고정한다.
#   두 곳이 각자 문자열을 들면 규격 변경 시 **예외 없이** 어긋난다 — 채점측은 마커를
#   "미등록 rule_id" 로 흘려보내고 경고만 남긴다(헌법 7장).
# 거동 불변: 이 PR 은 e1(=0)·e3 를 바꾸지 않는다 — 경고만 없앤다.
# =============================================================================
import dataclasses
import logging

import pytest

from src.common.context_score.axes.score_spc import _stream_score, score_spc
from src.common.context_score.config import ContextScoreConfig
from src.common.context_score.markers import CRAZY_MARKER_RULE_ID, MARKER_RULE_IDS
from src.common.context_score.publisher import _MARKER_RULE_ID


def _cfg(**over):
    """실 params 로드분에서 이 테스트가 쓰는 값만 고정 — 필드 목록 변화에 안 깨진다.

    `rule_base` 는 Nelson 룰만 두고 마커는 넣지 않는다(그게 정상 상태다).
    `size_per_sigma`·`k_sigma` 는 e3 기대값 산수를 고정하려고 못박는다.
    """
    base = dict(rule_base={"N1": 10.0, "N2": 12.0}, size_per_sigma=5.0, k_sigma=3.0)
    base.update(over)
    return dataclasses.replace(ContextScoreConfig.load(), **base)


def _marker_violation(current=1.0, upper=0.6, lower=0.0):
    """publisher._build_crazy_marker 가 내는 형태(anomaly arm)."""
    return {"rule_id": _MARKER_RULE_ID, "sensor_id": "C65", "sensor_window": "settled",
            "current_value": current, "control_limit_upper": upper,
            "control_limit_lower": lower, "chamber_id": "SIM_CH_1"}


def test_publisher_marker_is_from_single_source():
    """발행측 상수가 markers.py 정의와 같은 객체를 가리킨다."""
    assert _MARKER_RULE_ID == CRAZY_MARKER_RULE_ID
    assert _MARKER_RULE_ID in MARKER_RULE_IDS


def test_marker_is_not_in_rule_base():
    """마커는 Nelson 배점표에 없어야 한다 — 등재되면 e1 이중계상."""
    cfg = ContextScoreConfig.load()
    for rid in MARKER_RULE_IDS:
        assert rid not in cfg.rule_base, f"{rid} 가 rule_base 에 등재됨 (e1 이중계상 위험)"


def test_marker_does_not_warn(caplog):
    """마커 rule_id 는 '미등록' 경고를 내지 않는다 — 의도된 제외다."""
    from src.common.context_score.axes import score_spc as mod
    mod._WARNED_UNKNOWN_RULES.clear()
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        score_spc([_marker_violation()], _cfg())
    assert not [r for r in caplog.records if "미등록 rule_id" in r.getMessage()]


def test_unknown_rule_still_warns(caplog):
    """마커가 아닌 미등록 rule_id 는 종전대로 경고한다 (가드가 넓어지지 않았는지)."""
    from src.common.context_score.axes import score_spc as mod
    mod._WARNED_UNKNOWN_RULES.clear()
    v = _marker_violation()
    v["rule_id"] = "N99"
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        score_spc([v], _cfg())
    assert [r for r in caplog.records if "미등록 rule_id" in r.getMessage()]


@pytest.mark.parametrize("current,expected_e3", [(1.0, 20.0), (0.6, 0.0)])
def test_marker_score_unchanged(current, expected_e3):
    """거동 불변 — e1=0 유지, e3 는 종전 산식 그대로.

    σ = (0.6 − 0.0) / (2×3) = 0.1 · 초과 (1.0 − 0.6)/0.1 = 4σ → e3 = 5×4 = 20.
    """
    assert _stream_score([_marker_violation(current=current)], _cfg()) == pytest.approx(expected_e3)
