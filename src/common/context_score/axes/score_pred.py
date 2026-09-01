# =============================================================================
# axes/score_pred.py   —  [담당: A]  Step4  · 예측(P95) 축 순수 서브함수
# =============================================================================
# 역할: predicted_c65 를 P95 방향 가중항(float)으로 환산한다. 단독 알람 불가·
#       가중항 성격(그룹핑 2-C · e8) — 반환값은 combine/context_score 후처리에서
#       센서 알람 점수에 가산·곱해진다. 이 함수는 방향 가중값만 낸다.
#
# [산식]  (기획서 §3)
#   0) 결측/비활성 게이트 → 0.0
#   1) Phase 0~1(가한계) → 0.0            (재학습 전 예측 불신, 2-C)
#   2) gap = predicted_c65 − p95_threshold ; gap ≤ 0 → 0.0 (정상 방향)
#   3) clip(gap / p95_scale, 0, w_cap) × pred_weight
#
# [입력]  prediction: dict  (계획서 §4-① + 오케스트레이터 주입 런타임 키)
#   · predicted_c65 : 모델 출력 (실측 C65 아님 — 누수 금지 1-3)
#   · phase         : 오케스트레이터/consumer 주입 (provisional get_phase). 계약 필드 아님.
#   · p95_threshold : y_thresholds(챔버×레시피·버전 고정)에서 조회해 주입. 라이브 롤링 금지.
#                     (기획서 §3.2 — 매 wafer 재계산 시 순수성·재현성 붕괴)
#
# [규칙]
#   - 순수 함수·부작용 0. 매직넘버 금지 → cfg(ContextScoreConfig). print 금지 → logging.
#   - pred_weight=null = 축 비활성 → 0.0 ("확정 전 0", 조용한 매직 기본값 아님).
#   - pred_weight set 인데 scale/w_cap null → 명시적 에러(오설정, 6-1).
#
# [★③ total function 계약]  (계획서 §4-③ · 코드리뷰 §3-1·3-2)
#   **입력**(prediction dict)에는 어떤 값이 와도 raise 하지 않고 float 을 반환한다.
#   정상 결측(키 없음·None)은 조용히 0, **계약 위반**(비수치·bool·NaN·inf)은 0 + WARNING.
#   - `float()` 직행 금지 — 비수치 입력이 ValueError 로 wafer 전체 alert 을 죽였다(§3-1 실증).
#   - **bool 제외 필수** — `float(True)=1.0` 이 조용한 오계산을 만든다(§3-2 실증).
#   - 단, **설정**(cfg) 오류의 ValueError 는 이 계약 대상이 아니다(아래 _p95_direction_weight).
#     입력 가드가 설정 검증보다 **먼저** 돌아야 비수치 입력이 오설정 에러로 둔갑하지 않는다.
# =============================================================================
from __future__ import annotations

import logging
import math

from src.common.context_score.config import ContextScoreConfig

logger = logging.getLogger(__name__)


def _as_number(value, field: str) -> float | None:
    """수치 계약값 파싱. 정상 결측은 조용히 None, 계약 위반은 None + WARNING (score_ae 동일 패턴).

    구분 이유: 예측 미도달(정상 결측)은 운영상 흔해 로그가 소음이 되지만, 비수치·bool·
    NaN·inf 는 발행/주입측 계약 위반이라 표면화해야 한다(조용한 통과 금지, 6-1).
    bool 을 먼저 걸러내는 이유는 `bool` 이 `int` 서브클래스라 `isinstance(True, int)` 가
    True 이기 때문이다 — 가드 없이는 `float(True)=1.0` 이 정상 수치로 통과한다.
    NaN 은 어떤 비교도 False 라 가드가 없으면 gap 판정을 조용히 빠져나간다.
    """
    if value is None:
        return None                                   # 정상 결측 — 무로그
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("score_pred: %s 가 수치가 아님 → 축 0 처리 (type=%s, value=%r)",
                       field, type(value).__name__, value)
        return None
    v = float(value)
    if not math.isfinite(v):
        logger.warning("score_pred: %s 가 비유한값 → 축 0 처리 (value=%r)", field, value)
        return None
    return v


def _phase_value(phase) -> str | None:
    """주입된 phase 를 비교용 문자열로 정규화. Phase enum·str·None 모두 허용."""
    if phase is None:
        return None
    return str(getattr(phase, "value", phase))


def _p95_direction_weight(predicted_c65: float, p95_threshold: float,
                          cfg: ContextScoreConfig) -> float:
    """P95 초과 방향(higher_is_worse)만 정규화·포화·가중한 순수 산식 (기획서 §3.2).

    P95 이하(정상 방향)는 0. gap 을 p95_scale 로 정규화하고 w_cap 으로 포화한 뒤
    pred_weight 를 곱한다. pred_weight 가 set 인데 scale/w_cap 이 null 이면 오설정이므로
    명시적으로 실패한다(조용한 기본값 금지, 6-1).
    """
    gap = predicted_c65 - p95_threshold
    if gap <= 0.0:
        return 0.0                                  # P95 이하 = 정상 방향
    if cfg.pred_p95_scale is None or cfg.pred_w_cap is None:
        raise ValueError(
            "pred_weight 가 설정됐으나 pred_p95_scale/pred_w_cap 이 null 입니다 "
            "(params.yaml spc.context_score_pred_* 확인 — 조용한 기본값 금지 6-1)"
        )
    if cfg.pred_p95_scale <= 0.0:
        raise ValueError(f"pred_p95_scale 은 양수여야 합니다: {cfg.pred_p95_scale}")
    normalized = min(gap / cfg.pred_p95_scale, cfg.pred_w_cap)   # 정규화 + 상한 포화
    return normalized * cfg.pred_weight


def score_pred(prediction: dict, cfg: ContextScoreConfig) -> float:
    """예측 축 P95 방향 가중항(float). 단독불가·Phase 0~1 가중 0 (계획서 §4-②).

    Args:
        prediction: joined["prediction"] (predicted_c65 등) + 오케스트레이터 주입
            런타임 키(phase·p95_threshold). None/빈 dict 면 0.0 (★③ 결측 정책).
        cfg: 배점 설정. pred_weight=null 이면 축 비활성(0.0).

    Returns:
        방향 가중항. 아래 중 하나라도 해당하면 0.0:
        결측·pred_weight null·Phase 0~1·p95_threshold 미주입·**계약 위반 입력**(비수치·
        bool·NaN·inf, WARNING 동반)·predicted_c65 ≤ P95.
    """
    if not prediction:                                       # None·빈 dict (★③)
        return 0.0
    raw_predicted = prediction.get("predicted_c65")
    if raw_predicted is None:                                # 예측 결측 — 조용히 0 (무로그)
        return 0.0
    if cfg.pred_weight is None:                              # 축 비활성 (B 확정 전)
        return 0.0

    phase = _phase_value(prediction.get("phase"))
    if phase is not None and phase in cfg.pred_gated_phases:  # Phase 0~1 가중 0
        return 0.0

    raw_p95 = prediction.get("p95_threshold")                # y_thresholds 주입 (§3.2)
    if raw_p95 is None:
        logger.debug("score_pred: p95_threshold 미주입 → 0.0 (y_thresholds 조회·주입 seam 확인)")
        return 0.0

    # ★③ 입력 가드 — 설정 검증(_p95_direction_weight)보다 **먼저**. 순서가 뒤집히면
    #     비수치 입력 하나가 오설정 ValueError 로 둔갑해 wafer alert 을 죽인다.
    predicted = _as_number(raw_predicted, "predicted_c65")
    p95_threshold = _as_number(raw_p95, "p95_threshold")
    if predicted is None or p95_threshold is None:
        return 0.0

    return _p95_direction_weight(predicted, p95_threshold, cfg)
