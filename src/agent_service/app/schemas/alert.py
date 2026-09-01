"""fdc.alert 수신 계약 — AlertModel (Pydantic v2).

단일 소스: docs/API_Contract_명세서.md §3 (B → C, PM · 단일 경보 채널).
- 팀원 B 가 발행하는 fdc.alert 를 통합 Agent Service 가 수신할 때 이 모델로 파싱한다.
- fdc.alert 는 유일한 이상 경보 채널이므로(헌법 1-2), 이 모델의 필드 계약은
  Contract §3 과 100% 정합해야 한다. 필드 이름 변경·삭제 금지(헌법 2-2).

파싱 정책 (헌법 6-2 정합):
- `extra="ignore"` — 계약은 "새 필드 추가 가능"(2-2)이므로, 소비자(C)는 미래에 추가될
  필드에 대해 관대해야 한다(전방 호환). 알 수 없는 필드로 파이프라인이 죽지 않는다.
- 단, **정의된 필드의 타입·범위·enum 은 엄격히 검증**한다 — 손상 메시지는 스킵/로그(6-2)로
  걸러낼 수 있어야 하기 때문. (역직렬화 실패 = 스킵+로그는 Consumer 계층에서 처리)

함정 대응:
- `chamber_id` 는 SIM_CH_1~4 만 허용 (Contract §3·db/init.sql·데이터 전부 SIM_CH).
  옛 E14 표기는 deprecated — 패턴으로 원천 차단.
- `trigger_signal` 은 Contract 본문에 없는 필드 → B 규격확인 전까지 **optional 파싱**
  (있으면 보존, 없으면 None). extra="ignore" 로 삼켜지지 않도록 명시 선언한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --- 공통 enum/리터럴 (계약 문구 기준) ------------------------------------------
Severity = Literal["INFO", "WARNING", "CRITICAL"]

# 계약 §3 명문값 — Context Score 게이트 하한(B7). params `spc.context_score_agent_min` 과
# **같은 값이어야 한다**. 여기 상수로 둔 이유는 `AlertModel.agent_gate_open` docstring 참조
# (스키마 계층이 config 를 임포트하지 않기 위함 — 계약은 런타임 설정과 무관해야 한다).
CONTRACT_AGENT_GATE_MIN = 31
Window = Literal["transient", "settled"]  # C42=1(과도) / 정착 — Window 축 (v4.4)

CHAMBER_ID_PATTERN = r"^SIM_CH_[1-4]$"  # 가상 챔버 4개 (E1 — 2026-07-20 6→4, 장비 1대=4PM 멘토 확정). E14 = deprecated


class Violation(BaseModel):
    """violations[] 원소 — 개별 Nelson/관리한계 위반 (Contract §3)."""

    model_config = ConfigDict(extra="ignore")

    rule_id: str = Field(..., description="Nelson Rule 식별자 (예: N1, N3, N5)")
    sensor: str = Field(..., description="C코드 센서 ID (예: C11) — 판정은 C코드로(6-4)")
    # sensor_name: 정본(Contract §3, 최신)에는 **포함** 필드다 → fixtures 는 계약을 따라 넣는다.
    # 다만 파서는 optional 로 관용한다: 현행 발행기(P4-4 mock_alert_publisher, 7/10)가 아직
    # 이 필드를 안 넣기 때문. required 로 막으면 그 실입력에서 C 가 죽어 헌법 6-2("역직렬화
    # 실패해도 파이프라인 무중단")를 위반한다. 발행기가 계약 정합(sensor_name 추가)되면 required
    # 승격 검토. 표시명 단일 소스는 config/sensor_map.yaml(6-4)이라 없으면 그쪽에서 조회.
    sensor_name: Optional[str] = Field(default=None, description="표시용 이름(정본 有) — 미도착 시 sensor_map 조회(6-4)")
    window: Window = Field(..., description="transient(C42=1) / settled")
    severity: Severity = Field(..., description="변경 금지 필드 (헌법 2-1)")
    description: str = Field(..., description="위반 패턴 서술 (예: 6 points continuously increasing)")
    current_value: float = Field(..., description="위반 시점 센서 실측값")
    limit_version: str = Field(..., description="관리한계 버전 (예: v1)")
    control_limit_upper: float
    control_limit_lower: float

    @field_validator("sensor")
    @classmethod
    def _sensor_is_c_code(cls, v: str) -> str:
        """센서는 반드시 C코드(C+숫자)여야 한다 — 발표용 이름 하드코딩 차단 (6-4)."""
        if not (v.startswith("C") and v[1:].isdigit()):
            raise ValueError(f"sensor 는 C코드여야 함 (받음: {v!r})")
        return v


class Tttm(BaseModel):
    """tttm — fleet median 대비 이탈 정량 (Contract §3)."""

    model_config = ConfigDict(extra="ignore")

    reference: str = Field(..., description="비교 기준 (예: fleet_median) — B2")
    score: float
    top_gap_sensor: str
    gap_pct: float
    reference_suspect: bool = Field(
        ..., description="true = 다수 챔버 동시 이탈 → 참조/공통 원인 의심 (역방향 룰)"
    )
    # [B #113 · 2026-08-06 접수] 역방향 룰이 실제로 발동한 공통이동 센서 C코드 목록.
    #   ⚠️ **required 로 올리지 말 것** — 이 모델은 나머지 5필드가 전부 required 라
    #   여기에 required 를 하나 더 얹으면 tttm 을 5필드로 만드는 `generate_fixtures.py` 가
    #   먼저 깨지고, 그러면 평가 라운드가 통째로 멈춘다. 선례 = `sensor_name`
    #   (계약엔 有·publisher 발행엔 無 → optional 관용 처리).
    #   `top_gap_sensor`(worst·경로② 진단용)와는 **별개 채널**이다 — B 가 decouple 하면서
    #   worst=C62 인데 공통이동은 C17 인 masking 을 풀었다(계약 §3).
    suspect_sensors: list[str] = Field(
        default_factory=list,
        description="역방향 룰 발동 센서 C코드 목록 (공통이동 없으면 []). B가 dedup·정렬해 보낸다",
    )


class SuspectWindow(BaseModel):
    """suspect_window — 위반 추세 소급 구간 (Contract §3, optional)."""

    model_config = ConfigDict(extra="ignore")

    start_wafer: str
    end_wafer: str
    basis: str
    # [B 확정 2026-07-28 — `B4-1_계약필드_반영요청_C_v1`] B4-1 Step5 publisher 발행 확정.
    #   끝점만으로는 구간 내부를 채울 수 없다: wafer 번호 결손률 62.4%(실측 — `disposition.endpoints_only`)
    #   라 연번 전개하면 없는 wafer 가 목록의 다수가 된다. 이 필드는 연번을 채운 값이 아니라
    #   **Nelson 엔진이 위반 판정에 실제로 쓴 룰 창의 wafer 목록**이므로(`_make_violation` 의
    #   `[bp.wafer_id for bp in win]`) 결손률과 무관하다. 이름은 `qual_snapshots.wafer_ids`(QUAL 5장)·
    #   `tttm_window_wafers`(개수)와 혼동을 피해 `member_wafers` 로 정했다.
    #   정렬 규약: B 가 여러 위반 창의 **합집합 · dedup · numeric 정렬**까지 마쳐 보낸다
    #   (C `_by_number` 와 동일 — 문자열 정렬 시 `C64_1001 < C64_995` 로 시각이 역전되는 것을 양측 방어).
    #   미도착 시 `endpoints_only` 폴백이 그대로 동작한다.
    member_wafers: Optional[list[str]] = Field(
        default=None, description="구간 내 실재 wafer 목록(번호순·끝점 포함) — B 발행, 미도착 시 폴백"
    )


class SpcFlag(BaseModel):
    """prediction_context.spc_flags[] — SHAP 와 독립된 Nelson 병렬 채널 (헌법 3-3)."""

    model_config = ConfigDict(extra="ignore")

    sensor: str
    rule: str


class PredictionContext(BaseModel):
    """prediction_context — A 예측 결과 스냅샷 (Contract §3).

    shap_top3 는 §3 alert 안에서는 센서 C코드 리스트(["C11","C62","C17"]).
    (§2 fdc.prediction 의 객체형과 다름 — 알람에는 요약형만 실림.)
    """

    model_config = ConfigDict(extra="ignore")

    wafer_id: str
    predicted_c65: float
    shap_top3: list[str] = Field(..., description="SHAP 상위 3 센서 C코드")
    spc_flags: list[SpcFlag] = Field(default_factory=list)
    # [B 확정 2026-07-28 — `B4-1_계약필드_반영요청_C_v1`] wafer 처분 이중 확인의 두 번째 축.
    #   눈금: A 의 `ae_score` 를 ECDF 캘리브레이션한 [0,1] 값. `0.2 = VAL P98.5 = Qual 임계`로
    #   `qual.pass_ae_max`·`ct.ae_anomaly_threshold` 와 **같은 눈금**이라 config 임계와 직접 비교된다.
    #   미제공(None)이면 `decide_wafer` 가 SCRAP 을 내지 않는다 — 값 유무가 아니라 임계 비교로 판정.
    anomaly_score: Optional[float] = Field(
        default=None, description="이상 스코어 [0,1] — ae_score ECDF 캘리브레이션. 미제공 시 None"
    )


class AlertModel(BaseModel):
    """fdc.alert 단일 경보 메시지 (Contract §3 / DB spc_violations).

    변경 금지 필드 (헌법 2-1): wafer_id, chamber_id, rule_id, severity, context_score.
    (wafer_id·rule_id·severity 는 하위 객체에 위치 — prediction_context / violations[])
    """

    model_config = ConfigDict(extra="ignore")

    alert_id: str = Field(..., description="ALERT-<YYYYMMDD>-<CHAMBER>-<SEQ> (6-4)")
    timestamp: datetime
    chamber_id: str = Field(..., pattern=CHAMBER_ID_PATTERN, description="SIM_CH_1~4 (E14 deprecated)")
    violations: list[Violation] = Field(..., min_length=1, description="위반 1건 이상")
    tttm: Tttm
    context_score: int = Field(
        # 0~100 은 점수의 **정의역**(계약 §3)이라 params 대상이 아니다 — 튜닝 대상은 임계(B7)뿐.
        ..., ge=0, le=100,
        description="0~100. < 31 이면 Agent 미가동(기록만) — B7. 변경 금지 필드",
    )
    suspect_window: Optional[SuspectWindow] = None
    prediction_context: PredictionContext

    # 계약 본문에 없는 필드 — B 규격확인 전까지 optional 파싱 (있으면 보존).
    # extra="ignore" 에 삼켜지지 않도록 명시 선언.
    trigger_signal: Optional[Any] = Field(
        default=None, description="[B 규격확인 대기] Contract 본문 미정의 — optional"
    )

    # --- 파생 헬퍼 (게이트·라우팅 편의) ---
    @property
    def agent_gate_open(self) -> bool:
        """Context Score 게이트 — B7(>=31) 미만은 Agent 미가동(기록만).

        ⚠️ **운영 판정은 이 property 가 아니다.** 실제 게이트는 `pipeline.gate_open(alert,
        settings)` 이 params `spc.context_score_agent_min` 으로 한다. 이 헬퍼는 계약 명문값을
        스키마 안에서 확인하는 편의용이고, 현재 사용처는 `test_alert_schema` 2건뿐이다.

        params 를 참조하지 않는 이유: 스키마 계층이 config 를 임포트하면 계약 모델이 런타임
        설정에 묶인다(계약은 설정과 무관해야 한다). 대신 값을 상수로 꺼내 근거를 명시했다.
        ⚠️ `spc.context_score_agent_min` 을 바꾸면 이 상수도 같이 고쳐야 한다 — 갈라질 수 있는
        자리이므로, 헬퍼가 계속 안 쓰이면 삭제하는 편이 낫다(후속 판단).
        """
        return self.context_score >= CONTRACT_AGENT_GATE_MIN

    @property
    def max_severity(self) -> Severity:
        """violations 중 최고 심각도 — Incident/Inhibit 판단 보조."""
        order = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
        return max((v.severity for v in self.violations), key=lambda s: order[s])
