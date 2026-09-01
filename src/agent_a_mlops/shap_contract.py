# =============================================================================
# shap_contract.py   —  [담당: A]  `fdc.prediction.shap_top3` 발행 정렬 계약
# =============================================================================
# 역할: `shap_top3` 를 **발행 시점에 정렬·절단**해 소비자가 순서를 다시 만들 필요가
#       없게 만든다. 순수 함수·stdlib 전용(무거운 import 없음) — 테스트 가능하게 분리.
#
# [왜 발행 측이 보장하나]
#   `fdc.alert`(계약 §3)의 `prediction_context.shap_top3` 는 **센서 C코드 리스트**
#   (`["C11","C62","C17"]`)로 요약된다. `contribution` 이 버려지므로 **순서만이 유일한
#   순위 정보**로 남는다. 소비자(B publisher)가 순위를 복원하려면 원형을 재정렬해야 하고,
#   그 과정의 in-place `sort()` 한 줄이 캐시 원본의 SHAP 순위를 바꾼다 —
#   **헌법 3-3(SHAP 독립 채널: 룰 기반 로직의 순위 개입 금지) 위반이 사고로 발생**한다.
#   발행 측이 순서를 보장하면 소비자는 "읽어서 sensor 만 뽑는다" 외에 할 일이 없다.
#
# [정렬 규칙 — 계약]
#   **|contribution| 내림차순, 상위 3.**
#   - **절댓값 기준**인 이유: 부호는 기여 *방향*(예측을 올림/내림)이고, 순위는 *크기*다.
#     음의 큰 기여(-0.58)가 양의 작은 기여(+0.27)보다 중요한 근거다.
#   - ⚠️ 따라서 `contribution` 원값으로 재정렬하면 **순서가 달라진다**
#     (실증: [-0.58, 0.27, 0.15] → abs 정렬 [C11,C62,C17] vs raw 정렬 [C62,C17,C11]).
#     소비자는 재정렬하지 말고 **발행 순서를 그대로 신뢰**할 것.
#   - 동점은 입력 순서 유지(`sorted` 안정 정렬) — 같은 입력이면 같은 출력(재현성).
#
# [규칙]  순수 함수·부작용 0 · 매직넘버 금지(SHAP_TOP_N) · print 금지 → logging (6-1).
#         센서는 C코드 그대로, 표시명(`name`)은 표시 계층 전용 (6-4).
# =============================================================================
from __future__ import annotations

import logging
import math

log = logging.getLogger("mlops.shap_contract")

SHAP_TOP_N = 3          # 계약 §2 `shap_top3` — 상위 N 센서 (필드명이 N을 고정한다)

_FIELD_SENSOR = "sensor"
_FIELD_CONTRIBUTION = "contribution"


def shap_rank_key(item: dict) -> float:
    """SHAP 순위 키 = `|contribution|`. 부호는 방향이라 순위와 무관하다.

    결측·비수치·비유한값은 `0.0` 으로 떨어뜨려 **최하위**로 보낸다. 발행이 죽는 것보다
    해당 항목이 후순위로 밀리는 편이 낫다(예측 축은 단독 알람 불가한 보조 채널이고,
    SHAP 은 근거 표시용이라 한 건의 오염이 전체 발행을 막을 이유가 없다).
    `bool` 을 먼저 거르는 이유는 `bool` 이 `int` 서브클래스라 `float(True)=1.0` 이
    정상 기여도처럼 통과하기 때문이다.
    """
    if not isinstance(item, dict):
        return 0.0
    c = item.get(_FIELD_CONTRIBUTION)
    if isinstance(c, bool) or not isinstance(c, (int, float)):
        return 0.0
    c = float(c)
    return abs(c) if math.isfinite(c) else 0.0


def normalize_shap_top3(shap) -> list:
    """`shap_top3` 발행형으로 정규화 — |contribution| 내림차순 상위 `SHAP_TOP_N`.

    발행 길목(`build_message`)에서 한 번만 호출한다. 실모델 경로·더미 폴백·향후 추가될
    어떤 산출 경로든 이 함수를 통과하므로 **정렬 보장의 단일 지점**이다.
    이미 정렬된 입력에도 안전하다(멱등, 3원소라 비용 무시 가능).

    Args:
        shap: SHAP 항목 리스트. 각 원소는 `{sensor, name, contribution}` dict.
            None·빈 값·비-list 는 빈 리스트로 흡수한다(발행 중단 금지).

    Returns:
        정렬·절단된 새 리스트. **입력 리스트를 in-place 변경하지 않는다**
        (`sorted` 사용 — 호출측이 넘긴 객체 오염 금지).
    """
    if not shap:
        return []
    if not isinstance(shap, (list, tuple)):
        log.warning("shap_top3 가 리스트가 아님 → 빈 리스트로 발행 (type=%s)",
                    type(shap).__name__)
        return []

    items = [s for s in shap if isinstance(s, dict) and _FIELD_SENSOR in s]
    dropped = len(shap) - len(items)
    if dropped:
        log.warning("shap_top3 원소 %d건이 계약 형식 위반(dict·%s 필요) → 제외",
                    dropped, _FIELD_SENSOR)

    return sorted(items, key=shap_rank_key, reverse=True)[:SHAP_TOP_N]


def is_sorted_by_contribution(shap) -> bool:
    """발행형 정렬 계약 충족 여부 — 검증·테스트·리허설 점검용(부작용 0).

    운영 중 계약 위반을 조기에 잡으려면 발행 직전이나 소비 직후에 이 함수로 단언한다.
    """
    if not shap:
        return True
    keys = [shap_rank_key(s) for s in shap]
    return all(a >= b for a, b in zip(keys, keys[1:]))
