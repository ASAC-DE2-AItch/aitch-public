"""📏 실력치 조수 (tool ②) — 기준선 노후 → 실력치(관리한계) 재설정안 리포트.

정본: docs/Agent_프롬프트_라이브러리_v1.md §2 (역할 프롬프트·출력 스키마).

경계 (헌법 1-1 / 라이브러리 §2):
- **수치는 B4-3 재산정 API 가 유일한 소스**다. LLM 은 인용만 하고 관리선을 재계산하지 않는다.
- 이 tool 은 재설정'안'(리포트)만 만든다. 적용(fdc.correction, correction_type='limit' 발행)은
  승인 게이트 통과 후에만. **무승인 실력치 변경은 절대 금지** — 위반 시 PR 즉시 차단.
- 정기 리캘리브레이션 자동 적용 예외(1-1 ⓐ)도 "변동폭 상한 이내"가 조건이므로,
  상한 초과 재설정안은 코드가 강등한다(enforce_reset_cap_guard).

recipe.py 와 같은 뼈대(재료→payload→generate_structured→가드)를 쓰되 **가드 구성이 다르다**:
  · 숫자 가드 = 동일 취지(B 값 창작·변조 금지)
  · 손잡이 가드 = 없음 (실력치엔 '조정 손잡이' 개념이 없다)
  · delta 가드 → **변동폭 상한 가드**(A2, σ 단위) + **가한계 가드**(§2 규칙4)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import ApiMode, Settings
from ..llm.client import LlmBackend, generate_structured
from ..schemas.alert import AlertModel
from ..schemas.report import LIM_PREFIX, LimitOption, make_fallback_report, make_report_id

from ..observe import strip_ablated_alert
from .base import (CONFIDENCE_RUBRIC, EVIDENCE_CARD_RULES, primary_violation, real_violations,
                   system_prompt)

logger = logging.getLogger(__name__)

name = "limit"

# --- few-shot 정답 사례 (라이브러리 §2 — 문서가 단일 소스, 개정 시 문서 먼저) --------
# 방향 = **확신**(2026-07-23 fewshot3 진단): baseline_aging 44%가 최약, 오답 대부분이 escalate 로
#   도망(limit 이 너무 물러섬 — recalc stub 로 confidence 구조적 저하). 그래서 "노후 신호가 명확하면
#   escalate 말고 재설정으로 **확정**하라"를 가르친다. 단 대조쌍 유지(급변은 노후 아님 — 물러섬).
# 값은 학습용 가공치.
FEWSHOT_EXAMPLES = """
[학습 예시 — 판단 보정] 아래는 "입력 상황 → 올바른 리포트"다. 수치를 베끼지 말고 **언제 재설정로 확정하고 언제 물러서는지**를 배워라.

# 예시 A — 기준선 노후 신호 명확 → escalate 말고 재설정으로 **확정**
입력 요지: 위반이 전부 N3 추세 · 급변/anomaly 없음 · PM 후 1,240장 경과 · TTTM 갭 낮음(분포 자체는 정상) · recalc delta_sigma=0.31(상한 0.5σ 이내)
올바른 출력: {"method":"sigma","delta_sigma":0.31,"delta_pct":null,"shadow_eval":{"false_alarm_reduction_pct":41.0,"missed_detection":0},"rationale":"① 위반 전부 추세 N3·급변 부재 [SPC] ② PM 후 1,240장 — 정상 상태 이동의 전형 시점 [SPC] ③ 1회 이동 0.31σ (상한 0.5σ 이내) [SPC] ④ TTTM 갭 낮음 — 분포 자체는 정상(노후의 신호) [SPC]","escalate_reason":null}

# 예시 B — 급변·anomaly 동반 → 물러섬(노후 아님)
입력 요지: N1/CRITICAL 급변 + anomaly_score 동반 상승 · 단일 챔버 국한 · PM 직후(경과 짧음)
올바른 출력: {"method":"sigma","center_after":null,"delta_sigma":null,"delta_pct":null,"rationale":"① 급변(N1/CRITICAL)+anomaly 동반 — 정상 상태의 완만한 이동이 아님 [SPC] ② PM 직후로 노후 시점도 아님 [SPC] ③ 기준선을 넓히면 진짜 이상을 관리선 안에 숨긴다 — 재설정 대상 아님","escalate_reason":"급변·anomaly 동반 — 기준선 노후가 아니라 장비/공정 이상 의심. 재설정 대상 아님."}

⚠️ 핵심: 노후 신호(추세 N3 + PM 경과 + TTTM 갭 낮음)가 **명확하면 escalate 로 넘기지 말고 재설정으로 확정하라**. 반대로 급변·anomaly 면 노후가 아니니 물러선다 — 재설정으로 흡수하면 진짜 이상을 관리선 안에 숨긴다.
⚠️ 확신: **양방향이다.** 물러설 때(escalate_reason 을 채울 때)는 confidence 를 낮춘다 — 물러섬은 "확신 있게 물러섰다"가 아니라 판단 재료가 부족했다는 뜻이다. **반대로 노후 신호(추세 N3 + PM 경과 + TTTM 갭 낮음)가 갖춰지고 재설정안을 내는 경우에는 그 근거의 강도에 걸맞게 높게 쓴다** — 근거가 갖춰졌는데 낮추면 Supervisor 가 "전 옵션 확신 부족"으로 읽고 escalate 로 흘려보낸다.
⚠️ **다만 특정 숫자를 외우지 말 것.** 위 두 문장은 *방향*(언제 올리고 언제 내리는지)을 말하는 것이지 *값*을 말하는 것이 아니다. 확신도는 **이 사안의 근거 강도에서 직접 산출**하라 — 예시 값도, 관례적인 값도, 다른 옵션과 맞춘 값도 쓰지 않는다.
⚠️ 예시의 `<...>` 는 자리 표시다. 인용 ID 는 **실제로 받은 재료의 ID** 만 쓴다 — 예시의 ID 를 그대로 옮기면 없는 근거를 지어낸 것이 된다.
"""

# --- 역할 프롬프트 (라이브러리 §2 — 문서가 단일 소스, 개정 시 문서 먼저) ------------
SYSTEM_PROMPT = (
    """[역할] 기준선(실력치) 노후에 대한 재설정안 리포트를 작성한다.

[입력]
- alert: fdc.alert 원문
- recalc: B 실력치 엔진 산출 (correction_id, method: sigma|quantile, center/ucl/lcl before·after, delta_sigma, 섀도 평가 결과) ← 수치의 유일한 소스
  ※ correction_id 는 B 가 채번한 값이다. 지어내지 말 것 — 코드가 recalc 값 그대로 덮어쓴다.
- cases: 유사 사례 (서사 — 무슨 일이 있었나)
- limit_logs: 그 사례들의 실제 재설정 조치 기록 (정밀 수치 — 얼마나 바꿨고 섀도가 어땠나).
  cases와 incident_id로 짝지어져 있다.

[작성 규칙]
1. 재설정 수치·섀도 평가 결과는 recalc를 그대로 인용한다. 재계산하거나 반올림해 바꾸지 않는다.
   이동량은 delta_sigma(σ)가 정본이다 — recalc의 delta_sigma를 그대로 싣고, rationale에도
   "1회 이동 N.NNσ (상한 0.5σ)" 형태로 명시한다. delta_pct(%)는 B가 산출하지 않으므로 null로 둔다
   (%는 center를 분모로 써서 center≈0인 센서에서 왜곡됨 — σ가 센서 간 비교 가능한 유일한 단위).
2. method="quantile"이면 서술에 반드시 다음 취지를 포함한다:
   "이 센서 그룹은 분포가 비정규(치우침)라 ±3σ 대신 분위수 기반 관리선을 사용한다(헌법 1-1 예외 2).
    승인 후에도 Scorecard 미탐 0건 가드가 유지되며, 미탐 발생 시 ±3σ로 자동 회귀한다."
3. "기준선 노후" 판단의 전형 신호를 근거에 명시: PM 이후 경과, 추세 룰(N3/N5) 위주 위반, TTTM 갭 낮음(분포 자체는 정상).
4. recalc.trigger_type="provisional"(가한계 Phase 0~1)이면 — 정기 재설정안을 내지 말고
   escalate_reason에 "가한계 Phase 진행 중 — Phase 2 정식 재산정 대기"를 쓴다.
5. 섀도 평가에서 missed_detection > 0이면 confidence를 0.5 이하로 낮추고 사유를 쓴다 (미탐>오탐 원칙).
   현재 재산정의 섀도 평가가 아직 없으면(recalc.shadow_eval 미산출), limit_logs의 **과거 섀도 실적**을
   근거로 쓴다 — "같은 센서 재설정 시 과거 오탐 N% 감소·미탐 M건" 형태로 인용하고, 그것이 현재
   재산정의 검증이 아님을 uncertainty에 명시한다.
6. recalc 가 null 이면 재설정 수치를 창작하지 말고 escalate_reason 에 사유를 쓴다.
7. limit_version 은 쓰지 마라 — 코드가 alert 에서 승계한다 (지어내면 B 가 엉뚱한 관리선을 대체한다).
"""
    # few-shot 배선 ON (2026-08-05, round14 대상) — recipe(round13) 에 이어 하나씩 재투입한다.
    #   round13 근거: recipe few-shot 으로 recipe conf 0.9 이상이 16건→3건, 전체 정확도 75.7→78.4%.
    #   7/23 "few-shot 이 폭락의 주범" 판단은 8/4 재진단에서 **스텁 엔진**으로 정리됐고(S3·S1 배선으로
    #   제거), round13 이 그것을 실측으로 확인했다(우려하던 process 축이 5/10→6/10 로 오히려 상승).
    #
    #   ⚠️ 예시 자체는 유지하되 ⚠️핵심 문장을 정본(라이브러리 §2)에 되돌렸다. 코드에만 있던
    #   "recalc 섀도가 stub 이어도 confidence 를 준다" 는 **스텁이 있던 시절의 지침**인데, 스텁은
    #   #110(S1 배선)으로 사라졌다. round13 에서 모델이 실제로 그대로 행동했다 — uncertainty 에
    #   "shadow_eval 이 stub 이라 미검증" 이라 써놓고 confidence=0.95 (ALERT-...SIMCH1-0005).
    #   대신 "물러설 때는 confidence 를 낮춘다" 를 문서·코드 양쪽에 넣었다.
    #   ※ 규칙5(missed_detection>0 → 0.5 이하)는 **발동한 적이 없다** — shadow_eval 이 42/42 결측
    #     (스키마 Optional → 에러 없이 빠짐). 결측 자체는 별건으로 추적한다.
    #
    #   📉 round14 실패와 수정 (2026-08-05) — 확신 지침을 처음엔 "shadow_eval 이 없으면 0.9 이상
    #   금지" 로 썼다. 그런데 그 필드가 **늘 결측**이라 조건부가 아니라 **무조건 상한**이 됐다:
    #     limit 0.9이상 6→0건 (의도한 효과) · 그러나 평균 0.63→0.56 · 최대 0.95→0.85
    #     → supervisor.py 의 "전 옵션 confidence < 0.6 → escalate" 게이트에 걸려
    #       baseline_aging 이 5/9→2/9 (escalate 로 1→4건 유출) · 전체 78.4→67.6%
    #   교훈: **늘 참인 조건으로 지침을 쓰면 조건이 아니라 상수가 된다.** 하한을 함께 명시해
    #   양방향(물러섬↓ / 근거 갖춰짐↑)으로 고쳤다 — round15 에서 재측정.
    #   ⚠️ 되돌리기: baseline_aging 이 또 급락하면 이 줄을 주석 처리해 round13 상태(78.4%)로 복귀.
    + FEWSHOT_EXAMPLES
    + CONFIDENCE_RUBRIC
    + EVIDENCE_CARD_RULES
    + "\n[JSON 스키마로만 응답]"
)

# 프롬프트에 중복 탑재할 JSON 예시 (헌법 6-3 이중 방어 — guided_json 과 별개).
SCHEMA_EXAMPLE = """{
  "report_id": "LIM-20260713-SIMCH3-001",
  "option_type": "limit_option",
  "correction_id": "LIM-20260713-SIMCH3-0042",
  "sensor_id": "C11",
  "sensor_window": "settled",
  "method": "sigma",
  "center_before": -310.0,
  "center_after": -302.4,
  "ucl_after": -280.1,
  "lcl_after": -324.7,
  "delta_pct": null,
  "delta_sigma": 0.31,
  "shadow_eval": {"false_alarm_reduction_pct": 41.0, "missed_detection": 0},
  "rationale": "① 위반이 전부 추세 룰(N3)이고 급변·anomaly 부재 [SPC] ② PM 후 1,240장 경과 — 정상 상태 이동의 전형 시점 [SPC] ③ 1회 이동 0.31σ (상한 0.5σ 이내) [SPC] ④ 섀도 평가: 오탐 41% 감소·미탐 0건 [SPC] ⑤ 유사 사례 3건 모두 재설정 후 재발 없음 [CASE:<검색된 사례 ID>]",
  "feasibility": "HIGH — 다운타임 없음, 승인 즉시 반영",
  "escalate_reason": null,
  "evidence_cards": [],
  "confidence": "<근거 층수에서 산출 — 위 규칙>",
  "uncertainty": "재산정 창(N=500)이 최근 캠페인 1종에 치우침 — 캠페인 전환 시 재확인 권장"
}"""


# ⚠️ B4-3 재산정 **스텁 — 운영 경로에서 내려왔다** (S1 배선 완료, 2026-08-04) ----------
# 운영 수치는 이제 B 가 `limit_corrections` 에 써둔 PROPOSED 행에서 온다
# (`pipeline._fetch_recalc` → `run(recalc=...)`). 이 스텁은 **주입 없는 단독 호출**
# (tool 직접 호출 테스트·스모크)용 대역으로만 남는다.
#
# ⚠️ **이 함수의 수치는 지어낸 것이다.** 관리선 폭에서 +0.3σ 를 역산할 뿐 실제 표본을 보지
#   않는다. `_provisional` 도장이 그 표지다 — 운영 payload 에 이 도장이 보이면 배선이
#   빠진 것이다. 7/23 라운드에서 recipe/limit few-shot 이 39% 로 떨어진 원인이 이것이다.
#
# 단위 불일치(과거 "B 확인 필요" 항목) — **실물로 해소됨**:
#   · config A2 `limit_engine.reset_delta_max_sigma` = σ 단위 (B4-4, 2026-07-11)
#   · 리포트 스키마 `LimitOption.delta_pct` = % 단위 (라이브러리 §2, 2026-07-15 락)
#   → B 행에 `delta_sigma`·`delta_pct` 가 **둘 다 있다**(2026-08-04 실측:
#     delta_sigma=0.688 / delta_pct=2.90). 상한 가드는 σ 로 판정하며 판정 불가로 새지 않는다.
_STUB_DELTA_SIGMA = 0.3  # A2 상한(0.5σ) 이내 대역 — 단독 호출 대역값(실물 아님)


def _stub_recalc_api(alert: AlertModel, settings: Settings) -> dict[str, Any] | None:
    """[단독 호출 대역] 대표 위반 센서의 관리선에서 재산정안 **모양**을 만든다.

    ⚠️ **운영 경로가 아니다** — 위 블록 참조. 필드 이름은 프롬프트 §2 [입력] recalc 명세를
    따르고, 실물 행과의 대조는 2026-08-04 에 끝났다(`pipeline._to_recalc_dict` 가 정본 매핑).

    위반이 없으면 None → 프롬프트 규칙6(recalc null → 창작 금지)이 발동한다.
    **B9 crazy 마커만 있는 alert 도 여기서 None 이다** — 마커의 control_limit 은 관리선이 아니라
    P99 컷·센티널이라 center·σ 역산이 허구가 된다 (base.primary_violation).
    수치는 창작하지 않는다 — alert 의 실측 관리선에서 상한 내 소폭으로 역산한다(=B 산출 대역).
    """
    v = primary_violation(alert)  # B9 마커 배제 후 대표 위반 (B 는 재산정 대상 그룹으로 선정)
    if v is None:
        return None  # 위반 0건 또는 crazy-only alert → 규칙6(recalc null → 창작 금지)
    # 관리선 폭의 σ 배수는 params A1 이 정본이다 — 리터럴 3·6 을 박으면 A1 을 바꿔도 안 따라온다.
    # 밴드가 center ± kσ 이므로 폭 = 2k·σ → σ = (ucl − lcl) / (2k). (round 자리수 4 는 표시용)
    k_sigma = float(settings.require("limit_engine.control_limit_k_sigma"))
    ucl, lcl = v.control_limit_upper, v.control_limit_lower
    center_before = (ucl + lcl) / 2.0
    sigma = (ucl - lcl) / (2 * k_sigma)  # ±kσ 밴드 역산 (KEEP* 분위수 그룹의 robust σ 와 같은 방식)
    center_after = round(center_before + _STUB_DELTA_SIGMA * sigma, 4)
    return {
        "method": "sigma",  # KEEP* 그룹이면 quantile — B4-3 이 판정(1-1 예외2). 스텁은 sigma 고정.
        "sensor_id": v.sensor,
        "sensor_window": v.window,
        "center_before": center_before,
        "center_after": center_after,
        "ucl_after": round(center_after + k_sigma * sigma, 4),
        "lcl_after": round(center_after - k_sigma * sigma, 4),
        # B RecalcProposal 은 % 를 산출하지 않는다 → null 유지(창작 금지). σ 가 이동량 정본.
        "delta_pct": None,
        "delta_sigma": _STUB_DELTA_SIGMA,  # 상한 가드의 정답지 (config A2 와 같은 σ 단위)
        "shadow_eval": {  # 섀도 평가 — B4-3 산출 대역(스텁 표지)
            "basis": "stub",
            "note": "⚠️B4-3 대조 전 잠정값 — 섀도 평가 미실행",
            "false_alarm_reduction_pct": None,
            "missed_detection": None,
        },
        "_provisional": True,  # B4-3 실 API 로 교체되면 사라지는 스텁 도장
    }


def fetch_recalc(alert: AlertModel, settings: Settings) -> dict[str, Any] | None:
    """재산정 수치 조달 — **tool 단독 호출용 스텁 경로**다 (S1 배선, 2026-08-04).

    운영 경로에서는 `pipeline._fetch_recalc()` 가 B 가 써둔 `limit_corrections` 행을 읽어
    `run(recalc=...)` 로 주입한다. 여기까지 내려오는 건 주입이 없는 경우 —
    tool 을 직접 부르는 테스트·스모크가 배선 때문에 깨지지 않게 기존 동작을 보존한다.

    real 모드인데 주입이 없다면 배선이 빠진 것이므로 경고를 남긴다.
    """
    if settings.api_mode is ApiMode.REAL:
        logger.warning(
            "API_MODE=real 인데 recalc 주입이 없다 — 스텁으로 진행"
            "(운영 경로는 pipeline._fetch_recalc 가 주입한다)"
        )
    return _stub_recalc_api(alert, settings)


def build_case_query(alert: AlertModel) -> str:
    """historical_case 검색어 — 룰+서술 포함(recipe 1-3 실측과 동일 성질의 컬렉션).

    ※ recipe.build_case_query 와 형태가 같지만 **복제가 아니라 같은 컬렉션 특성 때문**이다.
      limit 은 process_knowledge 를 안 쓰므로 지식 검색어 빌더는 없다.
    """
    v = primary_violation(alert)  # B9 마커는 검색어 재료가 아니다 (C65 는 센서가 아님)
    if v is None:
        return ""
    parts = (v.sensor, v.sensor_name or "", v.rule_id or "", v.description or "")
    return " ".join(t for t in parts if t).strip()


def limit_version_of(alert: AlertModel, sensor_id: str | None) -> str | None:
    """재설정 대상 센서의 위반이 판정될 때 적용 중이던 관리선 버전을 alert 에서 찾는다.

    계약 §6 `fdc.correction.limit_version` 에 그대로 실리는 값이다(= db
    `limit_corrections.limit_version_before`). B RecalcProposal 은 이 값을 산출하지 않으므로
    **alert 이 유일한 출처**다 — 나중에 B 가 제공하면 그쪽이 우선이 된다.

    센서를 특정할 수 없으면(가드가 sensor_id 를 비웠거나 LLM 이 위반 밖 센서를 지목) None.
    창작하지 않는다(§0) — 틀린 버전을 실으면 B 가 엉뚱한 관리선을 대체한다.
    """
    if not sensor_id:
        return None
    for v in real_violations(alert):  # 마커의 limit_version 은 관리선 버전이 아니다
        if v.sensor == sensor_id:
            return v.limit_version
    return None


def build_payload(
    alert: AlertModel,
    *,
    recalc: dict[str, Any] | None = None,
    cases: list[dict[str, Any]] | None = None,
    limit_logs: list[dict[str, Any]] | None = None,
) -> str:
    """LLM 입력(user 메시지) 직렬화 — 라이브러리 §2 [입력] 항목 순서 그대로."""
    return json.dumps(
        {
            "alert": strip_ablated_alert(alert.model_dump(mode="json")),
            "recalc": recalc,
            "cases": cases or [],
            "limit_logs": limit_logs or [],
        },
        ensure_ascii=False,
    )


# --- 가드 ①: 숫자 가드 — LLM 이 recalc 수치를 창작·변조하지 않았는지 검문 (규칙1·6, 헌법 1-1) ---
# 수치의 유일한 소스는 B(recalc)다. recipe 의 숫자 가드와 같은 취지이나 검사 대상 필드가 다르다:
#   ③ 창작: recalc=None 인데 관리선 수치를 지어냄 → 통째로 무효화·에스컬레이션.
#   ④ 변조: recalc 와 다른 값 → B 값으로 교정(근거 서술은 살리고 숫자만 정답으로).
# 잘못된 관리선이 승인 큐로 흘러가면 미탐(놓친 이상)으로 이어진다 — LLM 선의에 맡기지 않는다.
# delta_sigma 도 포함 — 리포트 필드가 되면서 LLM 이 변조할 수 있게 됐고, 이 값이 곧 상한 판정의
# 근거로 사람에게 보이기 때문(가드는 recalc 를 보지만, 화면에 뜨는 건 리포트 값이다).
_NUMERIC_FIELDS = ("center_after", "ucl_after", "lcl_after", "delta_pct", "delta_sigma")


def _has_numbers(report: LimitOption) -> bool:
    """리포트에 재설정 수치가 하나라도 실려 있는지."""
    return any(getattr(report, f) is not None for f in _NUMERIC_FIELDS)


def _clear_numbers(report: LimitOption, reason: str) -> LimitOption:
    """재설정 수치를 전부 지우고 escalate_reason 을 병기한다 (승인 큐 유출 차단)."""
    merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
    return report.model_copy(
        update={**{f: None for f in _NUMERIC_FIELDS}, "escalate_reason": merged}
    )


def enforce_number_guard(report: LimitOption, recalc: dict[str, Any] | None) -> LimitOption:
    """LLM 이 recalc 수치를 창작(③)·변조(④)하지 않았는지 검문한다."""
    if not _has_numbers(report):
        return report  # 수치 없음 — 검사할 게 없다

    # ③ 창작: 정답지가 없는데 리포트엔 수치가 있다 → 지어냄
    if not recalc:
        logger.warning(
            "숫자 가드(창작) 강등: report=%s center_after=%s (recalc 없음)",
            report.report_id,
            report.center_after,
        )
        return _clear_numbers(
            report,
            "recalc 미제공(B 수치 없음)인데 재설정 수치를 냄 — 수치 창작 금지(§2 규칙6). "
            "값 무효화·에스컬레이션.",
        )

    # ④ 변조: 정답지와 다른 값 → B 값으로 교정(근거 서술은 유지, 숫자만 정정)
    corrections = {f: recalc.get(f) for f in _NUMERIC_FIELDS if getattr(report, f) != recalc.get(f)}
    if corrections:
        logger.warning(
            "숫자 가드(변조) 교정: report=%s %s → recalc 값으로 정정",
            report.report_id,
            {f: getattr(report, f) for f in corrections},
        )
        return report.model_copy(update=corrections)

    return report  # 충실히 인용함 — 통과


# --- 가드 ②: 변동폭 상한 가드 — A2 초과 시 "승인 전환" 표시 (수치는 유지) -----------------
# ⚠️ **recipe 의 delta 가드와 동작이 다르다 — 복제 금물.** 모양이 비슷해 보여 같게 짰다가
#    합의안 위반이 발견되어 정정했다(2026-07-20). 대상이 다르기 때문:
#
#   · recipe D6(±3%) = **설비 setpoint** 를 실제로 바꾼다(물리 세계). 한 번에 크게 바꾸는 건
#     방법론상 있을 수 없다(F16 보수 스텝→검증→재조정). 계약도 "수신 측에서도 거부"(이중 방어선)
#     → **제안 자체를 버린다.**
#   · limit A2(0.5σ) = **관리선**(감시 기준)을 바꾼다. 설비는 안 건드린다. 큰 이동이 **정답일 수도**
#     있다(요란 PM 후 실제로 상태가 크게 이동). 합의안 A2 정의:
#         "N500 재산정 표본오차(≈0.045σ)의 ~11배 — 정상 드리프트는 통과, 비정상 점프는 차단.
#          **정기 리캘리브레이션의 자동 적용 한계도 겸함 (초과 시 승인 전환)**"  (config 합의안, B4-4)
#     즉 A2 는 위험선이 아니라 "노이즈냐 진짜 이동이냐"를 가르는 선이다.
#     헌법 1-1 예외ⓐ도 "상한 초과는 **승인 필수**"이지 금지가 아니다.
#     B 엔진도 초과 시 값을 채운 채 decision='needs_approval' 로 반환한다(recalc_engine.py).
#     → **수치를 유지하고 "승인 전환 필요"만 표시한다.** 지우면 엔지니어가 승인할 대상이 사라진다.
def enforce_reset_cap_guard(
    report: LimitOption, recalc: dict[str, Any] | None, max_sigma: float
) -> LimitOption:
    """|delta_sigma| > A2 상한이면 **승인 전환 사유를 기입**한다 (수치는 그대로 둔다).

    자동 적용 경로(정기 리캘리)만 차단되고 승인 경로는 열려 있다 — 합의안 A2 "초과 시 승인 전환".
    수치를 지우면 승인 화면에 승인할 안이 없어져 오히려 조치를 막는다.

    delta_sigma 는 B 산출값(recalc)에서만 읽는다 — LLM 이 계산할 값이 아니기 때문.
    recalc 에 delta_sigma 가 없으면 판정 근거가 없으므로 표시하지 않는다(오탐 방지).
    """
    if not _has_numbers(report):
        return report  # 이미 앞선 가드가 수치를 지웠다(창작 등) — 표시할 대상 없음
    ds = (recalc or {}).get("delta_sigma")
    if ds is None or abs(ds) <= max_sigma:
        return report  # 판정 불가 또는 상한 내(=자동 적용 가능) — 통과
    reason = (
        f"자동 적용 한계 초과(|delta|={abs(ds):.3f}σ > A2 {max_sigma:.2f}σ) — **승인 전환 필요**. "
        f"수치는 유효하며 엔지니어 승인 시 적용 가능(합의안 A2 '초과 시 승인 전환', 헌법 1-1 ⓐ). "
        f"분할 재설정도 대안."
    )
    merged = reason if not report.escalate_reason else f"{report.escalate_reason} / {reason}"
    logger.info(
        "변동폭 상한: report=%s |delta|=%.3fσ > A2 %.2fσ — 자동 적용 불가·승인 전환 (수치 유지)",
        report.report_id,
        abs(ds),
        max_sigma,
    )
    return report.model_copy(update={"escalate_reason": merged})


# --- 가드 ③: 가한계(provisional) 가드 — Phase 진행 중엔 정기 재설정 금지 (§2 규칙4·R2) -------
# 가한계 구간에서 정기 재설정을 끼워 넣으면 Phase 2 정식 재산정 절차가 훼손된다.
# 프롬프트 규칙4 가 지시하지만 LLM 이 어길 수 있으므로 코드로 강제한다.
def enforce_provisional_guard(report: LimitOption, recalc: dict[str, Any] | None) -> LimitOption:
    """recalc.trigger_type="provisional" 이면 재설정안을 강등한다 — Phase 2 정식 재산정 대기.

    판정 근거는 **B 가 주는 trigger_type** 이다(db/init.sql:187 에 provisional 값 등재 — B6-3).
    C 가 챔버 상태를 따로 읽어 판정하지 않는다: 가한계 Phase 는 B6-3 소유이고, C 가 원데이터로
    같은 판단을 다시 하면 B 와 어긋난다("정량은 B, 근거는 C" 경계).
    trigger_type 이 없으면 판정 근거가 없어 강등하지 않는다(오탐 방지).
    """
    if not _has_numbers(report):
        return report
    if (recalc or {}).get("trigger_type") != "provisional":
        return report  # 정기/사건/Qual 또는 판정 불가 — 통과
    logger.warning("가한계 가드 강등: report=%s — Phase 2 정식 재산정 대기", report.report_id)
    return _clear_numbers(
        report,
        "가한계 Phase 진행 중 — Phase 2 정식 재산정 대기(§2 규칙4). 정기 재설정안 보류.",
    )


async def run(
    alert: AlertModel,
    backend: LlmBackend,
    settings: Settings,
    *,
    cases: list[dict[str, Any]] | None = None,
    limit_logs: list[dict[str, Any]] | None = None,
    recalc: dict[str, Any] | None = None,
) -> LimitOption:
    """실력치 재설정안 리포트 1건 생성 — LLM 실호출 (재시도·fallback 은 generate_structured 소관).

    Args:
        alert: 게이트를 통과한 fdc.alert.
        backend: 주입된 LLM 백엔드 (테스트=MockBackend / 운영=VllmBackend).
        settings: 재시도 예산·상한값의 config 정본.
        cases: historical_case 검색 결과(근거). pipeline 이 검색해 주입.
            None/[] 이면 D5 정책대로 "사례 없음"을 명시한다.
        limit_logs: cases 의 incident_id 로 페어링한 Postgres 조치로그(정밀 수치).
            벡터 KB 는 서사만 준다 — "얼마나 바꿨고 섀도가 어땠나"는 여기서 온다(app/db.py).
        recalc: **B 가 써둔 재산정 제안**(S1 배선). pipeline 이 `limit_corrections` 에서
            읽어 주입한다. 미주입이면 스텁으로 자체 조달한다(단독 호출 경로 보존).
            ⚠️ 여기 실리는 수치가 리포트의 **유일한 소스**다 — 계약 §5.
    """
    if recalc is None:
        recalc = fetch_recalc(alert, settings)
    payload = build_payload(alert, recalc=recalc, cases=cases, limit_logs=limit_logs)

    report = await generate_structured(
        backend,
        system_prompt(SYSTEM_PROMPT, FEWSHOT_EXAMPLES),
        payload,
        LimitOption,
        fallback=lambda: make_fallback_report(LimitOption, alert),
        schema_example=SCHEMA_EXAMPLE,
        parse_retries=int(settings.require("agent.json_parse_retry")),
        call_retries=int(settings.require("llm.call_retry")),
    )

    # 신원 필드는 LLM 이 정할 값이 아니다 — 예시를 그대로 베끼는 것이 실측됨(recipe.py 참조).
    #   · report_id     : C 채번
    #   · correction_id : **B(recalc) 채번** — db limit_corrections 가 NOT NULL·UNIQUE 라
    #     이게 없으면 승인 결과를 그 행에 못 붙인다. LLM 이 지어내면 엉뚱한 이력에 붙으므로 덮어쓴다.
    #     B 미연결(스텁) 구간에는 recalc 에 없어 null 로 남는다 — 창작보다 null 이 정직하다(§0).
    #   · limit_version : **alert 승계** — 대체 대상 관리선 버전(계약 §6 동명). 승인 대기 중
    #     B 정기 리캘리브레이션(헌법 1-1 예외)이 끼어들면 B 가 이 값으로 충돌을 감지한다.
    report = report.model_copy(update={
        "report_id": make_report_id(LIM_PREFIX, alert),
        "correction_id": (recalc or {}).get("correction_id"),
        "limit_version": limit_version_of(alert, report.sensor_id),
    })

    # 코드 강제 가드 3종 (LLM 이 규칙을 어겨도 위험한 재설정안은 승인 큐로 못 간다, 헌법 1-1).
    # 순서 = 숫자 → 상한 → 가한계:
    #   ① 숫자 가드: 창작·변조를 먼저 정정 — 이후 가드가 정답 정렬된 값을 본다.
    #   ② 상한 가드: 값이 정답이어도 변동폭이 A2 초과면 자동 적용 불가.
    #   ③ 가한계 가드: 상한 내여도 Phase 진행 중이면 시점이 틀렸다.
    report = enforce_number_guard(report, recalc)
    report = enforce_reset_cap_guard(
        report, recalc, float(settings.require("limit_engine.reset_delta_max_sigma"))
    )
    report = enforce_provisional_guard(report, recalc)

    logger.debug(
        "limit report: %s (alert %s, method=%s, conf=%.2f)",
        report.report_id,
        alert.alert_id,
        report.method,
        report.confidence,
    )
    return report
