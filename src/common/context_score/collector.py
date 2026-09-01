# =============================================================================
# collector.py   —  [담당: B 골격 · 조인 정책 공동]  Step3
# =============================================================================
# 역할: wafer_id 1:1 조인으로 `joined` 이벤트를 만든다. 여기서 "멈춘다" —
#       점수 매기기는 하지 않는다. 점수는 얇은 오케스트레이터(context_score.py)가
#       네 축 함수를 dispatch 한다 (계획서 §1 마지막 문단).
#
# [출력 — joined 이벤트 스키마]  (계획서 §4-①, 인터페이스 계약. 변경 시 A·B 합의)
#   joined = {
#       wafer_id,
#       chamber_id,
#       ts,
#       violations[],   # B — Nelson 위반 dict 전체
#                       #     (current_value·관리선·sensor_window 포함)
#       tttm,           # B — chamber_rollup (통째 전달 — 필드 필터 없음)
#                       #     {score, top_gap_sensor, reference_suspect,
#                       #      suspect_sensors[], ...}  ← suspect_sensors 자동 유입(decouple)
#       prediction,     # A — prediction_cache.get(wafer_id) 결과
#                       #     {predicted_c65, drift_score, anomaly_score,
#                       #      shap_top3, is_qual}
#       is_transient,   # C42 과도 플래그 (context_score 에서 m1 ×0.5 할인용)
#   }
#
# [★⑤ 파일 소유]  (계획서 §5 ★⑤)
#   B 초안: 이 파일은 B 단독 골격. 예측은 prediction_cache(A) "인터페이스만" 호출.
#   → A 코드를 직접 편집하지 않으므로 캐시 계약만 맞으면 병렬 작업 충돌 없음.
#   (필요 시 공동 파일로 전환 가능)
#
# [★③ 예측 결측 정책]  (계획서 §5 ★③ — B 초안)
#   prediction_cache.get(wafer_id) 가 None 이면:
#     - prediction 키를 None(또는 빈 dict)으로 두고 그대로 joined 발행.
#     - AE·pred 축 기여는 0 처리(오케스트레이터/축 함수에서), SPC/TTTM 알람은
#       지연 없이 진행. 로그만 남긴다.
#     - (선택) 짧은 TTL 유예 가능하나 SPC 축을 막지 않는다.
#
# [조인 정책 — 공동 결정]
#   - 키: wafer_id 1:1. chamber_id·ts 정합성 확인.
#   - 결측/중복/순서역전 처리 규칙을 여기 한 곳에서 명문화 (A·B 공동).
#
# [규칙]
#   - 데이터 누수 금지: 조인 과정에서 미래 정보(후행 타겟)를 끌어오지 않는다 (1-3).
#   - 매직넘버(TTL 등) → params.yaml.
#   - print 금지 → logging. docstring 필수.
# =============================================================================
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_TRANSIENT_WINDOW = "transient"   # is_transient 판정 리터럴 (initial_limits.py:185 유일값, #7)

# A PredictionCache.get() 축약키 → fdc.prediction §2 계약명 (스펙 §2 (f) 정규화).
#   A 캐시 내부 키는 "계약 고정 대상 아님"(가변)이라, 공유 인터페이스 joined["prediction"]엔
#   안정적 계약명으로 담아 하류(B9·alert·A축)가 A 내부 리네임에 커플링되지 않게 한다.
#   눈금 참고(값 해석은 안 하지만 하류 계약): drift_score = ae_drift_score(EWMA [0,1]) ·
#   anomaly_score = ae_score(ECDF 캘리 [0,1], 0.2=VAL P98.5) — D1 확정 2026-07-28
#   (Docs/score_ae_z발행_전환_변경점검_v1.md). 밴드 매핑은 axes/score_ae.py 책임.
_PREDICTION_KEY_MAP = {
    "pred": "predicted_c65",
    "drift": "drift_score",
    "anomaly": "anomaly_score",
    "shap": "shap_top3",
    "is_qual": "is_qual",
    "ts": "ts",
}


def _normalize_prediction(raw: dict | None) -> dict | None:
    """A get() 축약키 dict → §2 계약명 dict ((f)). 결측(None)은 None. 없는 키는 값 None."""
    if raw is None:
        return None
    return {contract: raw.get(short) for short, contract in _PREDICTION_KEY_MAP.items()}


class NullCache:
    """예측 캐시 플레이스홀더 (Step 3a) — 항상 miss(`get→None`).

    A Step2 `PredictionCache`가 브랜치에 착륙하기 전(3b)까지 consumer가 주입하는 기본값.
    `get` 인터페이스만 만족해 collector는 무크래시로 예측축 0 경로를 탄다(결정③·a1).
    """

    def get(self, wafer_id: str):
        """항상 None(캐시 miss) — 예측 결측과 동일 취급."""
        return None


def build_joined(wafer_id: str, chamber_id: str, ts: str, violations: list,
                 tttm_obj: dict | None, prediction_cache,
                 recipe_id: str | None = None) -> dict:
    """세 채널(SPC·TTTM·예측)을 wafer_id 1:1로 조인한 `joined` 이벤트 생성 (순수·부작용 0).

    §4 스키마 조립. `chamber_id`·`ts`는 seam 전달(wafer t0 기준, rollup엔 ts 없음). 예측은
    `prediction_cache.get(wafer_id)` — 결측(None)이면 대기 없이 진행하고 로그만 남긴다(결정③,
    AE·pred 축 기여는 combine에서 0). `is_transient`는 위반 중 transient 윈도우 존재 여부,
    `is_qual`은 예측에서 승계(예측 없거나 키 없으면 False). 점수 계산은 하지 않는다(오케스트레이터 몫).

    `recipe_id`는 seam(consumer)이 wafer raw row `C6`에서 추출해 주입한 값을 최상위로 승계한다
    (B4-1 Step5b 리뷰② — 위반 0건 crazy-only wafer도 챔버×recipe P99를 정타). 미지정=None.
    """
    prediction = _normalize_prediction(prediction_cache.get(wafer_id))   # A 축약키 → §2 계약명 (f)
    if prediction is None:
        # carryover P3 — 결측은 흔하고(예측 미도착·크래시 리플레이) 요약은 JoinCounter(P2)가 낸다.
        #   wafer마다 INFO면 로그 스팸이라 DEBUG로 강등한다(6-1).
        logger.debug("collector: wafer %s 예측 결측 — AE/pred 축 0으로 진행(대기 없음)", wafer_id)
    return {
        "wafer_id": wafer_id,
        "chamber_id": chamber_id,
        "recipe_id": recipe_id,
        "ts": ts,
        "violations": violations,
        "tttm": tttm_obj,
        "prediction": prediction,
        "is_transient": any(v.get("sensor_window") == _TRANSIENT_WINDOW for v in violations),
        "is_qual": bool(prediction.get("is_qual")) if prediction else False,
    }


# ── B2: 예측 조인 hit/miss 계측 ────────────────────────────────────────────────
#   build_joined는 **순수**(부작용 0) 유지 — 카운팅은 호출자(seam)가 반환값(joined)으로 한다
#   (함수 내부 상태 금지·리뷰 §2-3 권고). miss율이 높으면 예측·SPC 도착 간극이 커 조인 유예(grace)
#   필요 신호(step8 리허설 계측 입력).
def prediction_hit(joined: dict) -> bool:
    """joined가 예측 조인에 성공했는지(hit). miss(prediction None·키 결측)면 AE/pred 축 0 기여."""
    return joined.get("prediction") is not None


class JoinCounter:
    """예측 조인 hit/miss 누적 — seam(오케스트레이터 어댑터)이 build_joined 반환을 `record`.

    자체 상태만 가지며 build_joined를 건드리지 않는다(순수 보존). `miss_rate`로 조인 유예
    판단(§2-3·step8). 크래시 리플레이 구간(예측 미재소비→상시 miss)도 이 카운터로 함께 계측된다.
    """

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0

    def record(self, joined: dict) -> None:
        """joined 1건을 hit/miss로 집계."""
        if prediction_hit(joined):
            self.hits += 1
        else:
            self.misses += 1

    @property
    def total(self) -> int:
        """집계된 총 조인 건수."""
        return self.hits + self.misses

    @property
    def miss_rate(self) -> float:
        """miss / total — 표본 0이면 0.0(무크래시)."""
        return self.misses / self.total if self.total else 0.0
