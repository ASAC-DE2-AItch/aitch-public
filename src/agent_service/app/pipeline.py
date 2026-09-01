"""C4-1 관통 파이프라인 — alert 1건 → 게이트 → 3조수 병렬 팬아웃 → 리포트 3건.

흐름 (아키텍처 다이어그램 §1):
    AlertModel(파싱 완료) → context_score 게이트(B7) → asyncio 병렬 팬아웃 → OptionReport 3건

경계:
- **파싱(AlertModel)·소스(replay/kafka)는 이 모듈 밖** (schemas/·source.py). 여기는 "게이트+팬아웃"만.
- 게이트 임계값은 config/params.yaml `spc.context_score_agent_min`(B7) 이 단일 소스(헌법 6-1).
  AlertModel.agent_gate_open 은 계약 명문(31) 기본값일 뿐 — 운영 판정은 여기서 config 로 한다.
- 승인 게이트(interrupt)는 이 단계에 없다. Supervisor 통합 뒤 한 곳(헌법 1-4) — C5.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings
from .llm.client import LlmBackend
from .llm.factory import make_backend
from .schemas.alert import AlertModel
from .schemas.report import OptionReport
from .tools import TOOLS, AgentTool

logger = logging.getLogger(__name__)

# vectordb 는 **상대 임포트**(`..vectordb`)로 가져온다. 절대 경로(`src.agent_service.vectordb`)는
# repo 루트가 sys.path 에 있어야만 해석되어, 실행 위치가 바뀌면 조용히 ImportError 가 난다.
# 무중단 설계(6-2)가 그 예외를 삼키므로 **KB 근거가 통째로 빠진 채 리포트가 나온다** —
# 2026-07-20 EC2 스모크에서 '유사 사례 0건'으로 위장되어 실측 발견.

# ── 재료 0장 계측 (W16 · 2026-08-01) ──────────────────────────────────────────
# RAG 재료(KB 근거·knob_map) 조회는 **실패해도 성공해도 빈 값**으로 끝난다(무중단 6-2).
# 실패 경로는 각 except 가 경고를 남기지만 **조회가 성공하고 0건**인 경우는 지금까지 아무 흔적이
# 없었다 — 컬렉션 미적재·컬렉션명 오타·payload 불일치가 전부 "근거 없는 정상 Brief"로 나온다.
# 바로 위 2026-07-20 사례(import 실패가 '유사 사례 0건'으로 위장)와 TSR-0001(임베딩 torch 가
# 파이프 자식에서 WDAC 에 막힘)이 같은 모양으로 드러난다.
#
# knob_map 은 특히 조용하다 — 비면 `recipe.enforce_knob_guard` 가 판정 근거가 없어
# **가드 자체를 비활성**한다(오탐 방지 설계·의도된 동작). 즉 손잡이 화이트리스트 검사가 꺼진 채
# 계속 도는데, 지금은 기동 시 경고 한 줄 말고는 그 상태를 알 방법이 없다.
#
# → 흐름은 그대로 두고 **삼킨 자리에 카운터를 남긴다** (2026-07-28 관통 원칙 "조용히 실패하는
#   것을 눈에 보이게"). 판정·발행에는 관여하지 않는 계측 전용이다.
_KB_EMPTY: dict[str, int] = {}


def _note_kb_empty(collection: str, reason: str) -> None:
    """재료 0장을 컬렉션별로 세고 경고를 남긴다 (계측 전용 — 흐름 무변경)."""
    _KB_EMPTY[collection] = _KB_EMPTY.get(collection, 0) + 1
    logger.warning(
        "재료 0장 — collection=%s (%s · 누적 %d건). 리포트는 그 재료 없이 생성된다.",
        collection, reason, _KB_EMPTY[collection],
    )


def kb_empty_counts() -> dict[str, int]:
    """컬렉션별 재료 0장 누적 건수(사본) — 종료 요약·테스트용."""
    return dict(_KB_EMPTY)


def reset_kb_empty_counts() -> None:
    """카운터 초기화 — 테스트·라운드 경계용."""
    _KB_EMPTY.clear()


def gate_open(alert: AlertModel, settings: Settings) -> bool:
    """context_score 게이트 — B7 미만은 Agent 미가동(기록만). 임계값은 config 정본."""
    threshold = int(settings.require("spc.context_score_agent_min"))
    return alert.context_score >= threshold


def _load_knob_map() -> list[dict] | None:
    """Qdrant 에서 손잡이 매핑표(tuning_axis 5장)를 1회 조회 (1-2).

    Qdrant 미기동·조회 실패 시 None 반환 + 경고 — 파이프라인은 죽지 않는다(헌법 6-3).
    knob_map 이 None 이면 LLM 은 손잡이를 확정하지 못하고 정직하게 그 부재를 반영한다.
    """
    try:
        from ..vectordb import fetch_knob_map, get_qdrant_client

        cards = fetch_knob_map(get_qdrant_client())
        if not cards:
            _note_kb_empty("knob_map", "조회 성공·카드 0")
        return cards
    except Exception as exc:  # Qdrant 연결/조회 실패 — 무중단
        logger.warning("knob_map 조회 실패 — knob_map 없이 진행 (%s)", exc)
        _note_kb_empty("knob_map", f"조회 실패: {type(exc).__name__}")
        return None


def _evidence_top_k() -> int:
    """근거 검색 상한 — params `agent.rag_top_k`(D1).

    **왜 3인가** (2026-07-30 D1 을 5→3 으로 현행화하며 정리):
      ⓐ 프롬프트 예산 — 가변분(cases·kb_hits·logs) 한도 ≈3,600 토큰이고 top_k 가 그 손잡이다.
         recipe 는 여유가 거의 0이라 늘리면 400(예산 초과)이 재발한다 (사고 3건 이력).
      ⓑ Brief 근거가 계약상 3층 고정(`EvidenceBrief.evidence` min/max_length=3)이라 3건이면 닿는다.

    구 D1 값 `5 (필터 후)` 는 D2 `similarity_min` 필터를 전제했는데, 그 필터가 **코드 미사용**이다
    — 검색이 hybrid RRF 융합이라 융합 점수가 cosine 이 아니고, RRF 최댓값이 `2/(k+1)` 이라
    어떤 k 에서도 0.7 을 넘지 못한다(걸면 근거가 전멸한다). 전제가 없어 "5 검색 → 3 선별"이
    성립하지 않으므로 실동작(3)에 맞춰 문서를 현행화했다.
    필터 복원 + top_k 5 실험은 D18 재검토 항목이다.

    `require` 이므로 키가 없으면 명시적 에러 — 조용한 기본값 금지(6-1). 시그니처를 바꾸지 않고
    함수 안에서 읽는 이유: 호출부(`handle_alert*`)와 그 소스 형태를 고정하는 테스트가 있다.
    """
    from .config import load_settings

    return int(load_settings().require("agent.rag_top_k"))


#: 계측용 검색 폭 — **프롬프트에 실리는 수(top_k)와 별개**다 (2026-08-06, W40).
#  검색만 넓게 뽑아두면 `hit@3` 와 `hit@10` 을 **한 번의 라운드로** 같이 잴 수 있다.
#  프롬프트에는 여전히 top_k 만 실으므로 예산은 그대로다.
#  ⚠️ rerank(D16) 판단에 필요한 건 hit@10 자체가 아니라 **hit@10 − hit@3** 이다 —
#     그 차이가 크면 "정답이 4~10위에 있다"(끌어올릴 여지 있음), 작으면 애초에 못 찾는 것
#     (recall 문제 → 임베딩·청킹이지 rerank 가 아니다).
#  값은 `config/params.yaml` `agent.rag_measure_top_k` (6-4 — 매직 넘버는 전부 params).
def _search_top_k(prompt_top_k: int) -> int:
    """실제로 Qdrant 에 요청할 개수.

    **계측 라운드에서만** 넓힌다 (hit@3 과 hit@10 을 한 번의 라운드로 같이 재려고).
    운영에서는 프롬프트에 실리는 `top_k` 그대로다 — 넓혀 봐야 초과분은 버려지고
    Qdrant 조회만 3컬렉션 × 3배가 된다 (2026-08-06 PR #122 3차 리뷰 지적).
    top_k 가 이미 계측 폭 이상이면 계측 중이어도 그대로 둔다(더 뽑을 이유가 없다).
    """
    from .observe import is_observing

    if not is_observing():
        return int(prompt_top_k)
    from .config import load_settings

    return max(int(prompt_top_k), int(load_settings().require("agent.rag_measure_top_k")))


def _compute_tuning(alert: AlertModel, incident_id: str | None) -> dict | None:
    """튜닝 정량을 **B5-4 실 엔진**에서 조달한다 (S3 배선, 2026-08-04 · PR #83 하류).

    **왜 tool 이 아니라 여기인가.** `recipe.run` 은 `incident_id` 를 받지 않는데 온도 반복 정책
    (`resolve_temp_tuned_this_incident`)이 그 값을 요구한다. 그리고 DB 접촉은 이미 pipeline 몫이다
    (`_fetch_pairing_logs` 선례). 재료를 여기서 만들면 Supervisor 인용 가드의 `evidence_sources`
    에도 자연히 실린다 — 그 반대로 했다가 정답지가 비었던 사고가 있다(2026-07-20, 아래 주석).

    조달 경로 (전부 B 소유 — 우리는 부르기만 한다):
      · `resolve_alert_context(conn, alert_id)`        → recipe_id · step   (spc_violations 조인, D13)
      · `resolve_applied_setpoints(conn, ch, rc, st)`  → 직전 적용값        (D10 역주행 방지)
      · `resolve_temp_tuned_this_incident(conn, inc)`  → 온도 1스텝 소진 여부 (온도 A 정책)
      · `compute_tuning(...)` → `to_tuning_dict(...)`  → C 가 쓰는 dict     (어댑터도 B 제공)

    **실패는 스텁으로 폴백한다**(헌법 6-2 무중단). 폴백분에는 `_provisional` 도장이 남아
    "실물 아님"이 리포트 재료에 드러난다 — 조용히 진짜인 척하지 않는다.

    Returns: tuning dict(10필드) 또는 None(튜닝 대상 없음 — 위반 0건 등).
    """
    from .config import ApiMode, load_settings

    settings = load_settings()
    if settings.api_mode is not ApiMode.REAL:
        return _stub_tuning(alert)                      # 스텁 모드 — 실 DB 를 보지 않는다
    try:
        from src.agent_b_spc.recipe_engine import RecipeTuningConfig, compute_tuning, to_tuning_dict
        from src.agent_b_spc.recipe_writer import (
            resolve_alert_context, resolve_applied_setpoints, resolve_temp_tuned_this_incident,
        )

        from .db import sa_connect

        cfg = RecipeTuningConfig.load()                 # params.yaml + recipe_knob_map.yaml (B)
        with sa_connect() as conn:
            recipe_id, step = resolve_alert_context(conn, alert.alert_id)
            applied = resolve_applied_setpoints(conn, alert.chamber_id, recipe_id, step)
            temp_used = (resolve_temp_tuned_this_incident(conn, incident_id)
                         if incident_id else False)
        proposal = compute_tuning(
            violations=[v.model_dump() for v in alert.violations],
            shap_top3=list(alert.prediction_context.shap_top3),
            chamber_id=alert.chamber_id,
            recipe_id=recipe_id,
            applied_setpoints=applied,
            temp_tuned_this_incident=temp_used,
            cfg=cfg,
        )
        return to_tuning_dict(proposal)
    except Exception as exc:  # noqa: BLE001 — 조달 실패로 파이프라인을 죽이지 않는다(6-2)
        logger.warning(
            "B5-4 튜닝 엔진 조달 실패 — 스텁으로 진행(`_provisional` 도장 유지) (%s: %s)",
            type(exc).__name__, exc,
        )
        return _stub_tuning(alert)


def _stub_tuning(alert: AlertModel) -> dict | None:
    """스텁 경유 (tool 쪽 구현 재사용) — 실 엔진 미연결·조달 실패 시의 대역."""
    from .tools.recipe import _stub_tuning_api

    return _stub_tuning_api(alert)


def _fetch_recalc(alert: AlertModel) -> dict | None:
    """실력치 재설정 수치를 **B 가 써둔 행**에서 조달한다 (S1 배선, 2026-08-04).

    **왜 계산하지 않고 읽나.** 계약 §5 가 "옵션②는 B 실력치 재산정 API 산출값을 그대로 사용
    — C 가 수치를 재계산·수정하는 것 금지"라고 못박았다. 게다가 B 엔진(`recompute_group`)을
    부르려면 최근 N 장 표본이 필요한데 그건 B consumer 인메모리에만 있고 DB 에 없다
    (원값 저장 테이블 자체가 없다 — 2026-08-04 확인). 읽는 것이 유일한 정당한 경로다.

    **왜 tool 이 아니라 여기인가.** recipe(S3)와 같은 이유다 — 여기서 만들어야 Supervisor
    인용 가드의 `evidence_sources` 에 실려, B 가 채번한 `correction_id`(LIM-…)를 Brief 가
    인용해도 유령으로 오탐되지 않는다(2026-07-20 실측 11건 사고).

    **None 이 흔하다.** B 의 정기 재산정은 챔버가 조용할 때만 돌고(G1·G3) 우리 리포트는
    알람 때 만들어져 시점이 어긋난다. None 이면 규칙6(창작 금지)이 발동해 "재산정안 없음"으로
    나간다 — 지어내는 것보다 정직하다.

    Returns: recalc dict 또는 None(제안 없음·비-real 모드·조달 실패).
    """
    from .config import ApiMode, load_settings

    settings = load_settings()
    if settings.api_mode is not ApiMode.REAL:
        return None                                     # tool 이 스텁으로 자체 조달한다
    try:
        from src.agent_b_spc.recipe_writer import resolve_alert_context

        from .db import fetch_recalc_proposal, sa_connect
        from .tools.base import primary_violation

        v = primary_violation(alert)                    # B9 crazy 마커 배제한 대표 위반
        if v is None:
            return None                                 # 위반 0건·crazy-only → 재산정 대상 없음
        with sa_connect() as conn:                      # recipe_id·step 은 알람에 없다(B 리졸버)
            recipe_id, step = resolve_alert_context(conn, alert.alert_id)
        if recipe_id is None or step is None:
            logger.warning("재산정 조회 skip — alert_id=%s 의 recipe_id/step 미해결",
                           alert.alert_id)
            return None
        row = fetch_recalc_proposal(alert.chamber_id, recipe_id, step, v.window, v.sensor)
        return _to_recalc_dict(row)
    except Exception as exc:  # noqa: BLE001 — 조달 실패로 파이프라인을 죽이지 않는다(6-2)
        logger.warning("B 재산정 제안 조달 실패 — 수치 없이 진행 (%s: %s)",
                       type(exc).__name__, exc)
        return None


def _to_recalc_dict(row: dict | None) -> dict | None:
    """`limit_corrections` 행 → limit tool 이 쓰는 recalc dict (프롬프트 §2 [입력] 명세).

    B 는 `to_tuning_dict` 같은 어댑터를 limit 쪽엔 주지 않았다. 우리가 **DB 행**을 읽는
    구조라 컬럼이 곧 dict 이고, 이름만 프롬프트 명세에 맞춘다 — 값은 손대지 않는다(계약 §5).

    · `method`(sigma|quantile) — 제안 행엔 없어 활성 `control_limits` 에서 join 해 온다.
      1-1 예외 2(KEEP\\* 분위수 그룹)를 리포트가 구분해야 한다.
    · `missed_detection` — **B 가 채점하지 않는 것이 확정**(2026-07-22, shadow_eval.py §4)
      이라 항상 null 이 정상이다. "미채점"과 "0건"은 다르다.
    · `_provisional` 도장은 **붙이지 않는다** — 실물이기 때문.
    """
    if not row:
        return None
    return {
        "correction_id": row.get("correction_id"),      # B 채번(6-4) — C 는 승계만
        "method": row.get("method") or "sigma",
        "sensor_id": row.get("sensor_id"),
        "sensor_window": row.get("sensor_window"),
        "center_before": row.get("center_before"),
        "center_after": row.get("center_after"),
        "ucl_after": row.get("ucl_after"),
        "lcl_after": row.get("lcl_after"),
        "delta_pct": row.get("delta_pct"),
        "delta_sigma": row.get("delta_sigma"),           # 상한 가드의 정답지(σ 단위)
        "trigger_type": row.get("trigger_type"),         # 가한계 강등 가드가 읽는다
        "calc_window_n": row.get("calc_window_n"),
        "limit_version_before": row.get("limit_version_before"),
        "shadow_eval": {
            "basis": "b5-1b",                            # 스텁의 "stub" 을 대체
            "false_alarm_reduction_pct": row.get("shadow_false_alarm_reduction_pct"),
            "missed_detection": row.get("shadow_missed_detection"),
        },
    }


def _search_recipe_evidence(alert: AlertModel) -> dict:
    """레시피 근거 재료(kb_hits·cases)를 컬렉션별 검색어로 Qdrant 에서 조회한다 (1-3).

    검색어는 컬렉션마다 다르다(실측 2026-07-18): 지식은 센서 중심 짧게, 사례는 룰+서술 포함.
    검색어 구성은 recipe.build_kb_query/build_case_query(순수 함수)가 단일 소스.
    Qdrant 실패 시 빈 결과 + 경고 — 무중단(6-3). 빈 결과면 LLM 은 "근거 없음"으로 정직 반영.

    Returns: {"kb_hits": [...], "cases": [...]} — payload 에 그대로 실릴 형.
    """
    from .observe import ablated, record_retrieval
    from .tools.recipe import build_case_query, build_kb_query

    if ablated("kb"):  # ablation 라운드 — 재료를 빼고 판정이 얼마나 떨어지나 (W40)
        return {"kb_hits": [], "cases": []}

    top_k = _evidence_top_k()
    try:
        from ..vectordb import get_qdrant_client, search_kb

        client = get_qdrant_client()
        kb_q, case_q = build_kb_query(alert), build_case_query(alert)
        kb_pts = search_kb(client, "process_knowledge", kb_q, limit=_search_top_k(top_k))
        case_pts = search_kb(client, "historical_case", case_q, limit=_search_top_k(top_k))
        record_retrieval("process_knowledge", kb_q, kb_pts)
        record_retrieval("historical_case", case_q, case_pts)
        # ⚠️ **검색은 넓게, 프롬프트는 좁게.** 계측용으로 더 뽑아도 LLM 에 실리는 건 top_k 그대로라
        #   프롬프트 예산이 흔들리지 않는다(recipe 여유 13.6%). 이 덕에 hit@3 와 hit@10 을
        #   한 라운드로 같이 잰다 — rerank(D16) 판단에 필요한 건 hit@10 자체가 아니라 **그 차이**다.
        kb = [p.payload for p in kb_pts[:top_k]]
        cases = [p.payload for p in case_pts[:top_k]]
        if not kb:
            _note_kb_empty("process_knowledge", "검색 성공·히트 0")
        if not cases:
            _note_kb_empty("historical_case", "검색 성공·히트 0")
        return {"kb_hits": kb, "cases": cases}
    except Exception as exc:  # Qdrant 연결/검색 실패 — 무중단
        logger.warning("근거 검색 실패 — kb_hits/cases 없이 진행 (%s)", exc)
        # 실패도 결과는 0장이다 — 성공·실패를 한 카운터로 합쳐야 "이 라운드에 근거가
        # 몇 번 비었나"가 하나의 수로 잡힌다 (사유는 로그로 구분).
        _note_kb_empty("process_knowledge", f"검색 실패: {type(exc).__name__}")
        _note_kb_empty("historical_case", f"검색 실패: {type(exc).__name__}")
        return {"kb_hits": [], "cases": []}


def _search_manual_evidence(alert: AlertModel) -> dict:
    """정비 근거 재료(manual_hits)를 error_manual 컬렉션에서 조회한다 (C5-2).

    검색어 구성은 maintenance.build_manual_query(순수 함수)가 단일 소스.
    ⚠️ error_manual 은 증상→원인→조치 구조라 recipe 의 검색어 실측(2026-07-18)이 그대로
    적용되지 않는다 — 초안 검색어이며 EC2 스모크에서 품질 실측 후 조정한다.
    Qdrant 실패 시 빈 결과 + 경고 — 무중단(6-3). 빈 결과면 §6 KB 미커버 경로로 간다.

    Returns: {"manual_hits": [...]} — payload 에 그대로 실릴 형.
    """
    from .observe import ablated, record_retrieval
    from .tools.maintenance import build_manual_query

    if ablated("kb"):  # ablation 라운드 (W40)
        return {"manual_hits": []}

    top_k = _evidence_top_k()
    try:
        from ..vectordb import get_qdrant_client, search_kb

        manual_q = build_manual_query(alert)
        hits = search_kb(
            get_qdrant_client(), "error_manual", manual_q, limit=_search_top_k(top_k)
        )
        record_retrieval("error_manual", manual_q, hits)
        manual = [p.payload for p in hits[:top_k]]   # 프롬프트엔 top_k 만 (위 주석)
        if not manual:
            _note_kb_empty("error_manual", "검색 성공·히트 0")
        return {"manual_hits": manual}
    except Exception as exc:  # Qdrant 연결/검색 실패 — 무중단
        logger.warning("정비 매뉴얼 검색 실패 — manual_hits 없이 진행 (%s)", exc)
        _note_kb_empty("error_manual", f"검색 실패: {type(exc).__name__}")
        return {"manual_hits": []}


def _fetch_pairing_logs(cases: list[dict] | None) -> dict:
    """유사 사례의 incident_id 로 Postgres 조치로그를 페어링 조회한다 (C5-2).

    벡터 KB(historical_case)는 **서사**만 준다 — "무슨 일이 있었나". "얼마나 바꿨고 결과가
    어땠나"의 정밀 수치는 Postgres 로그에 있다(kb_schema.yaml:9 역할 경계, 2026-07-10).
    두 축은 incident_id 로 짝지어진다(형제 관계).

    Postgres 미기동·조회 실패 시 빈 결과 — 근거가 얕아질 뿐 파이프라인은 산다(헌법 6-2).

    Returns: {"limit_logs": [...], "recipe_logs": [...]}
    """
    from .db import fetch_limit_logs, fetch_recipe_logs, incident_ids_of

    ids = incident_ids_of(cases)
    if not ids:
        return {"limit_logs": [], "recipe_logs": []}
    return {"limit_logs": fetch_limit_logs(ids), "recipe_logs": fetch_recipe_logs(ids)}


def _fetch_predictions(alert: AlertModel) -> dict[str, dict]:
    """wafer 처분 판정의 입력 — **wafer별** 예측 수치를 모은다 (§4-3 "판정은 wafer별 개별").

    ⚠️ **alert 만으로는 구간을 판정할 수 없다.** `prediction_context` 는 **대표 wafer 1장**
    스냅샷인데 처분 대상은 `suspect_window` **구간 전체**라 축이 다르다. 값이 1장뿐이면
    `n_exceed`(임계 초과 장수)가 최대 1이라 판정 트리 STEP 3("2장 이상 → 공정 경로 R2R")이
    **구조적으로 발동할 수 없다** — B9 판정의 절반이 죽는다.

    그래서 2단으로 모은다:

        ① 주 경로 — `wafer_predictions` 조회 (구간 전체. 유일하게 축이 맞는 소스)
        ② 보험   — 대표 wafer 가 ①에서 빠지면 `alert.prediction_context` 로 메운다

    ②가 필요한 이유: `suspect_window` 는 **소급 구간**이고 wafer 번호는 시간순 단조 증가라,
    구간 대부분은 이미 처리·적재가 끝난 과거다. A sink 적재가 늦어 조회에서 빠질 수 있는 것은
    **알람을 유발한 가장 최근 1장뿐**이고, 그 한 장이 정확히 alert 이 싣고 온 wafer 다.
    (DB 값이 있으면 덮지 않는다 — 그쪽이 `fdc.actual` 병합까지 반영된 최신이다.)

    둘 다 없으면 빈 dict → 전 wafer "예측 조회 불가 → HOLD"(인터락 원위치·가역). 안전측이다.
    """
    from .db import fetch_predictions

    sw = alert.suspect_window
    ids = list(sw.member_wafers) if (sw is not None and sw.member_wafers) else None
    out = dict(fetch_predictions(alert.chamber_id, wafer_ids=ids))

    pc = alert.prediction_context
    if pc.wafer_id not in out:
        out[pc.wafer_id] = {
            "predicted_c65": pc.predicted_c65,
            "anomaly_score": getattr(pc, "anomaly_score", None),
        }
        logger.debug("대표 wafer %s 는 DB 미적재 — alert 값으로 보강(레이스 보험)", pc.wafer_id)
    return out


# `recipe_inputs` 에 담기지만 **recipe tool 에는 넘기면 안 되는** 키 (아래 ⚠️ 참조)
_LIMIT_ONLY = ("limit_logs", "recalc")


def _tool_kwargs(tool: AgentTool, recipe_inputs: dict | None, manual_inputs: dict | None) -> dict:
    """tool 별 추가 재료 선택 — 각 tool 이 자기 재료만 받는다.

    재료가 특정 tool 에만 필요할 때 계약(run 시그니처)을 모든 tool 에 퍼뜨리지 않으려는 것.
    각 run 은 재료를 keyword-only 로 받으므로 안 넘기는 tool 은 영향 없다.

    · recipe      : knob_map · kb_hits · cases · recipe_logs  (§1)
    · limit       : cases · limit_logs · recalc                (§2 — recalc = S1 배선, B 행 조달)
    · maintenance : manual_hits · cases                        (§3 — 정비는 조치로그 테이블 없음)

    cases·*_logs 는 recipe_inputs 에 함께 담겨 온다(같은 검색·조회분 재사용 — 중복 조회 방지).

    ⚠️ **recipe 는 `recipe_inputs` 를 통째로 받는다** — limit 전용 키가 섞이면 `recipe.run` 이
       모르는 kwarg 를 받아 `TypeError` 로 죽는다. 새 키를 recipe_inputs 에 넣을 때는
       `_LIMIT_ONLY` 에 등재할 것.
    """
    recipe_inputs = recipe_inputs or {}
    cases = recipe_inputs.get("cases")
    if tool.name == "recipe":
        return {k: v for k, v in recipe_inputs.items() if k not in _LIMIT_ONLY}
    if tool.name == "limit":
        return {"cases": cases, "limit_logs": recipe_inputs.get("limit_logs"),
                "recalc": recipe_inputs.get("recalc")}
    if tool.name == "maintenance":
        return {**(manual_inputs or {}), "cases": cases}
    return {}


async def fan_out(
    alert: AlertModel,
    backend: LlmBackend,
    settings: Settings,
    tools: tuple[AgentTool, ...] = TOOLS,
    *,
    recipe_inputs: dict | None = None,
    manual_inputs: dict | None = None,
) -> list[OptionReport]:
    """3조수를 asyncio 로 병렬 실행해 리포트 리스트를 모은다 (헌법 1-2 병렬).

    backend·settings 를 각 tool 에 주입한다(의존성 주입) — tool 은 어느 엔진인지 모른다.
    재료 묶음(recipe_inputs·manual_inputs)은 _tool_kwargs 로 각 tool 에만 배분된다.
    """
    reports = await asyncio.gather(
        *(
            tool.run(alert, backend, settings, **_tool_kwargs(tool, recipe_inputs, manual_inputs))
            for tool in tools
        )
    )
    return list(reports)


async def handle_alert(
    alert: AlertModel,
    settings: Settings,
    tools: tuple[AgentTool, ...] = TOOLS,
    backend: LlmBackend | None = None,
    recipe_inputs: dict | None = None,
    manual_inputs: dict | None = None,
    *,
    gate_override: bool = False,
) -> list[OptionReport]:
    """알람 1건 처리 — 게이트 통과 시에만 팬아웃. 미통과면 빈 리스트(기록만).

    Args:
        backend: 주입할 LLM 백엔드. None 이면 게이트 통과 후 settings 로 생성한다
            (게이트 미통과 시엔 만들지 않는다 — 미가동인데 엔드포인트를 요구하면 안 되므로).
        recipe_inputs: 근거 재료 묶음 {knob_map, kb_hits, cases, limit_logs, recipe_logs}. None 이면 게이트 통과
            후 Qdrant 에서 조회·검색한다(게이트 미통과 시엔 안 함 — backend 와 같은 지연 준비).
            테스트는 이 dict 를 주입해 Qdrant 를 타지 않는다.
        manual_inputs: 정비 근거 재료 묶음 {manual_hits}. recipe_inputs 와 같은 지연 준비 규칙.

    Returns: 리포트 리스트. 게이트 미통과 → [] (LLM 미가동, B7).
    """
    if not gate_open(alert, settings):
        if gate_override:
            # [AI 분석 요청] 수동 첫 분석 (2026-08-12) — 사람이 당긴 요청은 B7 임계의 대상이
            # 아니다(HITL — S3 각주 "저점수 건의 수동 요청 = 사람이 중요하다 판단한 사례").
            # 자동 경로는 불변 — 이 플래그는 reanalysis 폴러의 수동-첫-분석 경로만 세운다.
            logger.info(
                "gate override: alert=%s score=%d < B7 — 수동 요청으로 진행(트리거=사람)",
                alert.alert_id,
                alert.context_score,
            )
        else:
            logger.info(
                "gate closed: alert=%s score=%d < B7 — 기록만(Agent 미가동)",
                alert.alert_id,
                alert.context_score,
            )
            return []

    if backend is None:
        backend = make_backend(settings)
    if recipe_inputs is None:
        # 게이트 통과분만 Qdrant 조회 — knob_map(1-2, 전수) + 근거 검색(1-3, alert별).
        evidence = _search_recipe_evidence(alert)
        recipe_inputs = {
            "knob_map": _load_knob_map(),
            **evidence,
            **_fetch_pairing_logs(evidence.get("cases")),
        }
    if manual_inputs is None:
        manual_inputs = _search_manual_evidence(alert)
    reports = await fan_out(
        alert, backend, settings, tools, recipe_inputs=recipe_inputs, manual_inputs=manual_inputs
    )
    logger.info(
        "fan-out done: alert=%s score=%d → %d reports (%s)",
        alert.alert_id,
        alert.context_score,
        len(reports),
        ", ".join(r.option_type for r in reports),
    )
    return reports


async def handle_alert_with_brief(
    alert: AlertModel,
    settings: Settings,
    tools: tuple[AgentTool, ...] = TOOLS,
    backend: LlmBackend | None = None,
    recipe_inputs: dict | None = None,
    manual_inputs: dict | None = None,
    *,
    incident_id: str | None = None,
    incident_type: str | None = None,
    gate_override: bool = False,
) -> tuple[list[OptionReport], "SupervisorBrief | None"]:
    """관통 (C5-3): alert → 게이트 → 리포트 3종 → **Supervisor Brief 1건** (헌법 1-4).

    handle_alert 를 감싸는 얇은 층이다 — 기존 호출부(리포트만 필요한 곳)를 깨지 않으려는 것.

    incident_id 는 PM 그루퍼가 채번한다(계약 §4). alert 는 자기 incident_id 를 모르므로
    주입받으며, 미주입 시 **alert 1건 = Incident 1건**으로 임시 채번한다(M2 시연 경로).
    PM 그래프가 붙으면 실제 묶음 ID 를 넘겨주면 되고 이 함수는 그대로다.

    Returns: (리포트 리스트, Brief). 게이트 미통과면 ([], None) — LLM 미가동(B7).
    """
    from .schemas.report import INC_PREFIX, make_report_id
    from .supervisor import run as supervisor_run

    # 지연 계측 (C6-2) — DoD "slow-path 지연 목표 내". 예산은 params `agent.slow_path_max_sec`(D4).
    # 단계를 쪼개 재는 이유: 총계만 보면 초과 시 **어디를 손댈지 모른다**. 실제로 후보가 셋이다 —
    # 근거 준비(Qdrant+PG), 팬아웃(LLM 3콜 병렬), Supervisor(LLM 1콜). 첫 알람은 임베딩 모델
    # 로딩(bge-m3 약 2GB)이 prep 에 얹혀 유난히 느리다 → 웜업 여부 판단도 이 로그로 한다.
    t_start = time.perf_counter()

    # ⚠️ 재료를 **여기서 먼저 준비**한다. handle_alert 안에서 만들면 그건 지역 변수라
    #    Supervisor 인용 가드의 정답지(evidence_sources)가 비어 버린다 — 실존 문서 ID
    #    (PK-TUNE-PLASMA 등)가 "미확인 인용"으로 오탐됐다(2026-07-20 실측 10~11건).
    if gate_override or gate_open(alert, settings):
        if recipe_inputs is None:
            evidence = _search_recipe_evidence(alert)
            recipe_inputs = {
                "knob_map": _load_knob_map(),
                **evidence,
                **_fetch_pairing_logs(evidence.get("cases")),
                # S3 배선 — B5-4 실 엔진 산출(온도 반복 정책에 incident_id 가 필요하다).
                # ⚠️ 여기서는 **인자로 받은 incident_id 만** 쓴다. 아래에서 하는 임시 채번은
                #    alert 1건=Incident 1건 가정이라, 그 값으로 "이 Incident 에서 이미 온도를
                #    튜닝했나"를 물으면 **매번 새 Incident 처럼 보여 반복 가드가 무력화**된다.
                #    미주입이면 False(=첫 스텝)로 두는 편이 낫다 — 누적은 승인(HITL)이 게이트한다.
                "tuning": _compute_tuning(alert, incident_id),
                # S1 배선 — limit 수치도 B 가 써둔 행에서 조달한다(계약 §5, 재계산 금지).
                #   None 이 흔한 게 정상이다(정기 재산정은 조용할 때만 돈다) — 그때는
                #   규칙6 이 발동해 "재산정안 없음"으로 나간다.
                "recalc": _fetch_recalc(alert),
            }
        if manual_inputs is None:
            manual_inputs = _search_manual_evidence(alert)
    t_prep = time.perf_counter() - t_start

    reports = await handle_alert(
        alert, settings, tools, backend, recipe_inputs, manual_inputs,
        gate_override=gate_override,
    )
    t_fanout = time.perf_counter() - t_start - t_prep
    # ⚠️ 이 조건은 **"게이트 미통과"의 대용 표현**이다 (2026-07-28 명문화).
    #    `handle_alert` 가 빈 리스트를 내는 경우가 게이트 미통과뿐이라 지금은 두 조건이 등가다
    #    — tool 은 실패해도 fallback 리포트를 내므로 통과분은 항상 3건이다.
    #    **"리포트가 없으면 처분도 하지 말자"는 정책이 아니다.** 처분(apply_disposition)은
    #    supervisor.run 안에서 돌기 때문에, 이 줄이 곧 처분의 실행 조건이 되어 버린다:
    #      · 처분의 입력 = alert.suspect_window + _fetch_predictions + settings (리포트 아님)
    #      · 그런데 리포트 0건이면 supervisor 자체가 안 돌아 처분도 함께 멈춘다
    #      → **내용상 독립 · 실행상 종속.** (notes/04_참조/아키텍처_다이어그램.md §6-6)
    #    그래서 "crazy alert 은 조사할 게 없으니 tool 팬아웃만 스킵하자"가 성립하지 않는다.
    #    하려면 supervisor 진입 조건까지 함께 바꿔야 한다 — 별건.
    #    (crazy 에 한해선 그루퍼가 게이트와 무관하게 선행 격리하므로 wafer 안전은 보장된다)
    if not reports:
        # 게이트 미통과 — Supervisor 도 돌리지 않는다.
        # ⚠️ 여기서도 계측을 남긴다 (Claude 리뷰 #70, 2026-07-30): 이 return 이 `_log_latency`
        #    보다 먼저라 **게이트-미스 케이스의 소요가 전혀 안 남던** 자리다. 게이트 판정 자체가
        #    느려지는 회귀(예: alert 파싱·그루퍼 조회 지연)를 관측할 수단이 없어진다.
        #    게이트 미통과분은 prep·fanout·supervisor 가 모두 0 이라 total 이 곧 게이트 소요다.
        _log_latency(alert, t_start, t_prep, t_fanout, settings, gated=True)
        return [], None

    if backend is None:
        backend = make_backend(settings)
    if incident_id is None:
        incident_id = make_report_id(INC_PREFIX, alert)
        logger.debug("incident_id 미주입 — alert 1건=Incident 1건으로 임시 채번: %s", incident_id)

    brief = await supervisor_run(
        reports,
        alert,
        backend,
        settings,
        incident_id=incident_id,
        incident_type=incident_type,
        cases=(recipe_inputs or {}).get("cases"),
        # 인용 가드 정답지 — tool 에 들어간 **모든** 근거 재료. cases 만 주면 knob_map·KB 문서
        # ID(PK-…/EM-…)가 유령으로 오탐된다(2026-07-20 실측 11건).
        evidence_sources=[
            (recipe_inputs or {}).get("cases"),
            (recipe_inputs or {}).get("kb_hits"),
            (recipe_inputs or {}).get("knob_map"),
            (recipe_inputs or {}).get("limit_logs"),
            (recipe_inputs or {}).get("recipe_logs"),
            (manual_inputs or {}).get("manual_hits"),
            # S3 배선 — 튜닝안도 근거 재료다. dict 1건이라 리스트로 감싼다(known_refs 가
            # `list[dict]` 를 순회하며 _REF_KEYS 를 훑는다).
            # ⚠️ **제안 단계에는 `recipe_correction_id` 가 없다** — B 의 `TuningProposal`·
            #    `to_tuning_dict()` 에 그 필드가 아예 없고, 채번은 B 가 `recipe_corrections` 에
            #    persist 할 때 일어난다(헌법 6-4 — 채번 주체 B, C 는 승계만). 즉 지금 이 슬롯은
            #    비어 있는 게 정상이고, 승인·적용 단계에서 ID 가 실리면 그때부터 Brief 가 그
            #    ID 를 인용해도 유령이 아니게 된다. C 가 임의로 채워 넣지 않는다.
            [t] if (t := (recipe_inputs or {}).get("tuning")) else None,
            # S1 배선 — B 가 채번한 `correction_id`(LIM-…)를 Brief 가 인용해도 유령이 아니게.
            #   튜닝안과 같은 이유다(위) — 정답지에 안 실으면 실존 ID 가 오탐된다.
            [r] if (r := (recipe_inputs or {}).get("recalc")) else None,
        ],
        # wafer 처분 입력 — **여기서 조회한다.** 조회 실패·미적재면 빈 dict 가 넘어가고
        # 판정은 "예측 조회 불가 → HOLD"(원위치·가역)로 간다. 파이프라인은 멈추지 않는다.
        predictions=_fetch_predictions(alert),
    )

    _log_latency(alert, t_start, t_prep, t_fanout, settings)
    return reports, brief


def _log_latency(
    alert: AlertModel, t_start: float, t_prep: float, t_fanout: float, settings: Settings,
    *, gated: bool = False,
) -> None:
    """관통 1건의 단계별 소요를 남긴다 — C6-2 지연 실측 (params D4 예산 대비).

    예산 초과는 **WARNING 으로만** 남기고 파이프라인은 건드리지 않는다. 늦었다고 중간에
    끊으면 이미 쓴 LLM 비용을 버리면서 리포트도 못 내기 때문 — 지연은 관측 대상이지
    차단 대상이 아니다. 초과가 반복되면 대응은 코드가 아니라 config 다:
    일정표·params 주석대로 **게이트 점수 분포 재산정**(`fast_slow_cutoff`, B·C 공동)이 경로다.

    예산 키가 없으면 조용히 넘어가지 않고 INFO 로만 남긴다 — 계측 자체는 살려둔다.

    Args:
        gated: 게이트 미통과분(LLM 미가동). `gated` 표지를 붙이고 **예산 판정은 하지 않는다** —
            D4 는 slow-path(LLM 경로) 예산이라 미가동분에 적용하면 지표가 오염된다. 그래도
            남기는 이유는 게이트 판정 자체가 느려지는 회귀를 관측하기 위함이다
            (Claude 리뷰 #70 — 이 경로가 그동안 통째로 누락됐다).
    """
    total = time.perf_counter() - t_start
    t_sup = total - t_prep - t_fanout
    budget = settings.get("agent.slow_path_max_sec")

    # 계측 수집 (W40) — 평가 하네스가 켰을 때만 담긴다. 그동안 지연은 **로그로만** 나가서
    # 결과 jsonl 만으로는 D4(30초) 초과 여부를 판정할 수 없었다.
    from .observe import record_latency

    record_latency(total=total, prep=t_prep, fanout=t_fanout,
                   supervisor=(0.0 if gated else t_sup))

    if gated:
        logger.info(
            "latency: alert=%s total=%.1fs [gated — LLM 미가동, D4 예산 판정 제외]",
            alert.alert_id, total,
        )
        return

    detail = (
        "latency: alert=%s total=%.1fs (prep=%.1fs fanout=%.1fs supervisor=%.1fs)%s"
    )
    over = ""
    if isinstance(budget, (int, float)) and total > float(budget):
        over = f" ⚠️ D4 예산 {float(budget):g}s 초과"
        logger.warning(detail, alert.alert_id, total, t_prep, t_fanout, t_sup, over)
        return
    if isinstance(budget, (int, float)):
        over = f" [예산 {float(budget):g}s]"
    logger.info(detail, alert.alert_id, total, t_prep, t_fanout, t_sup, over)
