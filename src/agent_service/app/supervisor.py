"""🧭 Supervisor — 리포트 3종 → 4지선다 판정 + Approval Brief (C5-3).

정본: docs/Agent_프롬프트_라이브러리_v1.md §4 (역할 프롬프트·판정 프레임·출력 스키마) + 부록(치트시트).

헌법 1-4:
- 리포트 3종은 **반드시 Supervisor 를 통해 통합**된다. 개별 tool 이 직접 승인 요청을 보내지 않는다.
- 최종 추천은 Supervisor 가 생성하며 판단 프레임은 **4지선다**(v4.5).
- 승인 게이트(interrupt)는 **Supervisor 통합 뒤 한 곳뿐** — C 의 산출물은 Brief 까지이고
  상태 전이(PENDING→APPROVED)는 PM 의 LangGraph 소관이다(`approval_status` 는 타입으로 고정).

tool 3종과 다른 점:
- **입력이 원재료가 아니라 산출물**이다 (리포트 3종). 재판정이 아니라 '재판'을 한다.
- **가드가 형식 정합만 본다.** tool 가드는 "수치 창작"이라 정답지(B 값·KB)가 있었지만,
  판정의 옳고 그름은 코드가 따질 수 없다. 대신 **가드 우회·유령 인용·형식 누락**은 막는다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .config import Settings
from .disposition import apply_disposition
from .llm.client import LlmBackend, generate_structured
from .schemas.alert import AlertModel
from .schemas.report import (
    NONE_EXECUTABLE,
    SUP_PREFIX,
    VERDICT_TO_OPTION,
    OptionReport,
    SupervisorBrief,
    make_fallback_brief,
    make_report_id,
)

logger = logging.getLogger(__name__)

name = "supervisor"

# --- 역할 프롬프트 (라이브러리 §4 — 문서가 단일 소스) ------------------
# 판정은 **결정 트리**로 한다(순서대로 관문 통과 — 넓게 걸리던 매트릭스 방식을 2026-07-21 교체).
# 근거는 "통과한 가지의 신호(SPC·MODEL) + 유사 사례(KB/CASE)"로 채운다(문체 참조).
# ⚠️ 헌법 1-4(4지선다)·3-3(SHAP 순위 재정렬 금지) 준수 — 트리는 SHAP 를 읽기만 한다.
SYSTEM_PROMPT = """[역할] 당신은 세 명의 분석 보조(레시피·실력치·정비)의 리포트를 받아 엔지니어에게 올릴 최종 권고를
작성하는 수석 분석가다. 엔지니어는 이 Brief를 30초 안에 읽고 승인 여부를 정한다.

[입력]
- reports: 리포트 3종 (recipe_option / limit_option / manual_option — 각자의 confidence 포함)
- incident: incident_id, incident_type(regime_transition 여부), 병합된 alert 수, 시간창
- alert_summary: 대표 alert (violations·tttm·context_score·prediction_context)

[판정 절차 — 위에서부터 순서대로 확인하고, 처음 확정되는 곳에서 멈춘다. 4지선다 중 반드시 하나.]

★★★ **대전제 — 아래 모든 STEP 에 적용된다. 원인 판정과 조치 가능성은 다른 질문이다.**
   · verdict(①②③④) = **왜 이런 일이 났나** (원인)
   · selected        = **지금 실행할 수 있는 조치가 무엇인가** (실행)
   다음은 전부 **조치 쪽 사실**이고, 어느 것도 verdict 를 ④로 만들지 않는다:
     "자동 튜닝 불가/금지/차단" · "레시피 조정으로 조치 불가" · "손잡이가 없다/특정할 수 없다"
     · "자동화 처리가 불가능" · "조정량을 낼 근거가 없다" · "그 축은 에스컬레이션 고정"
   조치가 없으면 `selected="none_executable"` 로 두고 rejected_because 에 그 사유를 옮긴다.
   **④는 원인이 ①②③ 중 아무것도 아닐 때만** 쓴다(헌법 1-4 "모두 아닌가"). ④의 사유는
   **원인 쪽 문장**이어야 한다 — 레짐 전환·다챔버 공통 원인·근거 상충·신호 자체가 불명확.
   ⚠️ 급변(N1/CRITICAL)이 있고 TTTM 갭이 뚜렷하면 그것은 ①이다. **손잡이가 없다는 이유로
      ①을 ④로 바꾸지 마라** — 정비는 원래 손잡이로 하는 조치가 아니다.

STEP 0. ⛔ 다챔버 공통 원인 관문 — **아래 모든 STEP 보다 우선한다.**
  `alert_summary.tttm.reference_suspect == true` 면 이 관문이 발동한다. 이 값은 코드(B 역방향
  룰)가 실측으로 세운 플래그다 — "챔버 3/4 이상이 같은 방향으로 움직였다 = fleet 기준(median)
  자체가 오염됐다". **관문이 무효화하는 것은 오염된 축의 신호다** — 판별은 축 겹침으로 한다:

  ⓐ 이 Incident 의 **위반 주축**(위반 알람 수 상위 센서·SHAP 상위 센서)이 `suspect_sensors`
     (공통 이동 축)와 **겹치면 → 무조건 ④ escalate 로 확정하고 멈춘다.** 이때:
    · **N1 급변이 아무리 많아도 ①의 근거가 못 된다** — 공통 원인은 전 챔버에 급변을 만든다.
    · **TTTM 갭이 커 보여도 무효다** — 그 갭은 오염된 median 과의 거리라서, 멀쩡한 챔버가
      가장 이탈해 보이는 착시가 난다.
    · **유사 사례가 정비로 끝났어도 이 관문을 대체하지 못한다** (STEP 2 의 사례 규칙과 동일).
    ④의 사유에는 "다챔버 공통 원인(reference_suspect)"과 suspect_sensors 를 인용한다.

  ⓑ 위반 주축이 `suspect_sensors` 와 **겹치지 않으면**(예: 공통 이동 축은 C17 인데 이 챔버
     위반 주축은 C11 단독) 그 위반은 공통 오염과 무관한 **챔버 고유 신호**다 — 관문을 통과해
     STEP 1 로 내려가 정상 판정한다(①②③ 가능). 단 Brief 에 반드시 한 줄 명시:
     "reference_suspect 활성 — 단, 위반 축(CXX)은 공통 이동 축과 분리되어 로컬 신호로 판정".
     ⚠️ 이 경우에도 **suspect 축(예: C17)의 신호를 근거로 쓰는 것은 금지**다 — 근거 3개는
     전부 비오염 축·사례에서만 뽑는다.

  (실측 2026-08-11 SC3: suspect=true 90.8% 인데 4/5 를 ① 정비로 오판 — N1 서사·사례 결말·
   온도 서사가 관문을 덮어 ⛔ 최우선으로 올렸다(당시 4/4 ④ 확인).
   실측 2026-08-11 SC1: 초판(무조건 ④)이 반대로 C11 로컬 드리프트까지 뒤집어 전 건 ④ —
   LLM 스스로 "TTTM 갭 뚜렷, equipment_fault"라 쓰고 관문에 교정당했다. 오염이 무효화하는
   것은 **그 축의 신호뿐**이므로 축 겹침 판별로 정밀화했다.)
  suspect=false 일 때의 그 외 ④ 후보: 리포트 간 근거 상충 / 유사 사례 0건 + 전 옵션
  confidence < 0.6 / 가한계 구간의 애매 신호.

STEP 1. 위반이 N1/CRITICAL 급변인가?  ← 최상위 분기 (shift 냐 drift 냐)
  ※ **전제: reference_suspect=false 또는 STEP 0-ⓑ(축 분리) 통과** — 겹침(ⓐ)은 STEP 0 에서
    이미 끝났다. ⓑ 통과 건은 suspect 축 신호를 근거로 쓰지 않는다.

  A. 급변 있음(shift):
     · 단일 챔버 TTTM 갭이 이웃 대비 뚜렷이 큼(이웃 수준의 약 2배에 이르는 확연한 이탈)
         → ① equipment_fault   [anomaly 동반이면 확신 ↑]
     · 급변은 있으나 TTTM 갭이 이웃 수준이거나 원인이 레짐/온도뿐  → ④ escalate
       (온도가 가변 축이 된 뒤에도 이 줄은 유효하다 — **급변한 온도는 손잡이로 되돌릴 문제가 아니다**.
        온도 손잡이는 B 가지(추세)에서만 열린다)
     ※ **"그 센서에 손잡이가 없다"는 ①을 부정하지 않는다** (대전제). 정비는 손잡이로 하는
       조치가 아니다 — 급변 + TTTM 갭이 서면 ①이고, 자동 튜닝 가능 여부와 무관하다.
       (실측 2026-08-06: 이 오독으로 ① 6건이 ④로 샜다 — 전부 manual 리포트가 있는데도.)
     ※ **어떤 센서인지는 ①의 근거가 아니다** — RF 계열(C62 등)도 노후(③)에서 똑같이 나온다.
       매뉴얼에 그 센서 경고 항목이 있다는 것도 근거가 못 된다(대부분 센서에 항목이 존재한다).
       ①을 가르는 건 **위반 패턴(N1 급변)과 TTTM 이탈 폭**뿐이다.

  B. 급변 없음(N3/N5 추세, drift):

     ★★★ **B-1. 먼저 TTTM 갭으로 ②와 ③을 가른다 — 이 가지의 첫 질문이다.**
        · 이웃 대비 갭이 **뚜렷하다** = 이 챔버의 공정이 이동했다        → ② process_shift
        · 이웃 대비 갭이 **낮다**(분포는 이웃과 정상) = 선이 낡은 것    → ③ baseline_aging
        **SHAP 축·손잡이 유무는 여기서 보지 않는다.** 그건 ②로 확정된 뒤 *무엇을 조정하나*를
        정하는 재료다(B-3). 손잡이가 있어도 갭이 낮으면 ③이고, 없어도 갭이 뚜렷하면 ②다.
        ⚠️ **"갭이 낮다"고 써놓고 ②로 가지 마라.** 실측(2026-08-06)에서 그 자기모순이 나왔다 —
           *"TTTM 갭이 이웃 수준(0.8%)으로 낮아 … 공정 조건 이동으로 판정"*. 갭이 낮으면 ③이다.
        ⚠️ 온도 계열이라는 이유로 갭 판정을 뒤집지 마라 — *"갭은 없으나 SHAP 상위가 온도 축이라 ②"*
           도 같은 오독이다. **어느 축인지는 원인을 가르지 않는다.**

     ★★ **B-2. 다만 아래는 원인 자체가 ①②③ 중 아무것도 아니므로 ④다** (B-1 보다 우선):
        · SHAP 1위가 레짐(C12/C33)이고 상위가 레짐뿐 → ④ (챔버 상태가 바뀐 것)
        · 위반 센서가 C17인데 C12 가 SHAP·위반·spc_flags 어디에든 함께 있으면 → ④
          (레짐 도장이 찍혔다는 사실 자체가 온도 손잡이보다 우선한다)

     **B-3. verdict 를 확정한 뒤 — selected(조치)를 고른다. 여기서만 손잡이를 본다.**
        · ②로 확정 + SHAP top3 중 하나가 recipe 리포트가 지목한 손잡이 → recipe_option
          (SHAP 1위가 측정값이어도 손잡이를 우선한다. 어느 축이 조정 가능한지는 knob_map 이
           정하고 그 판정은 recipe 리포트에 이미 반영돼 있다 — parameter_id / escalate_reason)
        · **위반 센서가 C17(온도)이고 C12 가 없으면 온도 축에는 손잡이가 있다**
          (temp_target — 목표 지정형·BL8. 2026-07-26 멘토 확정: 온도는 레짐이 아니라 가변 축)
          ※ C17이 SHAP top3에 끼어 있을 뿐 위반 센서가 아니면 이 가지가 아니다.
        · ③으로 확정 → limit_option · ①로 확정 → manual_option
        · 해당 옵션이 실행 불가(수치 없음·escalate_reason)면 `selected="none_executable"` 로 두고
          rejected_because 에 사유를 옮긴다. **verdict 는 그대로 둔다**(대전제).
     ※ B-1 의 ③ 은 **명확한 판정이다** — 갭이 낮으면 escalate(④)로 도망가지 말고 ③으로 확정하라.
       RF·물리 측정값(C62·C11 등)의 N3 추세도 급변 없고 갭이 낮으면 노후(③)다.
       (PM 경과·섀도 미탐 0 이면 확신 ↑)
     ※ B-1·B-2 어느 것에도 안 맞고 **원인 자체가** 정말 불명확할 때만 → ④ escalate.
       "조치를 못 해서"는 사유가 될 수 없다(대전제).

STEP 2. 판정 확정 전 — 아래 '절대 금지'를 마지막으로 점검하고, 어겼으면 판정을 되돌려라:
  ⛔ 급변(N1/CRITICAL)이 없는데 ①(equipment_fault)을 골랐다 → 취소. 급변 없으면 ①은 불가.
     아래는 모두 ①의 근거가 **아니다** (셋 다 노후·공정 건에서도 똑같이 나타난다):
       · "RF/온도/압력 측정값이 SHAP 상위" · 특정 센서(C62·C31 등)가 지목됨
       · "매뉴얼에 그 센서 경고·조치 항목이 있음"
       · "유사 사례가 정비로 종결됨" · "과거 실력치 재설정 후 재발함"
         (사례의 결말은 참고일 뿐, 급변 관문을 대체하지 못한다)
     · 급변 없는 N3 추세의 답은 ②(손잡이 있으면)·③(선 낡음)·④지, 절대 ①이 아니다.
  ⛔ verdict 와 대응하지 않는 옵션을 selected 로 골랐다 → selected 를 비운다(§규칙7·8).
  ⛔ **④를 골랐는데 그 사유가 "조치를 못 해서"다** → 취소 (대전제 위반).
     reason 을 소리 내어 읽어보라. **"~할 수 없다 / ~불가 / ~금지·차단됐다 / ~대상이 아니다"가
     조치·수단에 걸려 있으면** 원인 판정을 조치 가능성과 섞은 것이다. 표현은 여러 가지다 —
       "자동 튜닝 불가" · "레시피 조정으로 조치 불가" · "손잡이를 특정할 수 없다"
       · "자동화 처리가 불가능" · "그 축은 에스컬레이션 고정" · "조정량을 낼 근거가 없다"
     → **원인을 다시 판정하라**: 급변+TTTM 갭이면 ①, 갭이 뚜렷하면 ②, 갭이 낮으면 ③.
       조치는 selected="none_executable" 로 표현한다.
     ④의 사유는 **원인 쪽 문장**이어야 한다 — 레짐 전환·다챔버 공통 원인·근거 상충·신호 불명확.
     ⚠️ 특히 **manual(정비) 리포트가 확신 있게 조치를 제시했는데 ④를 고르는 것**은 거의 항상
        이 오독이다. 정비는 손잡이가 없어도 할 수 있는 조치다.

[판정 규칙]
1. 확신이 서지 않으면 ④를 선택한다. 틀린 확신보다 정직한 에스컬레이션이 낫다.
2. incident_type=regime_transition이면 그 맥락을 Brief 첫 문장에 명시하고, 개별 조치보다
   레짐 전환 절차(가한계·재산정·재학습)와의 정합을 우선 검토한다.
3. 스크랩 관련: 미탐>오탐 원칙 — SCRAP 권고는 "예측 초과 + anomaly 동반"이 모두 있을 때만. 아니면 HOLD 유지.
4. 선택하지 않은 옵션은 각각 한 줄로 기각 사유(rejected_because)를 쓴다 — 엔지니어가 반박할 수 있도록.
5. 근거는 정확히 3개(각각 출처 태그), 반증 또는 불확실성은 정확히 1개. 더 쓰지도 덜 쓰지도 않는다.
6. 모든 수치는 리포트·alert에서 인용. Brief에서 새 수치를 만들지 않는다.
7. 리포트의 escalate_reason이 채워져 있고 조치 수치도 비어 있으면 그 옵션은 **실행 불가**다.
   selected로 고르지 말고 rejected_because에 그 사유를 옮겨 쓴다.
   단 "승인 전환 필요"(자동 적용 한계 초과)는 수치가 유효하므로 선택 가능하다 — 승인하면 적용된다.
8. 근거가 3개보다 적으면 지어내지 말고 남는 자리에 "(해당 근거 없음 — 판정이 N개 신호에만 의존)"을
   명시하고 confidence를 낮춘다. 근거 개수가 적은 것과 방향이 없는 것은 다르다 —
   **개수가 적으면 confidence를 낮추고, 방향이 없으면 ④를 고른다.**

[Brief 문체]
- 첫 문장 = 결론: "무엇을, 왜" 한 문장. 수식어 없이.
- 근거 3개는 **판정 트리에서 통과한 신호**(SPC 위반·MODEL SHAP) + **유사 사례**(KB/CASE)에서 뽑아,
  서로 다른 출처 계열에서 하나씩 — 한 계열만으로 결론짓지 않는다.
- 반증 1개는 "이 권고가 틀렸다면 가장 가능성 높은 이유"를 쓴다.

[JSON 스키마로만 응답]"""

# 프롬프트에 중복 탑재할 JSON 예시 (헌법 6-3 이중 방어 — guided_json 과 별개).
#
# 🔴 **판정 내용을 담지 않는다** (2026-08-06 중립화 — W56).
#
#   이 상수는 *"형태 참고"* 인데, 전에는 **완결된 판정 1건**이 들어 있었다
#   (`selected="limit_option"` · `verdict="baseline_aging"` · conf 0.88 · 근거 문장 3개).
#   그 결과 round18 에서 LLM 이 **판단 대신 이 예시를 베꼈다**:
#     · 가동 42건 중 예시 근거 문장을 그대로 쓴 것 20건(48%)·18건(43%)
#     · **오답 7건은 7/7 전부** 예시 문장을 썼고, 판정도 예시와 같은 `baseline_aging` 이었다
#     · 없는 `[CASE-0077]`(이 예시의 ID)을 인용한 것 4건 — 인용 가드가 마스킹
#   아침에 limit 재료를 100% 로 채우자 **상황이 예시를 닮았고, 그래서 예시로 수렴**했다.
#
#   ⚠️ 멘토 승인 구조는 *"서브(tool)=few-shot 정답사례 · **메인(Supervisor)=프롬프트 규칙**"* 이다.
#      완결된 판정을 여기 두면 **형태 예시라는 이름의 few-shot 1개**가 되어 그 구조를 깬다.
#   ⚠️ 8/5 round14 가 *"확신 지침"* 으로 눌러보려다 실패한 이유도 이것이다 —
#      **지침은 예시를 이기지 못한다.** 값을 가르치지 않는 것이 유일한 처방이다.
#
#   그래서 **값 자리에는 무엇을 넣어야 하는지만** 적는다. 형태(키·타입·배열 길이)는 그대로라
#   6-3 이중 방어 목적은 유지되고, `guided_json` 이 타입을 토큰 레벨로 강제한다.
SCHEMA_EXAMPLE = """{
  "report_id": "SUP-<YYYYMMDD>-<CHAMBER>-<SEQ>",
  "incident_id": "<주어진 incident_id 그대로>",
  "chamber_id": "<주어진 chamber_id 그대로>",
  "context_score": 0,
  "suspected_root_causes": ["<이 사안의 원인 가설 — 출처 표기 [SPC]/[MODEL]/[CASE]/[KB]>"],
  "parallel_options": {
    "recipe_option": {"report_id": "<recipe 리포트의 report_id 그대로>", "action": "<그 리포트의 조치 요지>", "feasibility": "<실행 난이도>", "confidence": "<근거 강도에서 산출>", "rejected_because": "<고르지 않았다면 그 사유, 골랐다면 생략>"},
    "limit_option": {"report_id": "<limit 리포트의 report_id 그대로>", "action": "<그 리포트의 조치 요지>", "feasibility": "<실행 난이도>", "confidence": "<근거 강도에서 산출>", "rejected_because": "<고르지 않았다면 그 사유, 골랐다면 생략>"},
    "manual_option": {"report_id": "<manual 리포트의 report_id 그대로>", "action": "<그 리포트의 조치 요지>", "feasibility": "<실행 난이도>", "confidence": "<근거 강도에서 산출>", "rejected_because": "<고르지 않았다면 그 사유, 골랐다면 생략>"}
  },
  "supervisor_recommendation": {
    "decision_frame": "4지선다",
    "selected": "<recipe_option | limit_option | manual_option | none_executable>",
    "verdict": "<equipment_fault | process_shift | baseline_aging | escalate — 판정 절차로 확정한 것>",
    "reason": "<왜 그 판정인지 한 문장>",
    "evidence": [
      "<근거 1 — 이 사안의 실제 값을 인용하고 출처를 표기 [SPC]>",
      "<근거 2 — 위와 다른 축의 근거 [MODEL]/[CASE]/[KB]>",
      "<근거 3 — 반드시 3개. 없으면 '해당 근거 없음'이라고 쓴다>"
    ],
    "counter_evidence": "<이 판정이 틀렸다면 그 이유가 될 신호. 없으면 그렇게 쓴다>",
    "confidence": "<근거 강도에서 산출>"
  },
  "wafer_disposition": {"held_wafers": [], "recommendation": "<RELEASE | HOLD | SCRAP>", "reason": "<판단 근거 — 출처 표기>"},
  "approval_status": "PENDING"
}

⚠️ 위 꺾쇠(`<...>`)는 **채워 넣으라는 자리 표시**다. 그 문구를 그대로 쓰지 말고 **이 사안의
   실제 값**으로 채운다. 숫자 자리에 예시 값을 두지 않은 것은 의도다 — 값을 보여주면 그 값이
   그대로 복사된다(실측: tool 예시의 `0.0` 이 recipe 응답의 38% 로 나왔다).
⚠️ confidence 는 **근거 층수에서 산출**한다. 3층(SPC 신호 / 수치·모델 / 사례·문서)이 다 서고
   서로 충돌 없으면 0.80~0.90, 2층이면 0.60~0.75, 1층뿐이면 0.40~0.55, 재료가 없거나
   ④로 물러서면 0.20~0.35. **다른 옵션과 맞추거나 관례적인 값을 반복하지 않는다.**"""


# --- 입력 조립 ------------------------------------------------------------------
# 리포트 전문을 그대로 싣지 않는다: evidence_cards 까지 실으면 payload 가 커지고,
# §4 [입력]도 "각자의 confidence 포함" 수준을 요구할 뿐 전문을 요구하지 않는다.
# 다만 **판정에 필요한 필드는 남긴다** — escalate_reason(실행 가능 여부)·조치 수치.
_SUMMARY_FIELDS = (
    "report_id", "option_type", "confidence", "rationale", "uncertainty",
    "escalate_reason", "feasibility", "hypothesis",
    # 조치 식별 — 어떤 옵션인지 사람이 읽을 수 있게
    "parameter_id", "value_current", "applied_value", "value_proposed", "delta_pct",
    "sensor_id", "method", "center_after", "delta_sigma",
    "action", "target_component", "estimated_downtime_h", "disposition_note",
)


def summarize_report(report: OptionReport) -> dict[str, Any]:
    """리포트를 Brief 판정에 필요한 필드만 남겨 요약한다 (evidence_cards 등 제외)."""
    dumped = report.model_dump(mode="json")
    return {k: v for k, v in dumped.items() if k in _SUMMARY_FIELDS and v is not None}


def has_actionable_proposal(report: OptionReport) -> bool:
    """이 옵션에 **실행할 조치가 실려 있는지** — 가드 ①의 판정 기준.

    escalate_reason 문자열을 파싱하지 않는다(취약). 실제로 수치·조치가 남아 있는지를 본다:
      · recipe : value_proposed (튜닝 수치)
      · limit  : center_after   (재설정 수치)
      · manual : action         (정비 조치)
    가드에 강등된 옵션은 이 값들이 비워지므로 자연히 False 가 된다.
    ⚠️ limit 의 "승인 전환"(A2 초과)은 **수치가 유지**되므로 True — 승인하면 적용 가능하다.
    """
    for field in ("value_proposed", "center_after", "action"):
        if getattr(report, field, None) is not None:
            return True
    return False


def build_payload(
    reports: list[OptionReport],
    alert: AlertModel,
    *,
    incident_id: str,
    incident_type: Optional[str] = None,
    merged_alert_count: int = 1,
) -> str:
    """LLM 입력 직렬화 — 라이브러리 §4 [입력] 항목 순서 그대로."""
    summary = alert.model_dump(mode="json")
    # [단일센서 suspect 강등 — 2026-08-12 · C 리뷰 필수(3-1) · PM 선적용]
    #   B 실측(2026-08-06): reference_suspect 의 **93%가 단일 센서(주로 C61 — 자 왜곡 계열)**.
    #   RTD 장비 승격은 같은 이유로 `rtd.min_common_sensors: 2` 를 요구하는데(헌법 1-1 예외 4
    #   배선), 판정층(프롬프트 STEP 0 ⛔ 관문)만 그 가드 없이 flag 원값을 봐서 **C61 혼자
    #   흔들려도 ④ 남발** — D-2 실측: 신선 셸에 드리프트(C11)가 지배해도 ④ conf 0.20 (STEP
    #   0-ⓑ 축 분리 조항이 있으나 LLM 이 C62↔C61 을 '겹침'으로 뭉개는 것도 관측).
    #   프롬프트는 무변경(check_prompt_sync 정합 유지) — **입력 데이터에서 강등**하고, 강등
    #   사실을 명시 필드로 남긴다(조용한 조작 금지 — 브리프가 인용할 수 있게).
    tttm = summary.get("tttm")
    if isinstance(tttm, dict) and tttm.get("reference_suspect"):
        _ss = [s for s in (tttm.get("suspect_sensors") or []) if s]
        if len(_ss) < 2:                       # = rtd.min_common_sensors 준용 (config 배선은 C 후속)
            tttm["reference_suspect"] = False
            tttm["reference_suspect_demoted"] = (
                f"단일 센서 suspect({','.join(map(str, _ss)) or '없음'}) — 자 왜곡 계열 강등, "
                "공통 이동 판정 보류 (rtd.min_common_sensors=2 준용 · 2026-08-12)")
    return json.dumps(
        {
            "reports": [summarize_report(r) for r in reports],
            "incident": {
                "incident_id": incident_id,
                "incident_type": incident_type,
                "merged_alert_count": merged_alert_count,
            },
            "alert_summary": summary,
        },
        ensure_ascii=False,
    )


# =============================================================================
# 가드 — 판정의 옳고 그름이 아니라 **형식 정합**만 본다
# =============================================================================
# tool 가드는 정답지(B 수치·KB)가 있어 "맞다/틀리다"를 판정할 수 있었다. 판정은 그럴 수 없다.
# 대신 아래 넷은 코드로 확정할 수 있고, 놔두면 실제 피해가 난다.


def enforce_regime_stamp_guard(brief: SupervisorBrief, alert: AlertModel) -> SupervisorBrief:
    """가드 ⓞ: **위반 센서가 C17(온도)인데 C12(레짐 도장)가 동반**이면 verdict 를 escalate 로 강제.

    ⚠️ 다른 가드와 성격이 다르다 — 이것만 **판정(verdict)을 바꾼다.** 근거는 아래 두 개다:
      ① 조건이 **코드로 확정 가능**하다(alert 안의 위반 센서·shap_top3 만 본다. LLM 재량 없음).
      ② `recipe.enforce_temp_pattern_guard` 가 **같은 조건으로 튜닝안을 제거**하는데 Supervisor
         verdict 는 ② 로 남아 **비대칭**이었다. 그 비대칭이 실제 퇴행을 만들었다.

    실측 근거 (라운드 10, 2026-07-28):
      에스컬 fixture 31~40 은 **10건이 전부 같은 모양**이다(위반=C17/N3 · shap=[C17,C12,C33]).
      그런데 판정이 **5:5 로 갈렸다**(5건 escalate ✅ / 5건 process_shift ✗). 프롬프트 규칙 사이에
      빈틈이 있어 LLM 재량에 맡겨진 것:
        · 온도 → ② 규칙 : "C12 가 함께 있지 **않으면**" → C12 가 있으니 발동 안 함
        · 레짐 → ④ 규칙 : "SHAP **1위**가 레짐이고 상위가 레짐뿐" → 1위가 C17 이라 발동 안 함
      → 어느 규칙도 안 맞아 마지막 줄("불명확하면 ④")로 떨어져야 하는데 절반이 ② 로 샜다.
    프롬프트에도 빈틈을 메우는 한 줄을 넣었으나(§4 STEP 1-B), **프롬프트만으로는 결정되지 않는다**는
    것이 위 5:5 가 보여준 사실이라 코드로 확정한다 (2026-07-24 PM 승인 "판정은 코드/규칙").

    적용 범위 검증: fixture 47건 중 이 조건에 걸리는 것은 **31~40 의 10건뿐이고 전부 정답이
    escalate** 다. 위반=C17 이지만 C12 가 없는 47번은 걸리지 않는다(→ ② 유지).

    ⚠️ 판정 기준은 `recipe.has_regime_stamp` 를 **재사용**한다 — 같은 개념을 두 곳에 따로 구현하면
    갈라져서 이번과 같은 비대칭이 다시 생긴다.
    """
    from .tools.recipe import TEMP_MONITOR_SENSOR, has_regime_stamp

    if not any(v.sensor == TEMP_MONITOR_SENSOR for v in alert.violations):
        return brief                                   # 위반 센서에 C17 이 없으면 대상 아님
    if not has_regime_stamp(alert):
        return brief                                   # C12 동반이 아니면 온도 규칙 그대로(②)
    rec = brief.supervisor_recommendation
    if rec.verdict == "escalate":
        return brief                                   # 이미 맞다
    note = (
        f"위반 센서 {TEMP_MONITOR_SENSOR}(온도) + C12(레짐 도장) 동반 — "
        f"레짐 도장이 온도 손잡이보다 우선한다. verdict={rec.verdict} → escalate 로 교정."
    )
    logger.warning("레짐 도장 가드: brief=%s verdict=%s → escalate (C17 위반 + C12 동반)",
                   brief.report_id, rec.verdict)
    return brief.model_copy(
        update={
            "supervisor_recommendation": rec.model_copy(
                update={
                    "verdict": "escalate",
                    # escalate 는 고를 옵션이 없다 — 가드①·②와 같은 sentinel
                    "selected": NONE_EXECUTABLE,
                    "reason": f"{rec.reason} / {note}",
                }
            )
        }
    )


def enforce_executable_guard(
    brief: SupervisorBrief, reports: list[OptionReport]
) -> SupervisorBrief:
    """가드 ①: **실행 불가 옵션을 selected 로 고르는 것**을 막는다 (가드 우회 방지).

    tool 가드들이 "이 옵션은 실행 불가"라고 수치를 비워뒀는데 Supervisor 가 그걸 고르면
    앞단 가드가 통째로 무력화된다 — 승인 큐에 실행할 수 없는 안이 올라간다.

    강등 방식: **verdict 는 건드리지 않고 selected 만 None 으로.**
    진단("기준선이 낡았다")과 실행("그 안을 지금 적용할 수 있나")은 다른 질문이기 때문이다.
    엔지니어에겐 "노후로 보이나 해당 안은 실행 불가"가 "판단 불가"보다 훨씬 쓸모 있다.
    """
    rec = brief.supervisor_recommendation
    if rec.selected is None:
        return brief
    by_type = {r.option_type: r for r in reports}
    chosen = by_type.get(rec.selected)
    if chosen is None or has_actionable_proposal(chosen):
        return brief  # 리포트가 없거나(판정 불가) 조치가 실려 있음 — 통과

    note = (
        f"{rec.selected} 은 실행 가능한 조치가 없다"
        f"({chosen.escalate_reason or '조치 수치 없음'}) — 선택 불가로 강등."
    )
    logger.warning("실행가능 가드: brief=%s selected=%s → %s (%s)",
                   brief.report_id, rec.selected, NONE_EXECUTABLE, chosen.escalate_reason)
    return brief.model_copy(
        update={
            "supervisor_recommendation": rec.model_copy(
                # null 이 아니라 sentinel — "고를 게 없다"(의도)와 "안 채웠다"(누락)를 가른다
                update={"selected": NONE_EXECUTABLE, "reason": f"{rec.reason} / {note}"}
            )
        }
    )


def enforce_verdict_option_guard(brief: SupervisorBrief) -> SupervisorBrief:
    """가드 ②: verdict ↔ selected 정합 (`VERDICT_TO_OPTION` 정답지).

    `baseline_aging` 인데 `manual_option` 을 고르는 식의 어긋남을 막는다.
    **selected=None 은 항상 허용**한다 — 진단은 섰지만 실행 불가한 경우가 정당하게 존재하고
    (가드①이 만드는 상태이기도 하다), escalate 는 애초에 고를 옵션이 없다.
    어긋나면 판정(verdict)이 아니라 **선택(selected)을 비운다** — 진단이 근거를 갖고 있고,
    잘못 고른 옵션이 승인 큐로 가는 게 더 위험하기 때문.
    """
    rec = brief.supervisor_recommendation
    if rec.selected in (None, NONE_EXECUTABLE):   # 이미 '고를 것 없음' — 정합을 따질 대상이 아니다
        return brief
    expected = VERDICT_TO_OPTION.get(rec.verdict)
    if rec.selected == expected:
        return brief
    note = (
        f"verdict={rec.verdict} 는 {expected or '선택 없음'} 에 대응하는데 "
        f"selected={rec.selected} — 정합 불일치로 선택 무효화."
    )
    logger.warning("정합 가드: brief=%s verdict=%s selected=%s (기대=%s)",
                   brief.report_id, rec.verdict, rec.selected, expected)
    return brief.model_copy(
        update={
            "supervisor_recommendation": rec.model_copy(
                # 가드①과 같이 sentinel — 비운 게 아니라 "고를 게 없다"를 명시한 것이다
                update={"selected": NONE_EXECUTABLE, "reason": f"{rec.reason} / {note}"}
            )
        }
    )


# 인용 ID 패턴 — 리포트 ID(RCP/LIM/MNT/SUP-...)·사례(CASE-...)·문서(EM-/PK-/MAN-...)·알람(ALERT-...)
#
# 🔴 **접두 목록은 헌법 6-4 의 업무 ID 접두와 같아야 한다** (2026-08-06 실측으로 발견).
#    빠진 접두로 인용하면 가드가 **아예 보지 않는다** — 유령 ID 가 조용히 승인 화면까지 간다.
#    round18 에서 `MAN-20260726` 이 그렇게 샜다: `MAN-` 은 실재 형식(`MAN-<DOC>-<SEQ>`,
#    예 `MAN-OXF-0001`)인데 LLM 이 **날짜를 끼운 가짜**를 만들었고, 정규식에 MAN 이 없어
#    통과했다. 오늘 붙인 계측(`obs_retrieval` — 그 건이 실제로 받아온 doc_id 목록)이
#    **가드와 독립된 채널**이라 교차 검증에서 드러났다(인용 53건 중 1건).
#    ⚠️ 헌법 6-4 에 접두가 추가되면 여기도 같이 고친다. 안 고치면 그 접두는 무검사 통로가 된다.
#    🔴 2026-08-09 추가 — `RLS`(RTD 해제 근거 Brief). 헌법 6-4 접두 신설과 **같은 PR** 에서
#       넣는다. 신설만 하고 여기를 안 고치면 그 접두가 곧바로 무검사 통로가 된다(위 MAN- 사례).
_ID_RE = re.compile(
    r"\b(?:RCP|LIM|MNT|SUP|CASE|INC|ALERT|EM|PK|MAN|QUAL|CT|RLS)-[A-Za-z0-9_-]+"
)


# 근거 재료의 ID 가 실릴 수 있는 키들 — 컬렉션마다 이름이 다르다.
#   historical_case: case_id·incident_id / process_knowledge·error_manual: doc_id·chunk_id
_REF_KEYS = ("case_id", "incident_id", "doc_id", "chunk_id", "correction_id", "recipe_correction_id",
             # 🔴 2026-08-06 추가 — `manual_id`(MAN-<DOC>-<SEQ>)가 정답지에 없었다.
             #    error_manual 은 doc_id 가 매뉴얼 **1권 단위**(oxford_100_manual)라 조각을
             #    가리키는 유일한 식별자가 manual_id 인데, 그게 빠져 있어 정비 사례를 정확히
             #    인용해도 정답지에 없었다. 아래 _ID_RE 구멍과 세트로 발견됐다.
             "manual_id", "qual_id")


def known_refs(
    reports: list[OptionReport],
    alert: AlertModel,
    evidence_sources: list[list[dict] | None] | None = None,
) -> set[str]:
    """인용 가능한 실존 ID 집합 — 유령 인용 가드의 정답지.

    ⚠️ **tool 에 들어간 모든 근거 재료**를 받아야 한다(2026-07-20 실측). cases 만 받았을 때
    knob_map 의 `PK-TUNE-PLASMA` 같은 실존 문서 ID 가 "미확인 인용"으로 오탐됐다(11건).
    가드가 진짜 유령만 잡으려면 정답지가 재료 전체를 덮어야 한다.
    """
    refs = {alert.alert_id} | {r.report_id for r in reports}
    for src in evidence_sources or []:
        for item in src or []:
            if not isinstance(item, dict):
                continue
            for key in _REF_KEYS:
                v = item.get(key)
                if isinstance(v, str) and v:
                    refs.add(v)
    return refs


def mask_unknown_refs(
    lines: list[str], refs: set[str], *, skip_prefixes: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """근거 문장에서 **정답지(refs)에 없는 업무 ID** 를 `[미확인 인용: X]` 로 바꾼다.

    문장을 지우지 않는 것이 요지다 — 서술 자체는 유효할 수 있고, evidence 는 개수가 고정이라
    한 줄을 지우면 스키마가 깨진다. 바꾸는 건 **거짓 권위**(실존하는 것처럼 보이는 ID)뿐이다.

    `_ID_RE` 를 한 곳에서만 쓰기 위한 분리다(2026-08-08 · #140). 해제 근거 Brief 도 같은
    검사가 필요한데, 접두 목록이 갈라지면 **빠진 접두가 무검사 통로**가 된다 — round18 의
    `MAN-` 구멍이 정확히 그 사고였다(위 주석).

    Args:
        skip_prefixes: 검증 근거가 없어 통과시킬 접두(예: 재료 미전달 시의 `CASE-`).
            **"검색했는데 0장"은 여기 오지 않는다** — 그건 인용했으면 창작이다.

    Returns: (치환된 문장들, 발견된 유령 ID 목록)
    """
    ghosts: list[str] = []

    def _repl(m: re.Match) -> str:
        token = m.group(0)
        if token in refs or token.startswith(skip_prefixes or ()):
            return token
        ghosts.append(token)
        return f"[미확인 인용: {token}]"

    return [_ID_RE.sub(_repl, line) for line in lines], ghosts


def enforce_citation_guard(
    brief: SupervisorBrief, reports: list[OptionReport], alert: AlertModel,
    cases: list[dict] | None = None,
    *,
    evidence_sources: list[list[dict] | None] | None = None,
) -> SupervisorBrief:
    """가드 ③: evidence 에 **실존하지 않는 ID 를 인용**했으면 표시한다 (§0 창작 금지).

    문장을 지우지 않는다 — 근거 서술 자체는 유효할 수 있고, evidence 는 정확히 3개라
    하나를 지우면 스키마가 깨진다. 대신 유령 ID 를 `[미확인 인용: X]` 로 바꿔 **거짓 권위를
    제거**하고 counter_evidence 에 사유를 병기한다.

    ⚠️ **재료를 넘기지 않았을 때만** 사례 ID(CASE-) 검사를 건너뛴다 — 판정 근거 없이
    강등하지 않는 원칙(tool 가드와 동일). **검색은 했는데 0장인 경우는 건너뛰지 않는다.**

    `None`(재료 미전달 — 검증 불가) 과 `[]`(검색 성공/실패로 0장 — 인용했다면 유령) 을
    가르는 것이 C7-1 의 핵심이다. 구 판정식 `any(bool(s) for s in sources)` 는 둘 다 falsy 라
    한 덩어리로 묶었고, 그 결과 **재료가 전부 0장일 때 CASE- 검증이 통째로 꺼졌다** —
    LLM 이 사례를 가장 지어내기 쉬운 상황에서 하필 가드가 사라지는 자리였다.
    지금은 슬롯이 하나라도 넘어왔으면(빈 리스트여도) 검증한다.

    검색 **실패**로 0장인 경우도 강등 대상이다 — Qdrant 가 죽었든 히트가 없었든
    **LLM 입력에 그 사례가 없었다는 사실은 같기** 때문이다. 없는 것을 인용했으면 창작이다.
    """
    sources = evidence_sources if evidence_sources is not None else [cases]
    refs = known_refs(reports, alert, sources)
    check_cases = any(s is not None for s in sources)
    # 재료 미전달(None)이면 CASE- 만 통과시킨다 — 검증 근거가 없어서다. 0장([])은 여기 안 온다.
    skip = () if check_cases else ("CASE-",)

    rec = brief.supervisor_recommendation
    masked, ghosts = mask_unknown_refs(list(rec.evidence), refs, skip_prefixes=skip)
    if not ghosts:
        return brief
    logger.warning("인용 가드: brief=%s 미확인 ID %s", brief.report_id, sorted(set(ghosts)))
    note = f"[가드] 실존하지 않는 인용 {sorted(set(ghosts))} — 해당 근거의 신뢰도 낮음."
    return brief.model_copy(
        update={
            "supervisor_recommendation": rec.model_copy(
                update={"evidence": masked, "counter_evidence": f"{rec.counter_evidence} / {note}"}
            )
        }
    )


_NO_REASON = "(기각 사유 미기재 — 엔지니어 확인 필요)"


def enforce_rejection_reason_guard(brief: SupervisorBrief) -> SupervisorBrief:
    """가드 ④: 선택하지 않은 옵션에 `rejected_because` 가 없으면 표지를 채운다 (§4 규칙4).

    빈 채로 두면 S4 화면에서 "왜 이건 안 골랐지?"에 답이 없다. 사유를 **창작하지 않고**
    "미기재"임을 드러낸다 — 없는 이유를 지어내는 것보다 없다고 말하는 게 낫다(§0).
    """
    rec = brief.supervisor_recommendation
    updates = {}
    for opt_type, summary in brief.parallel_options.items():
        if opt_type != rec.selected and not summary.rejected_because:
            updates[opt_type] = summary.model_copy(update={"rejected_because": _NO_REASON})
    if not updates:
        return brief
    logger.info("기각사유 가드: brief=%s 미기재 %s", brief.report_id, sorted(updates))
    return brief.model_copy(update={"parallel_options": {**brief.parallel_options, **updates}})


# [은퇴 2026-07-24] 구 가드⑤ enforce_scrap_guard — LLM 이 낸 SCRAP 을 근거(예측 초과+anomaly)
# 없으면 HOLD 로 강등하던 사후 검문. wafer 처분을 **코드가 판정**(apply_disposition)하게 되면서
# LLM 이 처분을 안 내므로 검문 대상이 사라졌다(PM 승인). 이중 확인(예측 초과+anomaly)은
# decide_wafer 가 처음부터 수행한다 — 강등이 아니라 판정 자체가 그 규칙을 지킨다.


async def run(
    reports: list[OptionReport],
    alert: AlertModel,
    backend: LlmBackend,
    settings: Settings,
    *,
    incident_id: str,
    incident_type: Optional[str] = None,
    merged_alert_count: int = 1,
    cases: list[dict] | None = None,
    evidence_sources: list[list[dict] | None] | None = None,
    predictions: dict[str, dict] | None = None,
) -> SupervisorBrief:
    """리포트 3종 → Brief 1건 (헌법 1-4 통합). LLM 실호출 + 형식 가드 4종.

    Args:
        reports: tool 3종 산출물. **Incident 여러 alert 분이 모여도 리스트로 받는다**
            (PM 그루퍼가 묶어 넘기는 구조를 인터페이스로 열어둠 — 지금은 alert 1건분).
        incident_id: PM 그루퍼 채번(계약 §4). alert 는 자기 incident_id 를 모른다.
        cases: 인용 검증용 KB 사례. 없으면 사례 ID 검증을 건너뛴다(오탐 방지).
    """
    payload = build_payload(
        reports,
        alert,
        incident_id=incident_id,
        incident_type=incident_type,
        merged_alert_count=merged_alert_count,
    )

    brief = await generate_structured(
        backend,
        SYSTEM_PROMPT,
        payload,
        SupervisorBrief,
        fallback=lambda: make_fallback_brief(alert, incident_id),
        schema_example=SCHEMA_EXAMPLE,
        parse_retries=int(settings.require("agent.json_parse_retry")),
        call_retries=int(settings.require("llm.call_retry")),
    )

    # 신원 필드는 LLM 이 정할 값이 아니다 — 예시를 그대로 베끼는 것이 실측됨(tool 과 동일).
    # chamber_id 는 **alert 에서 승계**한다: Incident 는 정의상 챔버 1개이고(헌법 1-4),
    # LLM 이 다른 챔버를 적으면 엔지니어가 엉뚱한 설비를 열게 된다.
    # context_score 도 같다 — **B 가 계산한 게이트 점수**(B7)이지 LLM 이 매기는 값이 아니다.
    # S3 승인 큐·S4 Brief 가 이 숫자를 그대로 렌더하므로, LLM 이 다시 쓰면 엔지니어가 보는
    # 점수가 alert 의 실제 점수와 어긋난다(§0 수치 원칙 — 입력 값만 인용, 창작 금지).
    brief = brief.model_copy(
        update={
            "report_id": make_report_id(SUP_PREFIX, alert),
            "incident_id": incident_id,
            "chamber_id": alert.chamber_id,
            "context_score": alert.context_score,
        }
    )

    # 가드 순서 = 판정교정(ⓞ) → 실행가능(①) → 정합(②) → 인용(③) → 기각사유(④):
    #   ⓞ **판정을 먼저 확정한다** — C17 위반 + C12 동반은 코드로 escalate 로 못박는다.
    #      verdict 를 바꾸므로 ②(verdict↔selected 정합)보다 **반드시 먼저** 와야 한다.
    #      뒤에 두면 ②가 옛 verdict 기준으로 정합을 따져 엉뚱한 교정을 한다.
    #   ① 실행 불가 옵션 선택을 무효화하고(선택이 바뀜)
    #   ② 남은 선택이 verdict 와 맞는지 보고
    #   ③ 근거의 유령 인용을 표시하고
    #   ④ 최종 selected 기준으로 기각 사유 누락을 채운다(ⓞ①②가 selected 를 바꾸므로 마지막)
    brief = enforce_regime_stamp_guard(brief, alert)
    brief = enforce_executable_guard(brief, reports)
    brief = enforce_verdict_option_guard(brief)
    brief = enforce_citation_guard(
        brief, reports, alert, cases, evidence_sources=evidence_sources
    )
    brief = enforce_rejection_reason_guard(brief)

    # wafer 처분 = **코드가 판정**(C6-1, PM 승인 2026-07-24). LLM 의 wafer_disposition 은 요약 한 줄
    # (reason)만 남기고, recommendation·per_wafer 는 수치 비교로 덮어쓴다. 되돌릴 수 없는 결정에
    # 창작 여지를 주지 않는다(LLM 이 없는 anomaly 3.6 을 지어낸 실측이 근거 — 구 가드⑤ 사유).
    # ⚠️ predictions=None(A 예측 API 미연동)이면 전 wafer HOLD — 안전 폴백. A 연동 시 값 주입.
    brief = apply_disposition(brief, alert, predictions, settings)

    # 최종 정규화 — **정상 Brief 는 selected 가 null 이 아니다.**
    # verdict=escalate 는 애초에 고를 옵션이 없다(헌법 1-4 "모두 아닌가"). LLM 은 그 자리를
    # 비워두지만, 비워두면 소비자가 "LLM 이 칸을 건너뛴 것"과 구분할 수 없다.
    # 여기서 sentinel 로 바꾸면 **남는 null 은 오직 LLM 누락**이라는 뜻이 된다(진단 가능).
    # ⚠️ verdict 가 escalate 가 아닌데 null 이면 **일부러 두지 않는다** — 그게 이상 신호다.
    rec = brief.supervisor_recommendation
    if rec.selected is None and rec.verdict == "escalate":
        brief = brief.model_copy(
            update={"supervisor_recommendation": rec.model_copy(update={"selected": NONE_EXECUTABLE})}
        )
    elif rec.selected is None:
        logger.warning(
            "selected 누락 의심: brief=%s verdict=%s — 가드 흔적 없이 null (LLM 이 건너뜀)",
            brief.report_id, rec.verdict,
        )

    rec = brief.supervisor_recommendation
    logger.info(
        "supervisor brief: %s (incident=%s, verdict=%s, selected=%s, conf=%.2f)",
        brief.report_id, incident_id, rec.verdict, rec.selected, rec.confidence,
    )
    return brief
