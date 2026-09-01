"""B5-4 Recipe 제안 적재·채번 (스펙 v2.2 §3-3).

- `resolve_alert_context`: alert 계약이 recipe_id·step을 안 실으므로(§1-5) spc_violations 조인(D13).
- `record_recipe_proposed`: 튜닝안을 recipe_corrections에 PROPOSED로 적재 + RCP 채번.
  recalc_writer._NEXT_SEQ/_SEQ_MAX_RETRY 패턴 이식(recipe_correction_id UNIQUE라 안전망 유효).

commit은 호출자(C 파이프라인). escalate·None 제안은 적재하지 않는다 — recipe_corrections는
제안 원장이며 승인 pending 정본은 approval_records(헌법 1-1).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

_SEQ_MAX_RETRY = 5   # 채번 경합 시 +1 재시도 (recalc_writer 선례)

# recipe_id·step 조달 — 챔버는 컬럼 조건(LIKE 아님: 'SIM_CH_3'의 '_'가 와일드카드)
_RESOLVE_CTX = text(
    "SELECT recipe_id, step FROM spc_violations WHERE alert_id = :aid LIMIT 1"
)

# 직전 적용분(D10) — chamber×recipe×step별 최신 APPLIED/VERIFIED value_after (knob별 1건)
_APPLIED_SETPOINTS = text(
    """
    SELECT DISTINCT ON (parameter_id) parameter_id, value_after
    FROM recipe_corrections
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND status IN ('APPLIED', 'VERIFIED')
    ORDER BY parameter_id, applied_at DESC NULLS LAST, created_at DESC
    """
)

# Incident당 PROPOSED 1행 보장 (헌법 1-4) — UNIQUE는 recipe_correction_id 단독이라 DB가 안 막는다
_HAS_PROPOSED = text(
    "SELECT 1 FROM recipe_corrections "
    "WHERE incident_id = :iid AND status = 'PROPOSED' LIMIT 1"
)

# 온도 A 정책 — 이 incident에 temp_target correction이 이미 있는가(손잡이 필터, _HAS_PROPOSED와 별개)
_TEMP_TUNED_THIS_INCIDENT = text(
    "SELECT 1 FROM recipe_corrections "
    "WHERE incident_id = :iid AND parameter_id = 'temp_target' "
    "AND status IN ('PROPOSED', 'APPLIED', 'VERIFIED') LIMIT 1"
)

_NEXT_SEQ = text(
    """
    SELECT COALESCE(MAX(CAST(SUBSTRING(recipe_correction_id FROM '[0-9]+$') AS INTEGER)), 0) + 1
    FROM recipe_corrections
    WHERE chamber_id = :ch AND recipe_correction_id LIKE :date_like
    """
)

_INSERT = text(
    """
    INSERT INTO recipe_corrections
      (recipe_correction_id, incident_id, chamber_id, recipe_id, step,
       parameter_id, value_before, value_after, delta_pct, shap_basis, status)
    VALUES
      (:rcid, :iid, :ch, :rc, :st, :pid, :vb, :va, :dp, CAST(:shap AS JSONB), 'PROPOSED')
    """
)


def resolve_alert_context(conn, alert_id: str) -> tuple[str | None, int | None]:
    """spc_violations에서 alert_id의 recipe_id·step 조회 (D13). 두 컬럼 모두 nullable."""
    row = conn.execute(_RESOLVE_CTX, {"aid": alert_id}).mappings().first()
    if row is None:
        return None, None
    step = row["step"]
    return row["recipe_id"], (int(step) if step is not None else None)


def resolve_applied_setpoints(conn, chamber_id: str, recipe_id: str | None,
                              step: int | None) -> dict:
    """직전 적용된 setpoint {parameter_id: value_after} 조달 (D10 — 반복 조정 역주행 방지).

    C가 `compute_tuning(applied_setpoints=…)`에 주입할 값을 B가 대신 조회한다(recipe_corrections
    도메인). chamber×recipe×step에서 status APPLIED/VERIFIED인 knob별 최신 value_after.
    recipe_id·step None이거나 적용 이력 없으면 빈 dict → 엔진은 명목값 시드(첫 스텝).
    """
    if recipe_id is None or step is None:
        return {}
    rows = conn.execute(_APPLIED_SETPOINTS,
                        {"ch": chamber_id, "rc": recipe_id, "st": int(step)}).mappings().all()
    return {r["parameter_id"]: r["value_after"] for r in rows if r["value_after"] is not None}


def _has_proposed(conn, incident_id: str) -> bool:
    """동일 incident에 이미 PROPOSED 행이 있는가."""
    return conn.execute(_HAS_PROPOSED, {"iid": incident_id}).scalar() is not None


def resolve_temp_tuned_this_incident(conn, incident_id: str) -> bool:
    """이 incident에 temp_target correction이 이미 있는가 (온도 A 정책, C ⓑ).

    C가 `compute_tuning(temp_tuned_this_incident=…)`에 주입할 값을 B가 대신 조회한다
    (`resolve_applied_setpoints`와 동일 패턴, recipe_corrections 도메인은 B 소유). 온도는
    명목 앵커가 없어(setpoint 부재) 누적 상한을 못 재므로 **Incident당 1스텝 자동 + 반복은 사람**:
      · 이 incident에 temp_target 이력 있음 → True → 엔진이 에스컬(사람 판단)
      · 없음(첫 온도 알람 또는 새 incident) → False → 엔진이 1스텝 자동 제안
    새 Incident는 incident_id가 달라 자동으로 False가 되어 리셋된다(A = Incident 단위).
    `_has_proposed`(손잡이 무관·PROPOSED만)와 달리 `parameter_id='temp_target'`로 한정하고
    APPLIED/VERIFIED까지 본다 — C4 등 타 손잡이 제안이 온도를 오탐 에스컬시키지 않도록.
    """
    return conn.execute(
        _TEMP_TUNED_THIS_INCIDENT, {"iid": incident_id}
    ).scalar() is not None


def record_recipe_proposed(conn, proposal, incident_id: str, chamber_id: str,
                           recipe_id: str | None, step: int | None,
                           shap_top3, *, now: datetime | None = None) -> str | None:
    """튜닝안을 recipe_corrections PROPOSED로 적재 + RCP 채번. commit은 호출자.

    반환 = 채번된 recipe_correction_id, 또는 적재 skip 시 None:
      · escalate/None 제안(수치 없음) — 원장 오염 방지(§3-3)
      · recipe_id·step None — init.sql NOT NULL 위반 회피(그냥 넣으면 터진다)
      · 동일 incident에 PROPOSED 기존재 — Incident당 1행 보장(1-4)

    `shap_basis`는 **A가 발행한 원본 shap_top3 그대로** 적재한다(forbidden 필터·첫매칭 절단
    결과가 아님 — 하류가 보는 SHAP이 룰로 가공되면 헌법 3-3 위반).
    """
    if proposal is None or proposal.parameter_id is None:
        logger.info("recipe 적재 skip — escalate/None 제안(수치 없음, incident=%s)", incident_id)
        return None
    if recipe_id is None or step is None:
        logger.warning("recipe 적재 skip — recipe_id/step None(NOT NULL 회피, incident=%s)", incident_id)
        return None
    if _has_proposed(conn, incident_id):
        logger.info("recipe 적재 skip — incident %s 이미 PROPOSED 존재(1-4)", incident_id)
        return None

    now = now or datetime.now(timezone.utc)
    date = now.strftime("%Y%m%d")
    date_like = f"RCP-{date}-%"
    base = {
        "iid": incident_id, "ch": chamber_id, "rc": recipe_id, "st": int(step),
        "pid": proposal.parameter_id, "vb": proposal.value_current,
        "va": proposal.value_proposed, "dp": proposal.delta_pct,
        "shap": json.dumps(shap_top3, ensure_ascii=False),   # 원본 그대로(§3-3)
    }
    for _ in range(_SEQ_MAX_RETRY):
        seq = conn.execute(_NEXT_SEQ, {"ch": chamber_id, "date_like": date_like}).scalar()
        rcid = f"RCP-{date}-{chamber_id}-{seq}"           # 무패딩(LIM 선례 대칭)
        sp = conn.begin_nested()
        try:
            conn.execute(_INSERT, {**base, "rcid": rcid})
            sp.commit()
            return rcid
        except IntegrityError:                            # 동시 첫 건이 같은 SEQ → +1 재시도
            sp.rollback()
    raise RuntimeError(f"recipe_correction_id 채번 재시도 {_SEQ_MAX_RETRY}회 소진 (chamber={chamber_id})")
