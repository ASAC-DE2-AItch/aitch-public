"""손잡이 화이트리스트 ↔ knob_map 정합 (C6-3 DoD "튜닝안에서 참조 확인").

**왜 이 파일이 있나**
C6-3 의 DoD 는 "적재"에서 끝나지 않는다 — 적재한 knob_map 이 **실제로 판정에 반영되는지**까지다.
설계 의도는 *"knob_map 을 수정하면 **코드 변경 없이** 허용 집합이 자동으로 바뀐다"* 이고
(`allowed_knobs` 가 화이트리스트를 knob_map 에서 뽑는다), 그 성질이 깨지면 KB 를 고쳐도
판정이 안 바뀌거나 반대로 막아야 할 것이 열린다.

**7/11 통보 수정 3건** (7/10 모델 실측 근거)
  · C12 손잡이 매핑 **제거** — C12 는 레짐(regime) 도장이라 튜닝 대상이 될 수 없다
  · RF_power **C31 → C1(RF_Power_Set)** 정정 — C31 은 실측값이라 손잡이가 아니다
  · 가스 축에 **C4·C5 추가** — 가스 setpoint 등재
  · (압력 카드 103 = '미확정' 유지 · 온도 카드 101 = **목표 지정형 BL8 `temp_target` 로 전환**)

Qdrant 없이 돈다 — 실적재분과 같은 모양의 knob_map 을 fixture 로 둔다. 실적재분 검증은
`scripts/`(수동) 또는 `check_kb_consistency.py` 가 담당하고, 여기서는 **가드의 계약**을 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from agent_service.app.schemas.alert import AlertModel  # noqa: E402
from agent_service.app.schemas.report import RecipeOption  # noqa: E402
from agent_service.app.tools import recipe as R  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"

# 2026-07-28 Qdrant 실적재분과 동일한 모양 (fetch_knob_map 반환 형태).
# knob_param 의 4가지 형태가 모두 들어 있다 — 리스트·문자열(C코드 괄호)·'미확정'·명명 손잡이.
KNOB_MAP = [
    {"axis": "가스", "knob_param": ["C4(가스유량_Setpoint_A)", "C5(가스유량_Setpoint_B)",
                                   "C48(Main_Gas_Flow_Set)"],
     "related_sensors": ["C4", "C5", "C48", "C15", "C16"], "doc_id": "PK-TUNE-GAS"},
    {"axis": "식각 시간", "knob_param": "step_time(C41)",
     "related_sensors": ["C41"], "doc_id": "PK-TUNE-ETCHTIME"},
    {"axis": "압력", "knob_param": "미확정",
     "related_sensors": ["C57", "C58"], "doc_id": "PK-TUNE-PRESSURE"},
    {"axis": "온도", "knob_param": "temp_target",
     "related_sensors": ["C17"], "doc_id": "PK-TUNE-TEMP"},
    {"axis": "플라즈마/이온에너지", "knob_param": ["C1(RF_Power_Set)"],
     "related_sensors": ["C11", "C31", "C1"], "doc_id": "PK-TUNE-PLASMA"},
]


def _alert(name: str = "02_baseline_aging_gate_edge.json") -> AlertModel:
    import json
    return AlertModel.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _option(parameter_id: str) -> RecipeOption:
    return RecipeOption(
        report_id="RCP-20260713-SIMCH2-0002", confidence=0.7, rationale="r", uncertainty="u",
        parameter_id=parameter_id, value_current=100.0, value_proposed=98.0, delta_pct=-2.0,
        hypothesis="process_condition_shift",
    )


# --- 화이트리스트 = knob_map 에서 뽑힌다 ------------------------------------------
def test_whitelist_is_derived_from_knob_map() -> None:
    """허용 집합이 knob_map 에서 나온다 — 코드에 손잡이 목록이 하드코딩돼 있지 않다."""
    assert R.allowed_knobs(KNOB_MAP) == {"C1", "C4", "C5", "C41", "C48", "temp_target"}


@pytest.mark.parametrize(
    ("parameter_id", "allowed", "why"),
    [
        # 7/11 통보 수정 3건
        ("C12", False, "레짐 도장 — 손잡이 절대 금지(7/10 확정)"),
        ("C1", True, "RF_Power_Set = setpoint (구 C31 에서 정정)"),
        ("C31", False, "RF 실측값 — 반응값이라 손잡이가 아니다"),
        ("C4", True, "가스유량 Setpoint A (7/11 추가)"),
        ("C5", True, "가스유량 Setpoint B (7/11 추가)"),
        # 나머지 축
        ("C48", True, "Main_Gas_Flow_Set"),
        ("C41", True, "step_time — 문자열 'step_time(C41)' 에서 C코드 추출"),
        ("temp_target", True, "목표 지정형(BL8) — C코드가 아니라 명명 손잡이"),
        # 막혀야 하는 것
        ("temp_setpoint", False, "옛 이름 — 2026-07-24 폐기. 정확 일치만 통과한다"),
        ("C17", False, "히터/척 온도 **측정** 센서 — parameter_id 금지"),
        ("C15", False, "가스유량 실측 — related_sensors 에는 있지만 손잡이가 아니다"),
        ("C57", False, "밸브개도 — 압력 축은 '미확정'"),
        ("C99", False, "미등록·존재하지 않는 C코드"),
    ],
)
def test_knob_guard_allows_only_mapped_knobs(parameter_id: str, allowed: bool, why: str) -> None:
    """knob_map 에 등재된 손잡이만 통과하고 나머지는 레짐 신호로 강등된다.

    ⚠️ `related_sensors` 에 있다고 손잡이가 되는 게 아니다 — C15·C31 이 그 반례다.
    SHAP 은 측정 센서를 지목하지만 돌리는 것은 setpoint 다.
    """
    out = R.enforce_knob_guard(_option(parameter_id), KNOB_MAP)
    kept = out.parameter_id == parameter_id and out.value_proposed is not None
    assert kept is allowed, f"{parameter_id}: 허용={kept} (기대 {allowed}) — {why}"
    if not allowed:
        assert out.hypothesis == "regime_signal"        # 강등 방향
        assert out.escalate_reason                       # 사유가 남는다


# --- "코드 변경 없이 허용 집합이 바뀐다" 성질 ------------------------------------
def test_removing_axis_from_knob_map_closes_that_knob() -> None:
    """knob_map 에서 가스 축을 빼면 C4 가 **즉시** 막힌다 — 코드 수정 없이."""
    without_gas = [c for c in KNOB_MAP if c["axis"] != "가스"]
    assert "C4" in R.allowed_knobs(KNOB_MAP)             # 있을 때는 열림
    assert "C4" not in R.allowed_knobs(without_gas)      # 빼면 닫힘
    out = R.enforce_knob_guard(_option("C4"), without_gas)
    assert out.hypothesis == "regime_signal"


def test_adding_ccode_to_knob_map_opens_it() -> None:
    """반대로 knob_map 에 C코드를 추가하면 그 순간 열린다 (C4·C5 추가가 그 사례)."""
    plus = KNOB_MAP + [{"axis": "테스트축", "knob_param": ["C77(Test_Set)"], "doc_id": "PK-X"}]
    assert "C77" not in R.allowed_knobs(KNOB_MAP)
    assert "C77" in R.allowed_knobs(plus)


def test_pressure_undetermined_blocks_without_special_casing() -> None:
    """'미확정' 은 C코드가 없어 **자동으로** 차단된다 — 압력 전용 예외 코드가 필요 없다."""
    pressure = [c for c in KNOB_MAP if c["axis"] == "압력"]
    assert R.allowed_knobs(pressure) == set()


def test_empty_knob_map_does_not_demote() -> None:
    """knob_map 조회 실패(빈 값)면 판정 근거가 없으므로 **강등하지 않는다**(오탐 방지)."""
    for empty in (None, []):
        out = R.enforce_knob_guard(_option("C4"), empty)
        assert out.parameter_id == "C4" and out.value_proposed is not None
