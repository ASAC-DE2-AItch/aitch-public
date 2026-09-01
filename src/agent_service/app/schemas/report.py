"""리포트 3종 출력 스키마 — recipe/limit/manual option (Pydantic v2).

단일 소스: docs/Agent_프롬프트_라이브러리_v1.md §1~§3 (리포트 3종) · API Contract §5.
- 3조수(Recipe·실력치·정비 tool)가 각자 LLM 호출로 조립해 반환하는 리포트의 형(shape).
- Supervisor(C5)가 이 3종을 매트릭스로 받아 4지선다 판정한다.

C4-1 Day2 단계 정책 (스텁):
- 수치·근거 필드(value_proposed·shadow_eval·shap_basis 등)는 **B 정량 엔진 API·RAG 미연결**이라
  전부 Optional(default None)로 둔다. tool 스텁은 신원 필드(report_id·option_type·confidence·
  rationale·uncertainty)만 채운 mock 리포트를 낸다. C5-1에서 실수치로 채운다.
- extra="forbid": 우리가 발행하는 리포트이므로 오타·계약 밖 필드를 조기 차단한다.
  (수신 계약 AlertModel 이 extra="ignore" 인 것과 방향이 반대 — inbound 관용 / outbound 엄격.)

report_id PREFIX (프롬프트 라이브러리·아키텍처 다이어그램 정본): RCP / LIM / MNT.
  ※ RCP 는 헌법 6-4 PREFIX 고정목록·API_Contract §5 모두 등재 완료(2026-07-11) — 정합.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .alert import AlertModel, Window

# --- report_id 헬퍼 -----------------------------------------------------------
# 포맷: <PREFIX>-<YYYYMMDD>-<CHAMBER>-<SEQ> (헌법 6-4 업무 ID 포맷)
RCP_PREFIX = "RCP"  # recipe_option
LIM_PREFIX = "LIM"  # limit_option
MNT_PREFIX = "MNT"  # manual_option
INC_PREFIX = "INC"  # incident (PM 그루퍼 채번이 정본 — C 는 미주입 시 임시 채번만)
# RLS = RTD 해제 근거 Brief (`release_briefs.report_id` — schemas/release.py · 계약 §8-D-1).
#
# 🔴 **`QUAL` 재사용을 폐기하고 신설한 접두다** (2026-08-09). 초안은 "재인증 축이니 QUAL,
#    `quals.qual_id` 와는 필드명으로 구분(6-4 LIM 선례)"이었는데 **선례가 아니었다**:
#    LIM 은 리포트와 correction 이 **같은 제안 1건**을 가리켜서 성립하는데, 이쪽은
#    Brief 와 Qual 판정이 **완전히 다른 대상**이다(Brief 는 Qual 이 생기기 전에 만들어지고,
#    Qual 로 이어지지 않을 수도 있다). 실제로 S7 승인 화면 한 곳에 `report_id`(Brief)·
#    `qual_id`(Qual 판정)·`release_qual_id`(과거 해제)가 **셋 다 QUAL- 로** 떠서 로그
#    grep 으로도 구분이 안 됐다. 헌법 6-4 에 접두 등재 + 적용 범위 명문화로 정리했다.
RLS_PREFIX = "RLS"


def chamber_token(chamber_id: str) -> str:
    """chamber_id 를 ID 세그먼트로 변환 (SIM_CH_5 → SIMCH5) — 계약 alert_id 표기와 정합."""
    return chamber_id.replace("_", "")


def make_report_id(prefix: str, alert: AlertModel) -> str:
    """리포트 ID 생성 — alert 의 날짜·챔버·SEQ 를 재사용해 결정적(deterministic)으로 부여.

    C4-1 Day2 스텁 정책: 자체 SEQ 카운터 대신 트리거 alert 의 SEQ 를 물려받는다(재현성).
    C5 통합 시 Incident 단위 SEQ 로 승격 검토.
    """
    date = alert.timestamp.strftime("%Y%m%d")
    seq = alert.alert_id.rsplit("-", 1)[-1]
    return f"{prefix}-{date}-{chamber_token(alert.chamber_id)}-{seq}"


# --- Evidence Card (라이브러리 §5 · C4-2) ------------------------------------
# 옵션별 근거 카드. claim 1개 + evidence 2~4개(출처 2종↑) + counter_evidence 필수.
# source_type = §0 수치 출처 태그([SPC]/[MODEL]/[KB]/[CASE])와 정합.
EvidenceSourceType = Literal["spc", "model", "kb", "case"]


class EvidenceItem(BaseModel):
    """근거 카드 안의 개별 근거 한 줄 — 출처+참조+스니펫 (§0 수치 출처 표기 원칙)."""

    model_config = ConfigDict(extra="forbid")

    source_type: EvidenceSourceType = Field(..., description="[SPC]/[MODEL]/[KB]/[CASE] (§0)")
    ref: str = Field(..., description="실존 ID만 — ALERT-…/CASE-…/doc_id 등 (창작 금지 §0)")
    snippet: str = Field(..., description="근거 한 줄 인용 (수치는 입력값만)")


class EvidenceCard(BaseModel):
    """옵션별 근거 카드 (§5). evidence 2~4개·서로 다른 출처 2종↑·counter_evidence 필수."""

    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(..., description="EC-<report_id>-<n>")
    claim: str = Field(..., description="이 카드가 뒷받침하는 한 문장 주장")
    evidence: list[EvidenceItem] = Field(
        ..., min_length=2, max_length=4, description="2~4개 (§5)"
    )
    counter_evidence: str = Field(
        ..., description="필수 — 반증 없으면 '반증 탐색 실패 — 확신 주의' (§5)"
    )
    uncertainty: str = Field(..., description="이 근거의 불확실성/추정 표지 (§0)")
    display_note: Optional[str] = Field(
        default=None, description="분위수 관리선 센서 등 표시 계층 주석 (§5, 6-4)"
    )

    @field_validator("evidence")
    @classmethod
    def _at_least_two_source_types(cls, v: list[EvidenceItem]) -> list[EvidenceItem]:
        """서로 다른 source_type 최소 2종 — 한 계열만으로 결론짓지 않도록 (§5)."""
        if len({item.source_type for item in v}) < 2:
            raise ValueError(
                "evidence는 서로 다른 source_type 2종 이상이어야 한다 (§5 — 한 계열 결론 금지)"
            )
        return v


# --- 공통 베이스 --------------------------------------------------------------
class OptionReportBase(BaseModel):
    """리포트 3종 공통 신원·확신 필드 (모든 option 이 반드시 채우는 것)."""

    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(..., description="<PREFIX>-<YYYYMMDD>-<CHAMBER>-<SEQ> (6-4)")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0~1. 낮으면 낮게 정직히 (라이브러리 §0)")
    rationale: str = Field(..., description="근거 서술 — 수치는 입력만 인용, 창작 금지 (§0)")
    uncertainty: str = Field(..., description="불확실성/추정 표지 (§0 불확실성 원칙)")
    # §5 Evidence Card (C4-2). 스텁/에스컬 단계는 빈 리스트, LLM 연결(C5-1) 시 채워짐.
    evidence_cards: list[EvidenceCard] = Field(default_factory=list)
    # 상한 초과·레짐 신호·가한계 등으로 옵션을 못 낼 때의 사유 (§6). 정상 리포트는 None.
    escalate_reason: Optional[str] = None


# --- ① Recipe Tuning 리포트 (라이브러리 §1) ------------------------------------
RecipeHypothesis = Literal[
    "process_condition_shift", "regime_signal", "insufficient_evidence"
]


class RecipeOption(OptionReportBase):
    """레시피 튜닝안 리포트 — 수치는 B5-4 튜닝 API, 서술·근거는 LLM (라이브러리 §1)."""

    option_type: Literal["recipe_option"] = "recipe_option"

    recipe_id: Optional[str] = None
    step: Optional[int] = None
    parameter_id: Optional[str] = Field(default=None, description="조정 손잡이 C코드 (knob_map 내)")
    parameter_name: Optional[str] = None
    value_current: Optional[float] = Field(
        default=None, description="명목값 — 레시피에 적힌 값(누적 계산의 기준)"
    )
    applied_value: Optional[float] = Field(
        default=None,
        description="지금 장비에 실제로 걸린 값 (B TuningProposal.applied_value 승계). "
                    "첫 스텝·escalate·온도(temp_target)에서는 None — 승인 화면 '현재값'의 정본",
    )
    value_proposed: Optional[float] = None
    delta_pct: Optional[float] = Field(default=None, description="|delta|>3%(D6) 이면 튜닝 금지·escalate")
    shap_basis: list[dict[str, Any]] = Field(default_factory=list)
    expected_effect: Optional[str] = None
    hypothesis: Optional[RecipeHypothesis] = None


# --- ② Limit Correction 리포트 (라이브러리 §2) ---------------------------------
LimitMethod = Literal["sigma", "quantile"]


class LimitOption(OptionReportBase):
    """실력치(기준선) 재설정안 리포트 — 수치는 B4-3 재산정 API (라이브러리 §2)."""

    option_type: Literal["limit_option"] = "limit_option"

    # B 가 채번하는 재설정 건의 신원. db `limit_corrections.correction_id` 가
    # VARCHAR(64) NOT NULL UNIQUE 라 이 값이 없으면 승인·적용 결과를 그 행에 못 붙인다
    # (chamber_id 와 같은 이유 — 2026-07-21 B 요청으로 추가).
    # ⚠️ LLM 이 만드는 값이 아니다 — 코드가 recalc 값을 **그대로** 주입한다(tools/limit.py).
    #    포맷은 LIM-<YYYYMMDD>-<CHAMBER>-<SEQ> (B 확인 2026-07-21)이나 C 가 검증·가공하지 않고
    #    받은 값을 그대로 싣는다(B 요청). B 미연결 구간은 null.
    # 🔄 출처 정정 (2026-07-30): 구 주석은 `RecalcProposal` 이 채번한다고 적었으나 그 dataclass 에는
    #    이 필드가 없다. 실제로는 `recalc_writer` 가 `limit_corrections` INSERT 시 SEQ 를 조회해
    #    `LIM-{date}-{ch}-{seq}` 로 조립한다. 채번 주체가 B 라는 점(헌법 6-4)은 그대로다.
    correction_id: Optional[str] = Field(
        default=None, description="B recalc_writer 가 limit_corrections 기록 시 채번한 값 그대로 (LIM-<YYYYMMDD>-<CHAMBER>-<SEQ> — B 확인 2026-07-21). C 는 가공·검증하지 않는다"
    )
    sensor_id: Optional[str] = Field(default=None, description="C코드 센서 ID (6-4)")
    # 이 제안이 **어느 버전의 관리선을 대체하는가**. 계약 §6 `fdc.correction.limit_version` 과 동명이고
    # db `limit_corrections.limit_version_before` 와 같은 값이다 (after 는 B 채번 — 새 control_limits 행).
    # ⚠️ LLM 이 만드는 값이 아니다 — 코드가 alert `violations[].limit_version` 을 승계한다.
    # 왜 제안 시점에 박아야 하나: 승인을 기다리는 동안 B 가 **정기 리캘리브레이션**(헌법 1-1 예외 —
    # 무승인 자동)으로 관리선을 갱신할 수 있다. 그때 이 값이 없으면 B 는 낡은 제안을 최신 버전 위에
    # 덮어써 버린다. "내가 본 버전"을 함께 보내야 B 가 충돌을 감지한다(낙관적 잠금).
    limit_version: Optional[str] = Field(
        default=None,
        description="재설정 대상 관리선 버전 — alert violations[].limit_version 승계 (계약 §6 동명). C 는 가공하지 않는다",
    )
    sensor_window: Optional[Window] = None
    method: Optional[LimitMethod] = Field(default=None, description="sigma | quantile(비정규 분포)")
    center_before: Optional[float] = None
    center_after: Optional[float] = None
    ucl_after: Optional[float] = None
    lcl_after: Optional[float] = None
    # 이동량은 σ 가 정본이다 (B4-4, 2026-07-11 %→σ 전환 — A2 상한 0.5σ·A3 누적 1.0σ).
    # delta_pct 는 v4.3 기획서(실력치를 %로 관리하던 시절)의 잔재로 계약에 남아 있으나,
    # B 재산정 엔진(RecalcProposal)은 % 를 산출하지 않는다 → 기본 null.
    # ⚠️ % 는 center 를 분모로 쓰므로 center≈0 인 센서에서 왜곡된다
    #    (실측 C32: center 1.5·σ 1.0 → 0.3σ 이동이 20% 로 표시). 삭제는 금지(헌법 2-2)이므로 유지만.
    delta_pct: Optional[float] = Field(
        default=None, description="[비권장] 이동량 %. B 미산출·center≈0 왜곡 — σ 를 볼 것"
    )
    delta_sigma: Optional[float] = Field(
        default=None,
        description="이동량 σ 단위 — 상한 판정 정본(A2 0.5σ). B RecalcProposal.delta_sigma 인용",
    )
    shadow_eval: Optional[dict[str, Any]] = Field(
        default=None, description="{false_alarm_reduction_pct, missed_detection}"
    )
    feasibility: Optional[str] = None


# --- ③ Maintenance 리포트 (라이브러리 §3) --------------------------------------
class ManualOption(OptionReportBase):
    """정비 조치안 리포트 — Error Manual RAG + 사례 (라이브러리 §3)."""

    option_type: Literal["manual_option"] = "manual_option"

    action: Optional[str] = Field(default=None, description="매뉴얼·사례에 있는 조치만 (창작 금지)")
    target_component: Optional[str] = None
    symptom_match: Optional[str] = None
    estimated_downtime_h: Optional[float] = None
    similar_cases: list[dict[str, Any]] = Field(default_factory=list)
    disposition_note: Optional[str] = Field(
        default=None, description="wafer 처분 의견 — SCRAP 단정 금지(예측+anomaly 이중확인)"
    )
    feasibility: Optional[str] = None


# 3조수 리포트의 유니온 — Supervisor 입력·팬아웃 결과 타입.
OptionReport = RecipeOption | LimitOption | ManualOption


# =============================================================================
# ④ Supervisor Brief (C5-3) — 라이브러리 §4 / 계약 §6 `fdc.agent`
# =============================================================================
# 헌법 1-4: 리포트 3종은 **반드시 Supervisor 를 통해 통합**된다. 최종 추천은 Supervisor 가
# 생성하며, 판단 프레임은 **4지선다**(v4.5). 승인 게이트(interrupt)는 이 뒤 한 곳뿐이다.
# C 의 산출물은 여기까지 — 승인 상태 전이는 PM 의 LangGraph 소관.

SUP_PREFIX = "SUP"  # supervisor brief (헌법 6-4 PREFIX 고정목록)

#: 4지선다 판정 (라이브러리 §4 `verdict` enum)
Verdict = Literal["equipment_fault", "process_shift", "baseline_aging", "escalate"]

#: 옵션 종류 — parallel_options 의 키 (실행안 3종)
OptionType = Literal["recipe_option", "limit_option", "manual_option"]

#: `selected` 전용 sentinel — "옵션들을 검토했고 **고를 것이 없다**".
#  parallel_options 에 대응 블록이 없으므로 OptionType 에는 넣지 않는다(키가 아니다).
NONE_EXECUTABLE = "none_executable"

#: `selected` 값 범위 = 실행안 3종 + sentinel.
#  **왜 null 을 쓰지 않고 sentinel 을 두는가** (2026-07-22):
#    null 은 "의도적으로 고를 게 없음"과 "LLM 이 칸을 안 채움"을 구분하지 못한다. 소비자는
#    둘 다 빈칸으로 보고, 전자를 위반으로 처리하면 정상 Brief 의 37%(라운드7c 실측)에
#    거짓 경보가 붙고, 후자를 통과시키면 진짜 누락을 놓친다.
#    → **정상 Brief 는 selected 가 절대 null 이 아니다.** 남은 null 은 LLM 누락 신호다.
#    §0 불확실성 원칙("0건이면 '없음'이라고 명시한다")과 같은 계열 — 침묵 대신 명시.
SelectedOption = Literal["recipe_option", "limit_option", "manual_option", "none_executable"]

#: verdict → 그 판정이 선택해야 하는 옵션 (정합 가드의 정답지, 라이브러리 §4 판정 프레임)
#: escalate 는 "모두 아님"이므로 선택 옵션이 없다 → None.
VERDICT_TO_OPTION: dict[str, Optional[str]] = {
    "equipment_fault": "manual_option",   # ① 진짜 장비 이상 → 정비 + wafer 처분
    "process_shift": "recipe_option",     # ② 공정 조건 이탈 → Recipe R2R 튜닝안
    "baseline_aging": "limit_option",     # ③ 기준선 노후 → 실력치 재설정
    "escalate": None,                     # ④ 모두 아님 → 공정 검토 에스컬레이션
}


class OptionSummary(BaseModel):
    """parallel_options 의 옵션 1건 — 리포트 전문이 아니라 Brief 용 요약.

    엔지니어가 30초 안에 읽는 화면(S4)이므로 원본 리포트를 그대로 싣지 않고 한 줄로 줄인다.
    """

    model_config = ConfigDict(extra="ignore")

    report_id: str = Field(..., description="원본 리포트 ID (RCP/LIM/MNT-...)")
    action: Optional[str] = Field(default=None, description="한 줄 조치 요약")
    feasibility: Optional[str] = Field(default=None, description="실행 난이도 (예: 'HIGH — 다운타임 없음')")
    confidence: float = Field(..., ge=0.0, le=1.0, description="원본 리포트의 confidence")
    rejected_because: Optional[str] = Field(
        default=None,
        description="선택하지 않은 이유 — selected 가 아닌 옵션엔 필수(§4). 왜 아닌지가 신뢰를 만든다",
    )


class SupervisorRecommendation(BaseModel):
    """4지선다 판정 + 근거 — Brief 의 핵심."""

    model_config = ConfigDict(extra="ignore")

    decision_frame: Literal["4지선다"] = "4지선다"
    # Optional 로 두는 이유는 "정상값"이 아니라 **누락 신호**를 표현하기 위해서다 —
    # 정상 Brief 에서는 코드가 항상 3종 중 하나 또는 NONE_EXECUTABLE 로 채운다(supervisor.run).
    # null 이 남아 있으면 LLM 이 칸을 건너뛴 것이고, 그건 소비자가 알아야 할 이상 신호다.
    selected: Optional[SelectedOption] = Field(
        default=None,
        description="실행할 옵션. 고를 것이 없으면 'none_executable'(verdict=escalate·가드 강등·fallback). null 은 LLM 누락 신호",
    )
    verdict: Verdict = Field(..., description="4지선다 판정 — 부록 치트시트 기준")

    @field_validator("selected", mode="before")
    @classmethod
    def _coerce_empty_selected(cls, v: Any) -> Any:
        """빈 문자열('')·공백을 null(누락 신호)로 강등한다.

        LLM 이 "고를 것 없음"을 sentinel('none_executable') 대신 ''(빈 문자열)로 뱉는 실측이
        있다(2026-07-23 fewshot1: Literal 위반으로 Brief 전체가 §6 fallback → verdict 유실,
        R8 대비 fallback 1→5 리그레션). '' 는 유효 enum 도 null 도 아니라 검증이 hard-fail 하는데,
        의미상 이는 '누락 신호'(=null)다. null 로 강등하면 파싱이 통과하고 supervisor.run 의
        기존 정규화가 이어받는다(verdict=escalate 면 none_executable 로 채움, 아니면 null 유지+로그).
        fallback(verdict 유실)보다 항상 낫다. 정상 sentinel·enum 값은 그대로 통과.
        """
        if isinstance(v, str) and v.strip() == "":
            return None
        return v
    reason: str = Field(..., description="판정 사유 한 문단 (엔지니어가 30초에 읽는다)")
    # 계약 v4.6 원문: Brief(§6)에 `evidence[]`(**정확히 3**)·`counter_evidence`(정확히 1).
    # Brief 는 S4 화면에 3줄로 렌더되므로(30초 화면) 개수가 고정이어야 레이아웃이 안 흔들린다.
    # ⚠️ §5 EvidenceCard 의 2~4개(범위)와 **다른 정책이다** — 카드는 "충분한가"(범위+출처 2종↑),
    #    Brief 는 "읽히는가"(고정 3). 한쪽을 바꿀 때 다른 쪽을 따라 바꾸지 말 것.
    # ⚠️ 강제의 대가: LLM 이 개수를 어기면 Brief 전체가 §6 fallback 으로 죽는다.
    #    ADR-31 방향("수정 없이 진행, 드랍률 >10% 면 조정")대로 **먼저 강제하고 실측**한다.
    #    완화 순서(합의): 재시도 → 정규화 레이어 → 필수/선택 분리.
    evidence: list[str] = Field(
        # 3 = 계약 v4.6 이 "근거 3층"으로 고정한 **스키마 계약값**이라 params 대상이 아니다
        # (튜닝하면 계약 위반). 검색 건수 top_k 와는 다른 축 — 그쪽은 조절 손잡이다.
        ...,
        min_length=3,
        max_length=3,
        description="근거 3층 (계약 v4.6 evidence[3]) — 출처 태그([SPC]/[MODEL]/[KB]/[CASE]) 병기",
    )
    counter_evidence: str = Field(
        ..., min_length=1, description="반증 1건 — 계약 v4.6 counter_evidence[1]. 한계를 숨기지 않는다(§0)"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)


# 처분 3종. HOLD = 원위치(인터락 기본값 — db wafer_dispositions.status DEFAULT 'HOLD'),
# RELEASE = 다음 공정으로 흘림, SCRAP = 폐기(Etch 는 rework 불가 — 되돌릴 수 없음).
DispositionVerdict = Literal["SCRAP", "RELEASE", "HOLD"]


class WaferVerdict(BaseModel):
    """wafer 1장의 처분 판정 — **판정은 wafer별 개별**(기획서 v4.5 §4-3).

    HOLD 는 의심 윈도우 단위로 잡되 SCRAP/RELEASE 는 장마다 다르다. §4-3 이 그 정밀도를
    이 기능의 존재 이유로 든다: *"wafer당 $15k~22k — 25장 중 3장만 버릴지 결정하는 정밀도가
    곧 비용 절감"*. 윈도우 전체에 판정 하나를 주면 22장을 함께 버리는 설계가 된다.

    **코드가 채운다 — LLM 아님.** 판정이 수치 비교(predicted_c65 vs B9)뿐이라 LLM 이 나을
    게 없고, 되돌릴 수 없는 결정이라 창작 여지를 주면 안 된다. 실측 근거 2건:
      · LLM 이 계약에 없는 `anomaly_score 3.6` 을 지어내 SCRAP 을 정당화 (가드⑤ 신설 사유)
      · LLM 이 `tttm.score 2.3` 을 anomaly_score 로 오인 인용 (fewshot3, 2026-07-24)
    """

    model_config = ConfigDict(extra="ignore")

    wafer_id: str
    recommendation: DispositionVerdict
    reason: str = Field(..., description="수치 인용 정형 문구 — 코드가 조립(감사 추적·재현성)")
    # 조회 실패 시 None. 값이 없으면 판정하지 말고 HOLD(원위치)로 두는 것이 규칙 —
    # limit 규칙6("recalc null 이면 창작 금지")과 같은 취지.
    predicted_c65: Optional[float] = Field(default=None, description="판정 근거 — A 예측 API 인용")
    anomaly_score: Optional[float] = Field(default=None, description="이중 확인 축 — 미제공 시 None")


class WaferDisposition(BaseModel):
    """wafer 처분 권고 — SCRAP 은 rework 불가라 단정 금지(§3 규칙3·헌법 1-1).

    2026-07-24: `per_wafer` 신설. 기존 3필드는 **이름·타입 불변 + 추가만**이라 헌법 2-2
    정합이며, 기존 소비자(PM 승인 게이트·S6 화면·계약 §6)가 깨지지 않는다.
      · held_wafers    = 의심 윈도우 전개 — HOLD 대상 전체 (윈도우 단위)
      · recommendation = 요약. per_wafer 중 **가장 무거운 판정**을 코드가 파생 (화면 한 줄용)
      · reason         = LLM 이 쓰는 한 줄 요약 (사람이 읽는 것)
      · per_wafer      = 장별 판정 + 수치 근거 (감사 추적)
    """

    model_config = ConfigDict(extra="ignore")

    held_wafers: list[str] = Field(default_factory=list, description="HOLD 중인 wafer_id")
    recommendation: Optional[DispositionVerdict] = None
    reason: Optional[str] = None
    per_wafer: list[WaferVerdict] = Field(
        default_factory=list, description="wafer별 개별 판정 (§4-3) — 코드가 채움"
    )


class SupervisorBrief(BaseModel):
    """Supervisor 통합 Brief — 승인 게이트로 넘어가는 최종 산출물 (라이브러리 §4·계약 §6).

    ⚠️ `approval_status` 는 `Literal["PENDING"]` 으로 **타입에 못 박는다**. C 가 APPROVED 를
    쓸 수 있으면 무승인 통과 경로가 열린다(헌법 1-1·1-4) — 승인 전이는 PM 게이트 소관.
    """

    model_config = ConfigDict(extra="ignore")

    report_id: str = Field(..., description="SUP-<YYYYMMDD>-<CHAMBER>-<SEQ> (6-4)")
    incident_id: str = Field(
        ...,
        description="PM 그루퍼가 채번(계약 §4). alert 는 자기 incident_id 를 모르므로 주입받는다",
    )
    # chamber_id 는 ID 문자열(SUP-…-SIMCH5-…)에도 들어 있지만 **독립 필드로 둔다**:
    #   · db `agent_reports.chamber_id` 가 NOT NULL — 없으면 적재 자체가 불가
    #   · 프론트 S3(결정 대기 큐)가 챔버를 컬럼으로 렌더
    #   · ID 파싱(SIMCH5 → SIM_CH_5)은 표기 규칙 의존이라 취약 — DB 에 컬럼이 따로 있는 이유가 그것
    # Incident 는 정의상 챔버 1개다(헌법 1-4: 같은 chamber_id + 겹치는 시간창).
    chamber_id: str = Field(..., description="SIM_CH_1~4 — alert 에서 승계(6-4 C코드 표기 그대로)")
    context_score: int = Field(..., ge=0, le=100)
    suspected_root_causes: list[str] = Field(
        default_factory=list, description="원인 후보 서술 — 출처 태그 병기"
    )
    parallel_options: dict[OptionType, OptionSummary] = Field(
        default_factory=dict, description="옵션 3종 요약 (S4 화면이 순위·비교로 렌더)"
    )
    supervisor_recommendation: SupervisorRecommendation
    wafer_disposition: WaferDisposition = Field(default_factory=WaferDisposition)
    approval_status: Literal["PENDING"] = "PENDING"


# --- §6 fallback 리포트 -------------------------------------------------------
# 리포트 클래스 → report_id PREFIX 매핑 (make_fallback_report 에서 사용).
_PREFIX_BY_CLASS: dict[type, str] = {
    RecipeOption: RCP_PREFIX,
    LimitOption: LIM_PREFIX,
    ManualOption: MNT_PREFIX,
}

_FALLBACK_MSG = "LLM 분석 실패 — 원문 alert 첨부, 수동 검토 요청"


def make_fallback_report(model_cls: type[OptionReport], alert: AlertModel) -> OptionReport:
    """LLM 응답 2회 파싱 실패 시 반환하는 고정 fallback 리포트 (라이브러리 §6·헌법 6-3).

    action 등 분석 필드는 전부 None(창작 금지), confidence=0.0, escalate_reason 에 사유.
    파이프라인 무중단을 위해 어떤 상황에서도 유효한 리포트를 낸다.
    """
    prefix = _PREFIX_BY_CLASS[model_cls]
    return model_cls(
        report_id=make_report_id(prefix, alert),
        confidence=0.0,
        rationale=f"{_FALLBACK_MSG} (§6 fallback).",
        uncertainty="LLM 응답 2회 파싱 실패로 fallback 리포트 반환 (헌법 6-3 무중단).",
        escalate_reason=_FALLBACK_MSG,
    )


def make_fallback_brief(alert: AlertModel, incident_id: str) -> "SupervisorBrief":
    """Supervisor 응답 파싱 실패 시 반환하는 고정 Brief (라이브러리 §6·헌법 6-3).

    **판정을 창작하지 않는다** — verdict=escalate 로 사람에게 넘긴다. 4지선다 중 하나를
    억지로 고르면 근거 없는 판정이 승인 큐에 뜨고, 엔지니어는 그게 실패 산출물인지 모른다.
    escalate 는 "모두 아님 → 근거 패키지와 함께 공정 검토"라 실패 경로와 의미가 맞는다.

    parallel_options 는 비운다(요약 실패). 원본 리포트 3종은 별도로 보존·전달된다.
    """
    return SupervisorBrief(
        report_id=make_report_id(SUP_PREFIX, alert),
        incident_id=incident_id,
        chamber_id=alert.chamber_id,
        context_score=alert.context_score,
        suspected_root_causes=[],
        parallel_options={},
        supervisor_recommendation=SupervisorRecommendation(
            selected=NONE_EXECUTABLE,   # 고를 것이 없음을 **명시** — null(누락)과 구분된다
            verdict="escalate",
            reason=f"{_FALLBACK_MSG} (§6 fallback) — 판정 창작 없이 에스컬레이션.",
            evidence=[
                "Supervisor 응답 파싱 2회 실패 [SPC]",
                "옵션 리포트 3종은 원본 그대로 보존됨 [SPC]",
                "자동 판정 근거 없음 — 사람 검토 필요 [SPC]",
            ],
            counter_evidence="리포트 3종 자체는 유효할 수 있음 — 통합 단계만 실패했다.",
            confidence=0.0,
        ),
        wafer_disposition=WaferDisposition(recommendation="HOLD", reason=_FALLBACK_MSG),
    )
