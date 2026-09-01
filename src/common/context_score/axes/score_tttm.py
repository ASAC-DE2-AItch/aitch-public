# =============================================================================
# axes/score_tttm.py   —  [담당: B]  Step4  · TTTM 축 순수 서브함수 (그룹핑 A2)
# =============================================================================
# 스펙: Docs/spec/B4-1_Step3-4_collector_score_스펙플랜.md §6.
# 역할: TTTM chamber_rollup → TTTM 기여 점수(raw float·부작용 0).
#
# [산식]  A2 = e4 − e5  (같은 산식 앞뒷면 → net)
#   e4 : score(=|gap_σ|) ≥ critical(3.0) → critical_pts(30) / ≥ warning(2.0) → warning_pts(15) / 그 외 0
#   e5 : reference_suspect=true → reference_suspect_penalty(20) 감점
#   반환 A2 = e4 − e5  (음수 허용 — 참조 오염이 총점을 끌어내림. 전역 clip은 combine)
#
# [규칙] 임계(warning 2.0·critical 3.0)는 기존 spc 키 재사용 · 매직넘버 cfg · 순수 · print 금지.
# =============================================================================
from __future__ import annotations

from src.common.context_score.config import ContextScoreConfig


def score_tttm(tttm: dict | None, cfg: ContextScoreConfig) -> float:
    """TTTM chamber_rollup → TTTM 축 raw 기여 (순수·부작용 0).

    e4(밴드 가점) − e5(reference_suspect 감점)의 net. 감점이 가점보다 크면 음수가 될 수 있고
    (참조 오염 신호가 총점을 끌어내림), 전역 0~100 clip·floor는 combine(공동)이 책임진다.
    rollup 결측(None·빈 dict, warmup 미충전)은 0.
    """
    if not tttm:
        return 0.0
    raw = tttm.get("score")
    # 비수치(str 등)·bool·결측은 0 취급 — `>=` 비교 TypeError 방지(§4-3 total 계약·방어 깊이)
    score = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
    if score >= cfg.tttm_critical:
        e4 = cfg.tttm_critical_pts
    elif score >= cfg.tttm_warning:
        e4 = cfg.tttm_warning_pts
    else:
        e4 = 0.0
    e5 = cfg.reference_suspect_penalty if tttm.get("reference_suspect") else 0.0
    return e4 - e5
