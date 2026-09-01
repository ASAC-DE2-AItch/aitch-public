"""B4-3: 리셋 기준점(사이클 시작 center·σ) 조회 — 누적 telescoping 기준점 (읽기 전용).

누적 판정(A3 1.0σ)은 `|새 center − 리셋 center| / 리셋 σ`로 telescoping 접힘 → 이력 delta
합산 불요, **리셋 지점만** 있으면 계산된다. 리셋 지점은 2-소스 병합(리뷰 M-A):
  ⓐ 기저 = control_limits `trigger_type='initial'`·`limit_version='v1'` (is_active 필터 금지)
  ⓑ 덮어쓰기 = limit_corrections `{qual,incident}` 또는 `status='APPROVED_APPLIED'` 최신 →
              그 `limit_version_after`의 control_limits 행

⚠️ `control_limits_loader.load()` 재사용 금지 — `WHERE is_active=true` 고정이라 재산정 후
   v1 기저(is_active=false)를 못 찾는다. 여기선 is_active 조건을 아예 넣지 않는다(불변식).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.agent_b_spc.recalc_engine import sigma_ref_of, EPS   # H4 공유 헬퍼 + 0-분모 가드 상수

logger = logging.getLogger(__name__)

# ⓐ 기저: initial v1 (is_active 조건 배제 — 재산정 후 false여도 조회돼야)
_SELECT_BASE = text(
    """
    SELECT center, sigma, ucl, lcl, method, limit_version
    FROM control_limits
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND sensor_window = :win AND sensor_id = :sn
      AND limit_version = 'v1' AND trigger_type = 'initial'
    """
)

# ⓑ 덮어쓰기 버전: 최신 리셋-타입 재설정의 limit_version_after
#    (created_at DESC, id DESC) — id(BIGSERIAL) tie-break로 동일 ts·out-of-order 순서 모호 제거
_SELECT_OVERRIDE_VERSION = text(
    """
    SELECT limit_version_after
    FROM limit_corrections
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND sensor_window = :win AND sensor_id = :sn
      AND limit_version_after IS NOT NULL
      AND (trigger_type IN ('qual', 'incident') OR status = 'APPROVED_APPLIED')
    ORDER BY created_at DESC, id DESC
    LIMIT 1
    """
)

# 덮어쓰기 버전의 control_limits 행 (is_active 무관 — 버전으로 특정)
_SELECT_BY_VERSION = text(
    """
    SELECT center, sigma, ucl, lcl, method, limit_version
    FROM control_limits
    WHERE chamber_id = :ch AND recipe_id = :rc AND step = :st
      AND sensor_window = :win AND sensor_id = :sn AND limit_version = :ver
    """
)


def _params(group_key: tuple) -> dict:
    ch, rc, st, win, sn = group_key
    return {"ch": ch, "rc": rc, "st": int(st), "win": win, "sn": sn}


def resolve_reset_ref(base_row: dict | None, override_row: dict | None, k_sigma: float) -> dict | None:
    """순수 병합: override 있으면 그것, 없으면 base. 둘 다 없으면 None.

    선택된 행의 center·σ_ref(sigma_ref_of — quantile은 robust σ)·**limit_version**을 반환한다.
    `limit_version`은 σ_ref 출처(=reference_id, B4-2:178)로, TTTM이 `tttm_comparisons`에 분모와
    정합하는 버전을 기록하게 한다(S4·D6-a). DB 미접촉이라 단위테스트가 쉽다(SQL 정확성은
    reset_ref 실 DB 테스트가 담당).
    """
    chosen = override_row if override_row is not None else base_row
    if chosen is None:
        return None
    return {"center": chosen["center"], "sigma_ref": sigma_ref_of(chosen, k_sigma),
            "limit_version": chosen.get("limit_version")}


def _fetch(conn, stmt, params) -> dict | None:
    row = conn.execute(stmt, params).mappings().first()
    return dict(row) if row is not None else None


def reset_ref(conn, group_key: tuple, k_sigma: float) -> dict | None:
    """그룹의 리셋 지점 {center, sigma_ref} 조회 (2-소스 병합). 기저 부재 시 None.

    ⓐ initial v1 기저 조회 → ⓑ 최신 리셋-타입 재설정의 버전 조회 → 있으면 그 버전의
    control_limits 행으로 기저 덮어쓰기 → resolve_reset_ref로 병합. commit 안 함(읽기 전용).
    """
    p = _params(group_key)
    base = _fetch(conn, _SELECT_BASE, p)
    override = None
    ver_row = conn.execute(_SELECT_OVERRIDE_VERSION, p).first()
    if ver_row is not None and ver_row[0] is not None:
        override = _fetch(conn, _SELECT_BY_VERSION, {**p, "ver": ver_row[0]})
    return resolve_reset_ref(base, override, k_sigma)


# ── A-warn: v1 initial 대비 누적 표류 (비차단 경고 — S4 승인 화면) ──────────────
#   기존 누적 가드(A3 `reset_cum_max_sigma_per_cycle`·recompute_group)는 **reset_ref 기준**이라
#   승인(APPROVED_APPLIED)/incident/qual 마다 reset_ref 가 갱신돼 **누적이 리셋**된다(위 _SELECT_OVERRIDE).
#   그래서 승인을 반복하면 v1 대비 총 표류를 A3 가 못 본다(실측: SIM_CH_3/C11 11회 승인 → 3.7σ).
#   여기 A-warn 은 **항상 v1 initial 고정 기준**이라 승인 반복 표류를 잡는다. **비차단** — 재산정
#   결정(auto/승인)에 안 쓰이고, 승인 워크플로/S4 가 값을 조회해 임계 초과 시 경고 배지만 띄운다.
#   경고 임계 = `config/params.yaml` `limit_engine.reset_cum_from_initial_warn_sigma` (표시층 소비).
def cum_from_initial_ref(base_row: dict | None, center: float, k_sigma: float) -> float | None:
    """순수: v1 initial 기저 행 + 후보 center → v1 대비 누적 표류(σ). base None/σ_ref≤EPS면 None.

    자(尺)는 v1 기저의 σ_ref(`sigma_ref_of` — quantile 은 밴드 robust σ). DB 미접촉이라 단위테스트
    쉬움(SQL 정확성은 실 DB 테스트가 담당). 비차단 경고용이므로 결정 로직과 분리한다.
    """
    if base_row is None:
        return None
    sig = sigma_ref_of(base_row, k_sigma)
    if sig <= EPS:
        return None
    return abs(center - base_row["center"]) / sig


def cum_from_initial(conn, group_key: tuple, center: float, k_sigma: float) -> float | None:
    """DB: v1 initial 기저 조회 → `cum_from_initial_ref`. 읽기 전용·commit 안 함. 기저 부재 시 None.

    S4 승인 화면 계약: 승인 워크플로가 **대기 proposal 의 new_center**로 호출 →
    반환 σ 가 `limit_engine.reset_cum_from_initial_warn_sigma` 초과면 **비차단 경고**를 표시한다.
    """
    base = _fetch(conn, _SELECT_BASE, _params(group_key))
    return cum_from_initial_ref(base, center, k_sigma)
