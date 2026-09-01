"""wafer 처분 판정 — SCRAP / RELEASE / HOLD (C6-1).

정본: 기획서 v4.5 **§4-3**(Interlock & Wafer Disposition — 의심 윈도우 모델) ·
config 합의안 **B9**(crazy wafer 판정) · 회의 브리핑 2026-07-06(현업 절차 확인).

**이 모듈은 tool 이 아니다.** 4지선다(조치)와 처분(wafer)은 배타적 선택지가 아니라 병렬 축이고
(④ escalate 를 골라도 wafer 는 처리해야 한다), 처분은 RAG 검색이 아니라 수치 비교라
LLM 을 부를 이유가 없다. 헌법 1-4 의 "리포트 3종"도 tool 을 셋으로 못박는다.

**판정을 코드가 하는 이유** — 되돌릴 수 없는 결정에 창작 여지를 주지 않는다. 실측 2건:
  · LLM 이 계약에 없는 `anomaly_score 3.6` 을 지어내 SCRAP 을 정당화 (가드⑤ 신설 사유)
  · LLM 이 `tttm.score 2.3` 을 anomaly_score 로 오인 인용 (fewshot3, 2026-07-24)

판정 트리 (§4-3 + B9):

    STEP 0. held_wafers ← 의심 윈도우 ∩ 실재 wafer 목록 (범위는 B, 목록은 A 예측 API)
            ※ 번호 연번 전개 금지 — 실데이터 결손률 62.4% (`endpoints_only` 참조)
    STEP 1. predicted_c65 조회 불가        → HOLD   (값 없으면 창작 금지 · 원위치 유지)
    STEP 2. predicted_c65 ≤ B9(1572)
            ├ anomaly ≥ 임계               → HOLD   (계측 대기 — anomaly 가드, PM 결정 2026-07-29)
            └ 그 외                         → RELEASE(품질 근거 없음 — 잡아둘 이유가 없다)
            ※ anomaly 는 품질의 제2의 자가 아니라 **예측 유효성 게이트**다 — AE 가 탐지하는 것이
              "이 입력이 모델이 학습한 세계 안인가"이므로. 급변 wafer 는 예측이 정상에 머물러
              (실측: 부스트 20조합 전부 임계 미달 — 트리 모델은 분포 밖을 외삽하지 않는다)
              가드가 없으면 센서가 튀고 AE 가 만점이어도 RELEASE 가 나온다.
              결과적으로 SCRAP 의 이중 확인과 대칭이 된다(자 2개가 "정상"을 가리켜야 RELEASE).
              **임계 미등재 구간에 미발동하는 것은 결함이 아니라 PM 이 정한 적용 순서**다
              (AE 포화 중 등재 = RELEASE 전멸 — 결정안 §8. 상세는 decide_wafer 주석).
    STEP 3. 초과 wafer 가 2장 이상          → HOLD   (공정 문제 — R2R 경로. "여러 장이면 R2R")
    STEP 4. 초과 1장(spot) + anomaly ≥ 임계  → SCRAP  ("한 장만 높게 나오면 스크랩")
            그 외                            → HOLD   (이중 확인 미충족 — 자 하나로는 못 버린다)
            ※ 임계 = config `spc.crazy_wafer.anomaly_score_min` (**선택적**).
              미설정이면 anomaly 가 와도 SCRAP 을 내지 않는다 — fail-safe(2026-07-28).
              B9 원문 `anomaly_score > 3×기준`의 "기준"이 미정의고 A 규격이 ae_score [0,1] 이라
              직접 비교가 성립하지 않는다. 재대조 확정 후 키를 등재한다.

장수 축은 **윈도우 폭이 아니라 임계 초과 장수**다. B9 원문이 "(1장 spot) … 연속 2장 이상 =
Incident 경로"이고 멘토 발언도 "한 장만 **높게 나오면** 스크랩"이라, 세는 대상은 crazy wafer 다.
(N3 추세면 윈도우가 6~14장이어도 초과가 1장일 수 있다 — 그 경우는 spot 이다.)

HOLD 가 기본값인 이유: 인터락으로 이미 세워둔 상태(db `wafer_dispositions.status DEFAULT 'HOLD'`)
라 HOLD 는 **원위치**다. 되돌릴 수 없는 SCRAP 과 달리 비용이 없어 fail-safe 가 성립한다.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .config import Settings
from .schemas.alert import AlertModel
from .schemas.report import (
    DispositionVerdict,
    SupervisorBrief,
    WaferDisposition,
    WaferVerdict,
)

logger = logging.getLogger(__name__)

# 요약(recommendation)에 올릴 순서 — **되돌릴 수 없는 쪽이 위**다. 비용 순위가 아니라
# 가역성 순위(analyze_quality ②축과 같은 축). 화면 한 줄 요약은 가장 무거운 판정을 보여준다.
_WEIGHT: dict[str, int] = {"SCRAP": 2, "HOLD": 1, "RELEASE": 0}

# wafer_id = <stem><번호><멀티챔버 접미사?> — 계약 §1: 멀티챔버 복제 시 `C64_516_CH_3` 형태가 된다.
# 번호만 뽑아 구간을 전개하되 stem·접미사가 다르면 전개하지 않는다(다른 계열을 섞지 않기 위해).
_WAFER_RE = re.compile(r"^(?P<stem>.*?)(?P<no>\d+)(?P<suffix>(?:_CH_\d+)?)$")

def _parse(wafer_id: str) -> Optional[tuple[str, int, str]]:
    """wafer_id → (stem, 번호, 접미사). 형식이 안 맞으면 None."""
    m = _WAFER_RE.match(wafer_id)
    if not m:
        return None
    return m.group("stem"), int(m.group("no")), m.group("suffix")


def _by_number(wafer_id: str) -> tuple[int, str]:
    """정렬 키 — 번호순(=처리 시간순). 문자열 정렬은 'C64_1001' < 'C64_995' 로 뒤집힌다."""
    p = _parse(wafer_id)
    return (p[1] if p else 0, wafer_id)


def endpoints_only(alert: AlertModel) -> list[str]:
    """윈도우 내부를 해결하지 못했을 때의 폴백 — **실재가 보장된 id 만** 남긴다.

    ⚠️ **번호를 연번으로 전개하면 안 된다.** 실데이터(`Data/문제1(하)/train_data.csv`) 실측:
        wafer 번호 범위 1~31,742 중 실재 11,939개 — **결손률 62.4%**
        연속 wafer 간 번호 차이는 1이 71%뿐이고 2·3·14·26 까지 점프한다.
    구간을 채우면 존재하지 않는 wafer 가 목록의 다수가 된다(DB 적재·화면 표시가 전부 거짓).
    번호는 시간순 단조 증가라 **구간(범위) 개념 자체는 유효**하다 — 내부를 지어내는 것만 금지.

    그래서 여기서는 alert 이 **실제로 준 id**(start·end·대표)만 돌려주고 미해결을 로그로 남긴다.
    정확한 목록은 `select_in_window(alert, 실재 wafer 목록)` 으로만 얻는다.
    """
    rep = alert.prediction_context.wafer_id
    sw = alert.suspect_window
    if sw is None:
        return [rep]  # N1 단발 — §4-3 "해당 wafer 1장". 이 경우는 폴백이 아니라 정답이다.

    logger.warning(
        "윈도우 내부 미해결(실재 목록 없음): %s ~ %s — 끝점만 사용. "
        "정확한 목록은 A 예측 API 조회 후 select_in_window 로 얻을 것",
        sw.start_wafer, sw.end_wafer,
    )
    return sorted({sw.start_wafer, sw.end_wafer, rep}, key=_by_number)


def select_in_window(alert: AlertModel, candidates: list[str]) -> list[str]:
    """A 예측 API 가 준 **실재 wafer 목록**에서 의심 윈도우 구간만 거른다 — 정본 경로.

    번호가 62% 결손이라(`endpoints_only` 참조) 구간을 채우는 대신 **있는 것만 거른다**.
    wafer 번호가 시간순 단조 증가라 (lo ≤ n ≤ hi) 비교가 곧 "그 구간에 처리된 wafer"가 된다.
    윈도우가 없으면 대표 wafer 1장으로 좁힌다.
    """
    rep = alert.prediction_context.wafer_id
    sw = alert.suspect_window
    if sw is None:
        return [rep] if rep in candidates else []

    a, b = _parse(sw.start_wafer), _parse(sw.end_wafer)
    if a is None or b is None:
        return sorted(set(candidates), key=_by_number)

    stem, lo, suffix = a
    hi = b[1]
    out = [
        w for w in candidates
        if (p := _parse(w)) and p[0] == stem and p[2] == suffix and lo <= p[1] <= hi
    ]
    return sorted(set(out), key=_by_number)


def _num(value: Any) -> Optional[float]:
    """숫자만 통과 — 문자열·None 은 '값 없음'으로 본다(창작 금지 경로로 보낸다)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def decide_wafer(
    wafer_id: str,
    predicted: Optional[float],
    anomaly: Optional[float],
    n_exceed: int,
    threshold: float,
    anomaly_min: Optional[float] = None,
) -> WaferVerdict:
    """wafer 1장 판정 (모듈 docstring 트리). `reason` 은 정형 문구 — 감사 추적·재현성.

    Args:
        anomaly_min: anomaly '동반' 인정 하한. **None(미설정)이면 SCRAP 을 내지 않는다** —
            아래 fail-safe 참조.
    """
    if predicted is None:
        return WaferVerdict(
            wafer_id=wafer_id,
            recommendation="HOLD",
            reason="예측 조회 불가 — 판정 근거 없음(수치 창작 금지)",
        )

    if predicted <= threshold:
        # ── anomaly 가드 (PM 결정 2026-07-29 — 결정안 §8 "RELEASE 는 유효한 예측에만") ──
        # **실측 근거**(§7ⓐ 모델 감도 프로브): C31·C11·C17·C62·C4 × 4~10σ 부스트 **20조합
        # 전부** predicted_c65 1572 미달(최대 반응 C11 +8σ → Δ+5.0). 트리 모델(XGBoost)은
        # 학습 분포 밖에서 리프가 포화되어 **외삽하지 않는다** — 급변 wafer 는 예측이 정상에
        # 머문다. 가드가 없으면 센서가 튀고 AE 가 만점이어도 "풀어줘라"가 나온다.
        #
        # **의미론(PM 확정)**: anomaly 는 품질의 제2의 자가 아니라 **예측 유효성 게이트**다 —
        # AE 가 탐지하는 것이 정확히 *"이 입력이 모델이 학습한 세계 안인가"* 이기 때문.
        #   · 모델이 **자격 있는 입력을 정상 판정**(AE 정상 + 예측 정상) → RELEASE
        #   · 모델이 **판정할 자격이 없는 입력**(AE 이상 → 예측값 자체가 무의미) → HOLD(계측 대기)
        # "모델 한계로 정상처럼 보이는 것"과 "정말 정상인 것"을 권고가 구분해야 한다.
        # 결과적으로 SCRAP 의 이중 확인과 대칭이 된다(자 2개가 "정상"을 가리켜야 RELEASE).
        if anomaly is not None and anomaly_min is not None and anomaly >= anomaly_min:
            return WaferVerdict(
                wafer_id=wafer_id, recommendation="HOLD",
                reason=(f"predicted_c65 {predicted:g} ≤ B9 임계 {threshold:g} 이나 "
                        f"anomaly {anomaly:g} ≥ 임계 {anomaly_min:g} — 계측 대기"),
                predicted_c65=predicted, anomaly_score=anomaly,
            )
        # ⚠️ 임계 미등재(anomaly_min=None) 구간에는 이 가드가 **발동하지 않는다** — 현행 RELEASE 유지.
        #   **이것은 결함이 아니라 PM 이 정한 적용 순서다**(결정안 §8 "적용 순서 — 키 등재가 스위치").
        #   값 0.5 는 확정됐으나(= AE 보정 앵커 P99.5, B 동의) **등재는 AE 정상화 후**다:
        #     · §7ⓑ 실측 — `LEAN85_AE_MODE=live` 에서 예측 200건 **전부 anomaly ≥ 0.5, max=1.0**.
        #       AE 가 시뮬 세계 전체를 극단 이상으로 판정 중이다(챔버 오프셋이 상관구조를 깨서).
        #     · 지금 등재하면 ⓐ **RELEASE 전멸**(전량 "이상" → 라인 정지) ⓑ 포화 anomaly 가
        #       SCRAP 의 절반-가짜 근거가 된다.
        #     · 포화 상태에선 SCRAP 도 어차피 c65 축이 막아 유효하게 안 나오므로 **잃는 것이 없다.**
        #   등재 시점 = SCRAP 개통 + 이 가드가 **동시에 켜지는 단일 스위치**(AE 재보정 = A, ~8/3).
        return WaferVerdict(
            wafer_id=wafer_id, recommendation="RELEASE",
            reason=f"predicted_c65 {predicted:g} ≤ B9 임계 {threshold:g} — 품질 근거 없음",
            predicted_c65=predicted, anomaly_score=anomaly,
        )

    over = f"predicted_c65 {predicted:g} > B9 임계 {threshold:g}"

    if n_exceed >= 2:
        return WaferVerdict(
            wafer_id=wafer_id, recommendation="HOLD",
            reason=f"{over} · 초과 {n_exceed}장 연속 — 공정 경로(R2R) 검토 대상, 스크랩 아님",
            predicted_c65=predicted, anomaly_score=anomaly,
        )

    # 1장 spot — 이중 확인(예측 초과 + anomaly 동반)이 충족돼야 SCRAP.
    #
    # ⚠️ **fail-safe (2026-07-28)**: 임계(`anomaly_min`)가 없으면 anomaly 가 와도 SCRAP 을 내지 않는다.
    #   구 구현은 `anomaly is not None`(값 유무)만 봤다. 지금은 B 가 alert 에 anomaly_score 를
    #   싣지 않아 항상 None → 항상 HOLD 라 문제가 드러나지 않지만, **필드가 붙는 순간**
    #   `anomaly=0.01`(정상)도 "동반 충족"으로 읽혀 SCRAP 이 과잉 발동한다. 그러면 이중 확인이
    #   "A 가 숫자를 보냈나"로 변질된다.
    #   임계값 자체는 **미결**이다 — config B9 원문이 `anomaly_score > 3×기준`인데 그 "기준"이
    #   정의돼 있지 않고(A: ae_raw 인가 ae_score 임계의 3배인가), A 규격은 `ae_score [0,1]`이라
    #   `3.0` 직접 비교는 성립하지 않는다. 확정 전까지 여기서 임의값을 쓰지 않고 **판정을 보류**한다.
    #   ⚠️ B 회신(2026-07-28)으로 **눈금은 확인**됐다 — `anomaly_score` = `ae_score` 의 ECDF
    #   캘리브레이션 [0,1] 이라 config 임계와 직접 비교는 성립한다. 다만 **하한을 얼마로 둘지**는
    #   별개이고(Qual 통과 기준 = 챔버·가역 vs SCRAP 동반 하한 = wafer·불가역), 예측 배선이
    #   붙어 실측이 가능해진 뒤 조정 권한자(B, 합의안 B절)와 확정한다. 그때까지 미등재.
    if anomaly is not None and anomaly_min is not None and anomaly >= anomaly_min:
        return WaferVerdict(
            wafer_id=wafer_id, recommendation="SCRAP",
            reason=(f"{over} · anomaly {anomaly:g} ≥ 임계 {anomaly_min:g} 동반 "
                    "— 이중 확인 충족(1장 spot)"),
            predicted_c65=predicted, anomaly_score=anomaly,
        )

    if anomaly is None:
        why = "anomaly 미확인"
    elif anomaly_min is None:
        why = f"anomaly {anomaly:g} 수신했으나 인정 임계 미확정(B9 재대조 대기)"
    else:
        why = f"anomaly {anomaly:g} < 임계 {anomaly_min:g}"
    return WaferVerdict(
        wafer_id=wafer_id, recommendation="HOLD",
        reason=f"{over} · {why} — 이중 확인 미충족",
        predicted_c65=predicted, anomaly_score=anomaly,
    )


def heaviest(verdicts: list[WaferVerdict]) -> Optional[DispositionVerdict]:
    """요약용 — 가장 무거운(되돌리기 어려운) 판정. 빈 목록이면 None."""
    if not verdicts:
        return None
    return max((v.recommendation for v in verdicts), key=lambda r: _WEIGHT[r])


def decide_dispositions(
    alert: AlertModel,
    predictions: Optional[dict[str, dict[str, Any]]],
    settings: Settings,
    held_wafers: Optional[list[str]] = None,
) -> WaferDisposition:
    """의심 윈도우 전체를 wafer별로 판정한다 (§4-3 "판정은 wafer별 개별").

    Args:
        predictions: {wafer_id: {"predicted_c65": float, "anomaly_score": float|None}}
            A 예측 API 응답. None·미수록 wafer 는 "조회 불가" 경로(HOLD)로 간다.
        held_wafers: 미지정 시 아래 우선순위로 목록을 정한다 —
            ① 인자 주입(mock·테스트) → ② alert `suspect_window.member_wafers`(B 발급, 가장 정확)
            → ③ A 예측 API 응답 ∩ 구간 → ④ `endpoints_only` 폴백(끝점만 — 반쪽).

    `reason`(한 줄 요약)은 채우지 않는다 — LLM 소관. 병합은 `apply_disposition`.
    """
    threshold = float(settings.require("spc.crazy_wafer.predicted_c65_threshold"))
    # anomaly 인정 하한 — **선택적**이다(`require` 아님). 미설정이면 SCRAP 을 내지 않는다(fail-safe).
    #   구 `anomaly_score_multiplier`(=3.0)를 쓰지 않는 이유: "3×기준"의 기준이 미정의고 A 규격이
    #   ae_score [0,1] 이라 직접 비교가 성립하지 않는다 → 하한 확정 후 이 키를 등재한다.
    #   **현재 params.yaml 에 이 키는 없다**(의도) — 없으면 `settings.get` 이 None 을 주고
    #   SCRAP 이 발동하지 않는다. 값이 정해지기 전까지 판정을 보류하는 것이 안전측이다.
    anomaly_min = _num(settings.get("spc.crazy_wafer.anomaly_score_min"))
    src = predictions or {}
    sw = alert.suspect_window
    if held_wafers is not None:
        wafers = held_wafers
    elif sw is not None and sw.member_wafers:
        # alert 이 목록을 실어준 경우 — 가장 정확하다. B 가 **위반 판정에 실제로 쓴 레코드**라
        # 예측 조회 실패분이 빠지지 않는다(A 경로는 예측이 있는 wafer 만 나온다).
        # 그래서 A 예측 경로보다 우선한다. B 는 합집합·dedup·numeric 정렬을 마쳐 보내지만
        # (B 회신 2026-07-28), 여기서 다시 정렬하는 것은 발행자 규약에 판정을 의존시키지 않기 위함.
        wafers = sorted(set(sw.member_wafers), key=_by_number)
    elif src:
        # A 예측 API 응답이 곧 "실재 wafer 목록"이다 — 거기서 구간만 거른다(차선 경로).
        wafers = select_in_window(alert, list(src)) or endpoints_only(alert)
    else:
        wafers = endpoints_only(alert)

    # 초과 장수를 먼저 센다 — 개별 판정이 "이 장 하나"가 아니라 "몇 장이 튀었나"에 달려 있다.
    n_exceed = sum(
        1 for w in wafers
        if (p := _num(src.get(w, {}).get("predicted_c65"))) is not None and p > threshold
    )

    per_wafer = [
        decide_wafer(
            w,
            _num(src.get(w, {}).get("predicted_c65")),
            _num(src.get(w, {}).get("anomaly_score")),
            n_exceed,
            threshold,
            anomaly_min,
        )
        for w in wafers
    ]

    summary = heaviest(per_wafer)
    logger.info(
        "처분 판정: alert=%s wafer %d장 (초과 %d) → %s [%s]",
        alert.alert_id, len(wafers), n_exceed, summary,
        " ".join(f"{v.recommendation[0]}" for v in per_wafer),
    )
    return WaferDisposition(held_wafers=wafers, recommendation=summary, per_wafer=per_wafer)


def apply_disposition(
    brief: SupervisorBrief,
    alert: AlertModel,
    predictions: Optional[dict[str, dict[str, Any]]],
    settings: Settings,
) -> SupervisorBrief:
    """Brief 의 wafer_disposition 을 코드 판정으로 덮어쓴다 (LLM 의 `reason` 한 줄은 보존).

    가드 계열과 같은 위치에서 돈다. LLM 이 낸 `recommendation`·`held_wafers` 는 근거가
    수치인데 창작 이력이 있어 신뢰하지 않는다 — 다만 사람이 읽는 요약 문장은 LLM 이 낫다.
    """
    decided = decide_dispositions(alert, predictions, settings)
    prev = brief.wafer_disposition
    if prev.recommendation and prev.recommendation != decided.recommendation:
        logger.info(
            "처분 교정: brief=%s LLM=%s → 코드=%s",
            brief.report_id, prev.recommendation, decided.recommendation,
        )
    return brief.model_copy(
        update={"wafer_disposition": decided.model_copy(update={"reason": prev.reason})}
    )
