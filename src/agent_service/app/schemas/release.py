"""RTD 해제 근거 Brief 스키마 (인프라 스텝 3 · #140 — Pydantic v2).

단일 소스: docs/Agent_프롬프트_라이브러리_v1.md §7 · API Contract §9 `/chambers/status`.

**무엇인가.** 챔버가 RTD 로 자동 정지되면(헌법 1-1 예외 4) 엔지니어는 *"다시 돌려도 되나"* 를
판단해야 한다. 이 Brief 는 그 판단의 **근거를 모아 놓은 것**이고, 회고 3층으로 구성된다:

    ⓐ 왜 섰나        — 발동 알람의 위반 센서·룰·심각도            (spc_violations)
    ⓑ 얼마나 심했나   — 정지 직전 창의 센서별 지속 이탈률           (spc_violations)
    ⓒ 과거엔 뭘로 풀렸나 — 같은 챔버의 지난 정지가 어떤 qual 로 닫혔나 (chamber_inhibits)

**「정지 이후 좋아졌나」는 담지 않는다** — 정지 중에는 그 챔버 wafer 가 0장이다
(`src/simulator/kafka_producer.py` 의 inhibit 스킵). 잴 재료 자체가 없으므로 회고 + 사례로 간다.

🔴 **이 Brief 는 해제를 실행하지 않는다** (헌법 1-1 예외 4 ⓒ — 해제 경로는 requal 승인 하류
하나뿐). `readiness` 는 **권고 값**이며, 그 사실을 타입에도 못 박는다(`release_authority`).
Supervisor Brief 의 `approval_status: Literal["PENDING"]` 과 같은 취지다 — C 가 승인·해제
상태를 만들 수 있으면 무승인 통과 경로가 열린다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .report import RLS_PREFIX, chamber_token

#: 재가동 준비도 — **권고**다. 해제는 R9 승인이 한다.
#:   ready       : 회고·사례 근거가 서고 남은 확인 항목이 없다 → requal 진행 권고
#:   conditional : 진행해도 되나 **선행 확인 항목**이 있다 (precheck_items 필수)
#:   not_ready   : 근거가 부족하거나 이상 신호가 남아 있다 → 정지 유지 권고
Readiness = Literal["ready", "conditional", "not_ready"]

#: 정지 스코프 — `chamber_inhibits.scope` 승계 (LLM 이 정하는 값이 아니다).
InhibitScope = Literal["chamber", "equipment"]


class TriggerViolation(BaseModel):
    """ⓐ 발동 알람의 위반 1건 — `spc_violations` 행 그대로(가공 없음)."""

    model_config = ConfigDict(extra="ignore")

    sensor_id: str
    rule_id: str
    severity: Optional[str] = None
    current_value: Optional[float] = None
    control_limit_upper: Optional[float] = None
    control_limit_lower: Optional[float] = None
    description: Optional[str] = None


class SensorBreach(BaseModel):
    """ⓑ 정지 직전 창의 센서 1개 이탈 추이 — RTD 본 판정과 **같은 정의**로 센다.

    `chamber_inhibit.persistent_breach` 가 쓰는 식(분자 = 그 센서가 N1 로 걸린 distinct 알람,
    분모 = 창 내 그 챔버 총 distinct 알람)을 그대로 재현한다. 정지시킨 근거를 그 근거의
    언어로 되돌려 보여주는 것이 이 층의 목적이라, 다른 식으로 다시 세면 의미가 어긋난다.
    """

    model_config = ConfigDict(extra="ignore")

    sensor_id: str
    n1_alerts: int = Field(..., ge=0, description="그 센서가 N1(한계 이탈)로 걸린 distinct 알람 수")
    breach_ratio: float = Field(..., ge=0.0, description="n1_alerts / total_alerts")


class PastRelease(BaseModel):
    """ⓒ 과거 해제 이력 1건 — `chamber_inhibits` 의 닫힌 행.

    **이 층이 핵심이다.** `release_qual_id`·`released_by` 가 남아 있어 *"이번에도 그렇게 하면
    된다"* 의 근거가 된다 (#118 리뷰가 "재료 충족"이라고 본 것이 이것).
    """

    model_config = ConfigDict(extra="ignore")

    chamber_id: str
    incident_id: str
    inhibited_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    released_by: Optional[str] = None
    release_qual_id: Optional[str] = None
    scope: Optional[str] = None
    downtime_h: Optional[float] = Field(default=None, description="released_at − inhibited_at (시간)")


class ReleaseRetrospect(BaseModel):
    """회고 3층 정량 — **코드가 채운다. LLM 은 이 값을 만들지 않는다.**

    `WaferVerdict` 가 코드 판정인 것과 같은 이유다: 수치는 DB 조회 결과이고, 되돌리기 어려운
    판단(생산 재개)의 근거라 창작 여지를 주면 안 된다. LLM 은 이 표를 **읽고 서술**할 뿐이다.
    """

    model_config = ConfigDict(extra="ignore")

    trigger_violations: list[TriggerViolation] = Field(default_factory=list, description="ⓐ")
    trigger_violations_total: int = Field(
        default=0,
        ge=0,
        description="ⓐ 의 **자르기 전** 총 건수. 위 배열보다 크면 상한에 잘렸다는 뜻",
    )
    persistent_sensors: list[SensorBreach] = Field(default_factory=list, description="ⓑ")
    window_minutes: int = Field(default=0, ge=0, description="ⓑ 를 센 창 길이(분)")
    window_total_alerts: int = Field(default=0, ge=0, description="ⓑ 분모 — 창 내 총 distinct 알람")
    past_releases: list[PastRelease] = Field(default_factory=list, description="ⓒ")


class ReleaseJudgement(BaseModel):
    """**LLM 이 채우는 부분만** 담은 응답 모델 — 이것이 `guided_json` 스키마가 된다.

    Brief 전체를 응답형으로 쓰지 않는 이유: 신원(report_id·incident_id…)과 회고 수치는
    코드가 정본인데, 응답 스키마에 그 칸이 있으면 **LLM 이 채울 수 있는 자리로 보인다**.
    실제로 supervisor 에서는 예시를 그대로 베끼는 일이 실측됐고(§4 주석), 매번 코드가
    덮어쓰는 방어를 걸어 왔다. 아예 칸을 없애면 그 방어가 구조가 된다 —
    "덮어쓴다"보다 "쓸 수 없다"가 낫다.
    """

    model_config = ConfigDict(extra="ignore")

    readiness: Readiness = Field(..., description="재가동 준비도 — 권고. 해제 실행 아님")
    summary: str = Field(..., description="첫 문장 = 결론. 엔지니어가 30초에 읽는다")
    evidence: list[str] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="근거 3층 — 출처 태그([SPC]/[CASE]/[KB]) 병기 (계약 v4.6 evidence[3] 정합)",
    )
    counter_evidence: str = Field(
        ..., min_length=1, description="반증 1건 — '지금 재가동하면 안 되는 이유'를 쓴다"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    precheck_items: list[str] = Field(
        default_factory=list,
        description="requal 전 확인 항목 — 매뉴얼·과거 사례에 있는 것만 (창작 금지 §0)",
    )


class ReleaseBrief(ReleaseJudgement):
    """RTD 해제 근거 Brief — S7(R9 승인) 화면이 읽는 근거 패키지.

    판단 6필드(`ReleaseJudgement`)에 **코드가 채우는** 신원·회고를 얹은 형이다.

    ⚠️ `release_authority` 는 `Literal["requal_approval_only"]` 로 **타입에 못 박는다**.
    이 Brief 가 어떤 값을 갖든 해제는 requal 승인 하류에서만 일어난다(헌법 1-1 예외 4 ⓒ).
    """

    # --- 신원 (코드가 채운다 — LLM 응답형에는 이 칸이 없다) -----------------------
    report_id: str = Field(..., description="RLS-<YYYYMMDD>-<CHAMBER>-<SEQ> (6-4)")
    incident_id: str = Field(..., description="발동 Incident — chamber_inhibits.incident_id 승계")
    chamber_id: str = Field(..., description="대표 챔버 (Incident 는 정의상 챔버 1개 — 1-4)")
    scope: InhibitScope = "chamber"
    trigger_alert_id: Optional[str] = None
    inhibited_at: Optional[datetime] = None

    # --- 근거 재료 (코드가 채운다) ------------------------------------------------
    retrospect: ReleaseRetrospect = Field(default_factory=ReleaseRetrospect)

    # --- 권한 (불변) ------------------------------------------------------------
    release_authority: Literal["requal_approval_only"] = "requal_approval_only"

    @classmethod
    def compose(
        cls, judgement: ReleaseJudgement, *, retrospect: ReleaseRetrospect, **identity: Any
    ) -> "ReleaseBrief":
        """판단 + 신원 + 회고 → Brief. 신원·회고는 **인자만** 반영된다(응답값 무시)."""
        return cls(
            **judgement.model_dump(),
            **identity,
            retrospect=retrospect,
        )


# --- report_id ----------------------------------------------------------------
def make_release_report_id(
    chamber_id: str, inhibited_at: Optional[datetime], trigger_alert_id: Optional[str],
    incident_id: str,
) -> str:
    """해제 Brief 의 report_id — **결정적**(deterministic)으로 만든다.

    같은 정지에는 항상 같은 ID 가 나와야 `report_id UNIQUE` + `ON CONFLICT DO NOTHING` 이
    중복 적재의 최종 방어선이 된다 (앞단 존재 확인은 LLM 호출을 아끼는 용도일 뿐 — 폴러가
    여러 개 뜨거나 재시작이 겹치면 그 확인은 경합에 진다).

    접두는 **RLS**(해제 축) — 헌법 6-4 등재분(2026-08-09 신설). `QUAL` 재사용은 폐기했다:
    Brief 와 `quals.qual_id` 는 **다른 대상**이라 6-4 의 접두 공유 선례(`LIM` — 같은 제안을
    두 각도에서)가 성립하지 않고, 실제로 S7 한 화면에 QUAL- 이 세 개·두 뜻으로 떴다.
    사유 전문은 `report.py` `RLS_PREFIX` 주석.
    """
    date = (inhibited_at.strftime("%Y%m%d") if inhibited_at else "00000000")
    seq = (trigger_alert_id or incident_id).rsplit("-", 1)[-1] or "0000"
    return f"{RLS_PREFIX}-{date}-{chamber_token(chamber_id)}-{seq}"


# --- §6 fallback --------------------------------------------------------------
_FALLBACK_MSG = "LLM 분석 실패 — 회고 재료 첨부, 수동 검토 요청"


def make_fallback_judgement() -> ReleaseJudgement:
    """LLM 응답 파싱 실패 시의 고정 판단 (라이브러리 §6 · 헌법 6-3 무중단).

    **`not_ready` 로 내려간다.** 실패 산출물이 "재가동해도 된다"로 보이면 안 되기 때문 —
    `make_fallback_brief` 가 verdict=escalate 로 물러서는 것, 처분 폴백이 HOLD(원위치)인 것과
    같은 방향이다. 회고 재료는 코드가 이미 모았으므로 **그대로 실려 나간다**(compose) —
    LLM 서술이 없어도 엔지니어는 표를 보고 판단할 수 있다.
    """
    return ReleaseJudgement(
        readiness="not_ready",
        summary=f"{_FALLBACK_MSG} (§6 fallback) — 자동 판단 없이 회고 재료만 전달한다.",
        evidence=[
            "해제 근거 Brief 생성 실패 — LLM 응답 파싱 2회 실패 [SPC]",
            "회고 재료(발동 알람·이탈 추이·과거 해제 이력)는 아래 표에 그대로 있다 [SPC]",
            "자동 판정 근거 없음 — 사람 검토 필요 [SPC]",
        ],
        counter_evidence="재료 자체는 유효할 수 있다 — 서술 단계만 실패했다.",
        confidence=0.0,
        precheck_items=[],
    )


def make_fallback_release_brief(
    *, report_id: str, incident_id: str, chamber_id: str,
    scope: str = "chamber", trigger_alert_id: Optional[str] = None,
    inhibited_at: Optional[datetime] = None,
    retrospect: Optional[ReleaseRetrospect] = None,
) -> ReleaseBrief:
    """fallback 판단 + 신원 + 회고 → Brief (호출측 편의 래퍼)."""
    return ReleaseBrief.compose(
        make_fallback_judgement(),
        retrospect=retrospect or ReleaseRetrospect(),
        report_id=report_id,
        incident_id=incident_id,
        chamber_id=chamber_id,
        scope=scope if scope in ("chamber", "equipment") else "chamber",
        trigger_alert_id=trigger_alert_id,
        inhibited_at=inhibited_at,
    )


def retrospect_from_materials(materials: dict[str, Any]) -> ReleaseRetrospect:
    """DB 조회 결과 dict → ReleaseRetrospect (형 검증은 Pydantic 이 한다)."""
    return ReleaseRetrospect.model_validate(
        {
            "trigger_violations": materials.get("trigger_violations") or [],
            # 자르기 전 총량 — 미주입이면 배열 길이로 폴백(= 안 잘림). 상한은 collect_materials.
            "trigger_violations_total": materials.get(
                "trigger_violations_total",
                len(materials.get("trigger_violations") or []),
            ),
            "persistent_sensors": materials.get("persistent_sensors") or [],
            "window_minutes": materials.get("window_minutes") or 0,
            "window_total_alerts": materials.get("window_total_alerts") or 0,
            "past_releases": materials.get("past_releases") or [],
        }
    )
