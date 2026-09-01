# =============================================================================
# axes/score_ae.py   —  [담당: A]  Step4  · AE(이상탐지) 축 순수 서브함수 (그룹핑 A3)
# =============================================================================
# 스펙: Docs/score_ae_설계_기획서_v1.md(§3.1(b)·§3.2·§3.3) +
#       Docs/score_ae_z발행_전환_변경점검_v1.md(D1 확정 · §4-3 의사코드).
# 역할: 예측 페이로드의 AE 두 채널(anomaly·drift)을 AE 축 기여 점수(float)로 낸다.
#
# [입력]  joined["prediction"] — {predicted_c65, drift_score, anomaly_score, shap_top3, is_qual}
#   · anomaly_score = **ae_score** (ECDF 캘리 [0,1]) — D1 확정 2026-07-28.
#       0.2 = VAL P98.5(= Qual 임계) · 0.5 = P99.5 · 1.0 = P99.9(이상은 클립).
#   · drift_score   = **ae_drift_score** (EWMA 레벨 [0,1]) — 계약 변경금지 필드.
#       clip((EWMA − B0 0.130)/(ALARM 0.200 − B0), 0, 1) — 유효 밴드폭 0.07이라 포화 쉬움.
#
# [산식]  A3 = max(e6, e7)
#   e6 anomaly : ae_band_edges(0.2/0.5/1.0) 밴드 → ae_band_pts(10/20/30).  경계 포함(`>=`)
#   e7 drift   : > drift_hi → pts_drift_hi / > drift_lo → pts_drift_lo / 그 외 0.  경계 초과(`>`)
#
# [왜 합산이 아니라 max 인가]
#   e6·e7은 **단일 `ae_raw` 하나에서 파생**된다(ae_v3 불가침 12):
#   ae_raw → ae_score(순간) → EWMA → ae_drift_score(누적) — 같은 원신호의 시간필터 차이일 뿐이라
#   독립 증거가 아니다. 레짐 전환 시 둘 다 켜지므로 선형합산하면 이중계상(그룹핑 A3).
#   실측(15,919 wafer): 합산 평균 28.65점 vs max 16.70점 — **+72% 과대**.
#
# [왜 z 표준화가 아니라 밴드 인가]  (D1 — 변경점검 v2 §8·§9)
#   ae_raw는 왜도 3.43 비대칭이라 σ 경계가 구조적으로 과발화한다. VAL 실측 z≥2 3.98%·z≥3 1.94%
#   (정규 가정 2.28%·0.13% 대비 최대 15배) → 최고 버킷이 최저 밴드보다 자주 터지는 모순.
#   ae_score 밴드는 calib(ECDF 앵커)가 꼬리확률을 보장 → 실측 1.53/0.51/0.10%로 설계값 정합.
#   헌법 1-1 예외2(비정규 분포 → 분위수 관리선)와 같은 계열의 판단.
#
# [★③ 결측 정책]  prediction 이 None/빈 값이면 0.0 (알람은 SPC/TTTM 로 무지연 진행).
#   정상 결측(키 없음/None)은 조용히 0, **계약 위반**(NaN·비수치·[0,1] 범위 밖)은 0 + WARNING 으로 구분한다.
#
# [규칙]
#   - 순수 함수·부작용 0. 재학습 유발 없음 — 입력 드리프트는 서빙 보호·에스컬레이션용 (헌법 3-3).
#   - SHAP 독립 채널(3-3): 이 함수는 점수만 낸다. shap_top3 순위에 개입하지 않는다.
#   - 밴드 경계·배점 등 매직넘버 금지 → params.yaml[spc].context_score_ae_* (6-1).
#   - print 금지 → logging (6-1). docstring 필수.
# =============================================================================
from __future__ import annotations

import logging
import math

from src.common.context_score.config import ContextScoreConfig

logger = logging.getLogger(__name__)

_UNIT_LO, _UNIT_HI = 0.0, 1.0     # 계약상 두 채널 모두 [0,1] (계약 §2 · ae_v3 §3)


def _as_unit_interval(value, field: str) -> float | None:
    """[0,1] 계약값 파싱. 정상 결측은 조용히 None, 계약 위반은 None + WARNING.

    구분 이유: 예측 미도달(정상 결측)은 운영상 흔한 일이라 로그를 남기면 소음이 되지만,
    NaN·문자열·범위 밖 값은 발행측 계약 위반 신호라 표면화해야 한다(조용한 통과 금지, 6-1).
    NaN 은 어떤 비교도 False 라 가드가 없으면 '0점 침묵 처리'로 새어나간다(변경점검 §3-b3).
    """
    if value is None:
        return None                                   # 정상 결측 — 무로그
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("score_ae: %s 가 수치가 아님 → 채널 0 처리 (type=%s, value=%r)",
                       field, type(value).__name__, value)
        return None
    v = float(value)
    if not math.isfinite(v):
        logger.warning("score_ae: %s 가 비유한값 → 채널 0 처리 (value=%r)", field, value)
        return None
    if not (_UNIT_LO <= v <= _UNIT_HI):
        logger.warning("score_ae: %s 가 계약 범위 [0,1] 밖 → 채널 0 처리 (value=%r)", field, v)
        return None
    return v


def _band_points(value: float, edges, points) -> float:
    """오름차순 경계에서 value 이상(`>=`)인 최상위 밴드의 배점. 미달이면 0.0.

    경계 포함 방향은 calib 정의("0.2 = VAL P98.5 = 상위 1.5% 이상")와 일치시킨다.
    """
    pts = 0.0
    for edge, p in zip(edges, points):
        if value >= edge:
            pts = p
    return pts


def score_ae(prediction: dict | None, cfg: ContextScoreConfig) -> float:
    """AE 축(A3) 기여 점수 — anomaly·drift 두 채널을 밴드 매핑 후 max 집계 (순수·부작용 0).

    두 채널은 단일 `ae_raw` 파생이라 선형합산을 금지하고 대표값(max)으로 뭉친다(그룹핑 A3).
    반환값은 이미 A3 그룹 내 집계가 끝난 값이므로, combine 은 A1·A2·A3 **그룹 간** 합산만 한다.
    예측 결측(None·빈 dict)이나 두 채널 모두 미발화면 0.0 — SPC/TTTM 알람 경로를 막지 않는다(★③).

    Args:
        prediction: joined["prediction"] (계약명 dict) 또는 None.
        cfg: 밴드 경계·배점 주입 설정(params.yaml 경유 — 매직넘버 금지).

    Returns:
        AE 축 기여 float (0.0 이상).
    """
    if not prediction:
        return 0.0

    anomaly = _as_unit_interval(prediction.get("anomaly_score"), "anomaly_score")
    drift = _as_unit_interval(prediction.get("drift_score"), "drift_score")

    # e6 — 순간·다변량 이상 (per-wafer ae_score 밴드)
    s_anomaly = 0.0 if anomaly is None else _band_points(
        anomaly, cfg.ae_band_edges, cfg.ae_band_pts)

    # e7 — 완만·누적 드리프트 (per-chamber EWMA 밴드)
    if drift is None:
        s_drift = 0.0
    elif drift > cfg.ae_drift_hi:
        s_drift = cfg.ae_pts_drift_hi
    elif drift > cfg.ae_drift_lo:
        s_drift = cfg.ae_pts_drift_lo
    else:
        s_drift = 0.0

    # 집계 = max 고정. `cfg.ae_aggregate` 를 여기서 분기하지 않는 대신, 지원하지 않는 값은
    # ContextScoreConfig.__post_init__ 이 기동 시점에 실패시킨다(코드리뷰 §3-5 — 조용한 max 방지).
    return float(max(s_anomaly, s_drift))       # 합산 금지 — 이중계상(실측 +72%)
