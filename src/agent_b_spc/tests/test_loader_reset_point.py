"""Step 7 S2 — control_limits_loader.load()의 is_reset_point 파생 플래그 단위 테스트.

스펙: Docs/B4-1_Step7_정기리캘리_라이브트리거_스펙플랜.md §3 D6-b.
is_reset_point = ⓐ 리셋-타입 correction(qual/incident/APPROVED_APPLIED)의 버전 OR ⓑ initial v1.
EXISTS는 표준 SQL이라 sqlite로 검증(실 스키마 왕복은 test_control_limits_loader가 담당).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from src.agent_b_spc.control_limits_loader import load

_DDL = """
CREATE TABLE control_limits (
    chamber_id TEXT, recipe_id TEXT, step INTEGER, sensor_window TEXT, sensor_id TEXT,
    method TEXT, center REAL, sigma REAL, ucl REAL, lcl REAL, limit_version TEXT,
    trigger_type TEXT, is_active INTEGER
);
CREATE TABLE limit_corrections (
    chamber_id TEXT, recipe_id TEXT, step INTEGER, sensor_window TEXT, sensor_id TEXT,
    limit_version_after TEXT, trigger_type TEXT, status TEXT
);
"""


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    return eng


def _cl(conn, sn, ver, tt, active=1):
    """활성 control_limits 행 1개 (그룹은 sensor_id로만 구분)."""
    conn.execute(text(
        "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
        "method, center, sigma, ucl, lcl, limit_version, trigger_type, is_active) VALUES "
        "('SIM_CH_1','C6_0',4,'settled',:sn,'sigma',100.0,10.0,130.0,70.0,:ver,:tt,:active)"),
        {"sn": sn, "ver": ver, "tt": tt, "active": active})


def _lc(conn, sn, ver_after, tt, status):
    """limit_corrections 행 1개 (그 sensor 그룹의 리셋 이력)."""
    conn.execute(text(
        "INSERT INTO limit_corrections (chamber_id, recipe_id, step, sensor_window, sensor_id, "
        "limit_version_after, trigger_type, status) VALUES "
        "('SIM_CH_1','C6_0',4,'settled',:sn,:va,:tt,:status)"),
        {"sn": sn, "va": ver_after, "tt": tt, "status": status})


def _gk(sn):
    return ("SIM_CH_1", "C6_0", 4, "settled", sn)


def test_initial_v1_is_reset_point():
    """ⓑ initial·v1 활성 행 → is_reset_point True (correction 없어도)."""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        _cl(conn, "C11", "v1", "initial")
    limits, _ = load(eng)
    assert limits[_gk("C11")]["is_reset_point"] is True


def test_periodic_without_correction_is_not_reset_point(engine):
    """정기(periodic) 재산정 활성 행 + 리셋 이력 없음 → False."""
    with engine.begin() as conn:
        _cl(conn, "C11", "v2", "periodic")
    limits, _ = load(engine)
    assert limits[_gk("C11")]["is_reset_point"] is False


def test_approved_applied_correction_is_reset_point(engine):
    """ⓐ status='APPROVED_APPLIED' correction의 버전 → True."""
    with engine.begin() as conn:
        _cl(conn, "C11", "v3", "periodic")                 # 상한 초과 승인된 periodic
        _lc(conn, "C11", "v3", "periodic", "APPROVED_APPLIED")
    limits, _ = load(engine)
    assert limits[_gk("C11")]["is_reset_point"] is True


def test_qual_incident_correction_is_reset_point(engine):
    """ⓐ trigger_type IN (qual, incident) correction → True."""
    with engine.begin() as conn:
        _cl(conn, "C11", "v4", "qual")
        _lc(conn, "C11", "v4", "qual", "APPLIED")
        _cl(conn, "C15", "v2", "incident")
        _lc(conn, "C15", "v2", "incident", "APPLIED")
    limits, _ = load(engine)
    assert limits[_gk("C11")]["is_reset_point"] is True
    assert limits[_gk("C15")]["is_reset_point"] is True


def test_proposed_periodic_not_yet_reset_point(engine):
    """periodic correction이 아직 PROPOSED(미승인) → ⓐ 불충족 → False."""
    with engine.begin() as conn:
        _cl(conn, "C11", "v5", "periodic")
        _lc(conn, "C11", "v5", "periodic", "PROPOSED")
    limits, _ = load(engine)
    assert limits[_gk("C11")]["is_reset_point"] is False
