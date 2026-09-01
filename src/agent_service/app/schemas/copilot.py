# -*- coding: utf-8 -*-
"""S9 Agent Copilot — 질의/응답 스키마 (화면 정의서 S9).

**쉬운 말 요약** — 엔지니어가 화면에서 "이 추천 왜 나왔어?" 하고 물으면, 지금 보고 있는
챔버·Incident 를 자동으로 붙여서 이미 만들어져 있는 리포트·근거를 자연어로 되돌려준다.
**새로 판단하지 않는다.** 있는 것을 찾아 읽어주는 채널이고, 승인·조치는 여기서 못 한다.

읽기 전용이라 **DB 에 적재하지 않는다** → 업무 ID 접두(6-4)를 새로 만들지 않는다.
(적재하기로 바뀌면 `report_id` 접두 신설 + `supervisor._ID_RE` + 커버리지 테스트가 세트다.)
"""

from __future__ import annotations

from typing import Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 근거 카드의 출처 종류 — 화면이 아이콘·색을 이걸로 가른다.
#  `report` = 이미 생성된 Supervisor/Agent 리포트, `spc` = 위반·관리선 실측,
#  `chamber` = 챔버 상태(가동/정지·uptime), `knob` = 레시피 손잡이 카드,
#  `glossary` = 용어 한 줄 정의, 나머지 셋은 KB 컬렉션(historical_case·error_manual·process_knowledge).
#
# 🔴 **재료 종류를 늘리면 여기도 같이 늘린다 — 안 늘리면 답 전체가 폴백으로 죽는다.**
#    2026-08-11 하루에 **두 번** 밟았다: ① 챔버 상태(`chamber`) ② 용어(`glossary`).
#    둘 다 모델이 재료에 맞는 kind 를 골라 보냈는데 enum 에 없어 검증 3회 실패 →
#    "답을 받지 못했습니다". **재료는 늘었는데 목록은 안 늘어난 것**이 유일한 원인이고,
#    증상은 "그 재료 무시"가 아니라 **답 전체 소실**이라 원인이 안 보인다.
#    ⚠️ `collect_materials` 에 `mats[...]` 키를 새로 넣으면 **반드시 여기를 먼저 본다.**
EvidenceKind = Literal["report", "spc", "chamber", "knob", "glossary",
                       "case", "manual", "knowledge"]


class CopilotContext(BaseModel):
    """화면이 자동 첨부하는 컨텍스트 (S9 "컨텍스트 자동 주입" — 독립 챗봇과의 차별점).

    전부 optional 이다 — S9 스펙이 *"컨텍스트 없이 전역 질의도 허용"* 이라고 명시한다.
    값이 없으면 좁히지 않고 답하며, 그 사실을 `scope_note` 에 적어 사람이 알게 한다.
    """

    model_config = ConfigDict(extra="ignore")

    chamber: Optional[str] = Field(default=None, description="현재 chamber 필터 (예: SIM_CH_3)")
    incident_id: Optional[str] = Field(default=None, description="보고 있는 Incident (INC-…)")
    sensor: Optional[str] = Field(default=None, description="센서 C코드 — 표시명 금지(6-4)")
    range: Optional[str] = Field(default=None, description="기간 표현 (예: 7d) — 표시용")


#: 재료 키·복수형 → 정식 kind. 모델은 **재료 키 이름을 그대로** kind 로 쓰는 경향이 있다
#  (실측: `mats["chambers"]` → `kind="chambers"`). 그건 자연스러운 추론이지 오류가 아니다.
_KIND_ALIASES = {
    "chambers": "chamber", "cases": "case", "manuals": "manual",
    "knobs": "knob", "knob_map": "knob", "reports": "report",
    "violations": "spc", "sensors": "spc", "sensor": "spc",
    "judgement": "report", "term": "glossary", "terms": "glossary",
}
#: 정규화도 실패했을 때 쓸 값. 표시 힌트일 뿐이라 **틀린 아이콘이 답 없음보다 낫다.**
_KIND_FALLBACK = "report"


class EvidenceRef(BaseModel):
    """근거 카드 1장 — **출처 없는 문장은 화면에 올리지 않는다**(S9 원칙).

    `ref` 는 인용 가드의 대조 대상이라 **실존 ID 여야 한다**. LLM 이 지어낸 ID 는
    `enforce_citation_guard` 가 걷어낸다.
    """

    model_config = ConfigDict(extra="ignore")

    kind: EvidenceKind
    ref: str = Field(..., description="실존 식별자 (CASE-… · MAN-… · SUP-… 등)")
    label: str = Field(..., description="사람이 읽는 한 줄 요약")

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: object) -> object:
        """🔴 **kind 하나 때문에 답 전체가 죽지 않게 한다** (2026-08-11 — 세 번 밟은 자리).

        `kind` 는 화면이 아이콘·색을 고르는 **표시 힌트**다. 그런데 Literal 검증에 걸리면
        재시도 3회가 전부 실패하고 **답이 통째로 폴백**된다 — 아이콘 하나 값으로 문장을
        버리는 셈이라 우선순위가 거꾸로다. 그래서 여기서 흡수한다:
          ① 재료 키 그대로 쓴 것(`chambers`)·복수형을 정식 kind 로 매핑
          ② 그래도 모르는 값이면 fallback (틀린 아이콘 < 답 없음)

        ⚠️ 이 관용은 **kind 에만** 준다. `ref`(인용 ID)는 관용하지 않는다 — 그건 표시가
        아니라 **사실 주장**이라 틀리면 유령 인용이 된다(가드가 따로 걷어낸다).
        """
        if not isinstance(v, str):
            return _KIND_FALLBACK
        k = v.strip().lower()
        k = _KIND_ALIASES.get(k, k)
        return k if k in get_args(EvidenceKind) else _KIND_FALLBACK


class CopilotAnswer(BaseModel):
    """Copilot 응답 1건.

    ⚠️ **액션 필드가 없는 것이 설계다.** S9 는 읽기 전용이고 승인·판정·조치는 정규 UI
    게이트로만 간다(원칙 ①). 조치가 필요한 답이면 `deeplink` 로 화면을 가리키기만 한다.
    """

    model_config = ConfigDict(extra="ignore")

    answer: str = Field(..., description="자연어 답변 — 근거 없는 단정 금지")
    evidence: list[EvidenceRef] = Field(default_factory=list)
    #: "승인하려면 S4로" — 경로만 준다. 여기서 실행되는 것은 없다.
    deeplink: Optional[str] = Field(default=None, description="정규 UI 경로 (예: /incidents/INC-…)")
    #: 어떤 스코프로 좁혀 답했는지 — 컨텍스트 자동 주입을 **사람이 눈으로 확인**하는 자리다.
    scope_note: str = Field(default="", description="적용한 컨텍스트 요약")
    #: 재료가 없어서 답을 못 세운 경우 true. 화면이 이걸 보고 "근거 없음"을 표시한다.
    #  헌법 7장 *"조용한 상태 게이트 무노출 금지"* — 빈손을 빈손으로 보이게 한다.
    unsupported: bool = Field(default=False, description="재료 부족 — 단정하지 않았음")


def make_fallback_answer(reason: str) -> CopilotAnswer:
    """LLM 실패·재료 부재 시 반환할 안전 응답 (헌법 6-3 fallback).

    **빈 문자열을 돌려주지 않는다** — 화면이 "답이 오다 말았다"와 "답할 수 없다"를
    구별하지 못하면 사용자는 새로고침을 반복한다. 이유를 실어 보낸다.
    """
    return CopilotAnswer(
        answer=f"지금은 답을 세울 근거가 없습니다 — {reason}. 화면의 해당 항목을 직접 확인해 주세요.",
        evidence=[],
        deeplink=None,
        scope_note="",
        unsupported=True,
    )
