"""provisional_seams 테스트 (B6-3-c c-4 — a seam 실 구현).

ProvisionalWriter(write_provisional → control_limits, trigger_type='provisional') ·
derive_restore_state(재시작 시 control_limits provisional 활성 → 상태 파생, 결정2 ⓓ).
실 DB skipif — TEST_CH_* INSERT→검증→rollback.
"""
from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc.seed_control_limits import make_engine
from src.agent_b_spc import provisional_seams as ps
from src.agent_b_spc.provisional_mode import Phase

_PROBE = 3
K = 3.0
_CH = "TEST_CH_PSEAM"
_GK = (_CH, "C6_0", 4, "settled", "C11")


def _db_available() -> bool:
    try:
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = create_engine(url, connect_args={"connect_timeout": _PROBE})
        try:
            with probe.connect() as c:
                return c.execute(text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='control_limits' AND column_name='trigger_type'")).scalar() == 1
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


_INS_CL = text(
    "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
    "limit_version, method, center, sigma, ucl, lcl, k_sigma, q_low, q_high, trigger_type, is_active) "
    "VALUES (:ch,:rc,:st,:win,:sn,:ver,:m,:c,:sg,:u,:l,:k,:ql,:qh,:tt,:a)")


def _seed_firm(conn, center=100.0, sigma=5.0):
    conn.execute(_INS_CL, {
        "ch": _CH, "rc": "C6_0", "st": 4, "win": "settled", "sn": "C11", "ver": "v1",
        "m": "sigma", "c": center, "sg": sigma, "u": center + K * sigma, "l": center - K * sigma,
        "k": K, "ql": None, "qh": None, "tt": "initial", "a": True})


def _rows(conn):
    return conn.execute(text(
        "SELECT limit_version, center, sigma, method, trigger_type, is_active "
        "FROM control_limits WHERE chamber_id=:ch ORDER BY id"), {"ch": _CH}).mappings().all()


def _prov_row(center=100.0, sigma=8.0, method="sigma"):
    return {"group_key": _GK, "center": center, "ucl": center + 40, "lcl": center - 40,
            "sigma": sigma, "method": method}


@pytest.mark.skipif(not _db_available(), reason="DB 미접속/스키마 미반영 — skip")
def test_write_provisional_new_active_version():
    """provisional row → control_limits 신규 active(trigger_type='provisional')·firm 비활성."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_firm(conn, center=100.0, sigma=5.0)
            ps.ProvisionalWriter(engine=None, _conn=conn).write_provisional([_prov_row(sigma=8.0)])
            rows = _rows(conn)
            assert len(rows) == 2
            v1 = next(r for r in rows if r["limit_version"] == "v1")
            v2 = next(r for r in rows if r["limit_version"] == "v2")
            assert v1["is_active"] is False                # 기존 firm 비활성
            assert v2["is_active"] is True and v2["trigger_type"] == "provisional"
            assert v2["sigma"] == 8.0                      # 광폭 인코딩 반영
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DB 미접속/스키마 미반영 — skip")
def test_derive_restore_state_marks_provisional_phase1():
    """control_limits에 provisional 활성 행 있는 챔버 → 상태 파생 PHASE_1(결정2 ⓓ)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(_INS_CL, {
                "ch": _CH, "rc": "C6_0", "st": 4, "win": "settled", "sn": "C11", "ver": "v2",
                "m": "sigma", "c": 120.0, "sg": 8.0, "u": 160.0, "l": 80.0, "k": K,
                "ql": None, "qh": None, "tt": "provisional", "a": True})
            state = ps.derive_restore_state(conn, seasoning_loud=1000)
            assert _CH in state
            assert state[_CH]["phase"] == Phase.PHASE_1
            assert state[_CH]["post_pm_count"] == 0        # 리셋 수용(ⓓ)
            assert state[_CH]["boundary"] == 1000          # A7 경계
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DB 미접속/스키마 미반영 — skip")
def test_derive_restore_state_excludes_firm_chambers():
    """firm(trigger_type≠provisional) 활성만 있는 챔버는 상태에 없음(정상 챔버)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_firm(conn)                               # trigger_type='initial'
            state = ps.derive_restore_state(conn, seasoning_loud=1000)
            assert _CH not in state
        finally:
            trans.rollback()


# ── B6-3-e ①: firm σ_ref 재파생 (재시작 광폭 오염 방어) ──────────────────────
def test_derive_settle_sigma_ref_from_inactive_firm():
    """비활성 firm-baseline(σ=5)에서 σ_ref 재파생 — active 광폭(σ=15) 아님."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            base = {"ch": _CH, "rc": "C6_0", "st": 4, "win": "settled", "sn": "C11",
                    "m": "sigma", "c": 100.0, "k": K, "ql": None, "qh": None}
            conn.execute(_INS_CL, {**base, "ver": "v1", "sg": 5.0, "u": 115.0, "l": 85.0,
                                   "tt": "initial", "a": False})       # firm(비활성)
            conn.execute(_INS_CL, {**base, "ver": "v2", "sg": 15.0, "u": 145.0, "l": 55.0,
                                   "tt": "provisional", "a": True})    # 광폭(active)
            out = ps.derive_settle_sigma_ref(conn, [_CH], k_sigma=K)
            assert out[_CH][_GK] == 5.0                                # firm σ(광폭 15 아님)
        finally:
            trans.rollback()


def test_derive_settle_sigma_ref_empty_when_no_firm():
    """firm-baseline 없음(provisional만) → 빈 dict(유예→폴백 안전)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(_INS_CL, {"ch": _CH, "rc": "C6_0", "st": 4, "win": "settled",
                                   "sn": "C11", "ver": "v1", "m": "sigma", "c": 100.0,
                                   "sg": 15.0, "u": 145.0, "l": 55.0, "k": K, "ql": None,
                                   "qh": None, "tt": "provisional", "a": True})
            assert ps.derive_settle_sigma_ref(conn, [_CH], k_sigma=K) == {}
        finally:
            trans.rollback()
