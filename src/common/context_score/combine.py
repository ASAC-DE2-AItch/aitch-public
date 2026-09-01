# =============================================================================
# combine.py   —  [담당: 공동]  Step4  · 축 점수 결합기
# =============================================================================
# 스펙: Docs/B4-1_Step4_5_5b_통합구현계획서_v2.md D-1·D-2·D-3 (원 스펙은
#       Docs/spec/_archive/B4-1_Step4_공통_orchestrator_combine_스펙플랜.md D1·D3).
#
# 역할: 네 축 기여를 하나의 raw 점수로 결합한다. "교체는 여기서만" 이뤄지고,
#       결합 시그니처는 불변이다 (계획서 §2, §4-②).
#
# [진화 경로]
#   naive_sum  →  grouped_2stage  (상관분석 §6 결과로 grouping·가중치 반영)
#   → 결합 방식이 바뀌어도 combine 의 입출력 시그니처와 축 서브함수 시그니처는
#     불변이라, A·B 의 축 함수는 건드리지 않는다.
#
# [시그니처(고정 계약)]  ※ D-1 개정 2026-07-28: list → **dict**
#   def combine(scores: dict[str, float], cfg) -> float
#   * 키는 AXIS_KEYS = ("spc", "tttm", "ae", "pred") — 오케스트레이터가 채운다.
#   * dict 인 이유: 인덱스 순서 실수로 인한 **침묵 오류**를 제거하고,
#     Step 6 grouped_2stage 가 그룹·가중치를 **이름으로** 접근하기 위함.
#
# [작성 가이드]
#   - 1단계(naive_sum): 단순 합. **combine-level 가중치 없음**(가중은 축 내부에서
#     이미 반영 — 룰 기본점·밴드 배점 등). 근거: 계획서 D-1.
#   - 2단계(grouped_2stage): analysis/ 상관분석 산출(grouping·가중치)을 주입.
#     · 그룹 내 결합 → 그룹 간 결합의 2단 구조. **이 함수 내부만 교체**(키·축 불변).
#   - 가중치·그룹 정의는 하드코딩 금지 → params.yaml (근거: 상관분석 문서).
#
# [규칙]
#   - 공동 파일. 시그니처 변경은 A·B 합의 필수.
#   - clip(clip_min~clip_max)·C42 과도 할인 같은 후처리는 여기 아니라
#     context_score.py(오케스트레이터) 책임 (D-2). combine 은 결합 전담.
#   - docstring 필수. print 금지 → logging.
# =============================================================================
from __future__ import annotations

# 축 키 고정 계약 — 오케스트레이터가 채우고 combine·계측이 읽는다.
#   순서는 표시·로그 안정성을 위한 것이며, naive_sum 은 순서에 의존하지 않는다.
AXIS_KEYS: tuple[str, ...] = ("spc", "tttm", "ae", "pred")


def combine(scores: dict[str, float], cfg) -> float:
    """축 기여 dict → 결합된 raw 점수 (naive_sum · 순수·부작용 0).

    v0 결합은 **단순 합**이다 (D-1·D-3). combine-level 가중치를 두지 않는 이유는
    가중이 이미 축 내부에 반영돼 있기 때문이다(룰 기본점·TTTM 밴드 가점·AE 밴드 배점).
    여기서 또 곱하면 같은 조정이 두 곳에 흩어져 튜닝 지점이 이원화된다.

    후처리(과도 할인·clip)는 **하지 않는다** — 오케스트레이터 책임(D-2). 그래야
    Step 6 에서 결합을 `grouped_2stage` 로 갈아끼울 때 후처리가 딸려가지 않는다.

    Args:
        scores: 축 기여 dict. 키 = `AXIS_KEYS`(spc·tttm·ae·pred). 값은 축이 낸
            raw float 이며 **음수일 수 있다**(score_tttm 의 e4−e5 net).
        cfg: 배점 설정(ContextScoreConfig). v0 naive_sum 은 사용하지 않으나,
            Step 6 grouped_2stage 가 가중치·grouping 을 읽을 자리라 시그니처에 유지한다.

    Returns:
        결합된 raw float. 빈 dict 는 0.0.
    """
    return float(sum(scores.values()))
