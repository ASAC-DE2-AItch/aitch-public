# =============================================================================
# context_score.py   —  [담당: 공동]  Step4  · 얇은 오케스트레이터
# =============================================================================
# 스펙: Docs/B4-1_Step4_5_5b_통합구현계획서_v2.md M1-2·D-2~D-7 (원 스펙은
#       Docs/spec/_archive/B4-1_Step4_공통_orchestrator_combine_스펙플랜.md §3-1).
#
# 역할: joined 를 받아 네 축 서브함수를 dispatch 하고 combine 으로 합친 뒤
#       후처리(과도 할인·clip)를 거쳐 최종 context_score(float)를 반환한다.
#
# [의사코드]
#   context_score(joined, cfg):
#       scores = {spc, tttm, ae, pred}                 # 4축 dispatch + _safe 소독
#       scores = 과도(C42)면 대상 키 ×transient_discount  # combine 前 (D-3)
#       raw    = combine(scores, cfg)                  # naive_sum (D-1)
#       score  = clip(raw, cfg.clip_min, cfg.clip_max) # 매직넘버 금지 — params (6-1)
#       log_contribution(...)                          # try/except 격리 (D-6)
#       return score
#
# [순수성 — 이 함수가 하지 않는 것]  (D-7)
#   - **enrichment 를 하지 않는다.** score_pred 가 읽는 런타임 주입 키
#     (`phase`·`p95_threshold`)는 seam(Step 5 배선)이 `build_joined()` **이후**
#     `joined["prediction"]` 에 채워 넘긴다. collector `_PREDICTION_KEY_MAP` 은
#     화이트리스트라 여기서 주입해도 통과하지 못하고 pred 축이 조용히 0 이 된다.
#   - **int 변환을 하지 않는다.** 반환은 float, `int(round(...))` 는 seam 1곳에서만
#     (D-5 — alert.py context_score 가 int 0~100).
#   - **게이트 판정을 하지 않는다.** 게이트(≥31)는 하류 C(`pipeline.gate_open`).
#
# [방어 계층]
#   축 함수는 "어떤 입력에도 raise 하지 않는 total function"이 1차 방어(계약 §4-③),
#   `_safe()` 는 그 **반환값**만 소독(NaN·Inf·None → 0.0)한다. 축 내부 예외까지
#   막지는 못하므로 최후 방어는 seam 의 호출 격리(P1-1)다 — 3층을 모두 둔다.
#
# [규칙]
#   - 데이터 누수 금지 (1-3). 매직넘버(clip 상·하한, 할인계수) → params.yaml.
#   - 공동 파일. 축 dispatch 순서/후처리 변경은 A·B 합의.
#   - docstring 필수. print 금지 → logging.
# =============================================================================
from __future__ import annotations

import logging
import math

from src.common.context_score.axes.score_ae import score_ae
from src.common.context_score.axes.score_pred import score_pred
from src.common.context_score.axes.score_spc import score_spc
from src.common.context_score.axes.score_tttm import score_tttm
from src.common.context_score.combine import AXIS_KEYS, combine
from src.common.context_score.instrumentation import log_contribution

logger = logging.getLogger(__name__)

# m1 과도(C42) 할인 대상 축 키 — 계획서 D-3 는 **(a) 전 키**를 채택했다.
#   근거: naive_sum 이 선형이라 "전 키 ×0.5" ≡ "합산 후 ×0.5"(그룹핑 문서의 전역 modifier)와
#   결과가 같고, 대상 축이 늘어도 호출부·시그니처가 안 바뀐다.
#   (b)안(spc·ae 만)으로 바꿀 일이 생기면 **이 상수만** 교체한다 — 국소화 지점.
#   ⚠️ Step 6 에서 combine 이 비선형(grouped_2stage)으로 바뀌면 "할인 위치"를
#      combine 前/後 중 어디로 둘지 재검토해야 한다(등가성이 깨짐).
_DISCOUNT_KEYS: tuple[str, ...] = AXIS_KEYS


def _safe(value) -> float:
    """축 산출값 소독 — None·비수치·NaN·Inf → 0.0 (값 전용 방어, D-9).

    bool 을 제외하는 이유는 `float(True)=1.0` 이 조용한 오계산을 만들기 때문이다
    (축 가드와 동일 원칙). 축이 **예외**를 던지는 경우는 여기서 못 막는다 —
    그건 축의 total function 계약(1차)과 seam 격리(최후)의 몫이다.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("context_score: 축 산출값이 수치가 아님 → 0.0 (value=%r)", value)
        return 0.0
    v = float(value)
    if not math.isfinite(v):
        logger.warning("context_score: 축 산출값이 비유한값 → 0.0 (value=%r)", value)
        return 0.0
    return v


def _apply_transient_discount(scores: dict[str, float], is_transient: bool,
                              cfg) -> dict[str, float]:
    """과도(C42=1) 웨이퍼면 `_DISCOUNT_KEYS` 에 ×`transient_discount` (combine 前·D-3).

    점화 과도 구간의 신호는 정상 운전 신호보다 신뢰도가 낮아 기여를 깎는다.
    새 dict 를 반환해 입력을 변이하지 않는다(순수 유지 — 계측이 원본을 다시 볼 수 있게).
    """
    if not is_transient:
        return dict(scores)
    return {k: (v * cfg.transient_discount if k in _DISCOUNT_KEYS else v)
            for k, v in scores.items()}


def _clip(value: float, low: float, high: float) -> float:
    """최종 점수를 [low, high] 로 클램프. 경계는 params 경유(0·100 하드코딩 금지, 6-1).

    `float()` 캐스팅은 clip 발동 시 경계값이 그대로 나가는데, cfg 를 int 경계로 직접
    생성한 경우(테스트 등) 반환 타입이 int 가 되는 것을 막는다 — D-5 "반환은 float".
    """
    return float(max(low, min(high, value)))


def _log_contribution(joined: dict, scores: dict, raw: float, final: float) -> None:
    """계측 훅 호출을 격리 (D-6) — 로그 실패가 점수 반환을 막지 않는다.

    contribution_log 는 side-channel(Step 6 분석 입력)이라, 디스크 오류·권한 문제로
    실패해도 알람 경로는 계속 가야 한다. 다만 조용히 삼키면 계측이 죽은 걸 모르므로
    warning 으로 표면화한다.
    """
    try:
        log_contribution(joined, scores, raw, final)
    except Exception:                                  # noqa: BLE001 — side-channel 격리
        logger.warning("contribution_log 기록 실패 — 점수 산출은 계속 (wafer=%s)",
                       joined.get("wafer_id"), exc_info=True)


def context_score(joined: dict, cfg) -> float:
    """joined → 최종 context_score (4축 dispatch → combine → 후처리). 순수·float 반환.

    축 기여를 `_safe` 로 소독해 dict 로 모은 뒤, 과도 할인(D-3)을 combine **전에**
    적용하고, combine(naive_sum) 결과를 params 경계로 clip 한다. 계측은 side-channel 로
    격리 호출한다(D-6).

    예측 결측(`prediction=None`)이어도 SPC/TTTM 축은 지연 없이 채점된다 — AE·pred 축이
    0 기여로 흡수할 뿐이다(★③). pred 축은 `pred_weight=null` 인 동안 항상 0(D-4).

    Args:
        joined: `collector.build_joined()` 산출. seam enrichment(`phase`·`p95_threshold`)가
            끝난 상태로 들어온다 — 이 함수는 주입하지 않는다(D-7).
        cfg: `ContextScoreConfig` — 배점·할인계수·clip 경계.

    Returns:
        최종 점수 float(`cfg.clip_min` ~ `cfg.clip_max`). int 변환은 seam 담당(D-5).
    """
    prediction = joined.get("prediction")
    scores = {
        "spc": _safe(score_spc(joined.get("violations") or [], cfg)),
        "tttm": _safe(score_tttm(joined.get("tttm"), cfg)),
        "ae": _safe(score_ae(prediction, cfg)),
        "pred": _safe(score_pred(prediction, cfg)),
    }
    scores = _apply_transient_discount(scores, bool(joined.get("is_transient", False)), cfg)
    raw = combine(scores, cfg)
    final = _clip(raw, cfg.clip_min, cfg.clip_max)
    _log_contribution(joined, scores, raw, final)
    return final
