# =============================================================================
# axes/score_spc.py   —  [담당: B]  Step4  · SPC 축 순수 서브함수 (그룹핑 A1)
# =============================================================================
# 스펙: Docs/spec/B4-1_Step3-4_collector_score_스펙플랜.md §5.
# 역할: Nelson 위반 리스트(joined["violations"]) → SPC 기여 점수(raw float·부작용 0).
#
# [산식]  스트림(챔버×recipe×step×window×센서)별 A1 집계 → 스트림 간 max
#   e1 = max(rule_base[rule_id])          # 룰 점수 합산 금지 — 대표 룰 1개(이중계상 방지)
#   e3 = size_per_sigma × max(초과σ)      # 위반 크기. σ=(UCL−LCL)/2k. σ≤0이면 e3=0(e1 유지)
#   e2 = 0                                # 지속성 v0 보류(무상태론 연속유지 판정 불가·N3가 보상)
#   stream_score = e1 + e3  (캡 없음 — 전역 clip은 combine)
#   score_spc = max(stream_score)         # 위반 0 → 0.0
#
# [규칙] raw 기여(clip·과도할인·타축은 combine) · 매직넘버 cfg · 센서 C코드(6-4) · 순수 · print 금지.
# =============================================================================
from __future__ import annotations

import logging

from src.common.context_score.config import ContextScoreConfig
from src.common.context_score.markers import MARKER_RULE_IDS

logger = logging.getLogger(__name__)

# 부록B 3-2 — 미등록 rule_id 최초 1회만 warning(이후 debug). 로그 전용 캐시라 반환값은 순수 불변.
_WARNED_UNKNOWN_RULES: set = set()


def _stream_key(v: dict) -> tuple:
    """스트림 식별 = (챔버×recipe×step×sensor_window×센서) — control_limits 유니크 키(넬슨 §1)."""
    return (v.get("chamber_id"), v.get("recipe_id"), v.get("step"),
            v.get("sensor_window"), v.get("sensor_id"))


def _exceedance_sigma(v: dict, cfg: ContextScoreConfig) -> float:
    """관리선 초과폭을 σ 단위로 (상·하한 대칭·밴드 내 0). σ=(UCL−LCL)/2k 유도.

    위반 dict엔 σ가 없어 sigma 그룹 밴드(center±kσ)에서 유도한다. **σ 가드(스펙 §5①)**:
    UCL−LCL ≤ 0(퇴화·flat)이면 0 반환(ZeroDivision 회피) — 호출부에서 e3만 0이 되고 e1은 유지.
    분위수 그룹은 밴드가 σ 기반이 아니나 밴드폭 대비라 단조성은 유지(엔진 sigma_ref 동일 한계).
    """
    ucl, lcl, cur = (v.get("control_limit_upper"), v.get("control_limit_lower"),
                     v.get("current_value"))
    if ucl is None or lcl is None or cur is None:   # 계약 위반 dict(키 결측) → 해당 위반만 e3=0(부록B 1-2②)
        logger.warning("score_spc: 위반 dict 관리선/현재값 키 결측 → e3=0 (rule_id=%r)", v.get("rule_id"))
        return 0.0
    band = ucl - lcl
    if band <= 0:
        return 0.0
    sigma = band / (2.0 * cfg.k_sigma)
    exceed = max(cur - ucl, lcl - cur, 0.0)
    return exceed / sigma


def _stream_score(vs: list, cfg: ContextScoreConfig) -> float:
    """단일 스트림 위반들 → e1(대표 룰점) + e3(최대 초과σ×배점). e2=0(v0 보류)."""
    for v in vs:                                    # 미등록/결측 rule_id는 0점 — 조용히 삼키지 말고 경고
        rid = v.get("rule_id")                      # 부록B 1-2② — 직접 인덱싱 대신 .get(결측 무크래시)
        # 합성 마커(B9 등)는 Nelson 룰이 아니라 **알람 종류 표지**라 e1(룰 기본점)을 안 주는 것이
        # 의도다 — 미등록이 아니므로 경고 대상에서 뺀다. 경고로 남기면 "params 실수"로 읽혀
        # rule_base 에 등재될 위험이 있고, 그러면 e1 이중계상이 된다 (markers.py 참조).
        if rid not in cfg.rule_base and rid not in MARKER_RULE_IDS:
            if rid not in _WARNED_UNKNOWN_RULES:    # 부록B 3-2 — 최초 1회만 warning(이후 debug, 로그 스팸 방지)
                _WARNED_UNKNOWN_RULES.add(rid)
                logger.warning("score_spc: 미등록 rule_id=%r → 기본점 0 (params rule_base 확인)", rid)
            else:
                logger.debug("score_spc: 미등록 rule_id=%r → 기본점 0", rid)
    e1 = max(cfg.rule_base.get(v.get("rule_id"), 0.0) for v in vs)
    e3 = cfg.size_per_sigma * max(_exceedance_sigma(v, cfg) for v in vs)
    return e1 + e3                                  # e2(지속성) = 0 (스펙 결정1)


def score_spc(violations: list, cfg: ContextScoreConfig) -> float:
    """Nelson 위반 리스트 → SPC 축 raw 기여 (순수·부작용 0).

    스트림(챔버×recipe×step×window×센서)별로 A1 집계(대표 룰점 + 크기)한 뒤 **스트림 간 결합**.
    violations는 한 wafer의 멀티 센서 위반 전체라, flat 집계하면 한 센서 modifier가 다른 센서
    대표점에 붙어 가짜 super-event(이중계상 §0)가 된다 → 반드시 스트림별. 위반 없으면 0.
    (축 **간** 결합·clip·과도 할인은 combine 담당 — 여기는 SPC 단일 축 raw.)

    **스트림 간 결합 = `s₁ + Σ(나머지) × cfg.stream_damping`** (s₁ = 최대 스트림 점수).
    `stream_damping = 0.0` 이면 **현행 max 와 수학적으로 동일**하다.

    **왜 손잡이인가 (T14 · 2026-08-09)**: max 는 수렴 증거를 버린다 — 다센서 사건(C11·C17·C62
    +4σ)에서 발화 스트림이 평균 **7.96개**(정상 1.02개)인데 **1개만** 점수에 반영돼 첫 게이트
    통과가 **62장 지연**됐다. `d=0.2` 면 **1장**이고 정상 오탐은 0.44%→0.73% 에 그친다.
    집계 변경은 점수 스케일을 바꿔 게이트(`context_score_agent_min`)의 의미도 함께 바꾸므로
    **배점·임계를 세트로 확정**한다(G8). 상세 = `analysis/step6/results/T14_stream_aggregate.md`.

    ⚠️ **스트림 *내부* e1 max(합산 금지)는 이 키와 무관하게 불변**이다 — 이중계상 방지라 별개 결정.
    """
    if not violations:
        return 0.0
    streams: dict[tuple, list] = {}
    for v in violations:
        if not isinstance(v, dict):             # 비-dict 원소(None 등) → skip(§4-4 방어 깊이·total 계약)
            logger.warning("score_spc: 비-dict 위반 원소 skip: %r", v)
            continue
        streams.setdefault(_stream_key(v), []).append(v)
    if not streams:                              # 전부 오염 → 기여 0 (max(빈) ValueError 회피)
        return 0.0
    scores = sorted((_stream_score(vs, cfg) for vs in streams.values()), reverse=True)
    if not cfg.stream_damping:                   # 0.0 — 현행 max 경로(부동소수 연산도 생략)
        return scores[0]
    return scores[0] + sum(scores[1:]) * cfg.stream_damping
