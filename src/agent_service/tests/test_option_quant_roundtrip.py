"""옵션 jsonb 정량 필드 **왕복** 테스트 (2026-08-03 — 승인 payload 전량 null 수정).

왜 왕복이냐: 이 버그는 **에러가 나지 않는다.** writer 가 요약만 쓰고, 게이트가 `.get()` 으로
없는 키를 읽어 None 을 돌려줄 뿐이라 어디서도 예외가 안 난다. 화면엔 수치가 보이는데
승인하면 `fdc.correction` 이 전부 null 로 나가는 것을 **눈으로 세서** 발견했다(PM).

그래서 "writer 가 쓴 것" 과 "게이트가 읽는 것" 을 한 테스트 안에서 잇는다 —
필드 이름이 어긋나는 순간 CI 가 빨개진다. 문서는 썩지만 테스트는 안 썩는다.

경로: `brief_to_row` → (jsonb) → `fetch_bundle` 이 주는 모양 → `to_gate_report` → `build_correction_payload`
      ※ DB 는 타지 않는다. jsonb 직렬화만 실제로 거치고 나머지는 순수 함수 조립.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_service.app.report_writer import _QUANT_FIELDS, brief_to_row  # noqa: E402
from agent_service.app.schemas.report import (  # noqa: E402
    LimitOption,
    OptionSummary,
    RecipeOption,
    SupervisorBrief,
    SupervisorRecommendation,
)
from orchestrator.approval_graph import build_correction_payload  # noqa: E402
from orchestrator.supervisor_adapter import to_gate_report  # noqa: E402

INC = "INC-20260803-SIMCH2-001"

# B 엔진이 채번한 값 — C 는 승계만 한다(헌법 6-4). 테스트에서도 지어내지 않는 형태로 둔다.
LIM_CORRECTION_ID = "LIM-20260803-SIMCH2-0007"


def _limit_report() -> LimitOption:
    return LimitOption(
        report_id="LIM-20260803-SIMCH2-0007",
        confidence=0.72,
        rationale="① N3 추세 [SPC] ② PM 경과 [SPC] ③ 유사 사례 [CASE]",
        uncertainty="섀도 미실행",
        correction_id=LIM_CORRECTION_ID,
        sensor_id="C11",
        limit_version="v3",
        method="sigma",
        center_before=-310.0,
        center_after=-302.4,
        ucl_after=-280.1,
        lcl_after=-324.7,
        delta_sigma=0.3,
    )


def _recipe_report() -> RecipeOption:
    return RecipeOption(
        report_id="RCP-20260803-SIMCH2-0007",
        confidence=0.78,
        rationale="① 가스 계열 N3 [SPC] ② SHAP 축=가스 [KB] ③ 사례 개선 [CASE]",
        uncertainty="감도 통계 표본 한정",
        parameter_id="C4",
        parameter_name="Gas_Set_A",
        value_current=40.0,
        applied_value=39.6,     # 이미 -1.0% 조정된 이력이 있는 상태 (명목 40.0 ≠ 실제 39.6)
        value_proposed=39.2,
        delta_pct=-2.0,
        hypothesis="process_condition_shift",
    )


def _brief(selected: str) -> SupervisorBrief:
    """옵션 블록은 **요약**만 담는다 — 실제 Brief 와 같은 모양(수치 없음)."""
    summary = {
        k: OptionSummary(report_id=f"{k[:3].upper()}-x", action="한 줄 요약", confidence=0.7)
        for k in ("recipe_option", "limit_option", "manual_option")
    }
    return SupervisorBrief(
        report_id="SUP-20260803-SIMCH2-0007",
        incident_id=INC,
        chamber_id="SIM_CH_2",
        context_score=72,
        parallel_options=summary,
        supervisor_recommendation=SupervisorRecommendation(
            selected=selected,
            verdict="baseline_aging" if selected == "limit_option" else "process_shift",
            reason="사유 한 문단",
            evidence=["① [SPC]", "② [MODEL]", "③ [KB]"],
            counter_evidence="반증 1건",
            confidence=0.72,
        ),
    )


def _bundle_from_row(row: dict) -> dict:
    """`fetch_bundle` 이 DB 에서 읽어 `to_gate_report` 에 넘기는 모양을 재현.

    fetch_bundle 은 jsonb 컬럼을 그대로 dict 로 돌려준다 — 여기서는 writer 가 만든
    JSON 문자열을 되파싱해 **직렬화 왕복까지** 실제로 거치게 한다.
    """
    lim = json.loads(row["limit_option"]) if row["limit_option"] else None
    rcp = json.loads(row["recipe_option"]) if row["recipe_option"] else None
    return {"lim": lim, "rcp": rcp}


# ---------------------------------------------------------------------------
# 1. limit — 승인 payload 의 변경 금지 필드가 살아서 나온다
# ---------------------------------------------------------------------------
def test_limit_quant_survives_roundtrip() -> None:
    brief = _brief("limit_option")
    row = brief_to_row(brief, [_limit_report(), _recipe_report()])
    parts = _bundle_from_row(row)

    report = to_gate_report(brief.model_dump(mode="json"), parts["lim"], parts["rcp"])
    payload = build_correction_payload(INC, "limit_option", report)

    # 계약 §6 변경 금지 필드 (헌법 2-1) — 하나라도 None 이면 승인해도 빈 값이 나간다
    assert payload["sensor"] == "C11"
    assert payload["limit_version"] == "v3"
    assert payload["correction_id"] == LIM_CORRECTION_ID
    assert payload["value_proposed"] == -302.4          # center_after → center_proposed
    assert payload["correction_type"] == "limit"


# ---------------------------------------------------------------------------
# 2. recipe — 동일
# ---------------------------------------------------------------------------
def test_recipe_quant_survives_roundtrip() -> None:
    brief = _brief("recipe_option")
    row = brief_to_row(brief, [_limit_report(), _recipe_report()])
    parts = _bundle_from_row(row)

    report = to_gate_report(brief.model_dump(mode="json"), parts["lim"], parts["rcp"])
    payload = build_correction_payload(INC, "recipe_option", report)

    assert payload["parameter_id"] == "C4"
    assert payload["delta_pct"] == -2.0
    assert payload["value_proposed"] == 39.2
    assert payload["correction_type"] == "recipe"


# ---------------------------------------------------------------------------
# 2-b. recipe — 승인 **화면**이 읽는 값 (2026-08-06, PR #122 리뷰)
# ---------------------------------------------------------------------------
def test_recipe_current_values_reach_the_approval_screen() -> None:
    """엔지니어가 보는 "현재값"이 **명목값이 아니라 실제 걸린 값**으로 가는지.

    2-a(payload)와 소비자가 다르다 — payload 는 `fdc.correction`(계약 §6, B·시뮬),
    이쪽은 `approval_records.original_value`(= to_gate_report 결과, 승인 화면)다.
    그래서 2-a 가 통과해도 화면은 여전히 명목값일 수 있고, 실제로 그랬다:
    fixture 에 `value_current=40.0` 이 8/3부터 있었는데 **통과 여부를 아무도 안 봤다**.

    가스 축은 조정 이력이 쌓이면 명목 40.0 ≠ 실제 39.6 이라, 화면이 명목값을 보이면
    엔지니어는 "40.0 → 39.2 (-2%)" 로 읽지만 장비에는 "39.6 → 39.2 (-1%)" 가 걸린다.
    """
    brief = _brief("recipe_option")
    row = brief_to_row(brief, [_limit_report(), _recipe_report()])
    parts = _bundle_from_row(row)

    report = to_gate_report(brief.model_dump(mode="json"), parts["lim"], parts["rcp"])
    rt = report["recipe_tuning"]

    assert rt["applied_value"] == 39.6      # 지금 걸린 값 — 화면의 "현재값"
    assert rt["value_current"] == 40.0      # 명목값 — 누적 계산의 기준
    assert rt["value_proposed"] == 39.2

    # 계약 §6 페이로드에는 **일부러 안 싣는다** (B 보유값 · 필드 추가는 소비자 리뷰 — 헌법 2-2).
    payload = build_correction_payload(INC, "recipe_option", report)
    assert "applied_value" not in payload


def test_applied_value_null_is_preserved_not_dropped() -> None:
    """온도 축(temp_target)·첫 스텝은 applied_value 가 **정상적으로 null** 이다 (D11·라이브러리 §1).

    키가 사라지면 화면이 "값 없음"과 "필드 자체가 없음"을 구분 못 한다 — limit 쪽에서
    `or` 폴백이 0.0 을 삼켰던 것과 같은 계열(2026-07-22)이라 방향만 반대로 고정한다.
    """
    rcp = _recipe_report()
    rcp.applied_value = None
    brief = _brief("recipe_option")
    parts = _bundle_from_row(brief_to_row(brief, [_limit_report(), rcp]))

    report = to_gate_report(brief.model_dump(mode="json"), parts["lim"], parts["rcp"])
    rt = report["recipe_tuning"]

    assert "applied_value" in rt and rt["applied_value"] is None
    assert rt["value_current"] == 40.0       # 명목값은 그대로 — 화면은 이것만 보이면 된다


# ---------------------------------------------------------------------------
# 3. 회귀 방어 — reports 미전달이면 그 필드들이 비어 나간다(= 이 버그의 모양)
# ---------------------------------------------------------------------------
def test_without_reports_payload_is_null() -> None:
    """상세 리포트를 안 넘기면 전량 null 이라는 것을 **명시적으로 고정**한다.

    이 테스트가 깨지는 방향(=요약만으로도 값이 찬다)이면 어딘가에서 수치를 창작하고 있다는
    뜻이다 — '정량은 B' 경계 위반이므로 그때도 알아야 한다.
    """
    brief = _brief("limit_option")
    row = brief_to_row(brief)                     # reports 미전달 — 종전 동작
    parts = _bundle_from_row(row)

    report = to_gate_report(brief.model_dump(mode="json"), parts["lim"], parts["rcp"])
    payload = build_correction_payload(INC, "limit_option", report)

    assert payload["sensor"] is None
    assert payload["limit_version"] is None
    assert payload["correction_id"] is None


# ---------------------------------------------------------------------------
# 4. None 인 필드는 키 자체를 싣지 않는다 (_pick 폴백이 살아 있어야 한다)
# ---------------------------------------------------------------------------
def test_none_fields_are_omitted_not_nulled() -> None:
    """`delta_pct` 는 B 가 산출하지 않아 limit 리포트에서 None 이다(§2 규칙1).

    이때 키를 `null` 로 실으면 `to_gate_report._pick` 의 "None 이면 legacy 이름으로 폴백" 이
    무력해진다 — 키 부재와 null 을 구분해야 한다.
    """
    row = brief_to_row(_brief("limit_option"), [_limit_report()])
    lim = json.loads(row["limit_option"])

    assert "delta_pct" not in lim          # None 이므로 아예 없다
    assert lim["center_after"] == -302.4   # 값이 있는 것만 실린다
    assert lim["action"] == "한 줄 요약"    # 요약 필드는 보존 — fetch_bundle 이 Brief 되조립에 쓴다


# ---------------------------------------------------------------------------
# 5. manual_option 은 정량을 싣지 않는다 (fdc.correction 경로가 아니다)
# ---------------------------------------------------------------------------
def test_manual_option_has_no_quant() -> None:
    row = brief_to_row(_brief("manual_option"), [_limit_report(), _recipe_report()])
    mnt = json.loads(row["manual_option"])
    assert set(mnt) <= {"report_id", "action", "feasibility", "confidence", "rejected_because"}


# ---------------------------------------------------------------------------
# 6. 🔴 알려진 잔여 갭 — recipe_correction_id 는 아직 null 이다 (명시적으로 고정)
# ---------------------------------------------------------------------------
def test_recipe_correction_id_is_still_null_known_gap() -> None:
    """`recipe_correction_id` 는 **B 채번 승계값**이라 B5-4 연동(#83) 전까지 null 이다.

    limit 쪽 `correction_id` 와 **대칭이 아니다.** 아래 5번 테스트가 "두 옵션 모두 같은
    경로"를 검증하는데, 그 문장만 보면 이 갭이 가려진다(Claude 리뷰 #96 지적).
    그래서 갭을 **테스트로 고정**한다 — 값이 차는 순간 이 테스트가 깨지고, 그때가
    `recipe_corrections.recipe_correction_id`(NOT NULL UNIQUE) 매칭을 배선할 시점이다.

    ⚠️ 이 테스트가 깨졌는데 B 연동을 안 했다면 **C 가 ID 를 창작한 것**이다 (헌법 6-4 위반).
    """
    row = brief_to_row(_brief("recipe_option"), [_limit_report(), _recipe_report()])
    parts = _bundle_from_row(row)
    report = to_gate_report(_brief("recipe_option").model_dump(mode="json"),
                            parts["lim"], parts["rcp"])
    payload = build_correction_payload(INC, "recipe_option", report)

    assert payload["recipe_correction_id"] is None      # ← #83 배선 시 이 줄을 바꾼다
    assert payload["correction_id"] is None             # recipe 경로에는 limit ID 가 없다
    # 대조: limit 쪽은 이미 채워진다
    lim_payload = build_correction_payload(INC, "limit_option", report)
    assert lim_payload["correction_id"] == LIM_CORRECTION_ID


@pytest.mark.parametrize("option", ["limit_option", "recipe_option"])
def test_value_proposed_survives_for_both_options(option: str) -> None:
    """두 옵션 모두 **`value_proposed` 경로**는 같다 — 한쪽만 고쳐지는 일이 없게.

    ⚠️ 이름을 좁혔다(구 `..._is_symmetric_for_both_options`). 두 경로가 **완전 대칭은
    아니다** — correction ID 는 recipe 쪽이 아직 비어 있다(위 6번 참조).
    """
    row = brief_to_row(_brief(option), [_limit_report(), _recipe_report()])
    parts = _bundle_from_row(row)
    report = to_gate_report(_brief(option).model_dump(mode="json"), parts["lim"], parts["rcp"])
    payload = build_correction_payload(INC, option, report)
    assert payload["value_proposed"] is not None


# ── PM #96 리뷰 공약: 게이트가 읽는 키 ⊆ 적재하는 키 (드리프트 가드) ──────────
_GATE_READS = {
    "limit_option": {"sensor_id", "limit_version", "center_after", "correction_id"},
    "recipe_option": {"parameter_id", "delta_pct", "value_proposed"},
}


@pytest.mark.parametrize("opt", ["limit_option", "recipe_option"])
def test_quant_fields_cover_what_gate_reads(opt: str) -> None:
    """게이트(`supervisor_adapter._pick` 계열)가 읽는 키가 `_QUANT_FIELDS` 에서 빠지면
    에러 없이 null 로 샌다(8/2 사고 구조) — 집합 포함으로 못박는다. 이 테스트가 빨개지면:
    게이트가 새 키를 읽기 시작했는데 writer 적재 목록(`_QUANT_FIELDS`)에 없다는 뜻이다."""
    assert _GATE_READS[opt] <= set(_QUANT_FIELDS[opt])
