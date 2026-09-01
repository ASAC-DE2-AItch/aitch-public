"""🎛️ 레시피 조수 (tool ①) — 공정 조건 이탈 → 원인 후보 + 레시피 튜닝안 리포트.

정본: docs/Agent_프롬프트_라이브러리_v1.md §1 (역할 프롬프트·출력 스키마).
C5-1 = "Root Cause Analysis + Recipe Tuning 리포트 조립" (일정표) — 완료 조건: 원인 후보+튜닝안+근거.

경계 (헌법 1-1 / 라이브러리 §1):
- **수치는 B5-4 튜닝 API 가 유일한 소스**다. LLM 은 인용만 하고 delta 를 재계산하지 않는다.
  B5-4(7/31) 전까지는 인터페이스 스텁으로 선행한다 — 일정표 C5-1 명시.
- 이 tool 은 튜닝'안'(리포트)만 만든다. 적용(fdc.correction 발행)은 승인 게이트 통과 후에만.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..config import Settings
from ..llm.client import LlmBackend, generate_structured
from ..schemas.alert import AlertModel
from ..schemas.report import RCP_PREFIX, RecipeOption, make_fallback_report, make_report_id

from ..observe import strip_ablated_alert
from .base import (CONFIDENCE_RUBRIC, EVIDENCE_CARD_RULES, primary_violation, real_violations,
                   system_prompt)

logger = logging.getLogger(__name__)

name = "recipe"

# --- few-shot 정답 사례 (라이브러리 §1 — 문서가 단일 소스, 개정 시 문서 먼저) --------
# 대조쌍: 조정 가능한 손잡이(knob_map 등재)면 튜닝안을 내고, 원인이 레짐 신호(C12)면 물러선다.
# 한 방향만 넣으면 쏠린다(maintenance few-shot 과 같은 취지 — TSR/학습노트). 값은 학습용 가공치.
FEWSHOT_EXAMPLES = """
[학습 예시 — 판단 보정] 아래는 "입력 상황 → 올바른 리포트"다. 수치를 베끼지 말고 **언제 튜닝안을 내고 언제 물러서는지**를 배워라.

# 예시 A — 조정 가능한 손잡이(가스 축) → 튜닝안 제시
입력 요지: SHAP 상위 C15(가스 측정) · knob_map 가스 축 knob_param=C4 · tuning delta_pct=-2.0(D6 ±3% 이내) · process_shift 신호
올바른 출력: {"parameter_id":"C4","value_proposed":39.2,"delta_pct":-2.0,"hypothesis":"process_condition_shift","rationale":"① N3 추세가 가스 계열에서 시작 [SPC] ② SHAP 상위 축=가스 → knob_map 손잡이 C4 [KB] ③ 유사 사례 소폭 튜닝으로 개선 [CASE]","escalate_reason":null}

# 예시 B — 원인이 레짐 신호(C12) → 물러섬(손잡이 아님)
입력 요지: SHAP 상위 C12(Vdc 연동 기준값 = 레짐 도장) · knob_map 어느 축 knob_param 에도 없음 · 압력 축은 knob_param='미확정'
올바른 출력: {"parameter_id":null,"value_proposed":null,"delta_pct":null,"hypothesis":"regime_signal","rationale":"① 원인 후보 상위가 C12 — 조정 손잡이가 아니라 레짐(챔버 상태) 신호 [SPC] ② knob_map 어느 축에도 등재 안 됨 — 튜닝 대상 없음 [KB] ③ 이건 손잡이로 되돌릴 문제가 아니다","escalate_reason":"레짐 신호(C12) — 레시피 튜닝 대상 아님. 상태 점검/에스컬레이션."}

# 예시 C — 온도 축 완만 드리프트 → 목표 지정형 튜닝안 (2026-07-26 멘토 확정: 온도는 레짐이 아니라 가변 축)
입력 요지: SHAP 상위 C17(척 온도 실측) · rule_id=N3 추세(급변 없음) · C12 미동반 · knob_map 온도 축 knob_param='temp_target' · tuning delta_pct=-1.2
올바른 출력: {"parameter_id":"temp_target","value_proposed":58.2,"delta_pct":-1.2,"hypothesis":"process_condition_shift","rationale":"① N3 추세만·급변 없음 — 이벤트가 아니라 이동 [SPC] ② SHAP 상위 C17 → 온도 축, 손잡이는 temp_target(목표 지정형) [KB] ③ 유사 사례에서 목표 온도 소폭 하향으로 개선 [CASE]","escalate_reason":null}

# 예시 D — 온도 축이지만 이 Incident 에서 이미 1스텝 소진 → 물러섬 (사유를 옮긴다)
입력 요지: SHAP 상위 C17 · N3 추세 · C12 미동반 · tuning 수치 전부 null + escalate_reason="온도축 — 이 Incident에서 이미 1스텝 튜닝(반복) — 명목 앵커 부재로 사람 판단으로 에스컬(A)"
올바른 출력: {"parameter_id":null,"value_proposed":null,"delta_pct":null,"hypothesis":"insufficient_evidence","rationale":"① N3 추세·급변 없음 — 온도 축 자체는 조정 대상 [SPC] ② 그러나 이번 Incident 에서 온도는 이미 1회 조정됨 [SPC] ③ 온도는 명목 앵커가 없어 누적 상한을 잴 수 없다 — 반복 조정은 사람 판단 [KB]","escalate_reason":"이번 Incident 에서 온도는 이미 1회 조정됨 — 추가 조정은 사람 판단(명목 앵커 부재). 새 Incident 면 다시 제안된다."}

⚠️ 핵심: "knob_param 에 없다"만으로 레짐이라 단정하지 말 것(측정 센서는 원래 knob_param 에 없다). 하지만 원인 후보 **상위가 레짐(C12)·압력뿐**이면 튜닝안을 내지 말고 물러선다.
⚠️ 물러설 때도 **왜 물러섰는지**를 남긴다 — 예시 D 처럼 B 가 준 사유를 사람 문장으로 옮긴다. "튜닝안 없음"만 쓰면 승인 화면에서 판단 근거가 사라진다.
⚠️ 위 예시에 **confidence 가 없는 것은 의도**다. 확신도는 **이 사안의 근거 강도에서 직접 산출**하라 — 예시 값도, 관례적인 값도, 다른 옵션과 맞춘 값도 쓰지 않는다.
⚠️ 예시의 `<...>` 는 자리 표시다. 인용 ID 는 **실제로 받은 재료의 ID** 만 쓴다 — 예시의 ID 를 그대로 옮기면 없는 근거를 지어낸 것이 된다.
"""

# --- 역할 프롬프트 (라이브러리 §1 — 문서가 단일 소스, 개정 시 문서 먼저) ------------
SYSTEM_PROMPT = (
    """[역할] 공정 조건 이탈에 대한 레시피 튜닝안 리포트를 작성한다.

[입력]
- alert: fdc.alert 원문 (violations, tttm, context_score, prediction_context)
- tuning: B 정량 엔진 산출 (parameter_id, value_current, applied_value, value_proposed, delta_pct, 감도 통계) ← 수치의 유일한 소스
  · value_current = **명목값**(레시피에 적힌 값 · 누적 계산의 기준) / applied_value = **지금 장비에 실제로 걸린 값**
  · applied_value 는 **첫 스텝·escalate·온도(temp_target)에서는 null** 이다 — 없는 값을 지어내지 마라
- knob_map: 손잡이 매핑 테이블 (축별: axis, knob_param=조정 setpoint, related_sensors=그 축 센서(측정 포함), cause_effect=측정→setpoint 연결, doc_id)
- kb_hits: Process Knowledge 검색 결과 (문서 스니펫 + doc_id)
- cases: 유사 사례 검색 결과 (case_id, similarity, 조치, is_success) — 서사
- recipe_logs: 그 사례들의 실제 튜닝 조치 기록 (parameter_id·value_before/after·delta_pct·검증 결과).
  cases와 incident_id로 짝지어져 있다 — "그때 무엇을 얼마나 바꿔서 통했나"의 정밀 수치.

[작성 규칙]
1. 튜닝 수치는 tuning 입력을 그대로 인용한다. delta를 재계산하거나 "조금 더" 같은 조정을 하지 않는다.
1-1. **"현재값"은 applied_value(지금 걸린 값)로 말한다** — 승인 화면에서 엔지니어가 보는 값이다.
   applied_value 가 있으면 "현재 39.6 (명목 40.0 대비 누적 -1.0%) → 39.204" 형태로 **둘 다** 쓴다.
   null 이면(첫 스텝·escalate·온도) 명목값만 쓰고 **누적 문구를 붙이지 않는다.**
   ⚠️ 명목값만 말하면 장비 실제와 달라진다 — 엔지니어가 틀린 현재값을 보고 승인하게 된다.
2. |delta_pct| > 3.0(D6)이면 튜닝안을 내지 말고 escalate_reason에 사유를 쓴다 — 상한 초과 제안은 금지.
3. 근거(rationale)는 3층으로 쓴다: ① 어떤 위반 패턴이(SPC) ② 어떤 물리 축을 가리키고(SHAP+KB) ③ 과거에 무엇이 통했나(CASE).
   ③은 recipe_logs가 있으면 정밀 수치로 쓴다 — "과거 동일 축 튜닝 N건, delta -2.1%로 개선 [RCP-...]".
   단 그 수치는 **과거 기록의 인용**이지 이번 튜닝안의 근거가 아니다(이번 수치는 tuning이 유일 소스).
4. 손잡이 판정은 **2단계**로 한다 — SHAP은 보통 "측정 센서"를 지목하고, 조정은 그 축의 "setpoint"로 하기 때문이다.
   ① SHAP 상위 센서가 어느 축인지 찾는다 — knob_map의 `related_sensors`(측정 센서 포함)와 `cause_effect`를 본다.
   ② 그 축의 knob_param에서 조정할 파라미터를 고르고, parameter_id에는 **setpoint C코드**를 쓴다(측정 C코드 아님).
   ※ 그 축 knob_param이 '에스컬레이션'이면 손잡이는 있으나 **조정량을 낼 근거가 없는 축**이다 —
     parameter_id=null, hypothesis=insufficient_evidence, escalate_reason에 그 카드의 사유를 옮긴다.
   ※ 어느 축의 related_sensors에도 없거나 그 축 knob_param이 '미확정'이면(예: C12 레짐 도장, 압력 축)
     튜닝안을 만들지 않는다 — 레짐 신호로 명시하고 hypothesis를 regime_signal로 준다.
     "knob_param 목록에 SHAP 센서가 없다"는 것만으로 레짐이라 단정하지 말 것 — 측정 센서는 원래 거기 없다.
4-1. **온도 축은 손잡이가 있다** (2026-07-26 멘토 확정 — "온도는 레짐이 아니라 가변 축").
   C17(척/히터 온도)은 그 축의 **측정 센서**이고, 조정 손잡이는 knob_map 온도 축의 `temp_target`
   (목표 지정형·BL8 — C코드가 아니라 이름이다). SHAP 상위가 C17이면 parameter_id에 "temp_target"을
   쓴다(C17을 그대로 쓰지 않는다). C17을 레짐이라는 이유로 물러서지 마라.
   ※ 단 다음이면 온도여도 튜닝안을 내지 않는다 — 손잡이로 되돌릴 문제가 아니다:
     ⓐ 급변: violations에 rule_id=N1 또는 severity=CRITICAL이 있다 (장비 이상 가능 — 정비/에스컬 소관)
     ⓑ C12 동반: violations·shap_top3에 C12가 함께 있다 (레짐 도장이 찍힘 — 챔버 상태가 바뀐 것)
   두 경우 escalate_reason에 그 사유를 쓴다.
4-2. **온도는 Incident당 1스텝이다.** 온도는 데이터에 setpoint 컬럼이 없어 **명목 앵커가 없고**,
   누적 상한(D6 ±3%)을 원리적으로 잴 수 없다(매번 현재값 대비 ±step → 무한 드리프트 가능).
   같은 Incident 에서 **두 번째 온도 사안부터는 B 가 수치를 내지 않고 에스컬레이션으로 보낸다**
   (새 Incident 면 리셋된다). 이때 tuning 은 escalate_reason 만 있고 수치가 전부 null → **6-1을 따른다.**
5. 이 레시피 튜닝은 "크기를 한 번에 맞추는 시스템"이 아니라 보수 스텝→검증→재조정 루프다(F16). expected_effect에 "1스텝 기대 효과"로 서술하고 단정하지 않는다.
6. tuning 이 null 이면 튜닝 수치를 창작하지 말고 hypothesis=insufficient_evidence + escalate_reason 에 사유를 쓴다.
6-1. **tuning 이 있어도 escalate_reason 이 채워져 있고 수치가 전부 null 이면, B 가 이미 "사람 판단"으로 보낸 것이다.**
   ⓐ 수치를 만들지 마라 ⓑ **B 의 escalate_reason 을 그대로 옮긴 뒤** 사람이 읽을 문장으로 풀어 써라.
   예: "이번 Incident 에서 온도는 이미 1회 조정됨 — 추가 조정은 사람 판단(명목 앵커 부재)."
   ⚠️ 사유를 지우고 "튜닝안 없음"으로만 쓰면 **왜 없는지가 사라진다.**
"""
    # few-shot 배선 ON (2026-08-05, round13 대상) — 2026-07-23 fewshot2 때 껐던 것을 되돌린다.
    #   당시 판단은 "이 대조쌍이 정답률 61→39% 폭락의 주범"이었으나, 2026-08-04 재진단에서 원인은
    #   **스텁 엔진**으로 정리됐다(대기_항목 「라운드 재개 전 체크리스트」). 그 스텁은 S3·S1 배선
    #   (#109·#110)으로 제거됐으므로 few-shot 의 실제 효과는 **아직 측정된 적이 없다** — round13 이
    #   첫 측정이다. 노리는 것은 ③축 recipe 과확신(round12: 정답 옵션이 confidence 1위가 아닌 11/19,
    #   그중 10건이 recipe 에 밀림 · 실측 0.95 반복)이며, 예시가 0.78/0.35/0.7 을 직접 가르친다.
    #   ⚠️ 되돌리기: ①축 process 가 급락하면(fewshot2 증상 = 튜닝안 0건) 이 줄을 다시 주석 처리한다.
    + FEWSHOT_EXAMPLES
    + CONFIDENCE_RUBRIC
    + EVIDENCE_CARD_RULES
    + "\n[JSON 스키마로만 응답]"
)

# 프롬프트에 중복 탑재할 JSON 예시 (헌법 6-3 이중 방어 — guided_json 과 별개).
SCHEMA_EXAMPLE = """{
  "report_id": "RCP-20260713-SIMCH3-001",
  "option_type": "recipe_option",
  "recipe_id": "C6_0",
  "step": 4,
  "parameter_id": "C4",
  "parameter_name": "Gas_Set_A",
  "value_current": 40.0,
  "applied_value": 39.6,
  "value_proposed": 39.2,
  "delta_pct": -2.0,
  "shap_basis": [{"sensor": "C15", "contribution": 0.44, "direction": "high"}],
  "expected_effect": "1스텝 기대: predicted_c65 하향 (감도 통계 기준, 검증 루프 전제) [MODEL]",
  "rationale": "① N3 추세 위반이 가스 계열에서 시작 [SPC] ② SHAP 상위 축이 가스 유량 — knob_map상 손잡이는 C4 [KB:<검색된 문서 ID>] ③ 유사 사례 2건에서 동일 방향 소폭 튜닝으로 개선 [CASE:<검색된 사례 ID>]",
  "hypothesis": "process_condition_shift",
  "escalate_reason": null,
  "evidence_cards": [],
  "confidence": "<근거 층수에서 산출 — 위 규칙>",
  "uncertainty": "감도 통계의 표본이 정착 구간 한정 — 과도 구간 거동은 미보증"
}"""


# ⚠️ B5-4(Recipe 정량 엔진, 7/31) 합의 전 **잠정 인터페이스 스텁** -------------------
# 일정표 C5-1("B5-4 전이면 인터페이스 스텁으로 선행") + 완충규칙 8("고정 delta 최소 시연") 근거.
# B5-4 확정 시 이 함수 하나만 실 API 호출로 교체한다 — tool 로직·프롬프트·payload 형은 불변.
# 값의 유일한 소스는 B(수치 창작 금지 §0)이므로, 스텁도 delta 를 지어내지 않고
#   alert 의 실측(current_value)에서 D6 상한(±3%) 내 소폭으로 역산한다(=B 감도 산출 대역).
_STUB_TUNING_DELTA_PCT = -2.0  # B5-4 대역 고정 delta (D6 ±3% 이내). B 확정 시 감도 통계로 대체.


def _stub_tuning_api(alert: AlertModel) -> dict[str, Any] | None:
    """[잠정] B5-4 튜닝 정량 API 대역 — SHAP 상위 중 위반 센서 1건의 소폭 튜닝안을 만든다.

    ⚠️ **계약 상태: C 단독 초안 — B 미대조.** 반환 dict 의 필드/타입은 프롬프트 §1 [입력]
    tuning 명세(parameter_id·value_current·value_proposed·delta_pct·감도통계)를 그대로 따른
    것이나, 이 스키마는 프롬프트 라이브러리 초안 이후 **B5-4 담당(B)과 실제 요청/응답 모양을
    맞춰본 적이 없다**(B 진행 지연). B 확정 시 필드명이 달라질 수 있으므로:
      · tuning dict 필드를 참조하는 코드는 이 스텁이 유일 계약 소스임을 전제하지 말 것.
      · B 확정 시 이 함수 하나만 실 API 로 교체 → 필드 매핑 차이는 여기서 흡수한다.
    위반이 없으면 None → 프롬프트 규칙 6(tuning null → insufficient_evidence)이 발동한다.
    **B9 crazy 마커만 있는 alert 도 None** — 'C65' 는 조정 손잡이가 아니라 예측 결과값이라
    튜닝 대상이 될 수 없다 (base.primary_violation).
    """
    v = primary_violation(alert)  # B9 마커 배제 후 대표 위반 (B 는 SHAP 감도로 선정)
    if v is None:
        return None
    current = v.current_value
    # 100.0 = %→비율 환산, 4 = 표시 자리수. 둘 다 단위·포맷이라 매직 넘버가 아니다(params 대상 아님).
    proposed = round(current * (1 + _STUB_TUNING_DELTA_PCT / 100.0), 4)
    return {
        "parameter_id": v.sensor,  # 조정 손잡이 후보 — knob_map(1-2) 통과 여부는 LLM 이 규칙4로 판정
        "value_current": current,
        "value_proposed": proposed,
        "delta_pct": _STUB_TUNING_DELTA_PCT,
        "sensitivity": {  # 감도 통계 — B5-4 산출 대역(스텁 표지)
            "basis": "stub",
            "note": "⚠️B5-4 합의 전 잠정값 — 감도 통계 미산출",
            "sample_window": "settled_only",
        },
        "_provisional": True,  # B5-4 실 API 로 교체되면 사라지는 스텁 도장
    }


# --- 1-3: 검색어 빌더 (순수 함수 — alert 만으로 구성, Qdrant 불필요) -----------------
# 실측(2026-07-18)으로 컬렉션별 최적 검색어가 갈렸다:
#   · process_knowledge(지식): 짧고 정확 — 룰·서술을 넣으면 노이즈로 엉뚱한 센서가 뜬다.
#   · historical_case(사례): 룰+패턴 서술 포함 — 같은 Nelson 룰·같은 드리프트 사례를 정확 매칭.
def _violation_terms(alert: AlertModel) -> tuple[str, str, str, str]:
    """대표 위반에서 검색어 재료를 뽑는다 (센서·표시명·룰·서술). 위반 0건이면 빈 문자열.

    B9 crazy 마커는 배제한다 — 'C65'/'B9' 로 KB·사례를 검색하면 노이즈만 걸린다.
    """
    v = primary_violation(alert)
    if v is None:
        return "", "", "", ""
    return v.sensor, (v.sensor_name or ""), (v.rule_id or ""), (v.description or "")


def build_kb_query(alert: AlertModel) -> str:
    """Process Knowledge 검색어 — 센서 중심 짧게(실측: 서술 넣으면 품질 하락)."""
    sensor, name, _rule, _desc = _violation_terms(alert)
    return " ".join(t for t in (sensor, name) if t).strip()


def build_case_query(alert: AlertModel) -> str:
    """historical_case 검색어 — 룰+패턴 서술 포함(실측: 같은 룰·드리프트 사례 정확 매칭)."""
    sensor, name, rule, desc = _violation_terms(alert)
    return " ".join(t for t in (sensor, name, rule, desc) if t).strip()


def build_payload(
    alert: AlertModel,
    *,
    tuning: dict[str, Any] | None = None,
    knob_map: dict[str, Any] | None = None,
    kb_hits: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    recipe_logs: list[dict[str, Any]] | None = None,
) -> str:
    """LLM 입력(user 메시지) 직렬화 — 라이브러리 §1 [입력] 항목 순서 그대로.

    재료가 없으면 None/[] 로 명시한다. "없음"을 숨기지 않아야 §0 불확실성 원칙대로
    모델이 "유사 사례 없음"·insufficient_evidence 로 정직하게 반응한다.
    """
    return json.dumps(
        {
            "alert": strip_ablated_alert(alert.model_dump(mode="json")),
            "tuning": tuning,
            "knob_map": knob_map,
            "kb_hits": kb_hits or [],
            "cases": cases or [],
            "recipe_logs": recipe_logs or [],
        },
        ensure_ascii=False,
    )


# --- 2단계: delta 상한(D6) 가드 — LLM 응답을 코드가 검문 (헌법 1-1) -------------------
# 프롬프트 규칙2가 "3% 초과 튜닝 금지"를 지시하지만, LLM 이 어길 수 있으므로 코드로 강제한다.
# 상한 초과 튜닝안이 승인 큐로 흘러가면 설비가 상한 넘게 바뀔 수 있어 — LLM 선의에 맡기지 않는다.
def enforce_delta_guard(report: RecipeOption, max_pct: float) -> RecipeOption:
    """|delta_pct| > D6 상한이면 튜닝안을 강등한다: 수치 필드 제거 + escalate_reason 기입.

    이미 LLM 이 규칙2를 지켜 escalate 했거나 튜닝안이 없으면(delta_pct=None) 그대로 둔다.
    강등 시 튜닝 수치(value_proposed·parameter_id 등)를 지워 승인 큐로 새지 않게 하고,
    hypothesis 는 손대지 않는다(원인 후보 판단은 LLM 소관 — 가드는 '적용 가능 여부'만 판정).
    """
    d = report.delta_pct
    if d is None or abs(d) <= max_pct:
        return report  # 튜닝안 없음 또는 상한 내 — 통과
    reason = (
        f"1스텝 상한 초과(|delta|={abs(d):.2f}% > D6 {max_pct:.1f}%) — 분할 스텝 또는 "
        f"에스컬레이션 필요. 상한 초과 튜닝안은 자동 적용 금지(헌법 1-1)."
    )
    # 기존 escalate_reason 이 있으면 보존하며 병기(LLM 이 다른 사유를 이미 썼을 수 있음).
    merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
    logger.warning(
        "delta 가드 강등: report=%s |delta|=%.2f%% > %.1f%% — 튜닝안 제거·에스컬레이션",
        report.report_id, abs(d), max_pct,
    )
    return report.model_copy(update={
        "value_proposed": None,   # 상한 초과 제안은 지운다 — 승인 큐로 새지 않게
        "delta_pct": None,
        "expected_effect": None,
        "escalate_reason": merged,
    })


# --- 2단계: 손잡이 가드 — 튜닝 대상이 진짜 조정 가능한 손잡이인지 검문 (규칙4, 헌법 1-1) ----
# LLM 이 낸 parameter_id 가 knob_map 의 실제 손잡이에 없으면 강등한다(=레짐 신호).
# **화이트리스트 방식**: "조정량을 낼 수 있다고 검증된 손잡이"만 knob_map 에 등재하므로
#   (2026-08-04 현재 **C4 · temp_target** 둘 — D11 스코프 축소로 C1·C5·C48·C41 은 카드에서
#   `knob_param='에스컬레이션'` 이 되어 여기 안 잡힌다), 목록에 없는 센서는 전부 튜닝 불가로 막는다 —
#   측정값(C17)·상태값(C12)·미등록·존재안함(C99)까지 다 걸러 안전측으로 판정한다.
#   knob_map 확장(데이터 파악 진전) 시 허용 집합이 자동으로 넓어진다(별도 코드 수정 불요).
#   ⚠️ 이 괄호의 값은 **참고일 뿐 소스가 아니다** — 정본은 Qdrant tuning_axis 카드이고 그 카드는
#   `config/recipe_knob_map.yaml`(B 소유, status:active) 과 맞춰져 있다. 셋의 정합은
#   `tests/test_knob_map_consistency.py` 가 검사한다(어긋나면 실패).
_C_CODE_RE = re.compile(r"C\d+")

# 명명 손잡이(C코드가 없는 정식 손잡이) 화이트리스트. temp 축은 setpoint 컬럼이 데이터 수집
# 밖이라 C코드가 없지만, BL8(목표 지정형, 2026-07-11 PM 확정)으로 temp_target 을 조작한다
# (recipe_applier.TEMP_TARGET_PARAM). C코드만 뽑으면 이게 걸러져 온도가 강등되므로 명시 등재.
# ⚠️ **화이트리스트 정신 유지**: 여기 든 정확한 문자열만 통과. '미확정'·'temp_setpoint'(옛 이름)·
#   오타는 여전히 막힌다. 새 명명 손잡이가 확정되면 이 집합에 추가한다.
# ※ "언제 그 출구로 보내나"(C17 → 온도 튜닝 vs 레짐 에스컬)는 2026-07-26 멘토 확정으로 해소 —
#   "온도는 레짐이 아니라 가변 축". 기본 경로 = 튜닝이고, 물러서는 예외는 아래 온도 패턴 가드가 강제한다.
TEMP_KNOB = "temp_target"
_NAMED_KNOBS = frozenset({TEMP_KNOB})
# 온도 축의 **측정 센서**(히터/척 온도). 손잡이가 아니라 결과가 찍히는 모니터 채널이다 —
# parameter_id 로 쓰는 것은 금지(§0). Supervisor 의 레짐 도장 가드가 "위반 센서가 이것인가"를
# 판단할 때 재사용한다(같은 개념을 두 곳에 따로 구현하면 갈라진다 — 라운드 10 비대칭의 교훈).
TEMP_MONITOR_SENSOR = "C17"


def allowed_knobs(knob_map: list[dict[str, Any]] | None) -> set[str]:
    """knob_map 카드들의 knob_param 에서 실제 손잡이(C코드 + 명명 손잡이)를 추출한 집합.

    knob_param 은 형태가 섞여 있다: 'step_time(C41)' · ['C4(...)', 'C5(...)'] · '미확정' · 'temp_target'.
    C코드 패턴(C\\d+)을 뽑고, 문자열 전체가 명명 손잡이 화이트리스트(_NAMED_KNOBS)면 그 이름도
    더한다 → '미확정'·'temp_setpoint'(옛 이름)은 어느 쪽에도 안 걸려 제외(=튜닝 불가). None/빈 값이면 빈 집합.
    """
    knobs: set[str] = set()
    for card in knob_map or []:
        kp = card.get("knob_param")
        items = kp if isinstance(kp, list) else [kp]
        for item in items:
            if isinstance(item, str):
                knobs.update(_C_CODE_RE.findall(item))
                if item.strip() in _NAMED_KNOBS:
                    knobs.add(item.strip())
    return knobs


def enforce_knob_guard(
    report: RecipeOption, knob_map: list[dict[str, Any]] | None
) -> RecipeOption:
    """parameter_id 가 knob_map 의 실제 손잡이(화이트리스트)에 없으면 강등 — 레짐 신호로 분류.

    튜닝안이 없거나(parameter_id=None) 손잡이가 맞으면 통과. knob_map 이 비었으면(조회 실패 등)
    판정 근거가 없으므로 강등하지 않는다(가드가 아예 작동 안 함 — 오탐 방지, 무중단 정신).
    강등 시 튜닝 수치를 제거하고 hypothesis=regime_signal, escalate_reason 기입.
    """
    pid = report.parameter_id
    allowed = allowed_knobs(knob_map)
    # 판정 불가(재료 없음) 또는 튜닝안 없음 또는 손잡이 맞음 → 통과
    if not allowed or pid is None or pid in allowed:
        return report
    reason = (
        f"{pid} 은 조정 가능한 손잡이가 아님(knob_map 미등재 — 측정/상태 신호 추정). "
        f"레시피 튜닝 대상 아님 → 상태 점검/에스컬레이션. 허용 손잡이: {sorted(allowed)}."
    )
    merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
    logger.warning(
        "손잡이 가드 강등: report=%s parameter_id=%s ∉ 허용%s — 레짐 신호로 분류",
        report.report_id, pid, sorted(allowed),
    )
    return report.model_copy(update={
        "parameter_id": None,   # 손잡이 아님 — 튜닝 수치 전체 무효화
        "value_proposed": None,
        "delta_pct": None,
        "expected_effect": None,
        "hypothesis": "regime_signal",  # 규칙4: 못 돌리는 신호는 레짐으로 명시
        "escalate_reason": merged,
    })


# --- 2단계: 온도 패턴 가드 — 온도 손잡이는 "추세"에만 열린다 (규칙4-1, 헌법 1-1) -----------
# 2026-07-26 멘토 확정으로 온도는 레짐이 아니라 가변 축이 됐다(기본 경로 = temp_target 튜닝).
# 다만 **온도가 급변으로 뛴 것**은 손잡이로 되돌릴 문제가 아니다 — 히터/척 냉각 계통 이상일 수
# 있고, 거기에 목표값 튜닝안을 얹으면 장비 이상을 손잡이로 덮는다(불가역 오답의 전형).
# 판정은 센서 신원이 아니라 **패턴**으로 가른다(§0 2026-07-22 원칙)는 것을 온도에 그대로 적용한 것.
# ※ 이 가드는 프롬프트 규칙4-1의 사후 검문이다 — "판정은 코드, 서술은 LLM"(2026-07-24 PM 승인).
_SHIFT_RULE_ID = "N1"          # 급변 룰 — 관리선 밖 단발 (추세 룰 N2·N3·N5 와 대비)
_SHIFT_SEVERITY = "CRITICAL"   # 급변 심각도
_REGIME_STAMP_SENSOR = "C12"   # Vdc 연동 기준값 = 레짐 도장 (손잡이 절대 금지 — 7/10 확정)


def has_shift(alert: AlertModel) -> bool:
    """급변(shift) 위반이 하나라도 있나 — N1 또는 CRITICAL. 없으면 추세(drift)로 본다.

    ⚠️ B9 crazy 마커는 severity='CRITICAL' 로 오므로 배제하지 않으면 **마커만으로 '급변 동반'**
      이 성립한다 — 실제 센서가 튄 적이 없는데 온도 가드가 발동한다 (B 확인요청 2026-07-28).
    """
    return any(
        v.rule_id == _SHIFT_RULE_ID or v.severity == _SHIFT_SEVERITY
        for v in real_violations(alert)
    )


def has_regime_stamp(alert: AlertModel) -> bool:
    """레짐 도장(C12)이 위반·SHAP 상위·spc_flags 어디에든 함께 찍혔나."""
    if any(v.sensor == _REGIME_STAMP_SENSOR for v in real_violations(alert)):
        return True
    pc = alert.prediction_context
    if _REGIME_STAMP_SENSOR in pc.shap_top3:
        return True
    return any(f.sensor == _REGIME_STAMP_SENSOR for f in pc.spc_flags)


def enforce_temp_pattern_guard(report: RecipeOption, alert: AlertModel) -> RecipeOption:
    """온도 손잡이(temp_target) 튜닝안을 급변·레짐 도장 동반 시 강등한다.

    온도 손잡이가 아닌 튜닝안(가스·파워·시간)이나 튜닝안 없음은 그대로 통과 — 이 가드는
    temp_target 한 축만 본다. 강등 시 hypothesis 처리가 두 갈래인 이유:
      · C12 동반 → `regime_signal`. 레짐 도장이 실제로 찍혔으므로 원인 가설을 바꾸는 게 정확하다.
      · 급변 → hypothesis 를 손대지 않는다. "급변이다"는 적용 가능 여부 판정이지 원인 가설이
        아니고(급변의 원인은 Supervisor 급변 관문이 TTTM 갭으로 가른다 — 헌법 1-4),
        여기서 regime_signal 을 찍으면 트리를 ④로 밀어 ①(정비)을 가린다. delta 가드와 같은 취지.
    """
    if report.parameter_id != TEMP_KNOB:
        return report  # 온도 손잡이 건이 아님 — 통과

    if has_regime_stamp(alert):
        reason = (
            f"레짐 도장({_REGIME_STAMP_SENSOR}) 동반 — 챔버 상태가 바뀐 신호다. "
            "온도 손잡이로 되돌릴 문제가 아님 → 상태 점검/에스컬레이션."
        )
        update: dict[str, Any] = {"hypothesis": "regime_signal"}
    elif has_shift(alert):
        reason = (
            f"급변 위반({_SHIFT_RULE_ID}/{_SHIFT_SEVERITY}) 동반 — 온도가 이동이 아니라 튄 것. "
            "장비 이상(히터·척 냉각 계통) 가능성이 있어 목표값 튜닝으로 덮지 않는다 → 정비/에스컬레이션 판정 대상."
        )
        update = {}
    else:
        return report  # 급변 없음 + C12 없음 = 추세성 온도 드리프트 — 튜닝안 통과 (기본 경로)

    merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
    logger.warning(
        "온도 패턴 가드 강등: report=%s (급변=%s, C12동반=%s) — temp_target 튜닝안 제거",
        report.report_id, has_shift(alert), has_regime_stamp(alert),
    )
    return report.model_copy(update={
        "parameter_id": None,
        "value_proposed": None,
        "delta_pct": None,
        "expected_effect": None,
        "escalate_reason": merged,
        **update,
    })


# --- 2단계: 숫자 가드 — LLM 이 tuning 수치를 창작·변조하지 않았는지 검문 (규칙1·6, 헌법 1-1) ---
# 수치의 유일한 소스는 B(tuning)다. LLM 은 인용만 해야 하는데 두 가지로 어길 수 있다:
#   ③ 창작: tuning=None(B가 값 없음)인데 value_proposed 를 지어냄 → 통째로 무효화·에스컬레이션.
#   ④ 변조: tuning 값과 다른 value_proposed → B 값으로 교정(근거 서술은 살리고 숫자만 정답으로).
# tuning 이 스텁이든 실 API 든 "LLM 이 주어진 값을 바꿨나"를 보는 것이므로 B 확정 후에도 유효.
def enforce_number_guard(
    report: RecipeOption, tuning: dict[str, Any] | None
) -> RecipeOption:
    """LLM 이 tuning 수치를 창작(③)·변조(④)하지 않았는지, 목표 지정형에 필요한 값이
    빠지지 않았는지(⑤) 검문한다.

    ③ tuning 이 None/빈값인데 report 에 value_proposed 가 있으면 창작 → 수치 무효화 + 에스컬레이션.
    ⑤ 목표 지정형 손잡이(temp_target·BL8)인데 value_current 가 없으면 → tuning 값으로 채우고,
       tuning 에도 없으면 강등. **④보다 먼저** 본다 — ④는 교정 후 즉시 반환하므로 뒤에 두면 건너뛴다.
    ④ tuning 값과 report 의 value_proposed/delta_pct 가 다르면 변조 → B 값으로 교정(경고).
    report 에 튜닝 수치가 없으면(이미 강등됨 등) 검사할 게 없어 통과.
    """
    # 이미 앞선 가드가 수치를 지웠으면 검사 불필요
    if report.value_proposed is None and report.delta_pct is None:
        return report

    tv = (tuning or {}).get("value_proposed")
    td = (tuning or {}).get("delta_pct")

    # ③ 창작: 정답지(tuning)에 값이 없는데 리포트엔 수치가 있다 → 지어냄
    if tv is None:
        reason = (
            "tuning 미제공(B 수치 없음)인데 튜닝 수치를 냄 — 수치 창작 금지(§1 규칙6). "
            "값 무효화·에스컬레이션."
        )
        merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
        logger.warning("숫자 가드(창작) 강등: report=%s value_proposed=%s (tuning 없음)",
                       report.report_id, report.value_proposed)
        return report.model_copy(update={
            "value_proposed": None, "delta_pct": None, "expected_effect": None,
            "escalate_reason": merged,
        })

    # ⑤ 목표 지정형(BL8) 필수값: temp_target 은 delta 가 아니라 value_current→value_proposed 로
    #   운전점을 옮긴다. 시뮬(`recipe_applier.py:100`)이 target 모드에서 **두 값을 모두** 요구하고
    #   없으면 **조용히 스킵**한다 — 승인까지 통과한 뒤 아무 일도 일어나지 않는 게 최악이라
    #   승인 큐 앞에서 막는다. tuning 이 value_current 를 주므로 우선 채우고(교정), 그마저 없으면 강등.
    if report.parameter_id == TEMP_KNOB and report.value_current is None:
        tc = (tuning or {}).get("value_current")
        if tc is not None:
            logger.warning(
                "숫자 가드(목표 지정형 보정): report=%s value_current 누락 → tuning 값 %s 로 채움",
                report.report_id, tc,
            )
            report = report.model_copy(update={"value_current": tc})
        else:
            reason = (
                f"목표 지정형 손잡이({TEMP_KNOB})인데 value_current(현 정착 레벨)가 없음 — "
                "시뮬레이터 target 모드가 스킵한다(BL8). 값 무효화·에스컬레이션."
            )
            merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
            logger.warning(
                "숫자 가드(목표 지정형) 강등: report=%s value_current 없음 (tuning 에도 없음)",
                report.report_id,
            )
            return report.model_copy(update={
                "value_proposed": None, "delta_pct": None, "expected_effect": None,
                "escalate_reason": merged,
            })

    # ④ 변조: 정답지와 다른 값 → B 값으로 교정(근거 서술은 유지, 숫자만 정정)
    if report.value_proposed != tv or report.delta_pct != td:
        logger.warning(
            "숫자 가드(변조) 교정: report=%s LLM(value=%s,delta=%s) → tuning(value=%s,delta=%s)",
            report.report_id, report.value_proposed, report.delta_pct, tv, td,
        )
        return report.model_copy(update={"value_proposed": tv, "delta_pct": td})

    return report  # 충실히 인용함 — 통과


async def run(
    alert: AlertModel,
    backend: LlmBackend,
    settings: Settings,
    *,
    knob_map: list[dict[str, Any]] | None = None,
    kb_hits: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    recipe_logs: list[dict[str, Any]] | None = None,
    tuning: dict[str, Any] | None = None,
) -> RecipeOption:
    """레시피 튜닝안 리포트 1건 생성 — LLM 실호출 (재시도·fallback 은 generate_structured 소관).

    Args:
        alert: 게이트를 통과한 fdc.alert.
        backend: 주입된 LLM 백엔드 (테스트=MockBackend / 운영=VllmBackend).
        settings: 재시도 예산·생성 파라미터의 config 정본.
        knob_map: SHAP 축→손잡이 매핑표(1-2). pipeline 이 Qdrant 에서 조회해 주입한다.
            None 이면 규칙4 판정 근거 없음 → LLM 은 튜닝 손잡이를 확정하지 못한다(정직).
        kb_hits: Process Knowledge 검색 결과(1-3, 근거 ②). pipeline 이 검색해 주입.
        cases: historical_case 검색 결과(1-3, 근거 ③). None/[] 이면 규칙 "유사 사례 없음".
        recipe_logs: cases 의 incident_id 로 페어링한 Postgres 튜닝 조치로그(정밀 수치).
            벡터 KB 는 서사만 준다 — "얼마나 바꿔서 통했나"는 여기서 온다(app/db.py).
        tuning: B5-4 엔진 산출 튜닝안 (S3 배선, 2026-08-04). pipeline 이 `_compute_tuning` 으로
            조달해 주입한다 — 온도 반복 정책이 `incident_id` 를 요구하는데 이 함수는 그것을
            받지 않기 때문(그리고 DB 접촉은 pipeline 몫이다).
            **미주입(None)이면 스텁으로 자체 조달**한다 — 이 tool 을 단독 호출하는 테스트·스모크가
            깨지지 않게. 그 경우 `_provisional` 도장이 남아 실물이 아님이 재료에 드러난다.
    """
    # 1-1: 튜닝 수치 — 주입분(B 실 엔진) 우선, 없으면 스텁 대역.
    # 1-2: knob_map(손잡이 번역표)을 실어 규칙4(손잡이 검증)를 LLM 이 판정하게 한다.
    # 1-3: kb_hits(근거②)·cases(근거③) — pipeline 이 컬렉션별 검색어로 조회해 주입한다.
    if tuning is None:
        tuning = _stub_tuning_api(alert)
    payload = build_payload(
        alert, tuning=tuning, knob_map=knob_map, kb_hits=kb_hits, cases=cases,
        recipe_logs=recipe_logs,
    )

    report = await generate_structured(
        backend,
        system_prompt(SYSTEM_PROMPT, FEWSHOT_EXAMPLES),
        payload,
        RecipeOption,
        fallback=lambda: make_fallback_report(RecipeOption, alert),
        schema_example=SCHEMA_EXAMPLE,
        parse_retries=int(settings.require("agent.json_parse_retry")),
        call_retries=int(settings.require("llm.call_retry")),
    )

    # report_id 는 LLM 이 정할 값이 아니다 — 예시를 그대로 베끼는 것이 실측됨(2026-07-16 스모크).
    # 네이밍 규칙(6-4)·결정성은 우리 소관이므로 무조건 덮어쓴다.
    # applied_value 도 같다 — **B 가 준 사실**(직전 적용값)이지 LLM 의 판단이 아니다(W21).
    #   숫자 가드(④)에 맡기지 않는 이유: 그 가드는 창작·목표지정형 분기에서 early return 하므로
    #   강등 경로에서 applied_value 가 LLM 값인 채로 남는다. 승인 화면의 '현재값'이라 조용히
    #   틀리면 엔지니어가 장비 실제와 다른 값을 보고 승인하게 된다.
    report = report.model_copy(update={
        "report_id": make_report_id(RCP_PREFIX, alert),
        "applied_value": (tuning or {}).get("applied_value"),
    })

    # 2단계: 코드 강제 가드 4종 (LLM 이 규칙을 어겨도 위험한 튜닝안은 승인 큐로 못 간다, 헌법 1-1).
    # 순서 = 숫자 → 손잡이 → 온도패턴 → delta:
    #   ① 숫자 가드(규칙1·6): tuning 수치 창작·변조를 먼저 정정 — 이후 가드가 정답 정렬된 값을 본다.
    #      목표 지정형(temp_target)의 value_current 누락도 여기서 채우거나 강등한다(BL8·⑤).
    #   ② 손잡이 가드(규칙4): 손잡이가 아니면 delta 를 볼 필요도 없이 레짐으로 강등.
    #   ③ 온도 패턴 가드(규칙4-1): 손잡이는 맞되(temp_target) 급변·C12 동반이면 강등.
    #      ②의 뒤여야 한다 — ②를 통과해 "진짜 온도 손잡이"로 확정된 건만 패턴을 따진다.
    #   ④ delta 가드(규칙2): 손잡이는 맞되 변화폭이 상한(D6) 초과면 에스컬레이션.
    report = enforce_number_guard(report, tuning)
    report = enforce_knob_guard(report, knob_map)
    report = enforce_temp_pattern_guard(report, alert)
    report = enforce_delta_guard(report, float(settings.require("agent.recipe_delta_max_pct")))

    logger.debug(
        "recipe report: %s (alert %s, hypothesis=%s, conf=%.2f)",
        report.report_id,
        alert.alert_id,
        report.hypothesis,
        report.confidence,
    )
    return report
