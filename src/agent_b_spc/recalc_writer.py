"""B4-3: 재산정 결과 DB 반영 — apply_auto(자동) / record_proposed(승인대기).

`apply_auto`: 자동 케이스만 control_limits 새 버전(기존 is_active=false) + limit_corrections
             (status=AUTO_APPLIED) 반영. 쓰기 순서 = UPDATE(false) 먼저 → INSERT(true)
             (idx_control_limits_active_one 부분 유니크 — L2).
`record_proposed`: 승인대기(needs_approval)만 limit_corrections status=PROPOSED 1행. control_limits
             완전 불변(관리선은 승인 후에야 바뀜 — M6). 전이는 승인 게이트(B5-1).

둘 다 commit 안 함 — 트랜잭션 경계는 호출자 소유(tttm_writer 패턴). 진입 가드로 decision 불일치
방어(호출자만 믿지 않는 이중 방어). 헌법 4-1(버전 보존)·3-2(control_limits 스키마 무변경).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.agent_b_spc.recalc_engine import (
    EPS, STATUS_AUTO_APPLIED, STATUS_PROPOSED, STATUS_SUPERSEDED)

logger = logging.getLogger(__name__)

_SEQ_MAX_RETRY = 5   # correction_id 채번 경합 시 +1 재시도(주 안전망 — FOR UPDATE는 행 없으면 무력)

# ① 현행(is_active) 행 — FOR UPDATE로 잠그며 읽어 동시성 확보(어차피 UPDATE 대상)
_SELECT_CURRENT = text(
    """
    SELECT limit_version, center, sigma, method, q_low, q_high, k_sigma
    FROM control_limits
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND sensor_window = :win AND sensor_id = :sn AND is_active
    FOR UPDATE
    """
)

# 신규 버전 = 그룹별 MAX(숫자 캐스팅)+1 (문자열 MAX 금지 — 'v9'>'v10' 역전 M2)
_NEXT_VERSION = text(
    """
    SELECT COALESCE(MAX(CAST(SUBSTRING(limit_version FROM 2) AS INTEGER)), 0) + 1
    FROM control_limits
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND sensor_window = :win AND sensor_id = :sn
    """
)

_DEACTIVATE = text(
    """
    UPDATE control_limits SET is_active = false
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND sensor_window = :win AND sensor_id = :sn AND is_active
    """
)

_INSERT_LIMIT = text(
    """
    INSERT INTO control_limits
      (chamber_id, recipe_id, step, sensor_window, sensor_id, limit_version, method,
       center, sigma, ucl, lcl, k_sigma, q_low, q_high, calc_window_n, trigger_type,
       is_active, effective_from)
    VALUES
      (:ch, :rc, :st, :win, :sn, :ver, :method, :center, :sigma, :ucl, :lcl, :k_sigma,
       :q_low, :q_high, :calc_window_n, :trigger_type, true, :effective_from)
    """
)

# 채번: 같은 (chamber, 날짜) 범위의 최대 SEQ+1. date는 8자리 숫자(언더스코어 없음 → LIKE 안전),
#   chamber는 컬럼으로 필터(언더스코어 와일드카드 회피). SEQ = correction_id 끝자리 숫자.
_NEXT_SEQ = text(
    """
    SELECT COALESCE(MAX(CAST(SUBSTRING(correction_id FROM '[0-9]+$') AS INTEGER)), 0) + 1
    FROM limit_corrections
    WHERE chamber_id = :ch AND correction_id LIKE :date_like
    """
)

_INSERT_CORRECTION = text(
    """
    INSERT INTO limit_corrections
      (correction_id, chamber_id, recipe_id, step, sensor_window, sensor_id,
       limit_version_before, limit_version_after, center_before, center_after,
       ucl_after, lcl_after, delta_pct, delta_sigma, trigger_type, settle_converged,
       shadow_false_alarm_reduction_pct, shadow_missed_detection,
       calc_window_n, status, effective_from)
    VALUES
      (:correction_id, :ch, :rc, :st, :win, :sn, :vb, :va, :center_before, :center_after,
       :ucl_after, :lcl_after, :delta_pct, :delta_sigma, :trigger_type, :settle_converged,
       :shadow_far, :shadow_missed,
       :calc_window_n, :status, :effective_from)
    """
)

# 같은 그룹(chamber·recipe·step·window·sensor)의 기존 live PROPOSED를 SUPERSEDED로 종결.
#   신규 PROPOSED INSERT 직전에 실행해 "그룹당 live PROPOSED = 1"을 보장한다(cross-path dedup).
#   정기 리캘리는 G2(_proposed_groups)로 이미 걸러져 여기선 매칭 0(no-op) — 실효는 Phase2 재수립
#   경로뿐(G2 없음). 옛 행은 삭제 않고 상태만 바꿔 감사 이력 보존(헌법 1-1 ⓑ).
_SUPERSEDE_LIVE_PROPOSED = text(
    """
    UPDATE limit_corrections SET status = :sup
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND sensor_window = :win AND sensor_id = :sn AND status = 'PROPOSED'
    """
)


def _gk_params(group_key: tuple) -> dict:
    ch, rc, st, win, sn = group_key
    return {"ch": ch, "rc": rc, "st": int(st), "win": win, "sn": sn}


def _current(conn, group_key: tuple, lock: bool):
    """현행(is_active) 행 조회. lock=True면 FOR UPDATE(apply_auto — 어차피 UPDATE 대상)."""
    stmt = _SELECT_CURRENT if lock else text(str(_SELECT_CURRENT).replace("FOR UPDATE", ""))
    row = conn.execute(stmt, _gk_params(group_key)).mappings().first()
    return dict(row) if row is not None else None


def _delta_pct(center_before, center_after):
    """표시용 signed %. |center_before| ≤ EPS(0중심 잔차 센서)면 NULL (0 나눗셈 가드 — L10)."""
    if abs(center_before) <= EPS:
        return None
    return (center_after - center_before) / center_before * 100.0


def _insert_correction(conn, proposal, cur, status, version_after, now, settle_converged=None,
                       shadow_far_pct=None, shadow_missed=None):
    """limit_corrections 1행 INSERT. correction_id 경합 시 SAVEPOINT 안에서 +1 재시도(H-B).

    `settle_converged`(B6-3-e): incident 재수립 제안의 정착이 σ-수렴(True)/상한 폴백(False)인지.
    `shadow_far_pct`·`shadow_missed`(Step7b D6): 정기 리캘리 needs_approval 소급 채점 —
    오탐 감소율(FLOAT)·미탐(SMALLINT, 미채점=NULL). settle_converged와 동형 positional-default라
    기존 호출부(apply_auto 등)는 무변경(전부 NULL). UNKNOWN은 0.0 아닌 NULL로 저장(D4).
    """
    ch = proposal.group_key[0]
    date = now.strftime("%Y%m%d")
    date_like = f"LIM-{date}-%"
    base = {
        **_gk_params(proposal.group_key),
        "vb": cur["limit_version"], "va": version_after,
        "center_before": cur["center"], "center_after": proposal.new_center,
        "ucl_after": proposal.new_ucl, "lcl_after": proposal.new_lcl,
        "delta_pct": _delta_pct(cur["center"], proposal.new_center),
        "delta_sigma": proposal.delta_sigma, "trigger_type": proposal.trigger_type,  # 결정5(B6-3-b)
        "settle_converged": settle_converged,        # B6-3-e — 미수렴 폴백 경고(NULL=비-재수립)
        "shadow_far": shadow_far_pct,                 # Step7b D6 — 오탐 감소율(NULL=미채점/UNKNOWN)
        "shadow_missed": shadow_missed,               # Step7b — 미탐(미채점=NULL, seam)
        "calc_window_n": proposal.n_used, "status": status, "effective_from": now,
    }
    for _ in range(_SEQ_MAX_RETRY):
        seq = conn.execute(_NEXT_SEQ, {"ch": ch, "date_like": date_like}).scalar()
        correction_id = f"LIM-{date}-{ch}-{seq}"
        sp = conn.begin_nested()
        try:
            conn.execute(_INSERT_CORRECTION, {**base, "correction_id": correction_id})
            sp.commit()
            return correction_id
        except IntegrityError:              # 동시 첫 건이 같은 SEQ 채번 → +1 재시도
            sp.rollback()
    raise RuntimeError(f"correction_id 채번 재시도 {_SEQ_MAX_RETRY}회 소진 (chamber={ch}, date={date})")


def apply_auto(conn, proposal) -> None:
    """자동 케이스: control_limits 새 버전 + limit_corrections(AUTO_APPLIED). commit=호출자.

    진입 가드로 auto_applied만 처리 — no_change의 new_*=None이 흘러들면 control_limits는
    NOT NULL 위반, limit_corrections는 NULL 이력이 조용히 남는다(후자가 더 위험).
    """
    if proposal.decision != "auto_applied":
        logger.warning("apply_auto: decision=%s (auto_applied 아님) — 쓰기 생략 %s",
                       proposal.decision, proposal.group_key)
        return
    cur = _current(conn, proposal.group_key, lock=True)
    if cur is None:
        logger.warning("apply_auto: 현행(is_active) 행 없음 — 쓰기 생략 %s", proposal.group_key)
        return

    now = datetime.now(timezone.utc)
    p = _gk_params(proposal.group_key)
    n = conn.execute(_NEXT_VERSION, p).scalar()
    new_version = f"v{n}"

    # ③ 쓰기 순서 불변식: UPDATE(기존 active=false) 먼저 → INSERT(신규 active=true) (L2)
    conn.execute(_DEACTIVATE, p)
    conn.execute(_INSERT_LIMIT, {
        **p, "ver": new_version, "method": cur["method"],
        "center": proposal.new_center, "sigma": proposal.new_sigma,
        "ucl": proposal.new_ucl, "lcl": proposal.new_lcl, "k_sigma": cur["k_sigma"],
        "q_low": cur["q_low"], "q_high": cur["q_high"],       # quantile이면 승계, sigma면 NULL
        "calc_window_n": proposal.n_used, "trigger_type": proposal.trigger_type,  # 결정5(B6-3-b)
        "effective_from": now,
    })
    # ④ 이력 (status=AUTO_APPLIED, delta_sigma 기록)
    _insert_correction(conn, proposal, cur, STATUS_AUTO_APPLIED, new_version, now)

    # ⑤ 밴드 폭 관측 로그 (M8 — 상한 판정 없음, W7 Scorecard 실측용). 스키마 무변경.
    sigma_before = cur["sigma"]
    band_ratio = proposal.new_sigma / sigma_before if sigma_before and sigma_before > EPS else float("nan")
    # ⚠️ delta_sigma 는 %s 다 (2026-08-12 수정 — 구 %.3g): `proposal.delta_sigma` 는 설계상
    #   `float | None`(recalc_engine.py:86 — 미산정 경로에서 None). %.3g 에 None 이 오면
    #   logging 내부 TypeError → **이 줄이 통째로 유실**된다 ("--- Logging error ---" 실측,
    #   TSR-0006 후속). 이 줄은 헌법 1-1 예외 1(자동 리캘리)의 관측 로그라, 자동 관리선 변경이
    #   일어날 때마다 조용히 안 보이게 되는 셈이었다 — 감사 정본(④ limit_corrections)은 별도라
    #   기록 유실은 아니고 관측 유실. None 은 "-" 로 찍는다 (grep 가능·정직).
    logger.info("recalc auto: %s v%s->%s center %.4g->%.4g sigma %.4g->%.4g (x%.3g) delta_sigma %s",
                proposal.group_key, cur["limit_version"], new_version, cur["center"],
                proposal.new_center, sigma_before, proposal.new_sigma, band_ratio,
                "-" if proposal.delta_sigma is None else format(proposal.delta_sigma, ".3g"))


def record_proposed(conn, proposal, settle_converged=None,
                    shadow_far_pct=None, shadow_missed=None) -> None:
    """승인대기 케이스: limit_corrections status=PROPOSED 1행만. control_limits 불변 (M6).

    감사 추적(헌법 1-1 ⓑ) + 승인 게이트(B5-1)가 읽을 DB 소스. center_after 등엔 제안값을
    채우되 관리선은 승인 후에야 바뀐다. 전이(PROPOSED→APPROVED_APPLIED/REJECTED)는 B5-1.
    `settle_converged`(B6-3-e): Phase 2 재수립 제안의 정착 수렴 여부(미수렴 폴백 경고, NULL=비-재수립).
    `shadow_far_pct`(Step7b): 정기 리캘리 소급 오탐 감소율(승인 Brief 표시·NULL=미채점/UNKNOWN).
    """
    if proposal.decision != "needs_approval":
        logger.warning("record_proposed: decision=%s (needs_approval 아님) — 쓰기 생략 %s",
                       proposal.decision, proposal.group_key)
        return
    cur = _current(conn, proposal.group_key, lock=False)
    if cur is None:
        logger.warning("record_proposed: 현행 행 없음 — 쓰기 생략 %s", proposal.group_key)
        return
    now = datetime.now(timezone.utc)
    # cross-path dedup: 신규 PROPOSED를 넣기 전에 같은 그룹의 옛 live PROPOSED를 SUPERSEDED로 종결.
    #   두 가드 통과 후(실제 INSERT가 뒤따를 때)만 실행 — supersede만 하고 insert 안 하면 그룹
    #   PROPOSED가 0이 되어 AWAITING_PHASE2 파생(_HAS_PROPOSED_ESTABLISH)이 깨진다. 같은 tx라 원자적.
    superseded = conn.execute(
        _SUPERSEDE_LIVE_PROPOSED,
        {**_gk_params(proposal.group_key), "sup": STATUS_SUPERSEDED}).rowcount
    if superseded:
        logger.info("record_proposed: 같은 그룹 기존 PROPOSED %d건 SUPERSEDED (신규 제안 우선): %s",
                    superseded, proposal.group_key)
    # version_after=None — 아직 적용 안 함(control_limits 불변)
    _insert_correction(conn, proposal, cur, STATUS_PROPOSED, None, now, settle_converged,
                       shadow_far_pct, shadow_missed)
