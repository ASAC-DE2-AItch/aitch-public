"""Supervisor 판정 + 형식 가드 4종 (C5-3).

정본 = docs/Agent_프롬프트_라이브러리_v1.md §4 + 부록 치트시트.

가드가 tool 과 다른 점: 판정의 옳고 그름은 코드가 못 따진다. **형식 정합**만 본다 —
가드 우회(실행 불가 옵션 선택) · verdict↔selected 어긋남 · 유령 인용 · 기각사유 누락.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_service.app import supervisor as sup  # noqa: E402
from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.llm.client import MockBackend  # noqa: E402
from agent_service.app.pipeline import handle_alert_with_brief  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402
from agent_service.app.schemas.report import (  # noqa: E402
    NONE_EXECUTABLE,
    LimitOption,
    ManualOption,
    RecipeOption,
    SupervisorBrief,
    WaferDisposition,
    make_fallback_brief,
)
from test_pipeline_fanout import (  # noqa: E402
    MOCK_BY_TYPE,
    STUB_MANUAL_INPUTS,
    STUB_RECIPE_INPUTS,
    RoutingBackend,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"
SETTINGS = load_settings()
INCIDENT = "INC-20260713-SIMCH4-0028"


def _alert(name: str = "28_equipment_fault_high.json") -> AlertModel:
    return AlertModel.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _reports(*, manual_action=None, limit_center=-302.4) -> list:
    """테스트용 리포트 3종 — 조치 유무를 파라미터로 조절(가드 ① 검증용)."""
    action = "Focus Ring 교체" if manual_action is None else manual_action
    return [
        RecipeOption(
            report_id="RCP-20260713-SIMCH4-0028", confidence=0.4, rationale="r",
            uncertainty="u", value_proposed=None, escalate_reason="손잡이 아님",
        ),
        LimitOption(
            report_id="LIM-20260713-SIMCH4-0028", confidence=0.45, rationale="r",
            uncertainty="u", center_after=limit_center,
        ),
        ManualOption(
            report_id="MNT-20260713-SIMCH4-0028", confidence=0.79, rationale="r",
            uncertainty="u", action=action,
            escalate_reason=None if action else "KB 미커버",
        ),
    ]


def _brief(**rec_over) -> SupervisorBrief:
    """가드 입력용 Brief — supervisor_recommendation 만 바꿔가며 검증."""
    base = dict(
        selected="limit_option", verdict="baseline_aging", reason="이유",
        evidence=["e1 [SPC]", "e2 [MODEL]", "e3 [CASE]"],
        counter_evidence="반증", confidence=0.8,
    )
    base.update(rec_over)
    return SupervisorBrief(
        report_id="SUP-20260713-SIMCH4-0028", incident_id=INCIDENT,
        chamber_id="SIM_CH_4", context_score=84,
        parallel_options={
            "recipe_option": {"report_id": "RCP-20260713-SIMCH4-0028", "confidence": 0.4},
            "limit_option": {"report_id": "LIM-20260713-SIMCH4-0028", "confidence": 0.45},
            "manual_option": {"report_id": "MNT-20260713-SIMCH4-0028", "confidence": 0.79},
        },
        supervisor_recommendation=base,
    )


# --- 가드 ⓞ: 레짐 도장(C12) 동반 온도 → escalate 강제 -------------------------
# 라운드 10 실측(2026-07-28): 에스컬 fixture 31~40 은 **10건이 전부 같은 모양**인데
# (위반=C17/N3 · shap=[C17,C12,C33]) 판정이 **5:5 로 갈렸다**. 프롬프트 두 규칙 사이에 빈틈이
# 있어(온도 규칙은 "C12 없으면", 레짐 규칙은 "C12 가 1위면") LLM 재량에 맡겨진 것.
# 코드로 확정한다 — 아래 테스트가 그 결정성을 고정한다.
ESCALATE_FIXTURES = [f"{n}_" for n in range(31, 41)]


def test_regime_stamp_guard_forces_escalate() -> None:
    """C17 위반 + C12 동반이면 verdict 를 escalate 로 교정하고 selected 를 비운다."""
    alert = _alert("32_escalate_gate_edge.json")
    brief = _brief(verdict="process_shift", selected="recipe_option")
    out = sup.enforce_regime_stamp_guard(brief, alert)
    rec = out.supervisor_recommendation
    assert rec.verdict == "escalate"
    assert rec.selected == sup.NONE_EXECUTABLE          # escalate 는 고를 옵션이 없다
    assert "레짐 도장" in rec.reason                     # 왜 바뀌었는지 근거에 남는다


def test_regime_stamp_guard_is_deterministic_across_all_escalate_fixtures() -> None:
    """에스컬 fixture 10건이 **전부** escalate 로 확정된다 — 5:5 로 갈리던 것을 고정.

    이 테스트가 깨지면 라운드 10 퇴행(escalate→process_shift 5건)이 되살아났다는 뜻이다.
    """
    seen = 0
    for path in sorted(FIXTURES.glob("*.json")):
        if not any(path.name.startswith(p) for p in ESCALATE_FIXTURES):
            continue
        alert = _alert(path.name)
        for wrong in ("process_shift", "baseline_aging", "equipment_fault"):
            out = sup.enforce_regime_stamp_guard(_brief(verdict=wrong), alert)
            assert out.supervisor_recommendation.verdict == "escalate", f"{path.name}/{wrong}"
        seen += 1
    assert seen == 10, f"에스컬 fixture 10건을 기대했는데 {seen}건"


def test_regime_stamp_guard_leaves_temp_without_c12_alone() -> None:
    """C17 위반이지만 C12 가 없으면 손대지 않는다 — 온도 튜닝 경로(②)를 막으면 안 된다.

    47번이 그 대조군이다(shap=[C17,C9,C11] — C12 없음). 이 테스트가 깨지면 온도 개통이 죽는다.
    """
    alert = _alert("47_process_shift_temp_drift.json")
    brief = _brief(verdict="process_shift", selected="recipe_option")
    out = sup.enforce_regime_stamp_guard(brief, alert)
    assert out.supervisor_recommendation.verdict == "process_shift"   # 그대로
    assert out.supervisor_recommendation.selected == "recipe_option"


def test_regime_stamp_guard_skips_when_c17_not_violated() -> None:
    """C17 이 SHAP 에만 끼어 있고 위반 센서가 아니면 대상이 아니다 (노후 fixture 보호).

    01~10 은 위반이 C11/C9/C62 인데 shap 에 C17 이 있다 — 여기까지 잡으면 통째로 escalate 로 샌다.
    """
    alert = _alert("02_baseline_aging_gate_edge.json")
    assert not any(v.sensor == "C17" for v in alert.violations)       # 전제 확인
    out = sup.enforce_regime_stamp_guard(_brief(verdict="baseline_aging"), alert)
    assert out.supervisor_recommendation.verdict == "baseline_aging"  # 손대지 않는다


# --- 가드 ①: 실행 불가 옵션 선택 차단 (가드 우회 방지) -------------------------
def test_executable_guard_blocks_unactionable_option() -> None:
    """조치가 비어 있는 옵션을 selected 로 고르면 무효화 — tool 가드 우회 차단.

    verdict 는 건드리지 않는다: 진단("낡았다")과 실행("지금 적용 가능한가")은 다른 질문.
    """
    out = sup.enforce_executable_guard(_brief(), _reports(limit_center=None))
    # null 이 아니라 sentinel — "고를 게 없다"(의도)와 "안 채웠다"(LLM 누락)를 가른다 (2026-07-22)
    assert out.supervisor_recommendation.selected == NONE_EXECUTABLE
    assert out.supervisor_recommendation.verdict == "baseline_aging"  # 진단 유지
    assert "실행 가능한 조치가 없다" in out.supervisor_recommendation.reason


def test_executable_guard_allows_actionable_option() -> None:
    """조치가 실려 있으면 통과."""
    out = sup.enforce_executable_guard(_brief(), _reports())
    assert out.supervisor_recommendation.selected == "limit_option"


def test_executable_guard_allows_approval_transfer_case() -> None:
    """A2 '승인 전환'은 escalate_reason 이 있어도 **수치가 유지**되므로 선택 가능하다.

    자동 적용만 막힌 것이지 조치가 없는 게 아니다 — 승인하면 적용된다(합의안 A2).
    가드가 문자열이 아니라 '수치 유무'를 보는 이유.
    """
    reports = _reports()
    reports[1] = reports[1].model_copy(
        update={"escalate_reason": "자동 적용 한계 초과 — 승인 전환 필요"}
    )
    out = sup.enforce_executable_guard(_brief(), reports)
    assert out.supervisor_recommendation.selected == "limit_option"


def test_has_actionable_proposal_per_option_type() -> None:
    """옵션마다 '조치'의 자리가 다르다 — recipe=value_proposed / limit=center_after / manual=action."""
    r, lim, man = _reports()
    assert sup.has_actionable_proposal(r) is False   # value_proposed None
    assert sup.has_actionable_proposal(lim) is True  # center_after 有
    assert sup.has_actionable_proposal(man) is True  # action 有


# --- 가드 ②: verdict ↔ selected 정합 -------------------------------------------
def test_verdict_option_guard_blocks_mismatch() -> None:
    """baseline_aging 인데 manual_option 을 고르면 선택 무효화 (VERDICT_TO_OPTION 대조)."""
    out = sup.enforce_verdict_option_guard(_brief(selected="manual_option"))
    assert out.supervisor_recommendation.selected == NONE_EXECUTABLE
    assert "정합 불일치" in out.supervisor_recommendation.reason


def test_verdict_option_guard_allows_match() -> None:
    """대응이 맞으면 통과."""
    for verdict, opt in (
        ("equipment_fault", "manual_option"),
        ("process_shift", "recipe_option"),
        ("baseline_aging", "limit_option"),
    ):
        out = sup.enforce_verdict_option_guard(_brief(verdict=verdict, selected=opt))
        assert out.supervisor_recommendation.selected == opt


def test_verdict_option_guard_allows_none_selected() -> None:
    """'고를 것 없음'은 항상 허용 — 진단은 섰지만 실행 불가한 경우가 정당하게 존재한다.

    null(LLM 누락)이든 sentinel(의도)이든 정합을 따질 대상이 아니므로 그대로 통과시킨다.
    """
    for sel in (None, NONE_EXECUTABLE):
        out = sup.enforce_verdict_option_guard(_brief(verdict="escalate", selected=sel))
        assert out.supervisor_recommendation.selected == sel


# --- 가드 ③: 유령 인용 ----------------------------------------------------------
def test_citation_guard_masks_unknown_id() -> None:
    """실존하지 않는 사례 ID 인용은 [미확인 인용] 으로 마스킹 — 거짓 권위 제거."""
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "유사 사례 [CASE-9999]"])
    out = sup.enforce_citation_guard(brief, _reports(), _alert(), [{"case_id": "CASE-0077"}])
    assert "[미확인 인용: CASE-9999]" in out.supervisor_recommendation.evidence[2]
    assert len(out.supervisor_recommendation.evidence) == 3  # 개수 유지(스키마 3개 고정)
    # 반증에도 사유를 병기 — 엔지니어가 "이 근거는 못 믿는다"를 알 수 있게
    assert "실존하지 않는 인용" in out.supervisor_recommendation.counter_evidence
    assert "CASE-9999" in out.supervisor_recommendation.counter_evidence


def test_citation_guard_passes_real_id() -> None:
    """실존 ID 는 그대로 둔다."""
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "사례 [CASE-0077]"])
    out = sup.enforce_citation_guard(brief, _reports(), _alert(), [{"case_id": "CASE-0077"}])
    assert out.supervisor_recommendation.evidence[2] == "사례 [CASE-0077]"


def test_citation_guard_skips_case_ids_without_cases() -> None:
    """cases 가 없으면 사례 ID 를 검증할 근거가 없다 → 강등하지 않는다 (오탐 방지)."""
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "사례 [CASE-9999]"])
    out = sup.enforce_citation_guard(brief, _reports(), _alert(), cases=None)
    assert "미확인" not in out.supervisor_recommendation.evidence[2]


def test_citation_guard_catches_fake_report_id() -> None:
    """리포트 ID 는 cases 없이도 검증 가능하다 — 우리가 발행한 ID 라서."""
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "리포트 LIM-9999-XXXX-0001 참조"])
    out = sup.enforce_citation_guard(brief, _reports(), _alert(), cases=None)
    assert "[미확인 인용: LIM-9999-XXXX-0001]" in out.supervisor_recommendation.evidence[2]


# --- 가드 ③ 보강: 재료 0장 (C7-1) ------------------------------------------------
# 「재료를 안 넘겼다(None)」와 「넘겼는데 0장이다([])」는 다른 상황이다. 구 판정식은 둘 다
# falsy 라 한 덩어리로 묶었고, 그 결과 **재료가 전부 0장일 때 CASE- 검증이 통째로 꺼졌다** —
# LLM 이 사례를 가장 지어내기 쉬운 상황에서 하필 가드가 없어지는 자리였다.
def test_citation_guard_flags_case_id_when_search_returned_zero() -> None:
    """검색은 했는데 0장인데 CASE- 를 인용하면 **유령이다** — 강등한다 (C7-1 핵심)."""
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "유사 사례 [CASE-9999]"])
    # 파이프라인이 실제로 넘기는 모양 — 슬롯은 있고 전부 0장(Qdrant 히트 0 또는 검색 실패)
    out = sup.enforce_citation_guard(
        brief, _reports(), _alert(), evidence_sources=[[], [], [], [], [], []]
    )
    assert "[미확인 인용: CASE-9999]" in out.supervisor_recommendation.evidence[2]
    assert "실존하지 않는 인용" in out.supervisor_recommendation.counter_evidence


def test_citation_guard_flags_case_id_when_cases_arg_is_empty_list() -> None:
    """구 `cases=` 인자 경로도 같다 — `[]` 는 '검증 불가'가 아니라 '없음'이다."""
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "유사 사례 [CASE-9999]"])
    out = sup.enforce_citation_guard(brief, _reports(), _alert(), cases=[])
    assert "[미확인 인용: CASE-9999]" in out.supervisor_recommendation.evidence[2]


def test_citation_guard_still_skips_when_all_sources_none() -> None:
    """재료를 아예 안 넘긴 호출은 **그대로 통과** — 오탐 방지 원칙은 유지한다.

    2026-07-20 에 실존 문서 ID 11 건을 유령으로 오탐한 이력이 있어, 이 경로를 좁히면 안 된다.
    """
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "유사 사례 [CASE-9999]"])
    out = sup.enforce_citation_guard(
        brief, _reports(), _alert(), evidence_sources=[None, None, None]
    )
    assert "미확인" not in out.supervisor_recommendation.evidence[2]


def test_citation_guard_flags_case_id_when_only_cases_slot_missing() -> None:
    """`cases` 슬롯만 미조회(None)여도 **다른 재료가 있으면 CASE- 는 검증한다** — 의도된 동작.

    `check_cases` 는 슬롯별이 아니라 **재료 전체에 대한 단일 토글**이다. cases 를 조회하지
    않았다면 LLM 입력에 사례가 없었다는 뜻이라, 그 상태의 `CASE-` 인용은 근거 자체가 없다.
    (설계 의도를 코드로 고정 — 자동 리뷰 확인 요청분 2026-08-04)
    """
    brief = _brief(evidence=["근거1 [SPC]", "근거2 [MODEL]", "유사 사례 [CASE-9999]"])
    out = sup.enforce_citation_guard(
        brief, _reports(), _alert(),
        # cases 미조회(None) · kb_hits 는 실재 → 토글 ON, CASE- 도 검증 대상이 된다
        evidence_sources=[None, [{"doc_id": "PK-TUNE-GAS"}], None],
    )
    assert "[미확인 인용: CASE-9999]" in out.supervisor_recommendation.evidence[2]


def test_citation_guard_keeps_real_ids_when_search_returned_zero() -> None:
    """0장이어도 alert·리포트 ID 는 실존한다 — 싸잡아 강등하지 않는다."""
    alert = _alert()
    brief = _brief(
        evidence=["근거1 [SPC]", "근거2 [MODEL]", f"알람 {alert.alert_id} 기준 판정"]
    )
    out = sup.enforce_citation_guard(brief, _reports(), alert, evidence_sources=[[], []])
    assert "미확인" not in out.supervisor_recommendation.evidence[2]
    assert alert.alert_id in out.supervisor_recommendation.evidence[2]


# --- 가드 ④: 기각 사유 누락 ------------------------------------------------------
def test_rejection_reason_guard_fills_missing() -> None:
    """선택 안 한 옵션에 기각 사유가 없으면 '미기재' 표지 — 사유를 창작하지 않는다(§0)."""
    out = sup.enforce_rejection_reason_guard(_brief(selected="limit_option"))
    assert out.parallel_options["recipe_option"].rejected_because == sup._NO_REASON
    assert out.parallel_options["manual_option"].rejected_because == sup._NO_REASON
    assert out.parallel_options["limit_option"].rejected_because is None  # 선택된 건 제외


# --- fallback (§6) ---------------------------------------------------------------
# --- 구 가드⑤(enforce_scrap_guard) 은퇴 2026-07-24 ------------------------------
# wafer 처분을 코드가 판정(apply_disposition, C6-1)하게 되며 LLM 의 SCRAP 을 사후 강등하던
# 가드⑤가 제거됐다. 이중 확인(예측 초과+anomaly)은 이제 decide_wafer 가 처음부터 수행한다 —
# 판정·배선 커버리지는 test_disposition.py. 관련 4개 테스트는 함께 은퇴.


def test_fallback_brief_escalates_without_inventing_verdict() -> None:
    """파싱 실패 시 판정을 창작하지 않고 escalate — 근거 없는 판정이 승인 큐에 뜨면 안 된다."""
    b = make_fallback_brief(_alert(), INCIDENT)
    assert b.supervisor_recommendation.verdict == "escalate"
    assert b.supervisor_recommendation.selected == NONE_EXECUTABLE  # 침묵 대신 명시
    assert b.supervisor_recommendation.confidence == 0.0
    assert b.approval_status == "PENDING"
    assert b.wafer_disposition.recommendation == "HOLD"


# --- 관통 (C5-3 DoD: Brief 1건 생성) ----------------------------------------------
SUP_JSON = json.dumps(
    {
        "report_id": "SUP-LLM이-지어낸-값",
        "incident_id": "INC-지어낸값",
        "chamber_id": "SIM_CH_9",
        # 게이트(31) 아래 값을 일부러 넣는다 — 이 값이 새어나가면 "게이트를 통과한 알람인데
        # Brief 에는 게이트 미만 점수"라는 모순이 화면에 뜬다. 모의는 적대적이어야 구멍을 잡는다.
        "context_score": 11,
        "suspected_root_causes": ["C32 단발 급변 [SPC]"],
        "parallel_options": {
            "recipe_option": {
                "report_id": "RCP-20260713-SIMCH4-0028", "action": "없음",
                "feasibility": "N/A", "confidence": 0.4, "rejected_because": "손잡이 축 아님",
            },
            "limit_option": {
                "report_id": "LIM-20260713-SIMCH4-0028", "action": "재설정",
                "feasibility": "HIGH", "confidence": 0.45, "rejected_because": "추세 근거 약함",
            },
            "manual_option": {
                "report_id": "MNT-20260713-SIMCH4-0028", "action": "Focus Ring 교체",
                "feasibility": "LOW", "confidence": 0.79,
            },
        },
        "supervisor_recommendation": {
            "decision_frame": "4지선다", "selected": "manual_option",
            "verdict": "equipment_fault", "reason": "장비 이상 — Focus Ring 교체 권고.",
            "evidence": ["N1 CRITICAL 급변 [SPC]", "anomaly 동반 [MODEL]", "매뉴얼 증상 일치 [KB]"],
            "counter_evidence": "추세 성분도 일부 존재", "confidence": 0.79,
        },
        "wafer_disposition": {
            "held_wafers": [], "recommendation": "HOLD", "reason": "예측 초과 [MODEL]",
        },
        "approval_status": "PENDING",
    },
    ensure_ascii=False,
)


def _run_brief(alert: AlertModel, sup_json: str = SUP_JSON):
    backend = RoutingBackend(MOCK_BY_TYPE, responses=[sup_json])
    return asyncio.run(
        handle_alert_with_brief(
            alert, SETTINGS, backend=backend,
            recipe_inputs=STUB_RECIPE_INPUTS, manual_inputs=STUB_MANUAL_INPUTS,
            incident_id=INCIDENT,
        )
    )


def test_escalate_null_is_normalized_to_sentinel() -> None:
    """verdict=escalate 인데 LLM 이 selected 를 비우면 코드가 sentinel 로 채운다.

    **정상 Brief 는 selected 가 절대 null 이 아니다** — 그래야 남은 null 이
    "LLM 이 칸을 건너뛴 것"이라는 단일한 뜻을 갖는다(소비자가 코드로 진단 가능).
    비워두면 '의도'와 '누락'이 화면에서 똑같이 빈칸이라 사람이 구분을 포기한다.
    """
    sup_json = json.loads(SUP_JSON)
    sup_json["supervisor_recommendation"]["verdict"] = "escalate"
    sup_json["supervisor_recommendation"]["selected"] = None
    _, brief = _run_brief(_alert(), json.dumps(sup_json, ensure_ascii=False))
    assert brief.supervisor_recommendation.selected == NONE_EXECUTABLE


def test_non_escalate_null_is_left_as_signal() -> None:
    """verdict 가 escalate 가 아닌데 null 이면 **일부러 두지 않는다** — 그게 이상 신호다.

    고를 옵션이 있는 판정인데 선택이 비었다는 건 LLM 이 건너뛴 것이다. sentinel 로 덮으면
    '의도적으로 없음'으로 위장돼 영영 안 드러난다.
    """
    sup_json = json.loads(SUP_JSON)
    sup_json["supervisor_recommendation"]["verdict"] = "baseline_aging"
    sup_json["supervisor_recommendation"]["selected"] = None
    _, brief = _run_brief(_alert(), json.dumps(sup_json, ensure_ascii=False))
    assert brief.supervisor_recommendation.selected is None      # 신호로 남는다
    assert brief.supervisor_recommendation.verdict == "baseline_aging"


def test_brief_disposition_is_code_decided(monkeypatch) -> None:
    """C6-1 배선 — Supervisor 통과 후 wafer_disposition 이 **코드 판정**으로 채워진다.

    DB 조회를 빈 결과로 고정해 외부 의존을 없앤다 — 그러면 **alert 보험 경로만** 남는다.
    C6-2 배선 전에는 여기서 전 wafer HOLD(값 없음)였는데, 배선 후에는 조회가 비어도
    **대표 wafer 1장은 alert `prediction_context` 값으로 판정된다**(레이스 보험).
    수치는 alert 이 실어온 A 예측값 그대로이고 코드가 지어내지 않는다.
    """
    from agent_service.app import db as db_mod

    monkeypatch.setattr(db_mod, "fetch_predictions", lambda *a, **k: {})

    alert = _alert()
    pc = alert.prediction_context
    _, brief = _run_brief(alert)
    wd = brief.wafer_disposition

    assert wd.per_wafer, "per_wafer 가 채워져야 한다(코드 판정 배선)"
    rep = next((v for v in wd.per_wafer if v.wafer_id == pc.wafer_id), None)
    assert rep is not None, "대표 wafer 는 DB 미적재여도 alert 값으로 판정돼야 한다"
    assert rep.predicted_c65 == pc.predicted_c65               # 인용이지 창작이 아니다

    # 임계(B9) 비교 결과는 fixture 값에 달려 있다 — 판정 자체를 못박지 않고 트리 정합만 본다.
    threshold = float(SETTINGS.require("spc.crazy_wafer.predicted_c65_threshold"))
    expected = "RELEASE" if pc.predicted_c65 <= threshold else "HOLD"  # 초과+anomaly 미확정 → HOLD
    assert rep.recommendation == expected

    # 조회로 못 채운 나머지 wafer 는 여전히 "조회 불가 → HOLD"(원위치) — 창작 금지 유지
    others = [v for v in wd.per_wafer if v.wafer_id != pc.wafer_id]
    assert all(v.predicted_c65 is None and v.recommendation == "HOLD" for v in others)


def test_pipeline_yields_three_reports_and_one_brief() -> None:
    """C5-3 DoD — alert 1건 → 리포트 3건 → Brief 1건 (헌법 1-4 통합)."""
    reports, brief = _run_brief(_alert())
    assert len(reports) == 3
    assert brief is not None
    assert brief.incident_id == INCIDENT
    assert brief.approval_status == "PENDING"


def test_brief_report_id_is_stamped_not_llm_value() -> None:
    """LLM 이 지어낸 report_id·incident_id 는 버리고 우리가 스탬프한다 (6-4·결정성)."""
    _, brief = _run_brief(_alert())
    assert brief.report_id == "SUP-20260713-SIMCH4-0028"
    assert "지어낸" not in brief.report_id
    assert brief.incident_id == INCIDENT
    # chamber_id 는 alert 에서 승계 — LLM 이 SIM_CH_9 라고 우겨도 무시한다.
    # 틀린 챔버가 실리면 엔지니어가 엉뚱한 설비를 연다(헌법 1-4: Incident 는 챔버 1개).
    assert brief.chamber_id == "SIM_CH_4"
    # context_score 도 마찬가지 — **B 가 계산한 게이트 점수**다(B7). LLM 이 11 이라고 써도
    # alert 값으로 덮는다. S3 승인 큐·S4 가 이 숫자를 렌더하므로, 새어나가면 엔지니어가
    # 보는 점수가 alert 과 어긋난다(§0 수치 원칙 — 입력 값만 인용).
    assert brief.context_score == _alert().context_score


def test_gate_closed_yields_no_brief() -> None:
    """게이트 미통과면 리포트도 Brief 도 없다 — Supervisor 를 아예 안 부른다(B7)."""
    backend = MockBackend([])  # 호출되면 RuntimeError
    reports, brief = asyncio.run(
        handle_alert_with_brief(
            _alert("01_baseline_aging_below_gate.json"), SETTINGS, backend=backend
        )
    )
    assert reports == [] and brief is None
    assert backend.calls == []


def test_brief_falls_back_when_llm_unparseable() -> None:
    """Supervisor 응답이 계속 깨져도 파이프라인은 산다 — escalate fallback (§6·헌법 6-3)."""
    backend = RoutingBackend(MOCK_BY_TYPE, responses=["깨진 JSON"] * 3)
    _, brief = asyncio.run(
        handle_alert_with_brief(
            _alert(), SETTINGS, backend=backend,
            recipe_inputs=STUB_RECIPE_INPUTS, manual_inputs=STUB_MANUAL_INPUTS,
            incident_id=INCIDENT,
        )
    )
    assert brief.supervisor_recommendation.verdict == "escalate"
    assert brief.report_id == "SUP-20260713-SIMCH4-0028"


def test_summarize_report_drops_evidence_cards() -> None:
    """Brief 입력은 요약 — evidence_cards 등 무거운 필드는 싣지 않는다(payload 크기)."""
    s = sup.summarize_report(_reports()[1])
    assert "evidence_cards" not in s
    assert s["report_id"] == "LIM-20260713-SIMCH4-0028"
    assert s["center_after"] == -302.4


def test_citation_guard_accepts_ids_from_all_evidence_sources() -> None:
    """정답지는 **tool 에 들어간 모든 근거 재료**여야 한다 (2026-07-20 실측 오탐 11건).

    cases 만 정답지로 쓰면 knob_map·KB 문서의 실존 ID(PK-…/EM-…)가 유령으로 오탐된다.
    """
    brief = _brief(evidence=[
        "손잡이 매핑 [PK-TUNE-PLASMA]", "매뉴얼 증상 [EM-042]", "사례 [CASE-0077]",
    ])
    out = sup.enforce_citation_guard(
        brief, _reports(), _alert(),
        evidence_sources=[
            [{"case_id": "CASE-0077"}],          # cases
            [{"doc_id": "EM-042"}],              # manual_hits
            [{"doc_id": "PK-TUNE-PLASMA"}],      # knob_map
        ],
    )
    for e in out.supervisor_recommendation.evidence:
        assert "미확인 인용" not in e


def test_citation_guard_still_catches_ghost_among_real_sources() -> None:
    """재료를 다 줘도 **없는 ID 는 여전히 잡는다** — 가드가 무력화되면 안 된다."""
    brief = _brief(evidence=[
        "근거1 [SPC]", "매뉴얼 [EM-042]", "사례 [CASE-9999]",  # CASE-9999 는 없음
    ])
    out = sup.enforce_citation_guard(
        brief, _reports(), _alert(),
        evidence_sources=[[{"case_id": "CASE-0077"}], [{"doc_id": "EM-042"}]],
    )
    assert "[미확인 인용: CASE-9999]" in out.supervisor_recommendation.evidence[2]
    assert "[EM-042]" in out.supervisor_recommendation.evidence[1]  # 실존은 그대로
