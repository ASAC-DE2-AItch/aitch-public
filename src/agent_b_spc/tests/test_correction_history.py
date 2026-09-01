"""correction_history.reset_ref 테스트 (B4-3 Task3 — 리셋 기준점 2-소스 병합).

순수: resolve_reset_ref(병합 로직 — override 우선·base 없으면 None·quantile robust σ).
실 DB(skipif): reset_ref 2-소스 쿼리 — initial 기저 / {qual,incident,APPROVED_APPLIED} 덮어쓰기 /
              (created_at DESC, id DESC) tie-break / quantile / 부재 그룹 None.
TEST_CH_* 전용 데이터 INSERT→검증→rollback (M4 — 기존 시딩 무의존).
"""
from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc import correction_history as ch
from src.agent_b_spc.seed_control_limits import make_engine

_PROBE_TIMEOUT_SEC = 3
K = 3.0


def _db_available() -> bool:
    """DATABASE_URL 짧은 타임아웃 접속 판정 (tttm/loader 테스트와 동일 패턴)."""
    try:
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = create_engine(url, connect_args={"connect_timeout": _PROBE_TIMEOUT_SEC})
        try:
            probe.connect().close()
        finally:
            probe.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


# ── 순수 병합 로직 (DB 불필요) ────────────────────────────────────────

def _sigma_row(center, sigma, ver="v1"):
    return {"center": center, "sigma": sigma, "ucl": center + K * sigma,
            "lcl": center - K * sigma, "method": "sigma", "limit_version": ver}


def _quantile_row(center, ucl, lcl, ver="v1"):
    return {"center": center, "sigma": 1.0, "ucl": ucl, "lcl": lcl, "method": "quantile",
            "limit_version": ver}


def test_resolve_base_only_sigma():
    """override 없음 → base(initial) 기준 center·σ·limit_version(v1)."""
    out = ch.resolve_reset_ref(_sigma_row(100.0, 10.0), None, K)
    assert out == {"center": 100.0, "sigma_ref": 10.0, "limit_version": "v1"}


# ── cum_from_initial (A-warn — v1 initial 대비 누적 표류, 비차단 경고) ──────────

def test_cum_from_initial_ref_sigma():
    """sigma method: |center − v1.center| / v1.sigma."""
    assert ch.cum_from_initial_ref(_sigma_row(100.0, 10.0), 130.0, K) == pytest.approx(3.0)


def test_cum_from_initial_ref_quantile_robust_sigma():
    """quantile: σ_ref = (ucl−lcl)/2k → (118−82)/6 = 6 → (118−100)/6 = 3.0."""
    assert ch.cum_from_initial_ref(_quantile_row(100.0, 118.0, 82.0), 118.0, K) == pytest.approx(3.0)


def test_cum_from_initial_ref_none_base_is_none():
    """v1 기저 부재 → None (경고 판정 불가)."""
    assert ch.cum_from_initial_ref(None, 130.0, K) is None


def test_cum_from_initial_ref_zero_sigma_is_none():
    """σ_ref 붕괴(≤EPS) → None (0 분모 방지)."""
    assert ch.cum_from_initial_ref(_sigma_row(0.0, 0.0), 5.0, K) is None


def test_resolve_override_wins():
    """override(승인적용/qual/incident) 있으면 base를 덮어쓴다."""
    out = ch.resolve_reset_ref(_sigma_row(100.0, 10.0), _sigma_row(120.0, 8.0), K)
    assert out == {"center": 120.0, "sigma_ref": 8.0, "limit_version": "v1"}


def test_resolve_reference_id_follows_chosen_row():
    """S4 3키화 — limit_version(=σ_ref 출처·reference_id)이 선택된 행을 따라간다(D6-a)."""
    out = ch.resolve_reset_ref(_sigma_row(100.0, 10.0, ver="v1"),
                               _sigma_row(120.0, 8.0, ver="v3"), K)
    assert out["limit_version"] == "v3"                # override 버전을 싣는다(활성 행 아님)


def test_resolve_no_base_no_override_is_none():
    """둘 다 없으면 None (M7 — 코어가 보수적 승인으로 처리)."""
    assert ch.resolve_reset_ref(None, None, K) is None


def test_resolve_quantile_robust_sigma():
    """quantile 그룹 base → robust σ = (ucl−lcl)/(2·k)."""
    out = ch.resolve_reset_ref(_quantile_row(100.0, 130.0, 70.0), None, K)
    assert out["center"] == 100.0
    assert out["sigma_ref"] == pytest.approx((130.0 - 70.0) / (2 * K))   # =10


# ── 실 DB 2-소스 조회 (skipif — TEST_CH_* INSERT→rollback) ─────────────

_CH = "TEST_CH_RESET"
_GK = (_CH, "C6_0", 4, "settled", "C11")

_INS_CL = text(
    "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
    "limit_version, method, center, sigma, ucl, lcl, trigger_type, is_active) VALUES "
    "(:ch, :rc, :st, :win, :sn, :ver, :method, :center, :sigma, :ucl, :lcl, :tt, :active)"
)
_INS_LC = text(
    "INSERT INTO limit_corrections (correction_id, chamber_id, recipe_id, step, sensor_window, "
    "sensor_id, limit_version_before, limit_version_after, trigger_type, status) VALUES "
    "(:cid, :ch, :rc, :st, :win, :sn, :vb, :va, :tt, :status)"
)


def _cl(conn, ver, center, sigma, tt, active, method="sigma", ucl=None, lcl=None):
    conn.execute(_INS_CL, {
        "ch": _CH, "rc": "C6_0", "st": 4, "win": "settled", "sn": "C11", "ver": ver,
        "method": method, "center": center, "sigma": sigma,
        "ucl": ucl if ucl is not None else center + K * sigma,
        "lcl": lcl if lcl is not None else center - K * sigma, "tt": tt, "active": active})


def _lc(conn, cid, ver_after, tt, status):
    conn.execute(_INS_LC, {
        "cid": cid, "ch": _CH, "rc": "C6_0", "st": 4, "win": "settled", "sn": "C11",
        "vb": "v1", "va": ver_after, "tt": tt, "status": status})


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_reset_ref_initial_only_returns_base():
    """periodic-only 사이클(리셋 이력 없음) → initial v1 기저 (is_active 무관)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _cl(conn, "v1", 100.0, 10.0, "initial", True)
            out = ch.reset_ref(conn, _GK, K)
            assert out == {"center": 100.0, "sigma_ref": 10.0, "limit_version": "v1"}
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_reset_ref_initial_inactive_still_found():
    """재산정 후 v1이 is_active=false여도 기저로 조회됨 (loader 재사용 금지 — is_active 필터 배제)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _cl(conn, "v1", 100.0, 10.0, "initial", False)   # 이미 비활성
            _cl(conn, "v2", 100.9, 10.0, "periodic", True)   # periodic은 리셋 아님
            out = ch.reset_ref(conn, _GK, K)
            assert out == {"center": 100.0, "sigma_ref": 10.0, "limit_version": "v1"}   # v1 기저 (periodic 무시)
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_reset_ref_approved_override_wins():
    """승인적용(APPROVED_APPLIED) 재산정이 있으면 그 버전이 리셋 지점."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _cl(conn, "v1", 100.0, 10.0, "initial", False)
            _cl(conn, "v2", 120.0, 8.0, "periodic", True)
            _lc(conn, "LIM-TEST-0001", "v2", "periodic", "APPROVED_APPLIED")
            out = ch.reset_ref(conn, _GK, K)
            assert out == {"center": 120.0, "sigma_ref": 8.0, "limit_version": "v2"}     # v2 덮어쓰기
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_reset_ref_incident_override_wins():
    """이상대응(incident) 재설정도 리셋 지점."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _cl(conn, "v1", 100.0, 10.0, "initial", False)
            _cl(conn, "v2", 90.0, 12.0, "incident", True)
            _lc(conn, "LIM-TEST-0002", "v2", "incident", "APPROVED_APPLIED")
            out = ch.reset_ref(conn, _GK, K)
            assert out["center"] == 90.0
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_reset_ref_tiebreak_higher_id():
    """동일 created_at(트랜잭션 NOW 고정)일 때 id DESC tie-break — 나중 삽입 승리."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _cl(conn, "v1", 100.0, 10.0, "initial", False)
            _cl(conn, "v2", 110.0, 10.0, "periodic", False)
            _cl(conn, "v3", 130.0, 10.0, "periodic", True)
            _lc(conn, "LIM-TEST-0003a", "v2", "periodic", "APPROVED_APPLIED")
            _lc(conn, "LIM-TEST-0003b", "v3", "periodic", "APPROVED_APPLIED")  # 나중(id↑)
            out = ch.reset_ref(conn, _GK, K)
            assert out["center"] == 130.0     # v3 (id 큰 쪽)
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_reset_ref_quantile_base_robust_sigma():
    """quantile 기저 → robust σ=(ucl−lcl)/2k."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _cl(conn, "v1", 100.0, 1.0, "initial", True, method="quantile", ucl=130.0, lcl=70.0)
            out = ch.reset_ref(conn, _GK, K)
            assert out["center"] == 100.0
            assert out["sigma_ref"] == pytest.approx(10.0)   # (130−70)/6
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_reset_ref_absent_group_none():
    """기저 부재 그룹 → None (코어가 보수적 승인)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            out = ch.reset_ref(conn, ("TEST_CH_ABSENT", "C6_0", 4, "settled", "C99"), K)
            assert out is None
        finally:
            trans.rollback()
