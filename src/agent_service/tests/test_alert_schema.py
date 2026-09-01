"""AlertModel + fixtures 검증 테스트 (C4-1 Day1).

오늘 만든 것을 잠그는 회귀 테스트. `pytest`로 한 번에 확인:
  · fixtures 30건 — 정상 29 파싱 / 파손 1 실패
  · 게이트(B7=31) 분기 · max_severity
  · sensor_name optional 관용(publisher형=無도 파싱) · E14 차단
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.schemas.alert import CONTRACT_AGENT_GATE_MIN, AlertModel  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"
ALL = sorted(FIXTURES.glob("*.json"))


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- fixtures 계약 검증 --------------------------------------------------------
@pytest.mark.parametrize(
    "path", [p for p in ALL if "BROKEN" not in p.name], ids=lambda p: p.name
)
def test_valid_fixtures_parse(path: Path) -> None:
    """정상 fixtures 29건은 전부 AlertModel로 파싱되어야 한다."""
    AlertModel.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_exactly_one_broken_and_it_fails() -> None:
    """의도적 파손은 정확히 1건이고, 그 1건만 검증에 실패해야 한다."""
    broken = [p for p in ALL if "BROKEN" in p.name]
    assert len(broken) == 1, "파손 fixture는 정확히 1건이어야 함"
    with pytest.raises(ValidationError):
        AlertModel.model_validate(json.loads(broken[0].read_text(encoding="utf-8")))


def test_fixture_total_count() -> None:
    """fixtures 총 건수 — 생성기(generate_fixtures) 와 어긋나면 stale 픽스처를 의심한다."""
    # 코어 40(trigger 4 × band 10) + 엣지 7. BANDS 6→10 확장(2026-07-21),
    # 엣지 6→7: temp_drift 추가(2026-07-26 온도 가변 축 — 신규 경로 커버리지).
    assert len(ALL) == 47


# --- 게이트(B7=31) 분기 --------------------------------------------------------
def test_gate_below_31_is_closed() -> None:
    a = AlertModel.model_validate(_load("01_baseline_aging_below_gate.json"))
    assert a.context_score == 22
    assert a.agent_gate_open is False  # 미가동(기록만)


def test_gate_at_31_is_open() -> None:
    a = AlertModel.model_validate(_load("02_baseline_aging_gate_edge.json"))
    assert a.context_score == 31
    assert a.agent_gate_open is True  # 경계값 포함(>=31)


def test_gate_min_matches_params() -> None:
    """계약 명문값과 params 운영값이 갈라지지 않는지 (PM 후속 요청 #69, 2026-07-30).

    `CONTRACT_AGENT_GATE_MIN`(schemas/alert.py)과 `spc.context_score_agent_min`(params)은
    **의도적으로 두 곳에 둔다** — 스키마 계층이 config 를 임포트하면 계약 모델이 런타임 설정에
    묶이기 때문이다(alert.py docstring). 소스를 합치지 않는 대신 **일치만 여기서 지킨다.**

    ⚠️ 이 테스트가 깨지면 둘 중 하나만 고친 것이다. 운영 판정은 `pipeline.gate_open` 이
    params 로 하므로 **params 가 정본**이고, 상수를 그 값에 맞춰야 한다.

    ※ `agent.fast_slow_cutoff` 도 현재 31 이지만 **여기서 함께 단언하지 않는다** — 그쪽 31 은
       "분기 비활성"(가동 건 전부 slow)이라는 별개 의미이고, fast/slow 분기를 켜면 의도적으로
       달라진다(params D4-보조 주석).
    """
    assert CONTRACT_AGENT_GATE_MIN == int(load_settings().require("spc.context_score_agent_min"))


# --- 파생 헬퍼 ----------------------------------------------------------------
def test_max_severity_picks_critical() -> None:
    """복수 위반(N1 CRITICAL + N3 WARNING)에서 최고 심각도는 CRITICAL."""
    a = AlertModel.model_validate(_load("43_edge_multi_violation.json"))
    assert a.max_severity == "CRITICAL"


# --- 관용/방어 파싱 ------------------------------------------------------------
def test_sensor_name_is_optional() -> None:
    """publisher형(sensor_name 미포함)도 파싱되어야 한다 (헌법 6-2 무중단)."""
    base = _load("01_baseline_aging_below_gate.json")
    for v in base["violations"]:
        v.pop("sensor_name", None)
    a = AlertModel.model_validate(base)
    assert a.violations[0].sensor_name is None


def test_suspect_sensors_defaults_to_empty_list() -> None:
    """B #113 신규 필드 — **없어도 파싱된다** (기존 fixtures 47건이 전부 미포함).

    ⚠️ 이 테스트가 이 파일에서 제일 중요한 축이다. `Tttm` 은 나머지 5필드가 required 라
    누군가 여기에도 `...` 를 붙이면 **fixtures 전량과 `generate_fixtures.py` 가 동시에 깨져
    평가 라운드가 통째로 멈춘다.** 기본값은 `None` 이 아니라 `[]` 여야 한다 —
    빈 목록이 "공통이동 없음"이라는 유효값이고, B publisher 도 `[]` 를 None 대체 대상에서
    뺐다(`_build_tttm` 의 `is not None`).
    """
    a = AlertModel.model_validate(_load("01_baseline_aging_below_gate.json"))
    assert a.tttm.suspect_sensors == []


def test_suspect_sensors_preserved_when_present() -> None:
    """실물이 오면 그대로 보존된다 — worst 와 달라도(decouple 취지) 손대지 않는다."""
    base = _load("42_edge_reference_suspect.json")
    base["tttm"]["suspect_sensors"] = ["C17"]
    a = AlertModel.model_validate(base)
    assert a.tttm.top_gap_sensor == "C11"      # worst (경로②)
    assert a.tttm.suspect_sensors == ["C17"]   # 공통이동 (역방향 룰) — 별개 채널


def test_chamber_id_rejects_deprecated_e14() -> None:
    """E14(deprecated)는 SIM_CH_ 패턴 위반으로 거부되어야 한다."""
    base = _load("01_baseline_aging_below_gate.json")
    base["chamber_id"] = "E14"
    with pytest.raises(ValidationError):
        AlertModel.model_validate(base)


def test_trigger_signal_optional_field_preserved() -> None:
    """계약 미정의 optional 필드(trigger_signal)는 있으면 보존된다."""
    a = AlertModel.model_validate(_load("41_edge_trigger_signal.json"))
    assert a.trigger_signal is not None


@pytest.mark.parametrize(
    "path", [p for p in ALL if "BROKEN" not in p.name], ids=lambda p: p.name
)
def test_shap_top3_are_distinct(path: Path) -> None:
    """shap_top3는 서로 다른 센서 3개여야 한다 (중복 금지 — 회귀 방지).

    (2026-07-13 발견: 회전 센서가 채움 후보와 겹쳐 C62가 두 번 들어간 버그. 이 테스트로 잠금.)
    """
    a = AlertModel.model_validate(json.loads(path.read_text(encoding="utf-8")))
    shap = a.prediction_context.shap_top3
    assert len(shap) == len(set(shap)), f"shap_top3 중복: {shap}"
