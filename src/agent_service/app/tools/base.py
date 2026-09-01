"""3조수(tool) 공통 계약 — Protocol + 스텁 표지.

각 tool 은 fdc.alert 하나 + LLM 백엔드 + settings 를 받아 자기 리포트 1건을 비동기로 반환한다.
파이프라인은 이 계약만 알면 tool 목록을 병렬(asyncio.gather)로 팬아웃할 수 있다 (헌법 1-2).

**의존성 주입 (C5-1)**: backend·settings 는 tool 이 스스로 만들지 않고 파이프라인이 넣어준다.
같은 tool 코드가 테스트에선 MockBackend 로, 운영에선 VllmBackend 로 돈다 (llm/factory.make_backend).
tool 은 "어느 엔진인지" 모른다 — 그게 경계다.

스텁 리포트는 confidence=0.0 + 아래 표지로 "아직 채우지 않았음"을 정직하게 드러낸다
(라이브러리 §0 불확실성 원칙 — 낮은 확신을 숨기지 않는다).
"""

from __future__ import annotations

from typing import Protocol

from ..config import Settings
from ..llm.client import LlmBackend
from ..schemas.alert import AlertModel, Violation
from ..schemas.report import OptionReport

# 아직 실연결 안 된 tool 의 표지 — 이 문자열이 리포트에 남아 있으면 미연결 상태.
STUB_RATIONALE = "[C4-1 Day2 스텁] B 정량 API·RAG 미연결 — 근거 미생성. C5-1에서 채운다."
STUB_UNCERTAINTY = "[스텁] 확신 산출 전 (mock 리포트)."


# --- ablation 훅 (W40, 2026-08-06) — 평가 라운드 전용, 운영은 그대로 -------------------
def system_prompt(prompt: str, fewshot: str) -> str:
    """few-shot ablation 라운드면 학습 예시를 뺀 프롬프트를 준다.

    세 tool 이 전부 `SYSTEM_PROMPT = (... + FEWSHOT_EXAMPLES)` 로 **import 시점에** 문자열을
    합쳐 두므로, 런타임 스위치는 합쳐진 것에서 도로 빼는 방식이 제일 얕다 —
    프롬프트 조립을 함수로 바꾸면 세 파일의 구조를 다 건드려야 하고, 그 구조를 고정한
    테스트도 함께 깨진다. 뺄 게 없으면 원본을 **그대로** 돌려주므로 운영 경로는 무변경이다.

    ⚠️ few-shot 의 실제 효과는 **아직 한 번도 측정된 적이 없다** (round13 은 정량 엔진이
    스텁이던 구간이라 무효 — 2026-08-05 발견). 이 스위치가 그걸 재기 위한 것이다.
    """
    from ..observe import ablated

    return prompt.replace(fewshot, "") if ablated("fewshot") else prompt


# --- B9 crazy 마커 배제 (B 확인요청 2026-07-28 → ⓐ 소비 측 제외로 합의) --------------
# B 의 crazy wafer alert 은 **센서 SPC 위반이 아니다** — predicted_c65 가 P99 컷을 넘었다는
# 다른 축의 판정이다. 그런데 계약 §3 의 violations 는 `min_length=1` 이라 빈 배열이 불가능해서,
# B 가 그 한 칸에 **합성 마커 1건**을 끼워 넣는다:
#     rule_id="B9"(Nelson 룰 아님 — 종류 표지) · sensor="C65"(센서 아님 — 예측 타겟)
#     control_limit_upper=P99 컷(관리선 아님) · control_limit_lower=0.0(센티널 — 의미 없음)
# 이걸 대표 위반으로 삼으면 tool 3종이 "C65 센서의 관리선을 (컷+0)/2 중심으로 재산정" 같은
# 허구를 만든다. 그래서 **위반 선정에서 배제**한다.
#
# ⚠️ 판별식 정본은 `rule_id == "B9"` 이며, orchestrator 의 `incident_grouper.is_crazy_alert()`
#    와 **같은 규칙**이다. 마커 규격이 바뀌면 두 곳이 함께 바뀌어야 한다 (B 회신 합의).
# ※ 배제 후 남는 위반이 없으면(crazy-only alert) 각 tool 은 기존 "위반 0건" 경로를 그대로 타서
#   recalc/tuning 을 None 으로 돌린다 → 프롬프트 규칙6(창작 금지)이 발동한다.
CRAZY_MARKER_RULE_ID = "B9"


def is_crazy_marker(violation: Violation) -> bool:
    """B9 crazy 마커인가 — Nelson 룰이 아니라 alert 종류를 표시하는 합성 위반."""
    return violation.rule_id == CRAZY_MARKER_RULE_ID


def real_violations(alert: AlertModel) -> list[Violation]:
    """B9 마커를 뺀 **진짜 센서 위반** 목록. crazy-only alert 이면 빈 리스트."""
    return [v for v in alert.violations if not is_crazy_marker(v)]


def primary_violation(alert: AlertModel) -> Violation | None:
    """대표 위반 1건 — 마커 배제 후 첫 위반. 남는 게 없으면 None (창작 금지 경로).

    ※ "첫 위반"은 스텁 규칙이다. B 는 재산정 대상 그룹(limit)·SHAP 감도(recipe)로 대표를
      고르므로, B 실 API 연결 시 선정 기준은 그쪽으로 옮겨간다. 마커 배제는 그때도 유효하다.
    """
    survivors = real_violations(alert)
    return survivors[0] if survivors else None


class AgentTool(Protocol):
    """3조수 tool 계약 — name + 비동기 run(alert, backend, settings) → 리포트 1건."""

    name: str

    async def run(  # pragma: no cover - 프로토콜 선언
        self, alert: AlertModel, backend: LlmBackend, settings: Settings
    ) -> OptionReport: ...

# --- §5 Evidence Card 규칙 (라이브러리 §5 — 3 tool 공통) --------------------------
# ⚠️ 2026-07-20 실측: 이 블록이 **없어서** LLM 이 카드를 즉흥 형식으로 만들었다.
#    guided_json 스키마에 EvidenceCard 구조는 있었지만 "어떻게 묶느냐"는 지시가 없었고,
#    모델은 **출처마다 카드 1장**({"type":"manual","content":...})으로 쪼갰다 — §5 의도와 반대.
#    그 결과 리포트 3건 중 1건꼴(16%)로 검증 실패 → fallback 사망.
#    카드는 tool 3종이 공유하는 §5 규격이므로 프롬프트도 한 곳에서 관리한다.
# --- 확신도 산출 규칙 (W61 · 2026-08-06) ----------------------------------------------
# 🔴 **왜 규칙이어야 하나.** 예시가 값을 가르치면 그 값이 그대로 복사된다(W58 실측:
#    limit 0.85 가 69% · maintenance 0.3 이 76% · recipe 0.4 가 43%). 그래서 값을 지웠더니
#    이번엔 **모델이 확신을 전반적으로 낮게 잡아** equipment_fault 가 7/9 → 4/9 로 떨어지고
#    escalate 가 18 → 22 로 늘었다(round22). 정답=ef 9건의 manual 확신이 0.65~0.75 에서
#    0.6 대로 주저앉은 것이 직접 원인이다.
#
#    **값을 가르치면 복사하고, 안 가르치면 움츠린다.** 그래서 값도 침묵도 아닌
#    **산출 방법**을 준다 — 근거가 몇 층 섰는지로 스스로 매기게 한다.
#    구간을 주는 것과 단일 값을 주는 것은 다르다: 단일 값은 상황과 무관하게 복사되지만,
#    구간은 **상황을 읽어야** 고를 수 있다.
CONFIDENCE_RUBRIC = """
[확신도(confidence) 산출 — 값을 외우지 말고 **근거 층수를 세어 계산**하라]
근거 3층 = ⓐ SPC 신호(위반 패턴·TTTM·PM 경과) ⓑ 수치·모델(제안값·SHAP·섀도) ⓒ 사례·문서(CASE·KB)
  3층 다 섬(충돌 없음) 0.80~0.90 · 2층 0.60~0.75 · 1층뿐 0.40~0.55 · 재료 없음/물러섬 0.20~0.35
⚠️ 물러섬은 재료가 부족했다는 뜻이니 낮춘다. 반대로 **근거가 갖춰졌는데 습관적으로 낮추지 마라**
   — 낮은 확신은 Supervisor 가 "전 옵션 확신 부족"으로 읽어 에스컬레이션으로 흘려보낸다.
⚠️ 예시에서 본 값·다른 옵션과 맞춘 값·0.85 같은 관례적인 값을 반복하지 마라.
"""


EVIDENCE_CARD_RULES = """
[근거 카드(evidence_cards) 규칙 — §5]
1. **카드 1장 = 주장(claim) 1개**다. 출처마다 카드를 쪼개지 마라 —
   하나의 주장을 여러 출처가 함께 떠받치는 구조다.
   (SPC 카드 / 매뉴얼 카드 / 사례 카드로 나누는 것은 틀린 해석이다.)
2. 각 카드는 반드시 다음 5개를 모두 채운다:
   card_id · claim · evidence[] · counter_evidence · uncertainty
3. evidence 는 2~4개, **서로 다른 source_type 최소 2종**(spc/model/kb/case).
   각 항목은 {source_type, ref, snippet} 세 칸을 모두 채운다.
4. ref 는 **실존 ID만** (ALERT-… / CASE-… / MAN-… / doc_id). 창작 금지.
5. counter_evidence 는 **claim 을 흔드는 사실**이다. 눈에 띄는 반증이 없더라도
   "반증 없음"류의 상투구로 채우지 말고, **이 claim 이 틀리려면 무엇이 사실이어야 하는지**를
   구체적으로 쓴다 (반증 조건). 예: "직전 PM 이 이 창에 포함됐다면 추세는 노후가 아니라 회복이다."
   불확실성 서술은 counter_evidence 가 아니라 uncertainty 칸에 쓴다.
6. 근거가 부족해 출처 2종을 못 채우면 **카드를 억지로 만들지 말고 evidence_cards 를 []로 둔다.**
   부실한 카드보다 없는 편이 낫다 — 근거 서술은 rationale 에 이미 실려 있다.

예시(형태 참고):
{"card_id":"EC-LIM-20260713-SIMCH3-001-1",
 "claim":"C11 상승은 기준선 노후이지 장비 이상이 아니다",
 "evidence":[{"source_type":"spc","ref":"ALERT-20260713-SIMCH3-0412","snippet":"N3 추세 위반, 급변 없음"},
             {"source_type":"case","ref":"CASE-0077","snippet":"동일 패턴 재설정 후 재발 없음"}],
 "counter_evidence":"이동 속도 과거 대비 1.4배 — 열화 전조 가능성 [SPC]",
 "uncertainty":"PM 직후 데이터가 재산정 창에 일부 포함"}
"""
