"""B5-1a: 승인된 재설정을 control_limits에 즉시 적용 (승인 루프 마지막 고리).

B4-3 재산정(recalc_engine)이 이동량이 커서 자동 적용을 못 하고 `record_proposed`로 남긴
승인대기(status='PROPOSED') 재설정을, 승인 결과로 받은 correction_id 하나를 입력받아
control_limits에 새 버전으로 반영하고 이력을 PROPOSED→APPROVED_APPLIED로 전이한다.

스펙: specs/B5-1a_승인적용_스펙플랜.md. 트랜잭션 경계·commit은 호출자 소유(recalc_writer 패턴).
Kafka fdc.correction 구독·라이브 배선·거부 전이·approver 기록은 B5-1 이월(§8).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sqlalchemy import text

# B4-3 무수정 재사용(§7 A안) — 이 상수·헬퍼는 recalc_writer에서 빌려온다.
#   ⚠️ recalc_writer에서 이름 변경·삭제 시 approval_apply 동반 수정 (top-level import라 즉시 ImportError로 드러남).
from src.agent_b_spc.recalc_writer import (
    _DEACTIVATE,
    _INSERT_LIMIT,
    _NEXT_VERSION,
    _current,
    _gk_params,
)
from src.agent_b_spc.tttm_engine import EPS   # FP 슬랙 공유 상수(1e-9) — 인라인 금지(6-1)
from src.agent_b_spc.recalc_engine import STATUS_SUPERSEDED   # None 원인 구분(C 리뷰 #111 §2)

logger = logging.getLogger(__name__)

_MAX_RECHECK = 3   # 동시 교체(승인 겹침) 시 현행 활성행 재조회 최대 횟수 (§4 동시성 ①)

# 승인 대상 PROPOSED 행 (correction_id + status 가드, 잠그며 읽어 멱등·경합 방어).
# *_modified = 엔지니어 수정 승인값(nullable, NULL=일반 승인) — 원안 *_after는 불변(수정승인 §4).
_SELECT_PROPOSED = text(
    """
    SELECT chamber_id, recipe_id, step, sensor_window, sensor_id,
           center_after, ucl_after, lcl_after,
           center_modified, ucl_modified, lcl_modified,
           calc_window_n, trigger_type
    FROM limit_corrections
    WHERE correction_id = :cid AND status = 'PROPOSED'
    FOR UPDATE
    """
)

# None 원인 구분용 — PROPOSED 가드 없이 현재 status만 조회(SUPERSEDED vs 부재/이미적용, C 리뷰 #111 §2).
_SELECT_STATUS = text("SELECT status FROM limit_corrections WHERE correction_id = :cid")

# 상태 전이 — status 가드로 멱등(2회 호출 시 두 번째는 0행 매칭)
_MARK_APPLIED = text(
    """
    UPDATE limit_corrections
    SET status = 'APPROVED_APPLIED', limit_version_after = :ver
    WHERE correction_id = :cid AND status = 'PROPOSED'
    """
)

# 영구 불량 입력(원안 NULL·구조 가드 위반) → APPLY_FAILED (재시도 방지, 수정승인 §5-3)
_MARK_FAILED = text(
    """
    UPDATE limit_corrections SET status = 'APPLY_FAILED'
    WHERE correction_id = :cid AND status = 'PROPOSED'
    """
)


def _coalesce(modified, after):
    """수정값 우선, NULL이면 원안 (수정승인 §4 COALESCE — modify/일반 승인 한 경로 통일)."""
    return after if modified is None else modified


def apply_approved(conn, correction_id: str) -> bool:
    """승인된 correction_id의 PROPOSED 재설정을 control_limits에 적용 + 상태 전이. commit=호출자.

    **수정 승인(Modify-and-Approve, B5-1 필드규칙)**: 엔지니어가 제안값을 수정해 승인하면
    `*_modified`(nullable)에 담겨 오고, 여기서 원안(`*_after`)에 오버레이한다 —
    `COALESCE(수정값, 원안)`. 전부 NULL이면 일반 승인(원안 그대로). **method별 처리(§4)**:
    - sigma 그룹: 센터만 수정 → σ=원안서 복원(`(ucl_after−center_after)/k`, 불변이라 가능),
      밴드=`center ± kσ` 재산출(대칭 유지).
    - 분위수 그룹: ucl/lcl 직접 수정(부분 가능)·center/σ 승계(밴드 재산출 없음).

    **구조 가드(§5-1)**: 해소값이 `finite`(σ 포함) + `ucl ≥ center ≥ lcl`(FP 슬랙 EPS) + (sigma) `σ>0`.
    위반·원안 NULL·(sigma) `k_sigma` NULL/0 = **영구 불량 입력** → `APPLY_FAILED` 마킹 + `return False`(★raise 금지 —
    호출자 트랜잭션이 APPLY_FAILED 커밋·재시도 방지). 크기 정책은 미검사(HITL 권한, §5-2).

    **반환**(★): 적용됨 `True` / 미적용(APPLY_FAILED·no-op·현행 부재) `False` — 호출자가 reload·
    firm 후속 분기(APPLY_FAILED에 stale 관리선으로 졸업 방지, §7). commit=호출자. 멱등(status 가드).
    """
    # 1. PROPOSED 행 조회 (없음/이미 처리 → 멱등 no-op)
    proposed = conn.execute(_SELECT_PROPOSED, {"cid": correction_id}).mappings().first()
    if proposed is None:
        # None 원인 구분(C 리뷰 #111 §2): SUPERSEDED(신규 제안에 밀려 승인 무효·되돌림 차단)는
        #   의도된 정상 동작이나 "이미적용/부재(멱등 재전달)"와 다른 사건이라, 같은 문장으로 나가면
        #   감사에서 "승인했는데 왜 미적용?"이 추적 불가. 문장만 갈라 구분(둘 다 정상 → INFO 유지).
        status = conn.execute(_SELECT_STATUS, {"cid": correction_id}).scalar()
        if status == STATUS_SUPERSEDED:
            logger.info("apply_approved: %s SUPERSEDED — 신규 제안에 밀려 미적용(승인 무효·되돌림 차단)",
                        correction_id)
        else:
            logger.info("apply_approved: %s PROPOSED 아님/없음 — no-op(멱등 재전달)", correction_id)
        return False
    group_key = (proposed["chamber_id"], proposed["recipe_id"], proposed["step"],
                 proposed["sensor_window"], proposed["sensor_id"])

    # 2. 현행(is_active) 관리선 조회 — 동시 교체 시 재조회(§4 동시성 ①)
    current = None
    for _ in range(_MAX_RECHECK):
        current = _current(conn, group_key, lock=True)
        if current is not None:
            break
    if current is None:                            # 현행 부재(설정 문제) — PROPOSED 유지(재시도 여지)
        logger.warning("apply_approved: %s 현행(is_active) 행 없음 — 적용 생략", group_key)
        return False

    # 3. NULL 프리체크 (★σ 계산·isfinite 前 — None 산술 TypeError→무한루프 방지). 원안 *_after는
    #    σ 복원·COALESCE 폴백 소스라 필수 → 영구 불량 → APPLY_FAILED (§5-3, §6①)
    if (proposed["center_after"] is None or proposed["ucl_after"] is None
            or proposed["lcl_after"] is None):
        conn.execute(_MARK_FAILED, {"cid": correction_id})
        logger.error("apply_approved: 원안 *_after NULL → APPLY_FAILED: cid=%s", correction_id)
        return False

    # 3.5. sigma 행 k_sigma 프리체크 (★NULL/0 → 나눗셈 raise→rollback→무한 재전달 방지, §5-3).
    #      *_after NULL 프리체크와 같은 fail-closed 원칙 — 영구 불량이라 APPLY_FAILED로 닫는다.
    k = current["k_sigma"]
    if current["method"] == "sigma" and not (k is not None and k > 0):
        conn.execute(_MARK_FAILED, {"cid": correction_id})
        logger.error("apply_approved: sigma 행 k_sigma 무효(%s) → APPLY_FAILED: cid=%s", k, correction_id)
        return False

    # 4. 수정값 오버레이 + method별 밴드 처리 (§4)
    if current["method"] == "sigma":
        center = _coalesce(proposed["center_modified"], proposed["center_after"])
        sigma = (proposed["ucl_after"] - proposed["center_after"]) / current["k_sigma"]  # 원안서 복원
        ucl = center + current["k_sigma"] * sigma
        lcl = center - current["k_sigma"] * sigma
        extra_ok = sigma > 0                       # 원안 σ=0 퇴화 차단(§5-1)
    else:                                          # quantile(및 sigma 외 전부 — DB method CHECK 없어 여기 흡수) — ucl/lcl 직접·부분 수정, center/σ 승계
        center = proposed["center_after"]
        sigma = current["sigma"]
        ucl = _coalesce(proposed["ucl_modified"], proposed["ucl_after"])
        lcl = _coalesce(proposed["lcl_modified"], proposed["lcl_after"])
        extra_ok = True

    # 5. 통일 구조 가드(§5-1, PM 통일) — finite(σ 포함 — 분위수 승계 NaN write 차단) +
    #    ucl≥center≥lcl(FP 슬랙 EPS) + extra
    ok = (math.isfinite(center) and math.isfinite(ucl) and math.isfinite(lcl)
          and math.isfinite(sigma)
          and (ucl - center >= -EPS) and (center - lcl >= -EPS) and extra_ok)
    if not ok:
        conn.execute(_MARK_FAILED, {"cid": correction_id})
        logger.error("apply_approved: 구조 가드 실패 → APPLY_FAILED: cid=%s method=%s "
                     "center=%.6g ucl=%.6g lcl=%.6g sigma=%.6g (ucl≥center≥lcl·σ>0 중 위반)",
                     correction_id, current["method"], center, ucl, lcl, sigma)
        return False   # ★정상 return(raise 금지) → 호출자 트랜잭션이 APPLY_FAILED 커밋(재시도 X)

    # 6. 신규 버전 — _NEXT_VERSION은 정수 스칼라(이미 MAX+1) → 'v' 접두 부착
    n = conn.execute(_NEXT_VERSION, _gk_params(group_key)).scalar()
    new_version = f"v{n}"

    now = datetime.now(timezone.utc)
    p = _gk_params(group_key)
    # 7. DEACTIVATE(기존 active=false) → INSERT(신규 active=true) (쓰기 순서 불변식 L2)
    conn.execute(_DEACTIVATE, p)
    conn.execute(_INSERT_LIMIT, {
        **p, "ver": new_version, "method": current["method"],
        "center": center, "sigma": sigma, "ucl": ucl, "lcl": lcl, "k_sigma": current["k_sigma"],
        "q_low": current["q_low"], "q_high": current["q_high"],   # quantile 승계 / sigma면 NULL
        "calc_window_n": proposed["calc_window_n"], "trigger_type": proposed["trigger_type"],
        "effective_from": now,
    })
    # 8. 상태 전이 (같은 f"v{n}"을 limit_version_after에)
    conn.execute(_MARK_APPLIED, {"cid": correction_id, "ver": new_version})
    modified = any(proposed[c] is not None
                   for c in ("center_modified", "ucl_modified", "lcl_modified"))
    logger.info("apply_approved: %s %s applied %s center=%.4g (modified=%s)",
                correction_id, group_key, new_version, center, modified)
    return True
