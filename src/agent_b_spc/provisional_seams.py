"""B6-3-c: a(ProvisionalMode) seam 실 구현 — 주입식 코어를 라이브 DB에 잇는다.

a는 부작용(writer·persist)을 주입받는 순수 코어다. 이 모듈이 그중 **DB에 닿는 seam**을
실물로 채운다:
- `ProvisionalWriter.write_provisional` — Phase 1 광폭 관리선을 control_limits에 적재
  (`trigger_type='provisional'`, init.sql:187 예약). recalc_writer.apply_auto 패턴 재사용.
- `derive_restore_state` — 재시작 복구(결정2 ⓓ): 전용 저장 없이 control_limits(provisional
  활성)·limit_corrections(PROPOSED)에서 phase를 파생한다. post_pm_count는 리셋 수용.

reload·set_excluded seam은 컨슈머가 이미 보유(reload_limits·TTTMEngine)라 여기서 안 다룬다.
persist는 결정2 ⓓ로 None(무저장).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.agent_b_spc.provisional_mode import Phase

logger = logging.getLogger(__name__)


class ProvisionalWriter:
    """Phase 1 광폭 provisional 관리선을 control_limits에 적재하는 write_provisional seam.

    `_make_provisional`이 만든 광폭 row(`{group_key, center, ucl, lcl, sigma, method}`)를
    `recalc_writer.apply_auto`(version bump·현행 비활성·INSERT·현행 메타 승계)로 쓴다 —
    `trigger_type='provisional'`. 현행 firm 행이 비활성되고 provisional이 새 active가 된다
    (firm 재수립 시 B5-1a apply가 다시 뒤집는다). commit은 자체 트랜잭션(prod) 또는 주입 conn(test).
    """

    def __init__(self, engine, _conn=None) -> None:
        self.engine = engine
        self._conn = _conn                       # 테스트 주입(기존 트랜잭션 내 쓰기·롤백)

    def write_provisional(self, rows: list) -> None:
        if not rows:
            return
        if self._conn is not None:
            self._write(self._conn, rows)
        else:
            with self.engine.begin() as conn:    # 호출자=commit(apply_auto는 no-commit)
                self._write(conn, rows)

    @staticmethod
    def _write(conn, rows: list) -> None:
        from src.agent_b_spc import recalc_writer as rw
        from src.agent_b_spc.recalc_engine import RecalcProposal

        for row in rows:
            proposal = RecalcProposal(
                group_key=row["group_key"], decision="auto_applied", method=row["method"],
                trigger_type="provisional", n_used=0, new_center=row["center"],
                new_ucl=row["ucl"], new_lcl=row["lcl"], new_sigma=row["sigma"],
                status="AUTO_APPLIED")
            rw.apply_auto(conn, proposal)        # 현행 firm 비활성 → provisional active INSERT


_PROVISIONAL_CHAMBERS = text(
    "SELECT DISTINCT chamber_id FROM control_limits "
    "WHERE trigger_type = 'provisional' AND is_active")
# B6-3-e ① — firm-baseline = 비활성·비-provisional 행 (그룹별 최신). provisional write가
#   비활성화한 직전 firm 행. ORDER BY effective_from → 마지막이 최신.
_FIRM_BASELINE_ROWS = text(
    "SELECT recipe_id, step, sensor_window, sensor_id, method, center, sigma, ucl, lcl "
    "FROM control_limits "
    "WHERE chamber_id = :ch AND NOT is_active AND trigger_type != 'provisional' "
    "ORDER BY effective_from, id")
_HAS_PROPOSED_ESTABLISH = text(
    "SELECT count(*) FROM limit_corrections "
    "WHERE chamber_id = :ch AND trigger_type = 'incident' AND status = 'PROPOSED'")


def derive_restore_state(conn, seasoning_loud: int) -> dict:
    """재시작 복구 상태 파생(결정2 ⓓ) — 전용 저장 없이 DB에서 phase를 유도한다.

    control_limits에 **provisional 활성** 행이 있는 챔버 = 가한계 진행 중. 그중 재수립
    **PROPOSED(trigger_type='incident') 이력이 있으면 AWAITING_PHASE2**(이미 정착·신호·제안됨,
    재신호·재제안 금지), 없으면 **PHASE_1**(정착 대기). `post_pm_count`는 파생 불가라 0 리셋
    (ⓓ 수용 — 최대 ~1,000 wafer 재수립 지연, 그동안 provisional 광폭이 보호). verdict='loud'
    함의(provisional-활성=요란), boundary=A7 요란 창.
    """
    state: dict = {}
    for ch in conn.execute(_PROVISIONAL_CHAMBERS).scalars().all():
        proposed = conn.execute(_HAS_PROPOSED_ESTABLISH, {"ch": ch}).scalar()
        phase = Phase.AWAITING_PHASE2 if proposed else Phase.PHASE_1
        state[ch] = {"phase": phase, "post_pm_count": 0, "verdict": "loud",
                     "boundary": seasoning_loud, "last_pm_count": None, "last_wafer_id": None}
        logger.info("가한계 복구: chamber=%s → %s (post_pm_count 리셋 — ⓓ)", ch, phase.value)
    return state


def derive_settle_sigma_ref(conn, chambers, k_sigma: float) -> dict:
    """재시작 복구용 firm σ_ref 재파생 (B6-3-e ①) — 비활성 firm-baseline 행에서.

    Phase 1 중 재시작 시 active=광폭 provisional이라 `sigma_ref_of(active)`=넓은 σ →
    밴드가 넓어져 **가짜 converged=True** 위험(오염 baseline에 firm 수립). provisional write가
    비활성화한 **직전 firm 행**(trigger_type!='provisional' 중 그룹별 최신)에서 σ_ref를 재파생해
    `restore(settle_sigma_ref=)`에 주입한다(별도 영속 없는 파생 모델). `chambers`=PHASE_1
    (정착 대기)만 — AWAITING_PHASE2는 이미 정착·판정 종료라 불요. 반환 `{chamber: {gk: σ_ref}}`.
    """
    from src.agent_b_spc.recalc_engine import sigma_ref_of

    out: dict = {}
    for ch in chambers:
        latest: dict = {}
        for r in conn.execute(_FIRM_BASELINE_ROWS, {"ch": ch}).mappings():
            gk = (ch, r["recipe_id"], r["step"], r["sensor_window"], r["sensor_id"])
            latest[gk] = dict(r)                 # ORDER BY effective_from → 마지막이 최신
        if latest:
            out[ch] = {gk: sigma_ref_of(r, k_sigma) for gk, r in latest.items()}
    return out
