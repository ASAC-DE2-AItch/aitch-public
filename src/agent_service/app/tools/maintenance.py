"""🔧 정비 조수 (tool ③) — 장비 이상 가능성 → 정비 조치안 리포트.

정본: docs/Agent_프롬프트_라이브러리_v1.md §3 (역할 프롬프트·출력 스키마).

경계 (헌법 1-1 / 라이브러리 §3):
- **조치는 manual_hits(Error Manual)·cases 에 있는 것만** 제안한다. 매뉴얼에 없는 정비 절차를
  창작하지 않는다 — 정비는 사람이 설비를 여는 물리 조치라 창작의 대가가 크다.
- 이 tool 은 조치'안'(리포트)만 만든다. 장비 정비 권고 확정은 승인 게이트 통과 후에만(헌법 1-1).
- wafer SCRAP 은 rework 불가라 "예측 초과 + anomaly 이중 확인" 없이 단정하지 않는다(§3 규칙3).

recipe.py 와 같은 뼈대(재료→payload→generate_structured→가드)를 쓰되 **가드 구성이 다르다**:
  · 숫자 가드 → **출처 가드**로 변형 (B 정량 API 가 없다 — 정답지가 검색 결과다)
  · 손잡이·delta 가드 = 없음 (정비엔 setpoint 개념이 없다)
  · downtime 가드 신설 (§3 규칙4 — 매뉴얼·사례에 없는 다운타임 창작 금지)

3조수 중 **유일하게 B 정량 API 도 Postgres 로그도 없다** (아키텍처 다이어그램 §2) —
근거가 전부 RAG 검색이라 "재료 없으면 값도 없다"가 가드의 뼈대다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import Settings
from ..llm.client import LlmBackend, generate_structured
from ..schemas.alert import AlertModel
from ..schemas.report import MNT_PREFIX, ManualOption, make_fallback_report, make_report_id

from ..observe import strip_ablated_alert
from .base import (CONFIDENCE_RUBRIC, EVIDENCE_CARD_RULES, primary_violation,
                   system_prompt)

logger = logging.getLogger(__name__)

name = "maintenance"

# --- few-shot 정답 사례 (라이브러리 §3 — 문서가 단일 소스, 개정 시 문서 먼저) --------
# 목적: confidence 인플레·불가역 오답 교정. maintenance 는 "뭘 찾아도 매뉴얼 항목이 있어"
#   늘 자신있게 정비를 제안하는 경향이 실측됐다(R8 불가역 오답 7건 전부 추세→정비 오판).
# 방식: **대조쌍** — 진짜 장비이상(급변+anomaly)엔 나가고, 추세만이면 물러선다(conf↓·escalate).
#   한 방향만 넣으면 반대로 쏠려 진짜 정비를 놓치므로(가역성 행렬 불가역 행=0 유지) 반드시 쌍으로.
# 값은 학습용 가공치 — 그대로 베끼지 말 것(수치는 입력에서 인용, report_id 는 코드가 채움).
FEWSHOT_EXAMPLES = """
[학습 예시 — 판단 보정] 아래는 "입력 상황 → 올바른 리포트"다. 수치를 베끼지 말고 **언제 정비를 제안하고 언제 물러서는지**를 배워라.

# 예시 A — 진짜 장비 이상(급변 + anomaly 동반) → 정비 제안, 높은 confidence
입력 요지: violations=[{sensor:C31, rule_id:N1, severity:CRITICAL, window:transient}] · anomaly_score 동반 상승 · 단일 챔버 국한(reference_suspect=false) · manual_hits 1건(출력 불안정 증상) · cases 1건(focus ring 교체 성공)
올바른 출력: {"action":"Focus Ring 점검·교체","symptom_match":"C31 N1 단발 급변 + anomaly 동반 — 매뉴얼 증상 일치 [KB]","rationale":"① N1 CRITICAL 급변 — 추세 아닌 이벤트 [SPC] ② anomaly 동반 — 센서 오류 아닌 상태 변화 [MODEL] ③ 매뉴얼 증상 일치 [KB] ④ 동일 증상 사례 교체로 종결 [CASE]","escalate_reason":null}

# 예시 B — 추세만·급변/anomaly 없음 → 물러섬(정비 근거 부족), 낮은 confidence
입력 요지: violations=[{sensor:C9, rule_id:N3, severity:WARNING, window:settled}] · anomaly 미동반 · 급변 없음 · manual_hits 에 온도 경고 '항목은 존재' · cases 유사도 낮음
올바른 출력: {"action":null,"symptom_match":"C9 N3 추세(6점 연속 상승)만 — 급변(N1/CRITICAL)·anomaly 부재로 장비 이상 전형 신호 없음 [SPC]","rationale":"① N3 추세만·급변 없음 — 갑작스런 고장 아님 [SPC] ② anomaly 미동반 — 상태 급변 아님 [MODEL] ③ 매뉴얼에 온도 항목은 있으나 '항목 존재'는 정비 근거가 아니다 [KB] ④ 추세성은 기준선 노후·공정 이탈 가능성 — 정비 단정 불가","escalate_reason":"추세성 이상 — 정비 근거 부족. 노후(실력치 재설정) 또는 공정 이탈 가능성, 근거 패키지로 에스컬레이션"}

⚠️ 핵심: manual_hits 에 관련 항목이 **있다는 사실만으로는** 정비 근거가 아니다(매뉴얼은 대부분 센서에 항목이 있다). 급변·anomaly 같은 **변별 신호**가 있을 때만 confidence 를 높인다.
⚠️ 위 예시에 **confidence 가 없는 것은 의도**다. 확신도는 **이 사안의 근거 강도에서 직접 산출**하라 — 예시 값도, 관례적인 값도, 다른 옵션과 맞춘 값도 쓰지 않는다.
⚠️ 예시의 `<...>` 는 자리 표시다. 인용 ID 는 **실제로 받은 재료의 ID** 만 쓴다 — 예시의 ID 를 그대로 옮기면 없는 근거를 지어낸 것이 된다.
"""

# --- 역할 프롬프트 (라이브러리 §3 — 문서가 단일 소스, 개정 시 문서 먼저) ------------
SYSTEM_PROMPT = (
    """[역할] 장비 이상 가능성에 대한 정비 조치안 리포트를 작성한다.

[입력]
- alert: fdc.alert 원문 (특히 severity, anomaly 계열, 급변 패턴)
- manual_hits: Error Manual 검색 결과 (증상→원인→조치, doc_id·페이지)
- cases: 유사 사례 (조치·다운타임·성공 여부)

[작성 규칙]
1. 조치는 manual_hits와 cases에 나온 것만 제안한다. 매뉴얼에 없는 정비 절차를 창작하지 않는다.
   manual_hits와 cases가 모두 비어 있으면 action=null + escalate_reason에 "KB 미커버"를 쓴다.
2. "진짜 장비 이상"의 전형 신호를 근거에 명시: N1/CRITICAL 급변, anomaly_score 동반 상승, 단일 챔버 국한,
   물리 신호(파워·온도·압력 계열) 이상. 추세 룰만 있고 급변·anomaly가 없으면 정비 근거 부족을 명시한다.
2-1. **사례의 결말은 정비 근거가 아니다** — "유사 사례가 정비로 종결됨"은 참고일 뿐 급변 관문을
   대체하지 못한다(§4 트리 STEP 2 ⛔와 동일 취지). 매뉴얼 항목 존재와 같은 이유로 변별력이 없다:
   노후·공정 이탈 건에서도 사례는 정비로 끝나 있기 때문이다. 급변·anomaly가 없는데 사례만
   근거로 정비를 제안하지 않는다.
3. crazy wafer(HOLD) 동반 시 wafer 처분 의견을 disposition_note에 쓴다 — 스크랩은 rework 불가이므로
   "예측+anomaly 이중 확인" 없이 SCRAP을 단정하지 않는다 (승인은 어차피 엔지니어 몫).
4. downtime은 매뉴얼·사례의 값만 인용. 없으면 null + "매뉴얼에 다운타임 정보 없음".
"""
    + FEWSHOT_EXAMPLES
    + CONFIDENCE_RUBRIC
    + EVIDENCE_CARD_RULES
    + "\n[JSON 스키마로만 응답]"
)

# 프롬프트에 중복 탑재할 JSON 예시 (헌법 6-3 이중 방어 — guided_json 과 별개).
SCHEMA_EXAMPLE = """{
  "report_id": "MNT-20260713-SIMCH3-001",
  "option_type": "manual_option",
  "action": "Focus Ring 마모 점검 및 교체",
  "target_component": "focus_ring",
  "symptom_match": "C31(RF_Forward) 단발 +6σ 급변 + anomaly 동반 — 매뉴얼 §4.2 '출력 불안정' 증상과 일치 [KB:<검색된 문서 ID>]",
  "estimated_downtime_h": 8.0,
  "similar_cases": [{"case_id": "<검색된 사례 ID>", "similarity": 0.83, "action_taken": "focus ring 교체", "is_success": true}],
  "disposition_note": "해당 wafer는 predicted_c65가 B9 임계 초과 + anomaly 동반 — HOLD 유지, 스크랩 여부는 승인 판단 [MODEL]",
  "rationale": "① N1 CRITICAL 단발 급변 — 추세가 아니라 이벤트 [SPC] ② anomaly_score 동반 — 센서 오류 아닌 상태 변화 [MODEL] ③ 매뉴얼 증상 일치 [KB] ④ 동일 증상 사례 1건 교체로 종결 [CASE]",
  "feasibility": "LOW — 정지 8h 필요",
  "escalate_reason": null,
  "evidence_cards": [],
  "confidence": "<근거 층수에서 산출 — 위 규칙>",
  "uncertainty": "manual_hits 1건뿐 — 증상 매칭이 단일 문서 의존"
}"""


# --- 검색어 빌더 (순수 함수 — alert 만으로 구성, Qdrant 불필요) -----------------------
# ⚠️ recipe 의 검색어 실측(2026-07-18)은 process_knowledge·historical_case 에 대한 것이다.
#    error_manual 은 **다른 컬렉션**(증상→원인→조치 구조)이라 최적 검색어가 다를 수 있다.
#    아래는 "증상 서술 중심" 초안 — EC2 스모크에서 검색 품질을 실측해 조정한다(C5-2).
def build_manual_query(alert: AlertModel) -> str:
    """Error Manual 검색어 — 증상 서술 중심(매뉴얼이 증상→원인→조치 구조라 서술이 매칭 키).

    severity 를 포함해 급변/CRITICAL 계열 증상 문서로 유도한다.
    B9 crazy 마커는 배제한다 — 'C65' 로 정비 매뉴얼을 뒤지면 엉뚱한 문서가 걸린다.
    """
    v = primary_violation(alert)
    if v is None:
        return ""
    parts = (v.sensor, v.sensor_name or "", v.description or "", v.severity)
    return " ".join(str(t) for t in parts if t).strip()


def build_case_query(alert: AlertModel) -> str:
    """historical_case 검색어 — 룰+서술 포함(recipe 1-3 실측과 동일 성질의 컬렉션)."""
    v = primary_violation(alert)  # B9 마커 배제 (rule_id='B9' 는 사례 매칭 키가 아님)
    if v is None:
        return ""
    parts = (v.sensor, v.sensor_name or "", v.rule_id or "", v.description or "")
    return " ".join(t for t in parts if t).strip()


def build_payload(
    alert: AlertModel,
    *,
    manual_hits: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
) -> str:
    """LLM 입력(user 메시지) 직렬화 — 라이브러리 §3 [입력] 항목 순서 그대로."""
    return json.dumps(
        {
            "alert": strip_ablated_alert(alert.model_dump(mode="json")),
            "manual_hits": manual_hits or [],
            "cases": cases or [],
        },
        ensure_ascii=False,
    )


# --- 가드 ①: 출처 가드 — 재료가 없으면 조치도 없어야 한다 (§3 규칙1, §6 KB 미커버) ----------
# recipe·limit 의 "숫자 가드"에 대응하지만 정답지가 B 수치가 아니라 **검색 결과**다.
# 정비 조치를 지어내면 사람이 설비를 열게 되므로(물리 위험) 코드로 강제한다.
#
# 검증 범위는 의도적으로 **"재료 유무"까지만** 둔다. 조치 문구가 매뉴얼 본문과 얼마나 일치하는지를
# 코드가 문자열로 판정하려 들면 오탐이 쏟아지고(동의어·요약·번역) LLM 판단을 죽인다 —
# "막을 수 있다 ≠ 막아야 한다". 문구 수준의 충실도는 프롬프트와 근거 카드(§5)가 담당한다.
def enforce_source_guard(
    report: ManualOption,
    manual_hits: list[dict[str, Any]] | None,
    cases: list[dict[str, Any]] | None,
) -> ManualOption:
    """manual_hits·cases 가 **모두 비었는데** 조치를 냈으면 강등한다 — 창작 차단.

    재료가 하나라도 있으면 통과(그 재료에 근거했는지는 프롬프트·근거 카드 소관).
    조치가 없으면(action=None) 검사할 게 없다.
    """
    if report.action is None:
        return report
    if manual_hits or cases:
        return report  # 근거 재료 존재 — 통과
    reason = (
        "manual_hits·cases 모두 0건인데 정비 조치를 냄 — 조치 창작 금지(§3 규칙1). "
        "KB 미커버 알람 → 조치 무효화·에스컬레이션(§6)."
    )
    merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
    logger.warning(
        "출처 가드 강등: report=%s action=%r (manual_hits·cases 0건)",
        report.report_id,
        report.action,
    )
    return report.model_copy(
        update={
            "action": None,  # 근거 없는 조치는 승인 큐로 못 간다
            "target_component": None,
            "estimated_downtime_h": None,
            "escalate_reason": merged,
        }
    )


# --- 가드 ②: downtime 가드 — 매뉴얼·사례에 없는 다운타임 창작 금지 (§3 규칙4) --------------
# 다운타임은 라인 정지 시간 = 생산 계획에 직결되는 수치다. 지어낸 값이 승인 화면에 뜨면
# 엔지니어가 그걸 근거로 판단하게 된다. 재료에 값이 없으면 리포트에도 없어야 한다.
def _downtime_values(
    manual_hits: list[dict[str, Any]] | None, cases: list[dict[str, Any]] | None
) -> bool:
    """재료(manual_hits·cases)에 다운타임 정보가 하나라도 있는지."""
    keys = ("estimated_downtime_h", "downtime_h", "downtime")
    for item in list(manual_hits or []) + list(cases or []):
        if isinstance(item, dict) and any(item.get(k) is not None for k in keys):
            return True
    return False


def enforce_downtime_guard(
    report: ManualOption,
    manual_hits: list[dict[str, Any]] | None,
    cases: list[dict[str, Any]] | None,
) -> ManualOption:
    """재료에 다운타임 근거가 없는데 estimated_downtime_h 를 냈으면 지운다 (§3 규칙4).

    uncertainty 에 사유를 병기해 "값이 사라진 이유"가 리포트에 남게 한다 — 조용히 지우면
    엔지니어가 "다운타임 정보 없음"과 "우리가 지웠음"을 구분할 수 없다.
    """
    if report.estimated_downtime_h is None:
        return report
    if _downtime_values(manual_hits, cases):
        return report  # 근거 있음 — 통과
    note = "매뉴얼·사례에 다운타임 정보 없음 — 추정값 제거(§3 규칙4)."
    logger.warning(
        "downtime 가드: report=%s estimated_downtime_h=%s 제거 (재료에 근거 없음)",
        report.report_id,
        report.estimated_downtime_h,
    )
    return report.model_copy(
        update={
            "estimated_downtime_h": None,
            "uncertainty": f"{report.uncertainty} / {note}" if report.uncertainty else note,
        }
    )


async def run(
    alert: AlertModel,
    backend: LlmBackend,
    settings: Settings,
    *,
    manual_hits: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
) -> ManualOption:
    """정비 조치안 리포트 1건 생성 — LLM 실호출 (재시도·fallback 은 generate_structured 소관).

    Args:
        alert: 게이트를 통과한 fdc.alert.
        backend: 주입된 LLM 백엔드 (테스트=MockBackend / 운영=VllmBackend).
        settings: 재시도 예산의 config 정본.
        manual_hits: error_manual 검색 결과(근거 ③). pipeline 이 검색해 주입.
        cases: historical_case 검색 결과(근거 ④). None/[] 이면 D5 정책대로 "사례 없음" 명시.
            manual_hits·cases 가 모두 비면 §6 KB 미커버 경로(action=null)로 간다.
    """
    payload = build_payload(alert, manual_hits=manual_hits, cases=cases)

    report = await generate_structured(
        backend,
        system_prompt(SYSTEM_PROMPT, FEWSHOT_EXAMPLES),
        payload,
        ManualOption,
        fallback=lambda: make_fallback_report(ManualOption, alert),
        schema_example=SCHEMA_EXAMPLE,
        parse_retries=int(settings.require("agent.json_parse_retry")),
        call_retries=int(settings.require("llm.call_retry")),
    )

    # report_id 는 LLM 이 정할 값이 아니다 — 예시를 그대로 베끼는 것이 실측됨(recipe.py 참조).
    report = report.model_copy(update={"report_id": make_report_id(MNT_PREFIX, alert)})

    # 코드 강제 가드 2종 (물리 조치·생산 계획에 직결되는 값만 — 나머지는 프롬프트 소관).
    # 순서 = 출처 → downtime: 출처 가드가 조치를 지우면 downtime 도 함께 지워지므로 먼저.
    report = enforce_source_guard(report, manual_hits, cases)
    report = enforce_downtime_guard(report, manual_hits, cases)

    logger.debug(
        "maintenance report: %s (alert %s, action=%r, conf=%.2f)",
        report.report_id,
        alert.alert_id,
        report.action,
        report.confidence,
    )
    return report
