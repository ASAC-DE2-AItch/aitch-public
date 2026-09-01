"""Supervisor Brief → agent_reports 적재 (계약 §5).

PM 승인 그래프와의 연결점. merge_reports(approval_graph.py) 경로③이 incident_id 로
agent_reports 의 최신 `SUP-%` 행을 읽어(fetch_bundle) 게이트로 넘긴다. 즉 **C 는 그래프를
직접 호출하지 않는다** — Brief 를 이 테이블에 써두면 PM 그래프가 알아서 집어간다(DB 경유 연결).

읽기(db.py)와 분리한다: db.py 는 "읽기 전용"으로 못박혀 있고(모듈 docstring), 적재는 성격이
다르다(쓰기·멱등·계약 §5 컬럼 매핑). 접속 정보(dsn)만 db.py 와 공유한다.

무중단 (헌법 6-2): 적재 실패는 예외를 삼키고 False + 경고. Postgres 가 없어도 리포트 생성
파이프라인은 살아야 한다 — 저장이 안 되면 그래프가 못 집어갈 뿐, alert 처리는 계속된다.

멱등 (헌법 1-2, at-least-once): report_id 는 UNIQUE 라 같은 Brief 재적재는 ON CONFLICT
DO NOTHING 으로 흡수한다. consumer 재전달·재처리에도 중복 행이 생기지 않는다.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from .db import connect

if TYPE_CHECKING:
    from .schemas.release import ReleaseBrief
    from .schemas.report import OptionReport, SupervisorBrief

logger = logging.getLogger(__name__)

# --- 옵션 jsonb 에 함께 싣는 정량 필드 (2026-08-03, PM 제보 — 승인 payload 전량 null) -----
#
# 왜 필요한가: Brief 의 `parallel_options` 는 **요약**(OptionSummary — report_id·action·
# feasibility·confidence·rejected_because)이라 수치가 없다. 그런데 승인 게이트는
# `supervisor_adapter.merge_reports` 가 이 jsonb 에서 꺼낸 값으로 `fdc.correction` 을 만든다.
# 요약만 적재하면 승인해도 payload 가 전부 null 로 나간다 (limit·recipe 공통).
#
# 이름은 **C 정본(라이브러리 §1·§2)** 을 그대로 쓴다 — 게이트 쪽 이름 변환은 이미
# `merge_reports._pick(sensor_id→sensor, center_after→center_proposed,
# limit_version→limit_version_current)` 가 흡수하고 있다(2026-07-22 반영분). 여기서 미리
# 바꿔 실으면 같은 사실이 두 이름으로 돌아다닌다 (헌법 §7 — 이중 유지보수).
#
# 값을 **가공하지 않는다**: 수치는 B 엔진 산출이고 correction ID 는 B 채번 승계다(헌법 6-4).
_QUANT_FIELDS: dict[str, tuple[str, ...]] = {
    "limit_option": (
        "correction_id",      # B recalc_writer 채번 승계 — 재채번 금지
        "sensor_id",          # 게이트: sensor
        "limit_version",      # 게이트: limit_version_current (낙관적 잠금 — §2-1)
        "method",
        "center_before", "center_after",   # 게이트: center_proposed ← center_after
        "ucl_after", "lcl_after",
        "delta_sigma", "delta_pct",        # 이동량 정본은 σ (%는 center≈0 에서 왜곡)
    ),
    "recipe_option": (
        "recipe_id", "step",
        "parameter_id",       # setpoint C코드 (측정 C코드 아님 — 2단계 판정)
        "parameter_name",
        "value_current",      # 명목값 — 레시피에 적힌 값(누적 계산 기준)
        "applied_value",      # 지금 장비에 걸린 값 (W21, 2026-08-05). B TuningProposal 승계.
                              #   첫 스텝·escalate·온도면 null. 명목값만 보여주면 장비 실제와 어긋난다.
                              # 승인 화면까지의 배선은 2026-08-06 완결 (PR #122 리뷰 지적).
                              #   여기(agent_reports.recipe_option JSONB) → to_gate_report.recipe_tuning
                              #   → approval_records.original_value 순으로 간다. 8/5 에 W21 로 이 줄을
                              #   추가하면서 **하류 화이트리스트를 안 고쳐** 하루 동안 화면에 안 닿았다.
                              #   왕복 고정 = tests/test_option_quant_roundtrip.py
                              #   `test_recipe_current_values_reach_the_approval_screen`.
                              # ⚠️ 계약 §6 `fdc.correction` 에는 싣지 않는다 (B 보유값 · 헌법 2-2).
        "value_proposed", "delta_pct",
    ),
    # manual_option 은 `fdc.correction` 을 만들지 않는다(WaferDispositioned 경로) — 정량 없음.
}

# fetch_bundle(supervisor_adapter) 이 읽는 순서와 정합해야 한다 — 컬럼을 * 로 긁지 않고 명시.
# agent_reports(db/init.sql): report_id UNIQUE · incident_id · chamber_id NOT NULL.
_INSERT = """
INSERT INTO agent_reports (
    report_id, incident_id, chamber_id, severity_score, suspected_root_causes,
    recipe_option, limit_option, manual_option,
    supervisor_recommendation, supervisor_reason, supervisor_confidence,
    supervisor_verdict, supervisor_evidence, supervisor_counter,
    disposition_recommendation
) VALUES (
    %(report_id)s, %(incident_id)s, %(chamber_id)s, %(severity_score)s, %(suspected_root_causes)s,
    %(recipe_option)s, %(limit_option)s, %(manual_option)s,
    %(supervisor_recommendation)s, %(supervisor_reason)s, %(supervisor_confidence)s,
    %(supervisor_verdict)s, %(supervisor_evidence)s, %(supervisor_counter)s,
    %(disposition_recommendation)s
)
ON CONFLICT (report_id) DO NOTHING
"""


def _jsonb(value: Any) -> Optional[str]:
    """dict/list → JSONB 문자열. None 은 SQL NULL 로 (빈 dict 를 만들지 않는다)."""
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _quant_of(reports: "list[OptionReport] | None", option_type: str) -> dict[str, Any]:
    """상세 리포트에서 그 옵션의 정량 필드만 뽑는다. 없으면 빈 dict.

    None 인 필드는 **싣지 않는다** — 키가 있는데 null 인 것과 키가 없는 것을 구분해야
    `merge_reports._pick` 의 "None 이면 legacy 로 폴백" 이 의도대로 동작한다.
    """
    if not reports:
        return {}
    names = _QUANT_FIELDS.get(option_type)
    if not names:
        return {}
    for r in reports:
        if getattr(r, "option_type", None) != option_type:
            continue
        return {k: v for k in names if (v := getattr(r, k, None)) is not None}
    return {}


def brief_to_row(
    brief: "SupervisorBrief", reports: "list[OptionReport] | None" = None
) -> dict[str, Any]:
    """SupervisorBrief → agent_reports 행 (계약 §5 컬럼 매핑).

    fetch_bundle 이 되읽어 조립하는 형태와 정합한다:
      · parallel_options[recipe/limit/manual] → 각 *_option JSONB 컬럼
      · supervisor_recommendation.selected/reason/confidence → 개별 컬럼
      · wafer_disposition → disposition_recommendation JSONB

    Args:
        reports: 같은 alert 의 **상세 리포트 3종**(`handle_alert_with_brief` 반환 첫 값).
            Brief 의 옵션 블록은 요약이라 수치가 없으므로, 여기서 정량 필드를 **병합**한다
            (2026-08-03 — 승인 payload 전량 null 수정). 요약 필드는 그대로 둔다:
            `fetch_bundle` 이 이 jsonb 로 Brief 의 `parallel_options` 를 되조립하기 때문이다.
            미전달(None)이면 종전과 동일하게 요약만 적재한다 — 하위 호환.
    """
    opts = brief.parallel_options or {}
    rec = brief.supervisor_recommendation

    def opt(name: str) -> Optional[str]:
        o = opts.get(name)
        if o is None:
            return None
        merged = {**o.model_dump(mode="json"), **_quant_of(reports, name)}
        return _jsonb(merged)

    return {
        "report_id": brief.report_id,
        "incident_id": brief.incident_id,
        "chamber_id": brief.chamber_id,
        "severity_score": brief.context_score,
        "suspected_root_causes": _jsonb(brief.suspected_root_causes or []),
        "recipe_option": opt("recipe_option"),
        "limit_option": opt("limit_option"),
        "manual_option": opt("manual_option"),
        "supervisor_recommendation": rec.selected,
        "supervisor_reason": rec.reason,
        "supervisor_confidence": rec.confidence,
        # 2026-07-31 신설 — 종전에는 verdict·evidence·counter 가 적재에서 유실돼 S4 근거·반증이
        # 비었다 (구 주석 "verdict 는 옵션 JSONB 안에"는 오기 — 옵션 스키마에 그 필드가 없다).
        "supervisor_verdict": rec.verdict,
        "supervisor_evidence": _jsonb(list(rec.evidence or [])),
        "supervisor_counter": rec.counter_evidence,
        "disposition_recommendation": _jsonb(
            brief.wafer_disposition.model_dump(mode="json") if brief.wafer_disposition else None
        ),
    }


# 재분석 재생성 — **같은 행 UPDATE** (PR #70 결정 4).
#   새 행을 만들지 않는 이유: fetch_bundle 이 `ORDER BY id DESC LIMIT 1` 로 최신 1행만
#   읽으므로 새 행을 쌓으면 옛 행이 버려지는 작업이 되고, 승인 화면이 어느 것을 봐야
#   하는지도 흐려진다.
#   `needs_reanalysis` 는 **내용 갱신과 같은 문장에서** 내린다 — 먼저 내리고 재생성이
#   실패하면 요청이 조용히 사라진다(사람이 버튼을 눌렀는데 아무 일도 안 일어남).
_UPDATE = """
UPDATE agent_reports SET
    chamber_id = %(chamber_id)s,
    severity_score = %(severity_score)s,
    suspected_root_causes = %(suspected_root_causes)s,
    recipe_option = %(recipe_option)s,
    limit_option = %(limit_option)s,
    manual_option = %(manual_option)s,
    supervisor_recommendation = %(supervisor_recommendation)s,
    supervisor_reason = %(supervisor_reason)s,
    supervisor_confidence = %(supervisor_confidence)s,
    -- 0002 신설 3종. 빠뜨리면 재생성해도 **판정·근거·반증만 옛 값**이 남아
    -- S4 화면이 새 판단과 옛 근거를 섞어 보여준다 (테스트가 잡은 실수 2026-08-03).
    supervisor_verdict = %(supervisor_verdict)s,
    supervisor_evidence = %(supervisor_evidence)s,
    supervisor_counter = %(supervisor_counter)s,
    disposition_recommendation = %(disposition_recommendation)s,
    brief_version = COALESCE(brief_version, 1) + 1,
    needs_reanalysis = FALSE
WHERE report_id = %(report_id)s
RETURNING brief_version
"""


def update_brief(
    brief: "SupervisorBrief", reports: "list[OptionReport] | None" = None
) -> Optional[int]:
    """재생성된 Brief 로 **기존 행을 덮어쓴다**. 성공 시 새 `brief_version`, 실패 시 None.

    `brief.report_id` 로 행을 찾으므로 **호출측이 기존 report_id 를 유지**해야 한다
    (재생성은 alert 에서 새 report_id 를 파생시키므로 덮어써 넘긴다 — `reanalysis.py`).

    ⚠️ **판단 변경 이력은 남지 않는다** (미결 D10). `brief_version` 은 카운터일 뿐이라
    "③에서 ①로 왜 바뀌었나"는 추적되지 않는다 — 헌법 1-1 이 approval_records 를
    "감사 추적 + RAG 학습 자산"으로 규정한 취지에서 보면 값이 높은 쪽이 유실된다.
    스키마 확장은 PM 결정 대기이므로, 그때까지는 **verdict 가 바뀌면 경고 로그**로 남긴다.
    """
    row = brief_to_row(brief, reports)
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_UPDATE, row)
                got = cur.fetchone()
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — 파이프라인 생존 우선 (6-2)
        logger.warning("[gate] 재생성 UPDATE 실패 (스킵·무중단): report=%s: %s",
                       brief.report_id, exc)
        return None
    if got is None:
        # report_id 가 안 맞는다 = 덮어쓸 행이 없다. 새로 INSERT 하지 않는다 —
        # 재분석은 "기존 판단을 갱신"하는 경로이고, 여기서 INSERT 하면 원본과 별개 행이 생긴다.
        logger.warning("[gate] 재생성 대상 행 없음 — report_id=%s (UPDATE 0행)", brief.report_id)
        return None
    version = int(got[0])
    logger.info("[gate] Brief 재생성: report=%s incident=%s v%d selected=%s",
                brief.report_id, brief.incident_id, version,
                brief.supervisor_recommendation.selected)
    return version


def save_brief(
    brief: "SupervisorBrief", reports: "list[OptionReport] | None" = None
) -> bool:
    """Brief 1건을 agent_reports 에 적재. 성공/이미존재=True, 적재 실패=False (무중단).

    report_id 가 `SUP-` 로 시작해야 PM 그래프(fetch_bundle의 `report_id LIKE 'SUP-%'`)가
    집어간다 — Brief.report_id 는 SUP 접두 규칙(6-4)이라 자연히 충족된다.

    Args:
        reports: 상세 리포트 3종. 옵션 jsonb 에 정량 필드를 함께 실어야 승인 payload
            (`fdc.correction`)가 null 로 나가지 않는다 — `brief_to_row` 참조.
    """
    row = brief_to_row(brief, reports)
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT, row)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — 파이프라인 생존 우선 (6-2)
        logger.warning("agent_reports 적재 실패 (스킵·무중단): report=%s: %s",
                       brief.report_id, exc)
        return False
    logger.info("agent_reports 적재: report=%s incident=%s selected=%s",
                brief.report_id, brief.incident_id, brief.supervisor_recommendation.selected)
    return True


# =============================================================================
# RTD 해제 근거 Brief 적재 (인프라 스텝 3 · #140)
# =============================================================================
# **agent_reports 를 쓰지 않는다.** `/incidents/recent` 의 LATERAL 이 그 테이블의 최신 1행을
# `SUP-%` 필터 없이 읽어 S3 결정 대기 큐의 title/verdict 로 렌더하기 때문이다
# (src/gateway/main.py:1115) — 다른 종류의 행을 섞으면 그 화면이 조용히 오염된다.
# 사유 전문·스키마는 db/migrations/0014_release_briefs.sql 헤더.
_INSERT_RELEASE = """
INSERT INTO release_briefs (
    report_id, incident_id, chamber_id, scope, trigger_alert_id, inhibited_at,
    readiness, summary, evidence, counter_evidence, confidence,
    precheck_items, retrospect
) VALUES (
    %(report_id)s, %(incident_id)s, %(chamber_id)s, %(scope)s, %(trigger_alert_id)s,
    %(inhibited_at)s, %(readiness)s, %(summary)s, %(evidence)s, %(counter_evidence)s,
    %(confidence)s, %(precheck_items)s, %(retrospect)s
)
ON CONFLICT DO NOTHING
"""


def release_brief_to_row(brief: "ReleaseBrief") -> dict[str, Any]:
    """ReleaseBrief → release_briefs 행 (컬럼 매핑)."""
    return {
        "report_id": brief.report_id,
        "incident_id": brief.incident_id,
        "chamber_id": brief.chamber_id,
        "scope": brief.scope,
        "trigger_alert_id": brief.trigger_alert_id,
        "inhibited_at": brief.inhibited_at,
        "readiness": brief.readiness,
        "summary": brief.summary,
        "evidence": _jsonb(list(brief.evidence or [])),
        "counter_evidence": brief.counter_evidence,
        "confidence": brief.confidence,
        "precheck_items": _jsonb(list(brief.precheck_items or [])),
        "retrospect": _jsonb(brief.retrospect.model_dump(mode="json")),
    }


def save_release_brief(brief: "ReleaseBrief") -> bool:
    """해제 근거 Brief 1건 적재. 성공/이미존재=True, 적재 실패=False (무중단 6-2).

    멱등 (헌법 1-2 at-least-once): `report_id UNIQUE` 와 `(incident_id, inhibited_at)` UNIQUE
    둘 다 `ON CONFLICT DO NOTHING` 이 흡수한다 — **컬럼을 지정하지 않은 `ON CONFLICT`** 라
    제약이 무엇이든 걸린다. 앞단의 `release_brief_exists` 는 LLM 호출을 아끼는 사전 확인일
    뿐이고(폴러 중복·재시작 경합에는 진다), 중복 방지의 최종 방어선은 여기다.
    """
    row = release_brief_to_row(brief)
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_RELEASE, row)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — 폴러 생존 우선 (6-2)
        logger.warning("release_briefs 적재 실패 (스킵·무중단): report=%s: %s",
                       brief.report_id, exc)
        return False
    logger.info("release_briefs 적재: report=%s incident=%s chamber=%s readiness=%s",
                brief.report_id, brief.incident_id, brief.chamber_id, brief.readiness)
    return True
