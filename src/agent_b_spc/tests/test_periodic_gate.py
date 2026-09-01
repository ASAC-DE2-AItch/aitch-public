"""Step 7 S6a — 무이상 게이팅 쿼리 (G2 PROPOSED·G3 incidents) 단위 테스트.

스펙: Docs/B4-1_Step7_정기리캘리_라이브트리거_스펙플랜.md §3 D2.
G3 = incidents.lifecycle NOT IN ('closed','actioned') 인 행 존재 → 챔버 차단.
G2 = limit_corrections.status='PROPOSED' 그룹 집합(자기 경로 중복만 막음).
표준 SQL이라 sqlite로 검증(G1 phase는 consumer 레벨).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from src.agent_b_spc.spc_consumer import _incident_blocked, _proposed_groups

_DDL = """
CREATE TABLE incidents (
    chamber_id TEXT, lifecycle TEXT
);
CREATE TABLE limit_corrections (
    chamber_id TEXT, recipe_id TEXT, step INTEGER, sensor_window TEXT, sensor_id TEXT, status TEXT
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


def _inc(conn, ch, lifecycle):
    conn.execute(text("INSERT INTO incidents (chamber_id, lifecycle) VALUES (:c, :l)"),
                 {"c": ch, "l": lifecycle})


def _lc(conn, ch, sn, status, recipe="C6_0", step=4, win="settled"):
    conn.execute(text("INSERT INTO limit_corrections (chamber_id, recipe_id, step, sensor_window, "
                      "sensor_id, status) VALUES (:c,:r,:st,:w,:sn,:s)"),
                 {"c": ch, "r": recipe, "st": step, "w": win, "sn": sn, "s": status})


# ── G3: incidents 차단 ────────────────────────────────────────────────────────
def test_g3_open_incident_blocks(engine):
    """open incident → 차단(True)."""
    with engine.begin() as conn:
        _inc(conn, "SIM_CH_1", "open")
        assert _incident_blocked(conn, "SIM_CH_1") is True


def test_g3_verifying_blocks(engine):
    """verifying(조치 효과 관측 창) → 차단 — 이 구간에 관리선 흔들면 관측 오염(D2)."""
    with engine.begin() as conn:
        _inc(conn, "SIM_CH_1", "verifying")
        assert _incident_blocked(conn, "SIM_CH_1") is True


def test_g3_closed_and_actioned_pass(engine):
    """closed·actioned만 있으면 통과(False) — actioned는 verifying으로 넘어가는 순간 상태(fail-safe 제외)."""
    with engine.begin() as conn:
        _inc(conn, "SIM_CH_1", "closed")
        _inc(conn, "SIM_CH_1", "actioned")
        assert _incident_blocked(conn, "SIM_CH_1") is False


def test_g3_no_incident_passes(engine):
    """incident 행 없음 → 통과(False)."""
    with engine.begin() as conn:
        assert _incident_blocked(conn, "SIM_CH_1") is False


def test_g3_other_chamber_isolated(engine):
    """다른 챔버 open은 이 챔버를 안 막는다."""
    with engine.begin() as conn:
        _inc(conn, "SIM_CH_2", "open")
        assert _incident_blocked(conn, "SIM_CH_1") is False


# ── G2: PROPOSED 그룹 집합 ────────────────────────────────────────────────────
def test_g2_returns_proposed_group_keys(engine):
    """PROPOSED 행의 그룹키(chamber 제외 4-튜플) 집합 반환."""
    with engine.begin() as conn:
        _lc(conn, "SIM_CH_1", "C11", "PROPOSED")
        _lc(conn, "SIM_CH_1", "C15", "PROPOSED", step=5)
        got = _proposed_groups(conn, "SIM_CH_1")
    assert got == {("C6_0", 4, "settled", "C11"), ("C6_0", 5, "settled", "C15")}


def test_g2_ignores_non_proposed(engine):
    """APPROVED_APPLIED·no_change 등 비-PROPOSED는 제외."""
    with engine.begin() as conn:
        _lc(conn, "SIM_CH_1", "C11", "APPROVED_APPLIED")
        _lc(conn, "SIM_CH_1", "C15", "PROPOSED", step=5)
        got = _proposed_groups(conn, "SIM_CH_1")
    assert got == {("C6_0", 5, "settled", "C15")}


def test_g2_empty_when_none(engine):
    """PROPOSED 없음 → 빈 집합."""
    with engine.begin() as conn:
        assert _proposed_groups(conn, "SIM_CH_1") == set()
