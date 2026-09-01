"""B4-3 Task5: 재산정 E2E 통합 왕복 (reset_ref → recompute_group → writer).

값 주입 → 판정 → (자동) control_limits 새 버전 / (승인) limit_corrections PROPOSED·control_limits
불변 / (변화없음) 무적재. 실 DB(skipif) + TEST_CH_* INSERT→검증→rollback (M4).
"""
from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc.correction_history import reset_ref
from src.agent_b_spc.recalc_engine import RecalcConfig, recompute_group
from src.agent_b_spc.recalc_writer import apply_auto, record_proposed
from src.agent_b_spc.seed_control_limits import make_engine

_PROBE_TIMEOUT_SEC = 3
_CH = "TEST_CH_E2E"
_GK = (_CH, "C6_0", 4, "settled", "C11")


def _db_available() -> bool:
    try:
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = create_engine(url, connect_args={"connect_timeout": _PROBE_TIMEOUT_SEC})
        try:
            with probe.connect() as c:
                has = c.execute(text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='limit_corrections' AND column_name='delta_sigma'")).scalar()
                return has == 1
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


_INS_CL = text(
    "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
    "limit_version, method, center, sigma, ucl, lcl, k_sigma, trigger_type, is_active) "
    "VALUES (:ch,'C6_0',4,'settled','C11','v1','sigma',:c,:sg,:u,:l,3.0,'initial',true)"
)


def _seed(conn, center=100.0, sigma=10.0):
    conn.execute(_INS_CL, {"ch": _CH, "c": center, "sg": sigma,
                           "u": center + 3 * sigma, "l": center - 3 * sigma})


def _current(conn):
    """현행(is_active) 관리선 — control_limits_loader가 반환하는 6키(트랜잭션 내 조회)."""
    row = conn.execute(text(
        "SELECT center, sigma, ucl, lcl, method, limit_version FROM control_limits "
        "WHERE chamber_id=:ch AND is_active"), {"ch": _CH}).mappings().first()
    return dict(row)


def _active(conn):
    return conn.execute(text(
        "SELECT limit_version, center FROM control_limits WHERE chamber_id=:ch AND is_active"),
        {"ch": _CH}).mappings().first()


def _cl_count(conn):
    return conn.execute(text("SELECT count(*) FROM control_limits WHERE chamber_id=:ch"),
                        {"ch": _CH}).scalar()


def _lc(conn):
    return conn.execute(text(
        "SELECT status, limit_version_after FROM limit_corrections WHERE chamber_id=:ch"),
        {"ch": _CH}).mappings().all()


def _run(conn, values, cfg, resolution=0.0):
    """오케스트레이션 1스텝: reset_ref → recompute_group → writer 분기 (B5-1 배선 축약)."""
    k = cfg.limit.k_sigma
    reset = reset_ref(conn, _GK, k)
    proposal = recompute_group(_GK, values, _current(conn), reset, resolution, cfg)
    if proposal.decision == "auto_applied":
        apply_auto(conn, proposal)
    elif proposal.decision == "needs_approval":
        record_proposed(conn, proposal)
    return proposal


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")
def test_e2e_auto_applied_writes_new_version():
    """자동: 판정→apply_auto→control_limits 새 버전 활성(신 center)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, center=100.0, sigma=10.0)
            p = _run(conn, [103.0] * 500, RecalcConfig.load())     # delta1=0.3 → auto
            assert p.decision == "auto_applied"
            act = _active(conn)
            assert act["limit_version"] == "v2" and act["center"] == 103.0
            assert _cl_count(conn) == 2                              # v1(false)+v2(true)
            assert _lc(conn)[0]["status"] == "AUTO_APPLIED"
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")
def test_e2e_needs_approval_history_only():
    """승인: 판정→record_proposed→limit_corrections PROPOSED, control_limits 불변."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, center=100.0, sigma=10.0)
            p = _run(conn, [106.0] * 500, RecalcConfig.load())     # delta1=0.6 → 승인
            assert p.decision == "needs_approval"
            act = _active(conn)
            assert act["limit_version"] == "v1" and act["center"] == 100.0   # 불변
            assert _cl_count(conn) == 1
            lc = _lc(conn)
            assert len(lc) == 1 and lc[0]["status"] == "PROPOSED"
            assert lc[0]["limit_version_after"] is None
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")
def test_e2e_no_change_writes_nothing():
    """변화없음: 양쪽 writer 무적재 (control_limits·limit_corrections 불변)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, center=100.0, sigma=10.0)
            p = _run(conn, [100.5] * 500, RecalcConfig.load())     # delta1=0.05 → 변화없음
            assert p.decision == "no_change"
            assert _cl_count(conn) == 1                              # v1만
            assert _lc(conn) == []                                   # 이력 없음
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")
def test_e2e_cumulative_from_reset_drives_approval():
    """리셋 대비 누적: v1(리셋)=100, 현행이 이미 드리프트한 상태에서 누적 초과 → 승인.

    reset_ref가 initial v1(100)을 기저로 잡고, 현행 v2가 113으로 이동해 있으면
    새 114는 1회는 작아도(0.1σ) 누적 1.4σ로 승인 — telescoping 실 DB 검증.
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, center=100.0, sigma=10.0)                   # v1 initial (리셋 기저)
            # 현행을 v2(113)로 만듦 — v1 비활성 후 periodic v2 삽입(드리프트 상태 재현)
            conn.execute(text("UPDATE control_limits SET is_active=false WHERE chamber_id=:ch"),
                         {"ch": _CH})
            conn.execute(text(
                "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
                "limit_version, method, center, sigma, ucl, lcl, k_sigma, trigger_type, is_active) "
                "VALUES (:ch,'C6_0',4,'settled','C11','v2','sigma',113.0,10.0,143.0,83.0,3.0,'periodic',true)"),
                {"ch": _CH})
            p = _run(conn, [114.0] * 500, RecalcConfig.load())     # delta1=0.1, cum=1.4
            assert p.decision == "needs_approval"
        finally:
            trans.rollback()
