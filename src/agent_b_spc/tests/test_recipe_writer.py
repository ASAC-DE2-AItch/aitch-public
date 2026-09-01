"""B5-4 recipe_writer 테스트 (스펙 v2.2 §3-3·§5).

순수 FakeConn — 선검사·채번·shap_basis 원본 적재 로직(무DB).
실 DB(skipif) — RCP 채번·(날짜,챔버) max+1·UNIQUE 재시도·JSONB 왕복.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from src.agent_b_spc import recipe_writer as rw
from src.agent_b_spc.recipe_engine import TuningProposal

_PROBE_TIMEOUT_SEC = 3
_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(parameter_id="C4", vc=40.0, vp=39.6, dp=-1.0):
    return TuningProposal(parameter_id=parameter_id, knob_axis="gas", basis_sensor="C15",
                          value_current=vc, value_proposed=vp, delta_pct=dp, direction="down",
                          sensitivity={"basis": "direction_only"}, escalate_reason=None)


def _escalated():
    return TuningProposal(parameter_id=None, knob_axis="gas", basis_sensor="C15",
                          value_current=None, value_proposed=None, delta_pct=None,
                          direction=None, sensitivity={}, escalate_reason="누적 상한 소진")


# ── 순수 FakeConn ───────────────────────────────────────────────────────
class _FakeResult:
    def __init__(self, scalar=None, row=None, rows=None):
        self._scalar, self._row, self._rows = scalar, row, rows or []

    def scalar(self):
        return self._scalar

    def mappings(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return self._rows


class _FakeSavepoint:
    def __init__(self, conn):
        self.conn = conn

    def commit(self):
        self.conn.committed += 1

    def rollback(self):
        self.conn.rolled_back += 1


class _FakeConn:
    """SQLAlchemy conn 미러 — 쿼리 SQL 부분문자열로 분기."""

    def __init__(self, *, has_proposed=False, next_seq=1, alert_row=None, integrity_fails=0,
                 applied_rows=None, temp_tuned=False):
        self.has_proposed = has_proposed
        self.next_seq = next_seq
        self.alert_row = alert_row
        self.integrity_fails = integrity_fails
        self.applied_rows = applied_rows or []
        self.temp_tuned = temp_tuned
        self.inserts: list[dict] = []
        self.committed = self.rolled_back = 0

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "value_after" in sql and "recipe_corrections" in sql and "SELECT" in sql:
            return _FakeResult(rows=self.applied_rows)
        if "FROM spc_violations" in sql:
            return _FakeResult(row=self.alert_row)
        if "parameter_id = 'temp_target'" in sql and "SELECT 1" in sql:
            return _FakeResult(scalar=1 if self.temp_tuned else None)
        if "status = 'PROPOSED'" in sql and "SELECT 1" in sql:
            return _FakeResult(scalar=1 if self.has_proposed else None)
        if "COALESCE(MAX" in sql:
            return _FakeResult(scalar=self.next_seq)
        if "INSERT INTO recipe_corrections" in sql:
            if self.integrity_fails > 0:
                self.integrity_fails -= 1
                raise IntegrityError("dup", {}, Exception())
            self.inserts.append(params)
            return _FakeResult()
        return _FakeResult()

    def begin_nested(self):
        return _FakeSavepoint(self)


# ── resolve_alert_context (D13) ─────────────────────────────────────────
def test_resolve_returns_recipe_and_step():
    conn = _FakeConn(alert_row={"recipe_id": "C6_0", "step": 4})
    assert rw.resolve_alert_context(conn, "ALERT-1") == ("C6_0", 4)


def test_resolve_missing_row_returns_none_pair():
    conn = _FakeConn(alert_row=None)
    assert rw.resolve_alert_context(conn, "ALERT-x") == (None, None)


def test_resolve_null_step_stays_none():
    conn = _FakeConn(alert_row={"recipe_id": "C6_0", "step": None})
    assert rw.resolve_alert_context(conn, "ALERT-1") == ("C6_0", None)


# ── resolve_applied_setpoints (D10 — 직전 적용분 조달) ───────────────────
def test_resolve_applied_returns_latest_per_knob():
    conn = _FakeConn(applied_rows=[{"parameter_id": "C4", "value_after": 39.6}])
    assert rw.resolve_applied_setpoints(conn, "SIM_CH_1", "C6_0", 4) == {"C4": 39.6}


def test_resolve_applied_skips_null_value_after():
    """value_after NULL 행은 제외(적용 수치 없음)."""
    conn = _FakeConn(applied_rows=[{"parameter_id": "C4", "value_after": None},
                                    {"parameter_id": "C5", "value_after": 50.0}])
    assert rw.resolve_applied_setpoints(conn, "SIM_CH_1", "C6_0", 4) == {"C5": 50.0}


@pytest.mark.parametrize("rc,st", [(None, 4), ("C6_0", None)])
def test_resolve_applied_none_context_returns_empty(rc, st):
    """recipe_id/step None → 빈 dict(쿼리 안 함)."""
    conn = _FakeConn(applied_rows=[{"parameter_id": "C4", "value_after": 39.6}])
    assert rw.resolve_applied_setpoints(conn, "SIM_CH_1", rc, st) == {}


# ── resolve_temp_tuned_this_incident (온도 A 정책, C ⓑ) ──────────────────
def test_temp_tuned_true_when_temp_correction_exists():
    """이 incident에 temp_target 이력 있음 → True(엔진이 반복 에스컬)."""
    conn = _FakeConn(temp_tuned=True)
    assert rw.resolve_temp_tuned_this_incident(conn, "INC-1") is True


def test_temp_tuned_false_on_new_incident():
    """이력 없음(첫 온도 알람 또는 새 incident) → False(엔진이 1스텝 제안 = A 리셋)."""
    conn = _FakeConn(temp_tuned=False)
    assert rw.resolve_temp_tuned_this_incident(conn, "INC-2") is False


# ── record 선검사 (적재 skip) ───────────────────────────────────────────
def test_escalate_proposal_not_recorded():
    conn = _FakeConn()
    assert rw.record_recipe_proposed(conn, _escalated(), "INC-1", "SIM_CH_1", "C6_0", 4,
                                     ["C15"], now=_NOW) is None
    assert conn.inserts == []


@pytest.mark.parametrize("rc,st", [(None, 4), ("C6_0", None), (None, None)])
def test_null_recipe_or_step_not_recorded(rc, st):
    """recipe_id/step None → NOT NULL 회피 skip(INSERT 안 함)."""
    conn = _FakeConn()
    assert rw.record_recipe_proposed(conn, _proposal(), "INC-1", "SIM_CH_1", rc, st,
                                     ["C15"], now=_NOW) is None
    assert conn.inserts == []


def test_existing_proposed_skips_new_seq():
    """동일 incident PROPOSED 기존재 → 신규 채번 skip(1-4)."""
    conn = _FakeConn(has_proposed=True)
    assert rw.record_recipe_proposed(conn, _proposal(), "INC-1", "SIM_CH_1", "C6_0", 4,
                                     ["C15"], now=_NOW) is None
    assert conn.inserts == []


# ── record 성공 경로 ─────────────────────────────────────────────────────
def test_record_returns_unpadded_rcp_id():
    conn = _FakeConn(next_seq=7)
    rcid = rw.record_recipe_proposed(conn, _proposal(), "INC-1", "SIM_CH_3", "C6_0", 4,
                                     ["C11", "C15"], now=_NOW)
    assert rcid == "RCP-20260730-SIM_CH_3-7"          # 무패딩


def test_record_maps_value_pair_and_delta():
    conn = _FakeConn()
    rw.record_recipe_proposed(conn, _proposal(vc=40.0, vp=39.6, dp=-1.0), "INC-1", "SIM_CH_1",
                              "C6_0", 4, ["C15"], now=_NOW)
    row = conn.inserts[0]
    assert row["vb"] == 40.0 and row["va"] == 39.6 and row["dp"] == -1.0
    assert row["pid"] == "C4" and row["rc"] == "C6_0" and row["st"] == 4


def test_shap_basis_is_original_not_filtered():
    """shap_basis = A 발행 원본 그대로(첫매칭·forbidden 필터 결과 아님, 3-3)."""
    conn = _FakeConn()
    original = ["C11", "C15", "C17"]                  # C11=forbidden축·C17=forbidden이지만 원본 보존
    rw.record_recipe_proposed(conn, _proposal(), "INC-1", "SIM_CH_1", "C6_0", 4,
                              original, now=_NOW)
    assert json.loads(conn.inserts[0]["shap"]) == original


def test_seq_retry_on_integrity_error():
    """채번 경합(IntegrityError) → SAVEPOINT rollback 후 재시도 성공."""
    conn = _FakeConn(next_seq=1, integrity_fails=1)
    rcid = rw.record_recipe_proposed(conn, _proposal(), "INC-1", "SIM_CH_1", "C6_0", 4,
                                     ["C15"], now=_NOW)
    assert rcid == "RCP-20260730-SIM_CH_1-1" and conn.rolled_back == 1 and conn.committed == 1


# ── 실 DB (skipif) ──────────────────────────────────────────────────────
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
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name='recipe_corrections'")).scalar()
                return has == 1
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_db_roundtrip_and_incident_dedup():
    """실 DB: INSERT → JSONB 왕복 → 동일 incident 2회차 skip. TEST INSERT→rollback."""
    from src.agent_b_spc.seed_control_limits import make_engine
    eng = make_engine()
    inc = "INC-TEST-RCPWRITER"
    with eng.connect() as conn:
        trans = conn.begin()
        try:
            rcid = rw.record_recipe_proposed(conn, _proposal(), inc, "TEST_CH_RCP", "C6_0", 4,
                                             ["C15", "C16"], now=_NOW)
            assert rcid == "RCP-20260730-TEST_CH_RCP-1"
            row = conn.execute(text(
                "SELECT parameter_id, value_before, value_after, shap_basis, status "
                "FROM recipe_corrections WHERE recipe_correction_id = :r"),
                {"r": rcid}).mappings().first()
            assert row["parameter_id"] == "C4" and row["status"] == "PROPOSED"
            assert row["shap_basis"] == ["C15", "C16"]
            # 2회차 동일 incident → skip(None)
            assert rw.record_recipe_proposed(conn, _proposal(), inc, "TEST_CH_RCP", "C6_0", 4,
                                             ["C15"], now=_NOW) is None
        finally:
            trans.rollback()
